"""
Ollama provider for local development (D23, D30).

Chat streams from /api/chat (newline-delimited JSON).
Embeddings use /api/embed with nomic-embed-text's required task prefixes.

The model is kept loaded between calls (keep_alive).
Every failure becomes a ProviderError subclass, so callers never see
raw httpx exceptions.
"""

import json
from collections.abc import AsyncIterator

import httpx

from .base import (
    EmbeddingKind,
    Message,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)

# nomic-embed-text requires these prefixes for better retrieval quality.
NOMIC_PREFIX = {
    "document": "search_document: ",
    "query": "search_query: ",
}

# nomic-embed-text was trained with these prefixes; without them retrieval quality drops.
NOMIC_PREFIX = {"document": "search_document: ", "query": "search_query: "}
DEFAULT_EMBED_BATCH = 32
MAX_EMBED_BATCH = 32


class OllamaProvider:
    """Provider for Ollama chat and text embedding models."""

    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        chat_model: str,
        embedding_model: str,
        dimensions: int,
        keep_alive: str = "30m",
        max_batch_size: int = DEFAULT_EMBED_BATCH,
        timeout_seconds: float = 180.0,
        connect_timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.chat_model = chat_model
        self.embedding_model = embedding_model
        self.dimensions = dimensions
        self.keep_alive = keep_alive
        self.max_batch_size = max(1, min(max_batch_size, 64))
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=connect_timeout),
            transport=transport,
        )

    def _error(self, exc: Exception) -> ProviderError:
        """Convert HTTP errors into Tutor AI provider errors."""

        if isinstance(exc, httpx.TimeoutException):
            return ProviderTimeout(
                f"Ollama timed out: {type(exc).__name__}",
                provider=self.name,
            )

        if isinstance(exc, httpx.TransportError):
            return ProviderUnavailable(
                "Ollama is not reachable. Is `ollama serve` running?",
                provider=self.name,
            )

        return ProviderError(
            f"Ollama error: {exc}",
            provider=self.name,
        )

    async def chat(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        """Stream chat responses from Ollama."""

        body = {
            "model": self.chat_model,
            "messages": [
                {
                    "role": message.role,
                    "content": message.content,
                }
                for message in messages
            ],
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }

        try:
            async with self._client.stream(
                "POST",
                "/api/chat",
                json=body,
            ) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode(errors="replace")[:200]

                    raise ProviderError(
                        f"Ollama chat returned {response.status_code}: {detail}",
                        provider=self.name,
                    )

                async for line in response.aiter_lines():
                    if not line.strip():
                        continue

                    event = json.loads(line)

                    if "error" in event:
                        raise ProviderError(
                            f"Ollama chat error: {event['error']}",
                            provider=self.name,
                        )

                    text = event.get(
                        "message",
                        {},
                    ).get("content", "")

                    if text:
                        yield text

                    if event.get("done"):
                        return

        except ProviderError:
            raise

        except (
            httpx.HTTPError,
            json.JSONDecodeError,
        ) as exc:
            raise self._error(exc) from exc

    async def embed(
        self,
        texts: list[str],
        *,
        kind: EmbeddingKind,
    ) -> list[list[float]]:
        """Generate text embeddings using the configured Ollama model."""

        prefix = NOMIC_PREFIX[kind] if self.embedding_model.startswith("nomic-embed-text") else ""

        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.max_batch_size):
            batch = [prefix + t for t in texts[start : start + self.max_batch_size]]
            try:
                response = await self._client.post(
                    "/api/embed",
                    json={
                        "model": self.embedding_model,
                        "input": batch,
                        "keep_alive": self.keep_alive,
                    },
                )

            except httpx.HTTPError as exc:
                raise self._error(exc) from exc

            if response.status_code != 200:
                raise ProviderError(
                    f"Ollama embed returned {response.status_code}",
                    provider=self.name,
                )

            embeddings = response.json().get(
                "embeddings",
                [],
            )

            if len(embeddings) != len(batch):
                raise ProviderError(
                    "Ollama returned a different number of embeddings",
                    provider=self.name,
                )

            for vector in embeddings:
                if len(vector) != self.dimensions:
                    raise ProviderError(
                        f"{self.embedding_model} returned {len(vector)} dimensions, expected {self.dimensions}",
                        provider=self.name,
                    )

            vectors.extend(embeddings)

        return vectors

    async def health(self) -> bool:
        """
        Check whether Ollama is reachable and both configured models
        are installed.

        Used when Ollama handles both chat and embeddings.
        """

        try:
            response = await self._client.get(
                "/api/tags",
                timeout=5.0,
            )

            if response.status_code != 200:
                return False

            names = {model.get("name", "") for model in response.json().get("models", [])}

        except (
            httpx.HTTPError,
            ValueError,
        ):
            return False

        return all(
            _has_model(names, model)
            for model in (
                self.chat_model,
                self.embedding_model,
            )
        )

    async def embedding_health(self) -> bool:
        """
        Check whether the configured Ollama embedding model is installed.

        Used when Gemini handles chat and Ollama handles embeddings.
        """

        try:
            response = await self._client.get(
                "/api/tags",
                timeout=5.0,
            )

            if response.status_code != 200:
                return False

            names = {model.get("name", "") for model in response.json().get("models", [])}

        except (
            httpx.HTTPError,
            ValueError,
        ):
            return False

        return _has_model(
            names,
            self.embedding_model,
        )

    async def aclose(self) -> None:
        """Close the Ollama HTTP client."""

        await self._client.aclose()


def _has_model(
    installed: set[str],
    wanted: str,
) -> bool:
    """
    Match implicit latest tags.

    Example:
    llama3.2 matches llama3.2:latest.
    An explicitly specified tag must match exactly.
    """

    return wanted in installed or (":" not in wanted and f"{wanted}:latest" in installed)
