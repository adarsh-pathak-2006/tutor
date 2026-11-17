"""Tests for LLMGatewayProvider: failover, mid-stream continuation, retries, and structured logging."""

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

    def __init__(self, fail_after_chunks: int = 0, chunks: list[str] | None = None) -> None:
        self.fail_after_chunks = fail_after_chunks
        self.chunks = chunks or [f"chunk_{i} " for i in range(fail_after_chunks)]
        self.aclose_called = False

    async def chat(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        for chunk in self.chunks[: self.fail_after_chunks]:
            yield chunk
        raise ProviderUnavailable("Simulated 429 rate limit exceeded", provider=self.name)

    async def embed(self, texts: list[str], *, kind: str = "document") -> list[list[float]]:
        raise ProviderUnavailable("Simulated embed failure", provider=self.name)

    async def health(self) -> bool:
        return False

    async def aclose(self) -> None:
        self.aclose_called = True


class MockTransientFlakyProvider:
    """Mock provider that fails N times pre-stream before succeeding."""

    name = "mock_flaky"
    chat_model = "flaky-model"
    embedding_model = "flaky-embed"
    dimensions = 768

    def __init__(self, failures_before_success: int = 1) -> None:
        self.failures_before_success = failures_before_success
        self.attempts = 0

    async def chat(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        self.attempts += 1
        if self.attempts <= self.failures_before_success:
            raise ProviderUnavailable("Transient network timeout", provider=self.name)
        yield "Recovered "
        yield "cleanly!"

    async def embed(self, texts: list[str], *, kind: str = "document") -> list[list[float]]:
        return [[0.1] * self.dimensions for _ in texts]

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass


class MockContinuationProvider:
    """Mock fallback provider that captures continuation messages and yields the remaining answer."""

    name = "mock_continuation"
    chat_model = "cont-model"
    embedding_model = "cont-embed"
    dimensions = 768

    def __init__(self, reply: str = "sunlight into chemical energy.") -> None:
        self.reply = reply
        self.captured_messages: list[Message] = []
        self.captured_max_tokens: int = 0

    async def chat(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        self.captured_messages = messages
        self.captured_max_tokens = max_tokens
        yield self.reply

    async def embed(self, texts: list[str], *, kind: str = "document") -> list[list[float]]:
        return [[0.2] * self.dimensions for _ in texts]

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass


def test_gateway_chat_primary_success(caplog: pytest.LogCaptureFixture) -> None:
    """Gateway successfully streams from primary and emits structured logs with token counts."""
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

    log_records = [r for r in caplog.records if r.name == "tutor_ai.providers.gateway"]
    events = [getattr(r, "event", None) for r in log_records]
    assert "gateway.chat.start" in events
    assert "gateway.chat.first_token" in events
    assert "gateway.chat.complete" in events

    complete_record = next(r for r in log_records if getattr(r, "event", None) == "gateway.chat.complete")
    assert getattr(complete_record, "recovery_mode", None) == "none"
    assert getattr(complete_record, "prompt_tokens", 0) > 0
    assert getattr(complete_record, "completion_tokens", 0) > 0
    assert getattr(complete_record, "total_tokens", 0) == (
        getattr(complete_record, "prompt_tokens", 0) + getattr(complete_record, "completion_tokens", 0)
    )


def test_gateway_chat_retry_succeeds_before_failover(caplog: pytest.LogCaptureFixture) -> None:
    """Gateway retries transient pre-stream failures with backoff and recovers on primary."""
    primary = MockTransientFlakyProvider(failures_before_success=1)
    fallback = MockProvider(dimensions=768)
    gateway = LLMGatewayProvider(
        primary=primary,
        fallback=fallback,
        max_retries=2,
        retry_backoff_seconds=0.0,
    )

    messages = [Message(role="user", content="Test retry")]

    async def _run():
        chunks = []
        with caplog.at_level(logging.INFO, logger="tutor_ai.providers.gateway"):
            async for chunk in gateway.chat(messages, max_tokens=100, temperature=0.7):
                chunks.append(chunk)
        return "".join(chunks)

    result = asyncio.run(_run())
    assert result == "Recovered cleanly!"
    assert primary.attempts == 2

    log_records = [r for r in caplog.records if r.name == "tutor_ai.providers.gateway"]
    events = [getattr(r, "event", None) for r in log_records]
    assert "gateway.chat.retry" in events
    assert "gateway.chat.fallback" not in events

    complete_record = next(r for r in log_records if getattr(r, "event", None) == "gateway.chat.complete")
    assert getattr(complete_record, "used_fallback", True) is False
    assert getattr(complete_record, "retries_attempted", 0) == 1
    assert getattr(complete_record, "recovery_mode", None) == "none"


def test_gateway_chat_failover_to_fallback(caplog: pytest.LogCaptureFixture) -> None:
    """Gateway catches primary failure after retries exhaust and smoothly fails over to fallback."""
    primary = MockFailingProvider(fail_after_chunks=0)
    fallback = MockProvider(dimensions=768)
    gateway = LLMGatewayProvider(
        primary=primary,
        fallback=fallback,
        max_retries=1,
        retry_backoff_seconds=0.0,
    )

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

    assert "gateway.chat.retry" in events
    # Verify failover was logged
    assert "gateway.chat.fallback" in events
    assert "gateway.chat.complete" in events

    complete_record = next(r for r in log_records if getattr(r, "event", None) == "gateway.chat.complete")
    assert getattr(complete_record, "used_fallback", False) is True
    assert getattr(complete_record, "active_provider", None) == "mock"
    assert getattr(complete_record, "recovery_mode", None) == "prestream_failover"


def test_gateway_chat_midstream_continuation_recovery(caplog: pytest.LogCaptureFixture) -> None:
    """When primary fails mid-stream, Gateway resumes seamlessly via fallback continuation."""
    primary = MockFailingProvider(
        fail_after_chunks=2,
        chunks=["Photosynthesis is the process ", "by which plants convert "],
    )
    fallback = MockContinuationProvider(reply="sunlight into chemical energy.")
    gateway = LLMGatewayProvider(primary=primary, fallback=fallback)

    messages = [Message(role="user", content="Explain photosynthesis")]

    async def _run():
        chunks = []
        with caplog.at_level(logging.INFO, logger="tutor_ai.providers.gateway"):
            async for chunk in gateway.chat(messages, max_tokens=150, temperature=0.7):
                chunks.append(chunk)
        return "".join(chunks)

    result = asyncio.run(_run())
    assert result == "Photosynthesis is the process by which plants convert sunlight into chemical energy."

    # Verify fallback received original prompt + partial assistant text + continuation instruction
    assert fallback.captured_messages[0].content == "Explain photosynthesis"
    assert fallback.captured_messages[1].role == "assistant"
    assert fallback.captured_messages[1].content == "Photosynthesis is the process by which plants convert "
    assert fallback.captured_messages[2].role == "user"
    assert "Continue" in fallback.captured_messages[2].content
    assert fallback.captured_max_tokens < 150

    log_records = [r for r in caplog.records if r.name == "tutor_ai.providers.gateway"]
    events = [getattr(r, "event", None) for r in log_records]
    assert "gateway.chat.midstream_continuation" in events
    assert "gateway.chat.complete" in events

    complete_record = next(r for r in log_records if getattr(r, "event", None) == "gateway.chat.complete")
    assert getattr(complete_record, "used_fallback", False) is True
    assert getattr(complete_record, "recovery_mode", None) == "midstream_continuation"


def test_gateway_chat_raises_midstream_if_no_fallback() -> None:
    """Gateway raises ProviderUnavailable mid-stream when no fallback is configured."""
    primary = MockFailingProvider(fail_after_chunks=2)
    gateway = LLMGatewayProvider(primary=primary, fallback=None)


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

    gw1 = LLMGatewayProvider(primary=primary_ok, fallback=fallback)
    assert asyncio.run(gw1.health()) is True

    gw2 = LLMGatewayProvider(primary=primary_fail, fallback=fallback)
    assert asyncio.run(gw2.health()) is True

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
    """build_provider correctly builds an LLMGatewayProvider with retry config from settings."""
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
