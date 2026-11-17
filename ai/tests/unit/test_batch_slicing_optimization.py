"""Separate test suite dedicated to the Ollama embedding batch-slicing optimization.

Verifies:
1. Exact non-overlapping batch slicing when max_batch_size != 32 (resolves tensor overlap defect).
2. Proper batch partitioning across diverse batch sizes (e.g. 16, 7, 1, 32, 64).
3. 1-to-1 input-to-output vector integrity and zero duplicate vector generation.
4. Integration with Settings (TUTOR_OLLAMA_MAX_BATCH_SIZE) and build_provider().
"""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from tutor_ai.providers.factory import build_provider
from tutor_ai.providers.ollama import DEFAULT_EMBED_BATCH, OllamaProvider
from tutor_ai.settings import Settings


def _create_mock_ollama_provider(
    recorded_requests: list[dict], max_batch_size: int = DEFAULT_EMBED_BATCH, dims: int = 4
) -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        recorded_requests.append(body)
        embeddings = [[0.1 * (i + 1)] * dims for i in range(len(body["input"]))]
        return httpx.Response(200, json={"embeddings": embeddings})

    return OllamaProvider(
        base_url="http://mock-ollama/",
        chat_model="llama3.2",
        embedding_model="nomic-embed-text",
        dimensions=dims,
        max_batch_size=max_batch_size,
        transport=httpx.MockTransport(handler),
    )


def test_batch_slicing_no_overlap_on_batch_size_16():
    """Verify that max_batch_size=16 produces clean, non-overlapping slices of 16 without duplicates."""
    recorded = []
    provider = _create_mock_ollama_provider(recorded, max_batch_size=16)

    # 48 items: with the previous bug (MAX_EMBED_BATCH=32), iteration 1 took [0:32], iteration 2 took [16:48],
    # causing items 16-31 to be sent twice!
    texts = [f"Item {i:02d}" for i in range(48)]
    vectors = asyncio.run(provider.embed(texts, kind="document"))
    asyncio.run(provider.aclose())

    # 1. Returned vector count must match text count
    assert len(vectors) == 48

    # 2. Must make exactly 3 requests (48 / 16 = 3)
    assert len(recorded) == 3

    # 3. Each batch must contain exactly 16 items
    assert [len(r["input"]) for r in recorded] == [16, 16, 16]

    # 4. Batches must not overlap; reconstructed items must match input order exactly
    reconstructed = [item.removeprefix("search_document: ") for r in recorded for item in r["input"]]
    assert reconstructed == texts

    # 5. Specifically verify items 16 to 31 are present exactly once
    for i in range(16, 32):
        key = f"Item {i:02d}"
        assert reconstructed.count(key) == 1, (
            f"Expected exactly 1 occurrence of {key}, found {reconstructed.count(key)}"
        )


@pytest.mark.parametrize(
    "batch_size,text_count,expected_batch_lengths",
    [
        (7, 20, [7, 7, 6]),
        (1, 5, [1, 1, 1, 1, 1]),
        (10, 25, [10, 10, 5]),
        (32, 35, [32, 3]),
        (64, 100, [64, 36]),
    ],
)
def test_batch_slicing_across_various_batch_sizes(batch_size: int, text_count: int, expected_batch_lengths: list[int]):
    """Verify slicing correctness and item ordering across arbitrary batch sizes."""
    recorded = []
    provider = _create_mock_ollama_provider(recorded, max_batch_size=batch_size)

    texts = [f"Text_{i}" for i in range(text_count)]
    vectors = asyncio.run(provider.embed(texts, kind="query"))
    asyncio.run(provider.aclose())

    assert len(vectors) == text_count
    assert [len(r["input"]) for r in recorded] == expected_batch_lengths

    reconstructed = [item.removeprefix("search_query: ") for r in recorded for item in r["input"]]
    assert reconstructed == texts


def test_batch_size_clamping():
    """Verify max_batch_size is clamped between 1 and 64 for safety."""
    dummy_transport = httpx.MockTransport(lambda r: httpx.Response(200))
    p_low = OllamaProvider(
        base_url="http://test/",
        chat_model="m",
        embedding_model="m",
        dimensions=4,
        max_batch_size=0,
        transport=dummy_transport,
    )
    assert p_low.max_batch_size == 1
    asyncio.run(p_low.aclose())

    p_high = OllamaProvider(
        base_url="http://test/",
        chat_model="m",
        embedding_model="m",
        dimensions=4,
        max_batch_size=100,
        transport=dummy_transport,
    )
    assert p_high.max_batch_size == 64
    asyncio.run(p_high.aclose())


def test_settings_max_batch_size_configuration():
    """Verify Settings loads TUTOR_OLLAMA_MAX_BATCH_SIZE and validates constraints."""
    s_default = Settings(
        _env_file=None,
        TUTOR_ENV="test",
        AI_DATABASE_URL="postgresql://u:p@localhost:5432/db",
        TUTOR_AI_SERVICE_TOKEN="a" * 32,
        TUTOR_AI_PROVIDER="mock",
    )
    assert s_default.ollama_max_batch_size == 32

    s_custom = Settings(
        _env_file=None,
        TUTOR_ENV="test",
        AI_DATABASE_URL="postgresql://u:p@localhost:5432/db",
        TUTOR_AI_SERVICE_TOKEN="a" * 32,
        TUTOR_AI_PROVIDER="mock",
        TUTOR_OLLAMA_MAX_BATCH_SIZE=16,
    )
    assert s_custom.ollama_max_batch_size == 16

    # Verify out-of-range validation
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            TUTOR_ENV="test",
            AI_DATABASE_URL="postgresql://u:p@localhost:5432/db",
            TUTOR_AI_SERVICE_TOKEN="a" * 32,
            TUTOR_AI_PROVIDER="mock",
            TUTOR_OLLAMA_MAX_BATCH_SIZE=0,
        )

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            TUTOR_ENV="test",
            AI_DATABASE_URL="postgresql://u:p@localhost:5432/db",
            TUTOR_AI_SERVICE_TOKEN="a" * 32,
            TUTOR_AI_PROVIDER="mock",
            TUTOR_OLLAMA_MAX_BATCH_SIZE=65,
        )


def test_factory_passes_max_batch_size():
    """Verify build_provider passes configured max_batch_size to OllamaProvider."""
    settings = Settings(
        _env_file=None,
        TUTOR_ENV="local",
        AI_DATABASE_URL="postgresql://u:p@localhost:5432/db",
        TUTOR_AI_SERVICE_TOKEN="a" * 32,
        TUTOR_AI_PROVIDER="ollama",
        TUTOR_OLLAMA_MAX_BATCH_SIZE=16,
    )
    provider = build_provider(settings)
    assert isinstance(provider, OllamaProvider)
    assert provider.max_batch_size == 16
    asyncio.run(provider.aclose())
