from unittest.mock import AsyncMock, MagicMock

import pytest

from tutor_ai.providers.base import (
    Message,
    ProviderTimeout,
    ProviderUnavailable,
)
from tutor_ai.providers.gemini import GeminiProvider


@pytest.fixture
def ollama():
    provider = MagicMock()
    provider.embedding_model = "nomic-embed-text"
    provider.dimensions = 768
    provider.embed = AsyncMock(return_value=[[0.1] * 768])
    provider.health = AsyncMock(return_value=True)
    provider.aclose = AsyncMock()
    return provider


@pytest.fixture
def gemini(ollama):
    provider = GeminiProvider(
        api_key="test-api-key",
        chat_model="test-model",
        ollama=ollama,
    )
    return provider


def test_provider_configuration(gemini):
    assert gemini.name == "gemini"
    assert gemini.chat_model == "test-model"
    assert gemini.embedding_model == "nomic-embed-text"
    assert gemini.dimensions == 768


def test_message_conversion(gemini):
    messages = [
        Message(role="system", content="You are a tutor."),
        Message(role="user", content="Explain photosynthesis."),
        Message(role="assistant", content="Plants make food."),
    ]

    system_instruction, contents = gemini._convert_messages(messages)

    assert system_instruction == "You are a tutor."
    assert len(contents) == 2
    assert contents[0].role == "user"
    assert contents[1].role == "model"


@pytest.mark.anyio
async def test_embeddings_delegate_to_ollama(gemini, ollama):
    messages = ["Photosynthesis"]

    result = await gemini.embed(messages, kind="query")

    ollama.embed.assert_awaited_once_with(messages, kind="query")
    assert len(result) == 1
    assert len(result[0]) == 768


@pytest.mark.anyio
async def test_health_checks_ollama_first(gemini, ollama):
    ollama.health.return_value = False

    result = await gemini.health()

    assert result is False


@pytest.mark.anyio
async def test_close_closes_both_clients(gemini, ollama):
    gemini._client.aio.aclose = AsyncMock()

    await gemini.aclose()

    gemini._client.aio.aclose.assert_awaited_once()
    ollama.aclose.assert_awaited_once()


@pytest.mark.anyio
async def test_timeout_error_mapping(gemini):
    # Verify timeout errors are converted into Tutor provider errors.
    import httpx

    async def fake_stream(*args, **kwargs):
        raise httpx.TimeoutException("timeout")

    gemini._client.aio.models.generate_content_stream = fake_stream

    messages = [Message(role="user", content="Hello")]

    with pytest.raises(ProviderTimeout):
        async for _ in gemini.chat(
            messages,
            max_tokens=100,
            temperature=0.2,
        ):
            pass


@pytest.mark.anyio
async def test_rate_limit_error_mapping(gemini):
    class FakeError(Exception):
        code = 429

    async def fake_stream(*args, **kwargs):
        raise FakeError("Rate limit")

    gemini._client.aio.models.generate_content_stream = fake_stream

    messages = [Message(role="user", content="Hello")]

    with pytest.raises(ProviderUnavailable):
        async for _ in gemini.chat(
            messages,
            max_tokens=100,
            temperature=0.2,
        ):
            pass
