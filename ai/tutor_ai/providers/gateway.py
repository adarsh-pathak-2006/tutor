"""LLM Gateway provider for unified routing, resilient failover, and structured observability."""

import logging
import time
from collections.abc import AsyncIterator

from .base import EmbeddingKind, Message, ModelProvider, ProviderError, ProviderUnavailable

logger = logging.getLogger("tutor_ai.providers.gateway")


class LLMGatewayProvider:
    """Intelligent gateway routing requests to primary and fallback model providers.

    Features:
    - Zero downstream contract changes: implements the standard ModelProvider interface.
    - Automatic failover: falls back to secondary provider if the primary encounters
      rate limits (429), timeouts, or connectivity errors before tokens are emitted.
    - Structured logging: records execution latency, chunk metrics, token counts, and
      fallback events for complete production observability.
    """

    name = "gateway"

    def __init__(
        self,
        *,
        primary: ModelProvider,
        fallback: ModelProvider | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback

        # Mirror model metadata from primary provider
        self.chat_model = primary.chat_model
        self.embedding_model = primary.embedding_model
        self.dimensions = primary.dimensions

    async def chat(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        """Stream chat tokens through primary provider, with automatic failover to fallback."""
        start_time = time.perf_counter()
        first_token_time: float | None = None
        emitted_chunks = 0
        active_provider = self.primary

        logger.info(
            "LLM Gateway routing chat request",
            extra={
                "event": "gateway.chat.start",
                "primary_provider": self.primary.name,
                "primary_model": self.primary.chat_model,
                "has_fallback": self.fallback is not None,
                "fallback_provider": self.fallback.name if self.fallback else None,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )

        try:
            async for chunk in self.primary.chat(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
            ):
                if first_token_time is None:
                    first_token_time = time.perf_counter()
                    logger.info(
                        "LLM Gateway received first token from primary",
                        extra={
                            "event": "gateway.chat.first_token",
                            "provider": self.primary.name,
                            "time_to_first_token_ms": round((first_token_time - start_time) * 1000, 1),
                        },
                    )
                emitted_chunks += 1
                yield chunk

        except Exception as primary_exc:
            # If we already yielded tokens to the client, we cannot cleanly switch providers mid-stream
            if emitted_chunks > 0 or self.fallback is None:
                duration_ms = round((time.perf_counter() - start_time) * 1000, 1)
                logger.error(
                    "LLM Gateway primary stream failed",
                    extra={
                        "event": "gateway.chat.error",
                        "provider": self.primary.name,
                        "error_type": type(primary_exc).__name__,
                        "emitted_chunks": emitted_chunks,
                        "duration_ms": duration_ms,
                    },
                )
                if isinstance(primary_exc, ProviderError):
                    raise
                raise ProviderUnavailable(
                    f"LLM Gateway primary provider {self.primary.name} failed: {primary_exc}",
                    provider=self.primary.name,
                ) from primary_exc

            # Failover: primary failed BEFORE any token was emitted to the client
            failover_start = time.perf_counter()
            logger.warning(
                "LLM Gateway primary provider failed; triggering fallback",
                extra={
                    "event": "gateway.chat.fallback",
                    "primary_provider": self.primary.name,
                    "primary_error": type(primary_exc).__name__,
                    "fallback_provider": self.fallback.name,
                    "fallback_model": self.fallback.chat_model,
                    "elapsed_before_failover_ms": round((failover_start - start_time) * 1000, 1),
                },
            )

            active_provider = self.fallback
            try:
                async for chunk in self.fallback.chat(
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                ):
                    if first_token_time is None:
                        first_token_time = time.perf_counter()
                    emitted_chunks += 1
                    yield chunk
            except Exception as fallback_exc:
                duration_ms = round((time.perf_counter() - start_time) * 1000, 1)
                logger.error(
                    "LLM Gateway fallback provider also failed",
                    extra={
                        "event": "gateway.chat.fallback_error",
                        "fallback_provider": self.fallback.name,
                        "error_type": type(fallback_exc).__name__,
                        "duration_ms": duration_ms,
                    },
                )
                if isinstance(fallback_exc, ProviderError):
                    raise
                raise ProviderUnavailable(
                    f"LLM Gateway fallback {self.fallback.name} failed: {fallback_exc}",
                    provider=self.fallback.name,
                ) from fallback_exc

        total_duration_ms = round((time.perf_counter() - start_time) * 1000, 1)
        logger.info(
            "LLM Gateway chat completed successfully",
            extra={
                "event": "gateway.chat.complete",
                "active_provider": active_provider.name,
                "model": active_provider.chat_model,
                "used_fallback": active_provider is self.fallback,
                "total_chunks": emitted_chunks,
                "duration_ms": total_duration_ms,
            },
        )

    async def embed(
        self,
        texts: list[str],
        *,
        kind: EmbeddingKind,
    ) -> list[list[float]]:
        """Route embedding generation to primary provider with latency metrics."""
        start_time = time.perf_counter()
        try:
            vectors = await self.primary.embed(texts, kind=kind)
            duration_ms = round((time.perf_counter() - start_time) * 1000, 1)
            logger.info(
                "LLM Gateway embedding completed",
                extra={
                    "event": "gateway.embed.complete",
                    "provider": self.primary.name,
                    "model": self.primary.embedding_model,
                    "text_count": len(texts),
                    "kind": kind,
                    "duration_ms": duration_ms,
                },
            )
            return vectors
        except Exception as exc:
            # If primary embedding fails and fallback exists, try fallback
            if self.fallback is not None:
                logger.warning(
                    "LLM Gateway primary embed failed; attempting fallback",
                    extra={
                        "event": "gateway.embed.fallback",
                        "primary_error": type(exc).__name__,
                        "fallback_provider": self.fallback.name,
                    },
                )
                vectors = await self.fallback.embed(texts, kind=kind)
                return vectors
            raise

    async def health(self) -> bool:
        """Check health of primary provider (and fallback if configured)."""
        primary_healthy = await self.primary.health()
        if not primary_healthy and self.fallback is not None:
            # Fallback is available even if primary is degraded
            fallback_healthy = await self.fallback.health()
            logger.warning(
                "LLM Gateway health: primary is unhealthy, fallback status",
                extra={
                    "event": "gateway.health.degraded",
                    "primary_healthy": False,
                    "fallback_healthy": fallback_healthy,
                },
            )
            return fallback_healthy
        return primary_healthy

    async def aclose(self) -> None:
        """Safely clean up resources for both providers."""
        try:
            await self.primary.aclose()
        finally:
            if self.fallback is not None:
                await self.fallback.aclose()
