"""Manual test for the Gemini API connection."""

import asyncio
import sys
from pathlib import Path

# Add the AI project directory to Python's import path.
AI_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AI_ROOT))

from tutor_ai.providers.base import Message
from tutor_ai.providers.factory import build_provider
from tutor_ai.settings import Settings


async def main():
    settings = Settings()
    provider = build_provider(settings)

    try:
        print("Provider:", provider.name)
        print("Testing Gemini API...")

        messages = [
            Message(
                role="user",
                content="Reply with exactly: Gemini connection successful.",
            )
        ]

        print("\nGemini response:")

        async for chunk in provider.chat(
            messages,
            max_tokens=50,
            temperature=0,
        ):
            print(chunk, end="", flush=True)

        print("\n\nGemini API test completed successfully.")

    except Exception as exc:
        print("\nGemini API test failed.")
        print("Error type:", type(exc).__name__)
        print("Error:", str(exc))

    finally:
        await provider.aclose()


if __name__ == "__main__":
    asyncio.run(main())
