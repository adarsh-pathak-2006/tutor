"""Tests for LLMGatewayProvider and structured logging observability."""

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest

from tests.conftest import make_settings
from tutor_ai.providers.base import Message, ProviderUnavailable
from tutor_ai.providers.factory import build_provider
from tutor_ai.providers.gateway import LLMGatewayProvider
from tutor_ai.providers.mock import MockProvider


class MockFailingProvider:
    """Mock provider that fails during chat or embed."""

    name = "mock_failing"
    chat_model = "fail-model"
    embedding_model = "fail-embed"
    dimensions = 768

    def __init__(self, fail_after_chunks: int = 0) -> None:
        self.fail_after_chunks = fail_after_chunks
        self.aclose_called = False

    async def chat(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        for i in range(self.fail_after_chunks):
            yield f"chunk_{i}"
        raise ProviderUnavailable("Simulated 429 rate limit exceeded", provider=self.name)

    async def embed(self, texts: list[str], *, kind: str = "document") -> list[list[float]]:
        raise ProviderUnavailable("Simulated embed failure", provider=self.name)

    async def health(self) -> bool:
        return False

    async def aclose(self) -> None:
        self.aclose_called = True


def test_gateway_chat_primary_success(caplog: pytest.LogCaptureFixture) -> None:
    """Gateway successfully streams from primary and emits structured logs."""
    primary = MockProvider(dimensions=768)
    fallback = MockProvider(dimensions=768)
    gateway = LLMGatewayProvider(primary=primary, fallback=fallback)

    messages = [Message(role="user", content="Hello tutor")]

    async def _run():
        chunks = []
        with caplog.at_level(logging.INFO, logger="tutor_ai.providers.gateway"):
            async for chunk in gateway.chat(messages, max_tokens=100, temperature=0.7):
                chunks.append(chunk)
        return "".join(chunks)

    result = asyncio.run(_run())
    assert len(result) > 0

    # Verify structured logs captured start, first token, and complete events
    log_records = [r for r in caplog.records if r.name == "tutor_ai.providers.gateway"]
    events = [getattr(r, "event", None) for r in log_records]
    assert "gateway.chat.start" in events
    assert "gateway.chat.first_token" in events
    assert "gateway.chat.complete" in events


def test_gateway_chat_failover_to_fallback(caplog: pytest.LogCaptureFixture) -> None:
    """Gateway catches primary failure before emission and smoothly fails over to fallback."""
    primary = MockFailingProvider(fail_after_chunks=0)
    fallback = MockProvider(dimensions=768)
    gateway = LLMGatewayProvider(primary=primary, fallback=fallback)

    messages = [Message(role="user", content="What is gravity?")]

    async def _run():
        chunks = []
        with caplog.at_level(logging.INFO, logger="tutor_ai.providers.gateway"):
            async for chunk in gateway.chat(messages, max_tokens=100, temperature=0.7):
                chunks.append(chunk)
        return "".join(chunks)

    result = asyncio.run(_run())
    assert len(result) > 0

    log_records = [r for r in caplog.records if r.name == "tutor_ai.providers.gateway"]
    events = [getattr(r, "event", None) for r in log_records]

    # Verify failover was logged
    assert "gateway.chat.fallback" in events
    assert "gateway.chat.complete" in events

    complete_record = next(r for r in log_records if getattr(r, "event", None) == "gateway.chat.complete")
    assert getattr(complete_record, "used_fallback", False) is True
    assert getattr(complete_record, "active_provider", None) == "mock"


def test_gateway_chat_raises_if_primary_fails_midstream() -> None:
    """Gateway will not switch to fallback if tokens were already emitted to client."""
    primary = MockFailingProvider(fail_after_chunks=2)
    fallback = MockProvider(dimensions=768)
    gateway = LLMGatewayProvider(primary=primary, fallback=fallback)

    messages = [Message(role="user", content="Tell me a story")]

    async def _run():
        chunks = []
        async for chunk in gateway.chat(messages, max_tokens=100, temperature=0.7):
            chunks.append(chunk)
        return chunks

    with pytest.raises(ProviderUnavailable):
        asyncio.run(_run())


def test_gateway_embed_success_and_fallback(caplog: pytest.LogCaptureFixture) -> None:
    """Gateway generates embeddings with primary, or fails over to fallback."""
    primary_fail = MockFailingProvider()
    fallback = MockProvider(dimensions=768)
    gateway = LLMGatewayProvider(primary=primary_fail, fallback=fallback)

    async def _run():
        with caplog.at_level(logging.INFO, logger="tutor_ai.providers.gateway"):
            return await gateway.embed(["test passage"], kind="document")

    vectors = asyncio.run(_run())
    assert len(vectors) == 1
    assert len(vectors[0]) == 768

    log_records = [r for r in caplog.records if r.name == "tutor_ai.providers.gateway"]
    events = [getattr(r, "event", None) for r in log_records]
    assert "gateway.embed.fallback" in events


def test_gateway_health() -> None:
    """Health reports True if primary is healthy, or if fallback is healthy when primary fails."""
    primary_ok = MockProvider(dimensions=768)
    primary_fail = MockFailingProvider()
    fallback = MockProvider(dimensions=768)

    # Primary ok
    gw1 = LLMGatewayProvider(primary=primary_ok, fallback=fallback)
    assert asyncio.run(gw1.health()) is True

    # Primary fail, fallback ok -> still returns True (degraded operational)
    gw2 = LLMGatewayProvider(primary=primary_fail, fallback=fallback)
    assert asyncio.run(gw2.health()) is True

    # Both fail
    gw3 = LLMGatewayProvider(primary=primary_fail, fallback=primary_fail)
    assert asyncio.run(gw3.health()) is False


def test_factory_builds_gateway_provider() -> None:
    """build_provider correctly builds an LLMGatewayProvider from settings."""
    settings = make_settings(
        TUTOR_AI_PROVIDER="gateway",
        TUTOR_GATEWAY_PRIMARY="mock",
        TUTOR_GATEWAY_FALLBACK="mock",
    )
    provider = build_provider(settings)
    assert isinstance(provider, LLMGatewayProvider)
    assert provider.name == "gateway"
    assert provider.primary.name == "mock"
    assert provider.fallback.name == "mock"
