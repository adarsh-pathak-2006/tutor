"""
Manual test for Ollama embedding generation.

Checks:
1. Ollama connectivity.
2. Embedding model availability.
3. Embedding generation.
4. Vector dimensions.
5. Vector validity.
"""

import asyncio
import math
import sys
from pathlib import Path

# Windows asynchronous event-loop compatibility
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Add AI directory to Python path
AI_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AI_ROOT))

from tutor_ai.providers.ollama import OllamaProvider
from tutor_ai.settings import Settings


async def main():

    provider = None

    try:
        print("\n================================")
        print(" OLLAMA EMBEDDING TEST")
        print("================================\n")

        settings = Settings()

        provider = OllamaProvider(
            base_url=settings.ollama_url,
            chat_model=settings.ollama_chat_model,
            embedding_model=settings.ollama_embed_model,
            dimensions=settings.embedding_dimensions,
            keep_alive=settings.ollama_keep_alive,
            timeout_seconds=settings.ollama_timeout_seconds,
        )

        print("Ollama URL:", settings.ollama_url)
        print("Embedding model:", settings.ollama_embed_model)
        print("Expected dimensions:", settings.embedding_dimensions)

        # 1. Health check
        print("Checking Ollama embedding model health...")

        healthy = await provider.embedding_health()

        if not healthy:
            raise RuntimeError("Ollama is unavailable or required models are missing.")

        print("Ollama health: SUCCESS")

        # 2. Generate embeddings
        texts = [
            "Photosynthesis is the process by which plants make food.",
            "Plants use sunlight, water and carbon dioxide.",
        ]

        print("\nGenerating embeddings...")

        vectors = await provider.embed(
            texts,
            kind="document",
        )

        print("Embedding generation: SUCCESS")
        print("Number of vectors:", len(vectors))

        # 3. Validate dimensions
        for index, vector in enumerate(vectors):
            if len(vector) != settings.embedding_dimensions:
                raise ValueError(f"Vector {index} has incorrect dimensions: {len(vector)}")

            if not all(math.isfinite(value) for value in vector):
                raise ValueError(f"Vector {index} contains invalid numeric values.")

            print(
                f"Vector {index + 1} dimensions:",
                len(vector),
            )

        # 4. Display sample
        print("\nFirst five values of vector 1:")
        print(vectors[0][:5])

        print("\n================================")
        print(" OLLAMA EMBEDDING TEST PASSED")
        print("================================")

    except Exception as exc:
        print("\n================================")
        print(" OLLAMA EMBEDDING TEST FAILED")
        print("================================")

        print("Error type:", type(exc).__name__)
        print("Error:", str(exc))

    finally:
        if provider is not None:
            await provider.aclose()
            print("\nOllama connection closed.")


if __name__ == "__main__":
    asyncio.run(main())
