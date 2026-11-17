r"""
Tutor AI - Whole Course Retrieval Diagnostic

Purpose:
  Search the ENTIRE course in ai.lesson_chunk for a question.
  This diagnostic deliberately does NOT filter by lesson_id.

It performs:
  1. Course inventory (number of chunks and distinct lessons).
  2. Course-wide vector search using the configured Ollama embedding model.
  3. Keyword search across all chunks in the course.
  4. Prints the source lesson IDs, headings, scores, and complete chunk text.

Read-only: this script does not insert, update, or delete database records.
Run from the ai directory:
    python .\investigation\search_entire_course.py
"""

import asyncio
import json
import selectors
import sys
from pathlib import Path

from sqlalchemy import func, or_, select

# Ensure imports work when launched from the ai directory or script directory.
AI_DIR = Path(__file__).resolve().parents[1]
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from tutor_ai import retrieval
from tutor_ai.db import create_engine
from tutor_ai.providers.factory import build_provider
from tutor_ai.schema import lesson_chunk
from tutor_ai.settings import get_settings

# Use the course already used by the successful Tutor load test.
# IMPORTANT: No lesson ID is used anywhere in this diagnostic.
COURSE_ID = "f0000000-0000-0000-0000-000000000001"

QUESTION = "What is an intelligent system? Explain with a practical example."

# Return more course-wide candidates than the normal tutor context.
VECTOR_TOP_K = 30
KEYWORD_LIMIT = 50

OUTPUT_FILE = Path(__file__).resolve().parents[1] / "course_wide_search_results.txt"


def section(title: str):
    print("\n" + "=" * 88)
    print(title)
    print("=" * 88)


async def main():
    settings = get_settings()
    provider = build_provider(settings)
    engine = create_engine(settings.database_url.get_secret_value())

    report = {
        "question": QUESTION,
        "course_id": COURSE_ID,
        "embedding_model": provider.embedding_model,
        "provider": provider.name,
        "lesson_filter": None,
        "course_inventory": {},
        "semantic_results": [],
        "keyword_results": [],
    }

    try:
        section("TUTOR AI - ENTIRE COURSE SEARCH")
        print(f"Question: {QUESTION}")
        print(f"Course ID: {COURSE_ID}")
        print(f"Embedding model: {provider.embedding_model}")
        print(f"Chat provider: {provider.name}")
        print("Lesson filter: NONE (searching all lessons in this course)")
        print("Database mode: READ ONLY")

        # Create query embedding using the same configured embedding model
        # used by Tutor retrieval.
        vectors = await provider.embed([QUESTION], kind="query")
        query_vector = vectors[0]

        async with engine.connect() as conn:
            # Course inventory, restricted only by course and embedding model.
            inventory_query = select(
                func.count(lesson_chunk.c.id).label("chunk_count"),
                func.count(func.distinct(lesson_chunk.c.lesson_id)).label("lesson_count"),
            ).where(
                lesson_chunk.c.course_id == COURSE_ID,
                lesson_chunk.c.embedding_model == provider.embedding_model,
            )
            inventory = (await conn.execute(inventory_query)).one()

            report["course_inventory"] = {
                "total_chunks": inventory.chunk_count,
                "distinct_lessons": inventory.lesson_count,
            }

            section("1. COURSE INVENTORY")
            print(f"Total indexed chunks: {inventory.chunk_count}")
            print(f"Distinct lessons: {inventory.lesson_count}")

            # Course-wide semantic search. No lesson_id predicate.
            semantic = await retrieval.search(
                conn,
                query_vector,
                course_id=__import__("uuid").UUID(COURSE_ID),
                model=provider.embedding_model,
                k=VECTOR_TOP_K,
                minimum=-1.0,
            )

            section(f"2. TOP {len(semantic)} SEMANTIC MATCHES ACROSS THE WHOLE COURSE")
            for rank, passage in enumerate(semantic, start=1):
                item = {
                    "rank": rank,
                    "similarity": round(passage.similarity or 0.0, 4),
                    "lesson_id": str(passage.lesson_id),
                    "chunk_id": str(passage.chunk_id),
                    "position": passage.position,
                    "heading": passage.heading,
                    "text": passage.text,
                }
                report["semantic_results"].append(item)

                print(f"\n[{rank}] Similarity: {item['similarity']}")
                print(f"Lesson ID: {item['lesson_id']}")
                print(f"Chunk ID: {item['chunk_id']}")
                print(f"Position: {item['position']}")
                print(f"Heading: {item['heading']}")
                print("-" * 88)
                print(item["text"])

            # Keyword search across every lesson in the course.
            # This is a second independent check, not limited to vector top-K.
            keyword_query = (
                select(
                    lesson_chunk.c.id,
                    lesson_chunk.c.lesson_id,
                    lesson_chunk.c.position,
                    lesson_chunk.c.heading,
                    lesson_chunk.c.text,
                )
                .where(
                    lesson_chunk.c.course_id == COURSE_ID,
                    lesson_chunk.c.embedding_model == provider.embedding_model,
                    or_(
                        lesson_chunk.c.text.ilike("%intelligent%"),
                        lesson_chunk.c.heading.ilike("%intelligent%"),
                        lesson_chunk.c.text.ilike("%intelligence%"),
                        lesson_chunk.c.heading.ilike("%intelligence%"),
                    ),
                )
                .order_by(lesson_chunk.c.lesson_id, lesson_chunk.c.position)
                .limit(KEYWORD_LIMIT)
            )
            keyword_rows = (await conn.execute(keyword_query)).all()

            section(f"3. KEYWORD MATCHES ACROSS THE WHOLE COURSE ({len(keyword_rows)})")
            for rank, row in enumerate(keyword_rows, start=1):
                item = {
                    "rank": rank,
                    "lesson_id": str(row.lesson_id),
                    "chunk_id": str(row.id),
                    "position": row.position,
                    "heading": row.heading,
                    "text": row.text,
                }
                report["keyword_results"].append(item)

                print(f"\n[Keyword {rank}]")
                print(f"Lesson ID: {item['lesson_id']}")
                print(f"Chunk ID: {item['chunk_id']}")
                print(f"Position: {item['position']}")
                print(f"Heading: {item['heading']}")
                print("-" * 88)
                print(item["text"])

        # Save a complete report for sharing. No credentials are included.
        OUTPUT_FILE.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        section("4. DIAGNOSTIC SUMMARY")
        print(f"Course chunks searched: {report['course_inventory']['total_chunks']}")
        print(f"Lessons searched: {report['course_inventory']['distinct_lessons']}")
        print(f"Semantic matches returned: {len(report['semantic_results'])}")
        print(f"Keyword matches returned: {len(report['keyword_results'])}")
        print(f"\nFull results saved to:\n{OUTPUT_FILE}")
        print("\nDONE. No database records were changed.")

    finally:
        await provider.aclose()
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(
            main(),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    except KeyboardInterrupt:
        print("\nSearch cancelled by user.")
    except Exception as exc:
        print(f"\nCOURSE SEARCH FAILED: {type(exc).__name__}: {exc}")
        raise
