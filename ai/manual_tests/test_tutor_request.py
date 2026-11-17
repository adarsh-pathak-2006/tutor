"""
Single-student Tutor AI integration test.

Tests:
- Service authentication
- Gemini chat generation
- Ollama embeddings and lesson retrieval
- SSE streaming response
"""

import json
import os
import uuid
from pathlib import Path

import requests

# ============================================================
# 1. ENVIRONMENT CONFIGURATION
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


def load_env(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f".env file not found: {path}")

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)

        os.environ.setdefault(
            key.strip(),
            value.strip().strip('"').strip("'"),
        )


load_env(ENV_FILE)

TOKEN = os.getenv("TUTOR_AI_SERVICE_TOKEN")

if not TOKEN:
    raise RuntimeError("TUTOR_AI_SERVICE_TOKEN is missing from .env")


BASE_URL = os.getenv(
    "TUTOR_AI_BASE_URL",
    "http://127.0.0.1:8001",
).rstrip("/")


# ============================================================
# 2. LESSON CONFIGURATION
# ============================================================

LESSON_ID = "0452e793-3dbc-5561-a6d9-c5803d612b53"

LESSON_VERSION_ID = "dd78edc5-91e3-5289-8c95-682895561174"

COURSE_ID = "f0000000-0000-0000-0000-000000000001"

LESSON_TITLE = "M19 Syllabus & Labs"


# ============================================================
# 3. STUDENT REQUEST
# ============================================================

LEARNER_REF = os.getenv(
    "TUTOR_TEST_LEARNER_REF",
    "lr_test_student_001",
)

QUESTION = " explain me arithmetic intensity"

body = {
    "learner_ref": LEARNER_REF,
    "chat_id": str(uuid.uuid4()),
    "class_number": 10,
    "lesson": {
        "id": LESSON_ID,
        "version_id": LESSON_VERSION_ID,
        "course_id": COURSE_ID,
        "title": LESSON_TITLE,
    },
    "mode": "explain",
    "message": QUESTION,
    "history": [],
    "pinned_chunk_ids": [],
    "guard": {
        "open_quiz": False,
        "protected_answers": [],
    },
    "limits": {
        "max_reply_tokens": 250,
    },
}


# ============================================================
# 4. API CONFIGURATION
# ============================================================

URL = f"{BASE_URL}/v1/tutor/turns"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
}


# ============================================================
# 5. SEND REQUEST AND PROCESS SSE
# ============================================================


def main():

    print("\n================================")
    print(" TUTOR AI SINGLE STUDENT TEST")
    print("================================")

    print("Provider configuration: Gemini + Ollama")
    print("Lesson:", LESSON_TITLE)
    print("Lesson ID:", LESSON_ID)
    print("Learner reference:", LEARNER_REF)
    print("Question:", QUESTION)

    print("\nSending request to Tutor AI...")
    print("-" * 60)

    final_answer = None
    error_data = None
    current_event = None

    try:
        with requests.post(
            URL,
            headers=HEADERS,
            json=body,
            stream=True,
            timeout=(15, 240),
        ) as response:
            print("HTTP Status:", response.status_code)

            if response.status_code != 200:
                print("\nAPI ERROR:")
                print(response.text[:2000])
                return

            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue

                if line.startswith("event:"):
                    current_event = line[6:].strip()

                elif line.startswith("data:"):
                    raw_data = line[5:].strip()

                    try:
                        data = json.loads(raw_data)
                    except json.JSONDecodeError:
                        print("Invalid SSE JSON:", raw_data)
                        continue

                    if current_event == "delta":
                        text = data.get("text", "")
                        if text:
                            print(text, end="", flush=True)

                    elif current_event == "final":
                        final_answer = data.get("text", "")

                    elif current_event == "error":
                        error_data = data
                        break

            print("\n\n" + "=" * 60)

            if error_data:
                print("TUTOR AI ERROR")
                print(json.dumps(error_data, indent=2))
                return

            if final_answer is not None:
                print("TUTOR AI FINAL RESPONSE")
                print("=" * 60)
                print(final_answer)
                print("\nSINGLE STUDENT TEST PASSED")

            else:
                print("No final response received.")
                print("SINGLE STUDENT TEST FAILED")

            print("=" * 60)

    except requests.RequestException as exc:
        print("\nREQUEST FAILED")
        print(type(exc).__name__, str(exc))


if __name__ == "__main__":
    main()
