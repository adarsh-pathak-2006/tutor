"""
Tutor AI - 10 Concurrent Student Load Test

Runs 10 concurrent requests against an already-running Tutor AI server.
Captures complete SSE answers and generates:
  - reports/tutor_load_test_report.html
  - reports/tutor_load_test_results.json

Run from the ai directory:
    python .\\manual_tests\test_tutor_load_10.py
"""

import asyncio
import html
import json
import os
import time
import uuid
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

import httpx

# ============================================================
# CONFIGURATION
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
AI_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = AI_DIR.parent
ENV_FILE = PROJECT_ROOT / ".env"

BASE_URL = os.getenv(
    "TUTOR_AI_BASE_URL",
    "http://127.0.0.1:8001",
).rstrip("/")
ENDPOINT = f"{BASE_URL}/v1/tutor/turns"

CONCURRENT_STUDENTS = 10
TIMEOUT_SECONDS = 180

REPORT_DIR = AI_DIR / "reports"
JSON_REPORT_FILE = REPORT_DIR / "tutor_load_test_results.json"
HTML_REPORT_FILE = REPORT_DIR / "tutor_load_test_report.html"

LESSON_ID = "0452e793-3dbc-5561-a6d9-c5803d612b53"
LESSON_VERSION_ID = "dd78edc5-91e3-5289-8c95-682895561174"
COURSE_ID = "f0000000-0000-0000-0000-000000000001"
LESSON_TITLE = "M19 Syllabus & Labs"

STUDENT_QUESTIONS = [
    "Explain the main concepts covered in this lesson.",
    "What is an intelligent system? Explain with an example.",
    "Explain the importance of typed schemas in software systems.",
    "How does system performance affect application reliability?",
    "Explain the role of hardware and GPU in computing.",
    "What is latency, and why is it important?",
    "Explain the difference between CPU and GPU processing.",
    "How can we improve the performance of an application?",
    "Explain the important topics in this lesson in simple words.",
    "Give me a short summary of the lesson with practical examples.",
]


# ============================================================
# ENVIRONMENT AND REQUEST
# ============================================================


def load_env_file():
    if not ENV_FILE.exists():
        raise FileNotFoundError(f"Root .env file not found: {ENV_FILE}")

    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # The project .env is authoritative for this manual test.
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def get_headers():
    token = os.getenv("TUTOR_AI_SERVICE_TOKEN")
    if not token:
        raise RuntimeError("TUTOR_AI_SERVICE_TOKEN is missing from root .env")

    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }


def build_payload(student_number: int) -> dict:
    return {
        "learner_ref": f"lr_load_student_{student_number:03d}",
        "chat_id": str(uuid.uuid4()),
        "class_number": 10,
        "lesson": {
            "id": LESSON_ID,
            "version_id": LESSON_VERSION_ID,
            "course_id": COURSE_ID,
            "title": LESSON_TITLE,
        },
        "mode": "explain",
        "message": STUDENT_QUESTIONS[student_number - 1],
        "history": [],
        "pinned_chunk_ids": [],
        "guard": {
            "open_quiz": False,
            "protected_answers": [],
        },
        "limits": {"max_reply_tokens": 250},
    }


# ============================================================
# SSE PARSING
# ============================================================


def collect_text(value):
    """Extract likely answer text from common tutor SSE payload shapes."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(collect_text(item) for item in value)
    if not isinstance(value, dict):
        return ""

    # Common text/delta fields used by streaming APIs.
    for key in ("text", "delta", "content", "token", "answer", "reply"):
        item = value.get(key)
        if isinstance(item, str):
            return item
        if isinstance(item, (dict, list)):
            extracted = collect_text(item)
            if extracted:
                return extracted

    # Some APIs nest the final payload.
    for key in ("final", "message", "response", "data", "output"):
        item = value.get(key)
        if isinstance(item, (dict, list, str)):
            extracted = collect_text(item)
            if extracted:
                return extracted
    return ""


async def read_sse_response(response: httpx.Response) -> dict:
    result = {
        "final_received": False,
        "error": None,
        "events_received": 0,
        "answer": "",
        "events": [],
        "raw_preview": "",
    }

    current_event = None
    data_lines = []
    delta_parts = []
    final_answer = ""

    def process_event():
        nonlocal current_event, data_lines, final_answer

        if not data_lines:
            current_event = None
            return

        raw_data = "\n".join(data_lines)
        result["events_received"] += 1

        try:
            parsed = json.loads(raw_data)
        except json.JSONDecodeError:
            parsed = raw_data

        event_type = current_event
        if isinstance(parsed, dict):
            event_type = parsed.get("type") or parsed.get("event") or current_event

        result["events"].append(
            {
                "event": event_type or "message",
                "data": parsed,
            }
        )

        if not result["raw_preview"]:
            result["raw_preview"] = raw_data[:1000]

        if event_type == "error" or (isinstance(parsed, dict) and parsed.get("error")):
            if isinstance(parsed, dict):
                result["error"] = parsed.get("error", parsed)
            else:
                result["error"] = parsed

        if event_type == "final":
            result["final_received"] = True
            extracted = collect_text(parsed)
            if extracted:
                final_answer = extracted
            elif isinstance(parsed, dict):
                # Preserve the structured final event if its answer uses
                # a project-specific schema not recognized above.
                final_answer = json.dumps(parsed, ensure_ascii=False, indent=2)

        elif event_type in ("delta", "token", "content"):
            extracted = collect_text(parsed)
            if extracted:
                delta_parts.append(extracted)

        # Some implementations send a final marker inside the JSON payload.
        if isinstance(parsed, dict):
            if parsed.get("final") is True:
                result["final_received"] = True
            if any(k in parsed for k in ("answer", "reply")):
                extracted = collect_text(parsed)
                if extracted:
                    final_answer = extracted
                    result["final_received"] = True

        current_event = None
        data_lines = []

    async for line in response.aiter_lines():
        if line.startswith("event:"):
            current_event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
        elif line == "":
            process_event()

    process_event()

    # Prefer the complete final answer; otherwise combine streamed deltas.
    result["answer"] = final_answer or "".join(delta_parts)
    if result["answer"].strip() and not result["error"]:
        result["final_received"] = True

    return result


# ============================================================
# ONE STUDENT REQUEST
# ============================================================


async def test_student(
    client: httpx.AsyncClient,
    headers: dict,
    student_number: int,
) -> dict:
    learner_ref = f"lr_load_student_{student_number:03d}"
    question = STUDENT_QUESTIONS[student_number - 1]
    payload = build_payload(student_number)

    print(f"[Student {student_number:02d}] Sending request...")
    started = time.perf_counter()

    result = {
        "student": student_number,
        "learner_ref": learner_ref,
        "question": question,
        "status_code": None,
        "success": False,
        "response_time_seconds": None,
        "events_received": 0,
        "answer": "",
        "error": None,
        "event_details": [],
    }

    try:
        async with client.stream(
            "POST",
            ENDPOINT,
            headers=headers,
            json=payload,
        ) as response:
            result["status_code"] = response.status_code

            if response.status_code != 200:
                body = await response.aread()
                result["error"] = body.decode("utf-8", errors="replace")[:2000]
            else:
                stream = await read_sse_response(response)
                result["events_received"] = stream["events_received"]
                result["answer"] = stream["answer"]
                result["error"] = stream["error"]
                result["event_details"] = stream["events"]
                result["success"] = (
                    stream["final_received"] and stream["error"] is None and bool(stream["answer"].strip())
                )
                if not result["success"] and not result["error"]:
                    result["error"] = (
                        "No complete answer/final SSE event was detected. Inspect event_details in the JSON report."
                    )

    except httpx.TimeoutException:
        result["error"] = f"Request timed out after {TIMEOUT_SECONDS} seconds."
    except httpx.ConnectError:
        result["error"] = "Could not connect to Tutor AI server on port 8001."
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"

    result["response_time_seconds"] = round(time.perf_counter() - started, 3)

    status = "SUCCESS" if result["success"] else "FAILED"
    print(
        f"[Student {student_number:02d}] {status} | "
        f"HTTP {result['status_code']} | "
        f"{result['response_time_seconds']} sec"
    )
    return result


# ============================================================
# HTML REPORT
# ============================================================


def make_html_report(report: dict) -> str:
    summary = report["summary"]
    results = report["results"]
    success_rate = summary["successful_requests"] / summary["total_requests"] * 100 if summary["total_requests"] else 0

    cards = []
    for item in results:
        ok = item["success"]
        status_class = "success" if ok else "failed"
        status_label = "SUCCESS" if ok else "FAILED"
        answer = item.get("answer") or ("No answer was captured for this request.")
        error_html = ""
        if item.get("error"):
            error_html = f'<div class="error-box"><strong>Error</strong><br>{html.escape(str(item["error"]))}</div>'

        cards.append(f"""
        <details class="student-card {status_class}">
          <summary>
            <span class="student-name">Student {item["student"]:02d}</span>
            <span class="badge {status_class}">{status_label}</span>
            <span class="time">{item.get("response_time_seconds", 0):.3f}s</span>
            <span class="chevron">＋</span>
          </summary>
          <div class="student-content">
            <div class="meta-grid">
              <div><label>Learner reference</label><strong>{html.escape(item["learner_ref"])}</strong></div>
              <div><label>HTTP status</label><strong>{item.get("status_code") or "N/A"}</strong></div>
              <div><label>SSE events</label><strong>{item.get("events_received", 0)}</strong></div>
            </div>
            <div class="section-title">Student question</div>
            <div class="question">{html.escape(item.get("question", ""))}</div>
            <div class="section-title">Gemini tutor answer</div>
            <div class="answer">{html.escape(answer)}</div>
            {error_html}
          </div>
        </details>
        """)

    generated = html.escape(report["timestamp"])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tutor AI | 10 Student Load Test</title>
<style>
:root {{
  --bg:#f4f7fb; --panel:#fff; --ink:#172033; --muted:#667085;
  --line:#e7ebf2; --blue:#3659d9; --green:#168653; --red:#c83d4d;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font-family:Inter,Segoe UI,Arial,sans-serif; }}
.wrap {{ max-width:1180px; margin:0 auto; padding:34px 22px 60px; }}
.hero {{ background:linear-gradient(120deg,#172b65,#3e5cda);
  color:white; border-radius:22px; padding:30px 32px;
  box-shadow:0 12px 32px #253d8425; }}
.eyebrow {{ text-transform:uppercase; letter-spacing:2px;
  font-size:11px; opacity:.78; font-weight:700; }}
h1 {{ margin:10px 0 8px; font-size:30px; }}
.subtitle {{ margin:0; opacity:.85; }}
.hero-meta {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:22px; }}
.hero-meta span {{ background:#ffffff20; padding:8px 12px;
  border-radius:9px; font-size:12px; }}
.section-heading {{ display:flex; justify-content:space-between;
  align-items:end; gap:12px; margin:30px 0 14px; }}
h2 {{ margin:0; font-size:20px; }}
.muted {{ color:var(--muted); font-size:13px; }}
.metrics {{ display:grid; grid-template-columns:repeat(5,1fr); gap:13px; }}
.metric {{ background:var(--panel); border:1px solid var(--line);
  border-radius:15px; padding:18px; }}
.metric label {{ display:block; color:var(--muted); font-size:12px; }}
.metric strong {{ display:block; margin-top:8px; font-size:26px; }}
.metric .unit {{ font-size:13px; color:var(--muted); font-weight:500; }}
.rate {{ height:9px; background:#e8edf5; border-radius:99px;
  overflow:hidden; margin-top:17px; }}
.rate span {{ display:block; height:100%; width:{success_rate:.1f}%;
  background:var(--green); border-radius:99px; }}
.student-list {{ display:grid; gap:12px; }}
.student-card {{ background:white; border:1px solid var(--line);
  border-left:4px solid var(--green); border-radius:14px;
  overflow:hidden; box-shadow:0 3px 12px #18294b08; }}
.student-card.failed {{ border-left-color:var(--red); }}
summary {{ list-style:none; cursor:pointer; display:flex;
  align-items:center; gap:15px; padding:17px 19px; }}
summary::-webkit-details-marker {{ display:none; }}
.student-name {{ font-weight:700; min-width:105px; }}
.badge {{ font-size:10px; font-weight:800; letter-spacing:.6px;
  padding:6px 9px; border-radius:30px; }}
.badge.success {{ color:#087346; background:#e4f6ed; }}
.badge.failed {{ color:#a92739; background:#fdebed; }}
.time {{ margin-left:auto; font-weight:700; font-variant-numeric:tabular-nums; }}
.chevron {{ color:var(--muted); font-size:20px; }}
.student-content {{ border-top:1px solid var(--line); padding:20px; }}
.meta-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }}
.meta-grid div {{ background:#f7f9fc; padding:12px; border-radius:10px; }}
.meta-grid label {{ display:block; font-size:11px; color:var(--muted); margin-bottom:5px; }}
.meta-grid strong {{ font-size:13px; overflow-wrap:anywhere; }}
.section-title {{ font-size:12px; text-transform:uppercase; letter-spacing:1px;
  font-weight:800; color:var(--muted); margin:20px 0 8px; }}
.question {{ background:#f2f5ff; border:1px solid #e2e8ff;
  padding:13px 15px; border-radius:10px; line-height:1.55; }}
.answer {{ white-space:pre-wrap; line-height:1.7; padding:17px;
  background:#fbfcfe; border:1px solid var(--line); border-radius:10px;
  font-size:14px; }}
.error-box {{ margin-top:15px; color:#9f2637; background:#fff0f1;
  border:1px solid #ffd6da; padding:13px; border-radius:10px;
  white-space:pre-wrap; overflow-wrap:anywhere; }}
.footer {{ margin-top:28px; color:var(--muted); font-size:12px;
  text-align:center; }}
@media(max-width:850px) {{ .metrics {{ grid-template-columns:repeat(2,1fr); }} }}
@media(max-width:560px) {{
  .wrap {{ padding:18px 12px 40px; }} .hero {{ padding:23px 20px; }}
  h1 {{ font-size:24px; }} .metrics {{ grid-template-columns:1fr 1fr; }}
  .meta-grid {{ grid-template-columns:1fr; }} summary {{ gap:9px; }}
  .student-name {{ min-width:auto; }} .badge {{ font-size:9px; }}
}}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <div class="eyebrow">Performance test report</div>
    <h1>Tutor AI · Concurrent Student Load Test</h1>
    <p class="subtitle">10 students submitted lesson questions at the same time.</p>
    <div class="hero-meta">
      <span>Provider: Gemini + Ollama embeddings</span>
      <span>Lesson: {html.escape(report["lesson"]["title"])}</span>
      <span>Concurrency: {summary["total_requests"]} students</span>
    </div>
  </header>

  <div class="section-heading">
    <h2>Test overview</h2>
    <span class="muted">Generated: {generated}</span>
  </div>
  <section class="metrics">
    <div class="metric"><label>Success rate</label><strong>{success_rate:.0f}%</strong><div class="rate"><span></span></div></div>
    <div class="metric"><label>Successful</label><strong>{summary["successful_requests"]}<span class="unit"> / {summary["total_requests"]}</span></strong></div>
    <div class="metric"><label>Failed</label><strong>{summary["failed_requests"]}</strong></div>
    <div class="metric"><label>Average response</label><strong>{summary["average_response_time_seconds"]}<span class="unit"> sec</span></strong></div>
    <div class="metric"><label>Total test duration</label><strong>{summary["total_execution_time_seconds"]}<span class="unit"> sec</span></strong></div>
  </section>

  <div class="section-heading">
    <h2>Student responses</h2>
    <span class="muted">Select a student to view the full question and AI answer</span>
  </div>
  <section class="student-list">
    {"".join(cards)}
  </section>
  <div class="footer">
    Tutor AI test report · Lesson ID: {html.escape(report["lesson"]["lesson_id"])}
    <br>Response times are measured by this test client; this run is not a production capacity certification.
  </div>
</div>
</body>
</html>"""


# ============================================================
# RUN TEST AND SAVE REPORTS
# ============================================================


async def run_load_test():
    load_env_file()
    headers = get_headers()

    print("\n" + "=" * 66)
    print("TUTOR AI - 10 CONCURRENT STUDENT LOAD TEST")
    print("=" * 66)
    print(f"Server: {BASE_URL}")
    print(f"Endpoint: {ENDPOINT}")
    print("Provider: Gemini + Ollama embeddings")
    print(f"Lesson: {LESSON_TITLE}")
    print(f"Concurrent students: {CONCURRENT_STUDENTS}")
    print("=" * 66)
    print("\nStarting concurrent requests...\n")

    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS)) as client:
        tasks = [test_student(client, headers, number) for number in range(1, CONCURRENT_STUDENTS + 1)]
        results = await asyncio.gather(*tasks)

    total_time = round(time.perf_counter() - started, 3)
    successful = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]
    response_times = [r["response_time_seconds"] for r in results if r["response_time_seconds"] is not None]

    average = round(sum(response_times) / len(response_times), 3) if response_times else 0
    fastest = min(response_times) if response_times else None
    slowest = max(response_times) if response_times else None

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    report = {
        "test_name": "Tutor AI 10 Concurrent Student Load Test",
        "timestamp": datetime.now(UTC).isoformat(),
        "server": BASE_URL,
        "endpoint": ENDPOINT,
        "provider": "Gemini + Ollama embeddings",
        "lesson": {
            "title": LESSON_TITLE,
            "lesson_id": LESSON_ID,
            "lesson_version_id": LESSON_VERSION_ID,
        },
        "configuration": {
            "concurrent_students": CONCURRENT_STUDENTS,
            "timeout_seconds": TIMEOUT_SECONDS,
        },
        "summary": {
            "total_requests": CONCURRENT_STUDENTS,
            "successful_requests": len(successful),
            "failed_requests": len(failed),
            "success_rate_percent": round(len(successful) / CONCURRENT_STUDENTS * 100, 2),
            "total_execution_time_seconds": total_time,
            "average_response_time_seconds": average,
            "fastest_response_seconds": fastest,
            "slowest_response_seconds": slowest,
        },
        "results": results,
    }

    JSON_REPORT_FILE.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    HTML_REPORT_FILE.write_text(
        make_html_report(report),
        encoding="utf-8",
    )

    print("\n" + "=" * 66)
    print("LOAD TEST SUMMARY")
    print("=" * 66)
    print(f"Total students:       {CONCURRENT_STUDENTS}")
    print(f"Successful requests:  {len(successful)}")
    print(f"Failed requests:      {len(failed)}")
    print(f"Success rate:         {report['summary']['success_rate_percent']}%")
    print(f"Total execution time: {total_time} seconds")
    print(f"Average response:     {average} seconds")
    if response_times:
        print(f"Fastest response:     {fastest} seconds")
        print(f"Slowest response:     {slowest} seconds")

    print("\nSTUDENT ANSWER PREVIEW")
    print("-" * 66)
    for item in results:
        print(f"\nStudent {item['student']:02d} | {'SUCCESS' if item['success'] else 'FAILED'}")
        print(f"Question: {item['question']}")
        if item["answer"]:
            preview = item["answer"].strip()
            print("Answer:")
            print(preview[:700] + ("..." if len(preview) > 700 else ""))
        else:
            print(f"Answer unavailable. Error: {item['error']}")

    print("\n" + "=" * 66)
    print("REPORT FILES")
    print("=" * 66)
    print(f"HTML dashboard: {HTML_REPORT_FILE}")
    print(f"JSON details:   {JSON_REPORT_FILE}")

    if failed:
        print("\nLOAD TEST FINISHED WITH FAILURES.")
    else:
        print("\nALL 10 REQUESTS COMPLETED SUCCESSFULLY.")

    try:
        webbrowser.open(HTML_REPORT_FILE.resolve().as_uri())
        print("\nOpened HTML dashboard in your default browser.")
    except Exception as exc:
        print(f"\nCould not open browser automatically: {exc}")
        print("Open the HTML report path manually.")


if __name__ == "__main__":
    try:
        asyncio.run(run_load_test())
    except KeyboardInterrupt:
        print("\nLoad test stopped by user.")
    except Exception as exc:
        print("\nLOAD TEST COULD NOT START")
        print(f"{type(exc).__name__}: {exc}")
