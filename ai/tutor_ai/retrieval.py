"""Finding the lesson passages the tutor may use (design §5 "Retrieval", §6).

- pin_context(): chosen once when a chat starts. The current lesson's passages come first (all of them if they fit
  the context budget; otherwise the ones closest to the student's first question, kept in lesson order), then the
  most similar passages from other lessons of the same course, if budget remains and they are relevant enough.
- passages_by_id(): later turns reload exactly the pinned passages, so the prompt prefix stays byte-identical.
- best_similarity(): how close a new message is to the pinned passages; below the threshold it is off the pinned
  context (design §6: one extra retrieval, then off-topic handling in §7).

Only vectors from the provider's current embedding model are ever compared (a model change means a re-index).
Similarity is cosine similarity (1 - cosine distance), in [-1, 1].
"""

import uuid
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .providers.base import ModelProvider
from .schema import lesson_chunk

# Relevance thresholds per provider until evals calibrate them (TUTOR_AI_MIN_SIMILARITY overrides). Hashed
# bag-of-words vectors (mock) score far lower than a trained model for the same pair of texts.
DEFAULT_MIN_SIMILARITY = {"mock": 0.10, "ollama": 0.45}
FALLBACK_MIN_SIMILARITY = 0.50

# In-memory LRU cache for query embeddings: (normalized_query, model) -> vector
_QUERY_EMBED_CACHE: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_DEFAULT_MAX_CACHE_ENTRIES = 2000


def clear_query_cache() -> None:
    """Clear query embedding cache (useful for testing and memory resets)."""
    _QUERY_EMBED_CACHE.clear()


def get_query_cache_stats() -> dict[str, int]:
    """Return cache statistics."""
    return {"size": len(_QUERY_EMBED_CACHE)}


async def get_query_embedding(
    provider: ModelProvider,
    query: str,
    *,
    max_entries: int = _DEFAULT_MAX_CACHE_ENTRIES,
) -> list[float]:
    """Retrieve embedding vector for query from LRU cache or compute it via provider."""
    normalized = " ".join(query.strip().lower().split())
    if not normalized:
        return []
    key = (normalized, provider.embedding_model)
    if key in _QUERY_EMBED_CACHE:
        _QUERY_EMBED_CACHE.move_to_end(key)
        return _QUERY_EMBED_CACHE[key]

    vectors = await provider.embed([query], kind="query")
    if not vectors:
        return []
    vector = vectors[0]

    if max_entries > 0:
        if len(_QUERY_EMBED_CACHE) >= max_entries:
            _QUERY_EMBED_CACHE.popitem(last=False)
        _QUERY_EMBED_CACHE[key] = vector
    return vector


@dataclass(frozen=True)
class Passage:
    chunk_id: uuid.UUID
    lesson_id: uuid.UUID
    heading: str
    text: str
    token_count: int
    position: int
    similarity: float | None = None


def min_similarity(configured: float | None, provider_name: str) -> float:
    if configured is not None:
        return configured
    return DEFAULT_MIN_SIMILARITY.get(provider_name, FALLBACK_MIN_SIMILARITY)


_COLUMNS = (
    lesson_chunk.c.id,
    lesson_chunk.c.lesson_id,
    lesson_chunk.c.heading,
    lesson_chunk.c.text,
    lesson_chunk.c.token_count,
    lesson_chunk.c.position,
)


def _passage(row: Any, similarity: float | None = None) -> Passage:
    return Passage(
        chunk_id=row.id,
        lesson_id=row.lesson_id,
        heading=row.heading,
        text=row.text,
        token_count=row.token_count,
        position=row.position,
        similarity=similarity,
    )


def _similarity(vector: Sequence[float]) -> Any:
    return 1 - lesson_chunk.c.embedding.cosine_distance(list(vector))


async def lesson_passages(conn: AsyncConnection, lesson_id: uuid.UUID, model: str) -> list[Passage]:
    query = (
        select(*_COLUMNS)
        .where(lesson_chunk.c.lesson_id == lesson_id, lesson_chunk.c.embedding_model == model)
        .order_by(lesson_chunk.c.position)
    )
    return [_passage(row) for row in (await conn.execute(query)).all()]


async def search(
    conn: AsyncConnection,
    vector: Sequence[float],
    *,
    course_id: uuid.UUID,
    model: str,
    k: int,
    minimum: float = -1.0,
    exclude_lesson: uuid.UUID | None = None,
) -> list[Passage]:
    """The k passages of a course most similar to `vector`, best first, at or above `minimum`."""
    # Candidates the HNSW index examines (default 40). pgvector filters by course *after* the index scan, so a wider
    # scan keeps k results available when a course is a small part of the table.
    await conn.execute(text("SET LOCAL hnsw.ef_search = 100"))
    distance = lesson_chunk.c.embedding.cosine_distance(list(vector))
    query = select(*_COLUMNS, _similarity(vector).label("similarity")).where(
        lesson_chunk.c.course_id == course_id, lesson_chunk.c.embedding_model == model
    )
    if exclude_lesson is not None:
        query = query.where(lesson_chunk.c.lesson_id != exclude_lesson)
    rows = (await conn.execute(query.order_by(distance).limit(k))).all()
    return [_passage(row, float(row.similarity)) for row in rows if row.similarity >= minimum]


async def passages_by_id(
    conn: AsyncConnection, chunk_ids: Sequence[uuid.UUID], model: str, vector: Sequence[float] | None = None
) -> list[Passage]:
    """The given passages in the given order; ids that no longer exist (re-indexed lesson) are left out."""
    if not chunk_ids:
        return []
    columns = [*_COLUMNS, _similarity(vector).label("similarity")] if vector is not None else list(_COLUMNS)
    query = select(*columns).where(lesson_chunk.c.id.in_(list(chunk_ids)), lesson_chunk.c.embedding_model == model)
    found = {
        row.id: _passage(row, float(row.similarity) if vector is not None else None)
        for row in (await conn.execute(query)).all()
    }
    return [found[i] for i in chunk_ids if i in found]


async def best_similarity(
    conn: AsyncConnection, chunk_ids: Sequence[uuid.UUID], vector: Sequence[float], model: str
) -> float | None:
    """The highest similarity between `vector` and the given passages (None if none of them exist)."""
    if not chunk_ids:
        return None
    query = select(func.max(_similarity(vector))).where(
        lesson_chunk.c.id.in_(list(chunk_ids)), lesson_chunk.c.embedding_model == model
    )
    value = (await conn.execute(query)).scalar()
    return None if value is None else float(value)


def _fill(candidates: Sequence[Passage], budget: int) -> list[Passage]:
    """Greedy: take candidates in order while they fit; a passage too big for what is left is skipped."""
    chosen: list[Passage] = []
    used = 0
    for passage in candidates:
        if used + passage.token_count <= budget:
            chosen.append(passage)
            used += passage.token_count
    return chosen


async def pin_context(
    engine: AsyncEngine,
    provider: ModelProvider,
    *,
    lesson_id: uuid.UUID,
    course_id: uuid.UUID,
    query: str | None,
    budget_tokens: int,
    related_k: int,
    minimum: float,
    max_cache_entries: int = _DEFAULT_MAX_CACHE_ENTRIES,
) -> list[Passage]:
    """Choose the passages for a new chat. Empty if the lesson isn't indexed (for this embedding model)."""
    model = provider.embedding_model
    query_text = query or ""
    has_query = bool(query_text.strip())
    vector: list[float] | None = None
    async with engine.connect() as conn:
        lesson = await lesson_passages(conn, lesson_id, model)
        if not lesson:
            return []
        total_lesson_tokens = sum(p.token_count for p in lesson)
        if total_lesson_tokens <= budget_tokens:
            chosen = lesson
        else:
            if has_query:
                vector = await get_query_embedding(provider, query_text, max_entries=max_cache_entries)
                scored = await passages_by_id(conn, [p.chunk_id for p in lesson], model, vector)
                best_first = sorted(scored, key=lambda p: (-(p.similarity or 0.0), p.position))
                chosen = sorted(_fill(best_first, budget_tokens), key=lambda p: p.position)
            else:
                chosen = _fill(lesson, budget_tokens)
        remaining = budget_tokens - sum(p.token_count for p in chosen)
        related: list[Passage] = []
        if related_k > 0 and remaining > 0 and has_query:
            if vector is None:
                vector = await get_query_embedding(provider, query_text, max_entries=max_cache_entries)
            found = await search(
                conn, vector, course_id=course_id, model=model, k=related_k, minimum=minimum, exclude_lesson=lesson_id
            )
            related = _fill(found, remaining)
    return [replace(p, similarity=None) for p in chosen] + related
