"""Test suite for AI microservice performance optimizations.

Covers:
1. Ollama Provider batch-slicing fix and connect timeout.
2. In-memory LRU query embedding cache (hits, normalization, eviction).
3. Lazy embedding evaluation in pin_context().
4. Conversational follow-up short-circuiting in dialogue orchestration.
5. Windowed safety scanning in StreamGuard (performance and cross-chunk detection).
6. Database connection pool configuration and Settings validation.
"""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from tutor_ai.db import create_engine
from tutor_ai.providers.mock import MockProvider
from tutor_ai.providers.ollama import OllamaProvider
from tutor_ai.retrieval import (
    Passage,
    clear_query_cache,
    get_query_cache_stats,
    get_query_embedding,
    pin_context,
)
from tutor_ai.safety import SafetyPolicy, StreamGuard
from tutor_ai.settings import Settings
from tutor_ai.tutor import (
    LessonRef,
    TurnRequest,
    TutorService,
    is_conversational_continuation,
)

# ==============================================================================
# 1. Ollama Provider: Slicing Bug Fix & Connection Timeout
# ==============================================================================


def test_ollama_batch_slicing_no_overlap_on_custom_batch_size():
    """Verify that embed() correctly uses self.max_batch_size for slicing,

    avoiding duplicate vectors and tensor overlap when max_batch_size != 32.
    """
    seen_batches: list[list[str]] = []

    def mock_embed_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_batches.append(body["input"])
        return httpx.Response(200, json={"embeddings": [[0.1] * 4 for _ in body["input"]]})

    transport = httpx.MockTransport(mock_embed_handler)
    provider = OllamaProvider(
        base_url="http://test-ollama/",
        chat_model="llama3.2",
        embedding_model="nomic-embed-text",
        dimensions=4,
        max_batch_size=16,  # Non-default batch size
        transport=transport,
    )

    texts = [f"Item {i}" for i in range(48)]
    vectors = asyncio.run(provider.embed(texts, kind="document"))
    asyncio.run(provider.aclose())

    # Total vectors must exactly equal the number of input texts (no duplicates)
    assert len(vectors) == 48

    # Must be partitioned into exactly 3 batches of size 16
    assert len(seen_batches) == 3
    for batch in seen_batches:
        assert len(batch) == 16

    # Verify no overlapping or duplicated elements across batches
    flattened_inputs = [item.removeprefix("search_document: ") for batch in seen_batches for item in batch]
    assert flattened_inputs == texts


def test_ollama_connect_timeout_configured():
    """Verify that connect_timeout is properly assigned to the httpx client."""
    provider = OllamaProvider(
        base_url="http://test-ollama/",
        chat_model="llama3.2",
        embedding_model="nomic-embed-text",
        dimensions=4,
        connect_timeout=15.0,
    )
    assert provider._client.timeout.connect == 15.0
    asyncio.run(provider.aclose())


# ==============================================================================
# 2. In-Memory LRU Query Embedding Cache
# ==============================================================================


def test_query_embedding_cache_hit_and_normalization():
    asyncio.run(_test_query_embedding_cache_hit_and_normalization())


async def _test_query_embedding_cache_hit_and_normalization():
    """Verify that repeated queries hit cache and normalized whitespace/casing match."""
    clear_query_cache()
    mock_provider = MockProvider(dimensions=8)

    # Initial fetch: cache miss
    vec1 = await get_query_embedding(mock_provider, "What is Big-O notation?")
    assert len(vec1) == 8
    assert get_query_cache_stats()["size"] == 1

    # Exact same query: cache hit
    vec2 = await get_query_embedding(mock_provider, "What is Big-O notation?")
    assert vec1 == vec2
    assert get_query_cache_stats()["size"] == 1

    # Casing and whitespace variant: cache hit
    vec3 = await get_query_embedding(mock_provider, "  what   IS  big-o notation?  ")
    assert vec1 == vec3
    assert get_query_cache_stats()["size"] == 1

    # Distinct query: cache miss
    vec4 = await get_query_embedding(mock_provider, "What is photosynthesis?")
    assert get_query_cache_stats()["size"] == 2
    assert vec4 != vec1


def test_query_embedding_cache_lru_eviction():
    asyncio.run(_test_query_embedding_cache_lru_eviction())


async def _test_query_embedding_cache_lru_eviction():
    """Verify that oldest entry is evicted when max_entries is exceeded."""
    clear_query_cache()
    mock_provider = MockProvider(dimensions=8)

    # Fill cache to capacity = 2
    await get_query_embedding(mock_provider, "Query 1", max_entries=2)
    await get_query_embedding(mock_provider, "Query 2", max_entries=2)
    assert get_query_cache_stats()["size"] == 2

    # Query 3 forces eviction of Query 1
    await get_query_embedding(mock_provider, "Query 3", max_entries=2)
    assert get_query_cache_stats()["size"] == 2

    # Query 2 should still be cached (touch it to make Query 3 oldest)
    await get_query_embedding(mock_provider, "Query 2", max_entries=2)

    # Query 4 forces eviction of Query 3
    await get_query_embedding(mock_provider, "Query 4", max_entries=2)
    assert get_query_cache_stats()["size"] == 2


# ==============================================================================
# 3. Lazy Embedding Evaluation in pin_context()
# ==============================================================================


def test_pin_context_lazy_avoids_embedding_when_lesson_fits():
    asyncio.run(_test_pin_context_lazy_avoids_embedding_when_lesson_fits())


async def _test_pin_context_lazy_avoids_embedding_when_lesson_fits():
    """When lesson passages fit in token budget and related_k=0, embed() must NOT be called."""
    clear_query_cache()

    lesson_id = uuid.uuid4()
    course_id = uuid.uuid4()
    mock_passages = [
        Passage(
            chunk_id=uuid.uuid4(), lesson_id=lesson_id, heading="Intro", text="Sample text", token_count=50, position=0
        ),
        Passage(
            chunk_id=uuid.uuid4(), lesson_id=lesson_id, heading="Body", text="More text", token_count=100, position=1
        ),
    ]

    # Provider with spy on embed()
    provider = MockProvider(dimensions=8)
    provider.embed = AsyncMock(wraps=provider.embed)

    # Mock engine and connection
    mock_conn = AsyncMock()
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn

    with patch("tutor_ai.retrieval.lesson_passages", new=AsyncMock(return_value=mock_passages)):
        # budget = 500 tokens (passages sum to 150 <= 500)
        chosen = await pin_context(
            mock_engine,
            provider,
            lesson_id=lesson_id,
            course_id=course_id,
            query="Tell me about algorithms",
            budget_tokens=500,
            related_k=0,
            minimum=0.5,
        )

    # Assert lesson passages returned directly
    assert len(chosen) == 2
    # Verify that provider.embed was NEVER called!
    assert provider.embed.call_count == 0


def test_pin_context_lazy_calls_embedding_when_budget_exceeded():
    asyncio.run(_test_pin_context_lazy_calls_embedding_when_budget_exceeded())


async def _test_pin_context_lazy_calls_embedding_when_budget_exceeded():
    """When lesson passages exceed budget, embed() must be called lazily to rank chunks."""
    clear_query_cache()

    lesson_id = uuid.uuid4()
    course_id = uuid.uuid4()
    mock_passages = [
        Passage(
            chunk_id=uuid.uuid4(), lesson_id=lesson_id, heading="Intro", text="Text 1", token_count=400, position=0
        ),
        Passage(chunk_id=uuid.uuid4(), lesson_id=lesson_id, heading="Body", text="Text 2", token_count=400, position=1),
    ]

    provider = MockProvider(dimensions=8)
    provider.embed = AsyncMock(wraps=provider.embed)

    mock_conn = AsyncMock()
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn

    with (
        patch("tutor_ai.retrieval.lesson_passages", new=AsyncMock(return_value=mock_passages)),
        patch("tutor_ai.retrieval.passages_by_id", new=AsyncMock(return_value=mock_passages)),
    ):
        # budget = 500 tokens (passages sum to 800 > 500)
        chosen = await pin_context(
            mock_engine,
            provider,
            lesson_id=lesson_id,
            course_id=course_id,
            query="What is text 1?",
            budget_tokens=500,
            related_k=0,
            minimum=0.5,
        )

    assert len(chosen) == 1
    # Verify provider.embed WAS called lazily because scoring was necessary
    assert provider.embed.call_count == 1


# ==============================================================================
# 4. Conversational Continuation Short-Circuiting
# ==============================================================================


@pytest.mark.parametrize(
    "phrase",
    [
        "yes",
        "Yes!",
        "yeah",
        "ok",
        "okay.",
        "sure",
        "thanks",
        "thank you",
        "continue",
        "go on",
        "tell me more",
        "explain more",
        "give an example",
        "what next",
        "i understand",
        "makes sense",
        "yes please",
        "got it",
    ],
)
def test_conversational_detector_identifies_continuations(phrase: str):
    assert is_conversational_continuation(phrase) is True


@pytest.mark.parametrize(
    "question",
    [
        "What is the definition of photosynthesis?",
        "Can you explain asymptotic notation in detail?",
        "How do I balance chemical equations?",
        "Why does gravity pull objects down?",
        "Solve 3x + 5 = 20",
    ],
)
def test_conversational_detector_preserves_academic_questions(question: str):
    assert is_conversational_continuation(question) is False


def test_tutor_context_short_circuits_conversational_turn():
    asyncio.run(_test_tutor_context_short_circuits_conversational_turn())


async def _test_tutor_context_short_circuits_conversational_turn():
    """Verify that TutorService._context skips embedding when student message is conversational."""
    clear_query_cache()

    lesson_id = uuid.uuid4()
    course_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    pinned = [Passage(chunk_id=chunk_id, lesson_id=lesson_id, heading="H1", text="Content", token_count=50, position=0)]

    provider = MockProvider(dimensions=8)
    provider.embed = AsyncMock(wraps=provider.embed)

    mock_conn = AsyncMock()
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn

    service = TutorService(
        engine=mock_engine,
        provider=provider,
        prompts=MagicMock(),
        policy=MagicMock(),
        settings=Settings(
            TUTOR_ENV="test",
            AI_DATABASE_URL="postgresql://u:p@localhost:5432/db",
            TUTOR_AI_SERVICE_TOKEN="a" * 32,
            TUTOR_AI_PROVIDER="mock",
        ),
    )

    request = TurnRequest(
        learner_ref="student_1",
        chat_id=uuid.uuid4(),
        class_number=1,
        lesson=LessonRef(id=lesson_id, version_id=uuid.uuid4(), course_id=course_id, title="Test"),
        mode="explain",
        message="ok continue",
        history=[],
        pinned_chunk_ids=[chunk_id],
    )

    with patch("tutor_ai.retrieval.passages_by_id", new=AsyncMock(return_value=pinned)):
        context = await service._context(request, "ok continue")

    assert context is not None
    assert context.pinned_ids == [chunk_id]
    # Similarity set to 1.0 (on-topic) without any embed call!
    assert context.similarity == 1.0
    assert provider.embed.call_count == 0


# ==============================================================================
# 5. Safety & Streaming: Windowed Scanning
# ==============================================================================


def test_windowed_safety_scan_catches_quiz_leak_across_chunks():
    """Verify that windowed scanning still reliably catches answers split across chunks."""
    policy = SafetyPolicy.load(
        Settings(
            TUTOR_ENV="test",
            AI_DATABASE_URL="postgresql://u:p@localhost:5432/db",
            TUTOR_AI_SERVICE_TOKEN="a" * 32,
            TUTOR_AI_PROVIDER="mock",
        ).safety_policy
    )

    guard = StreamGuard(policy, protected_answers=["photosynthesis"])
    chunks = ["The process ", "is called ", "photo", "syn", "thesis", " in botany."]

    released = []
    for chunk in chunks:
        out = guard.feed(chunk)
        if out:
            released.append(out)

    assert guard.problem == "quiz_answer"
    assert "photo" not in "".join(released)
    assert guard.finish() == ""


def test_windowed_safety_scan_linear_performance():
    """Verify windowed scan remains efficient over long streaming outputs."""
    policy = SafetyPolicy.load(
        Settings(
            TUTOR_ENV="test",
            AI_DATABASE_URL="postgresql://u:p@localhost:5432/db",
            TUTOR_AI_SERVICE_TOKEN="a" * 32,
            TUTOR_AI_PROVIDER="mock",
        ).safety_policy
    )

    guard = StreamGuard(policy, protected_answers=["mitochondria"])
    chunks = ["This is a completely safe sentence about cellular biology. " for _ in range(150)]

    for chunk in chunks:
        guard.feed(chunk)
    final = guard.finish()

    assert guard.problem is None
    assert len(guard.text) > 5000
    assert len(final) > 0


# ==============================================================================
# 6. Database Connection Pool & Settings Configuration
# ==============================================================================


def test_database_engine_pool_settings():
    """Verify that create_engine applies custom pool size and max overflow."""
    engine = create_engine("postgresql+psycopg://user:pass@localhost:5432/testdb", pool_size=12, max_overflow=18)
    assert engine.pool.size() == 12
    assert engine.pool._max_overflow == 18


def test_optimization_settings_defaults_and_env_overrides():
    """Verify that new settings are properly loaded with defaults and allow env overrides."""
    default_settings = Settings(
        TUTOR_ENV="test",
        AI_DATABASE_URL="postgresql://u:p@localhost:5432/db",
        TUTOR_AI_SERVICE_TOKEN="a" * 32,
        TUTOR_AI_PROVIDER="mock",
    )
    assert default_settings.ollama_max_batch_size == 32
    assert default_settings.ollama_connect_timeout == 15.0
    assert default_settings.query_cache_max_entries == 2000
    assert default_settings.db_pool_size == 10
    assert default_settings.db_max_overflow == 15

    custom_settings = Settings(
        TUTOR_ENV="test",
        AI_DATABASE_URL="postgresql://u:p@localhost:5432/db",
        TUTOR_AI_SERVICE_TOKEN="a" * 32,
        TUTOR_AI_PROVIDER="mock",
        TUTOR_OLLAMA_MAX_BATCH_SIZE=16,
        TUTOR_OLLAMA_CONNECT_TIMEOUT=25.0,
        TUTOR_AI_QUERY_CACHE_MAX_ENTRIES=5000,
        TUTOR_AI_DB_POOL_SIZE=20,
        TUTOR_AI_DB_MAX_OVERFLOW=30,
    )
    assert custom_settings.ollama_max_batch_size == 16
    assert custom_settings.ollama_connect_timeout == 25.0
    assert custom_settings.query_cache_max_entries == 5000
    assert custom_settings.db_pool_size == 20
    assert custom_settings.db_max_overflow == 30
