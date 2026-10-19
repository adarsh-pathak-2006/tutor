#!/usr/bin/env bash
# Rehearse the hosted AI service locally before deploying (docs/engineering/deployment.md, D41).
#
# Runs the same build and start commands as render.yaml, with the hosted settings (TUTOR_ENV=hosted, mock provider),
# then checks what Render and Django will rely on:
#   1. build: install + alembic upgrade head        5. Django sync_ai_index indexes every published lesson
#   2. start command exactly as on Render            6. a real student tutor turn through Django, end to end
#   3. /health answers 200, API docs hidden          7. clean up (stop the service, delete the test chat)
#   4. /v1 needs the service token
# Uses tutor/.env for AI_DATABASE_URL and TUTOR_AI_SERVICE_TOKEN (never printed) and your local database.
# Output is also saved to .rehearsal.log (git-ignored). Prints "REHEARSAL: PASS" or "REHEARSAL: FAIL".
set -uo pipefail
cd "$(dirname "$0")"
AI_VENV="${TUTOR_AI_VENV:-$HOME/.venvs/tp-ai}"
PLATFORM_VENV="${TUTOR_VENV:-$HOME/.venvs/tp-platform}"
PORT="${REHEARSAL_PORT:-8011}"
URL="http://127.0.0.1:$PORT"

{
  fail() { echo "FAIL: $*"; echo "REHEARSAL: FAIL"; exit 1; }
  pass() { echo "ok:   $*"; }

  # Both services read tutor/.env themselves; the script only needs the token (for curl) and checks the URL exists.
  env_value() { grep -E "^$1=" ../.env | tail -1 | cut -d= -f2- | sed -e "s/^[\"']//" -e "s/[\"']\$//"; }
  TOKEN="${TUTOR_AI_SERVICE_TOKEN:-$(env_value TUTOR_AI_SERVICE_TOKEN)}"
  [ -n "${AI_DATABASE_URL:-$(env_value AI_DATABASE_URL)}" ] || fail "AI_DATABASE_URL is not set in tutor/.env"
  [ ${#TOKEN} -ge 32 ] || fail "TUTOR_AI_SERVICE_TOKEN in tutor/.env must be at least 32 characters"
  export TUTOR_ENV=hosted TUTOR_AI_PROVIDER=mock TUTOR_TRUSTED_PROXIES=1  # override .env, as Render's env vars do

  echo "== 1. build (as render.yaml buildCommand)"
  source "$AI_VENV/bin/activate"
  pip install -q -r requirements.txt || fail "pip install"
  alembic upgrade head || fail "alembic upgrade head"
  pass "dependencies installed, migrations at head"

  echo "== 2. start (as render.yaml startCommand, PORT=$PORT)"
  uvicorn tutor_ai.main:create_app --factory --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips '*' \
    > .rehearsal-server.log 2>&1 &
  SERVER=$!
  trap 'kill $SERVER 2>/dev/null' EXIT
  for _ in $(seq 1 30); do curl -s -o /dev/null "$URL/health" && break; sleep 1; done
  kill -0 $SERVER 2>/dev/null || { tail -20 .rehearsal-server.log; fail "the service did not start"; }
  pass "service started"

  echo "== 3. health and docs"
  health=$(curl -s -w ' %{http_code}' "$URL/health")
  echo "      /health -> $health"
  [[ "$health" == *'"ok":true'*'"provider":"mock"'*' 200' ]] || fail "/health should be 200 with ok true and provider mock"
  [ "$(curl -s -o /dev/null -w '%{http_code}' "$URL/docs")" = 404 ] || fail "/docs must be hidden when hosted"
  pass "healthy, docs hidden"

  echo "== 4. service token"
  [ "$(curl -s -o /dev/null -w '%{http_code}' "$URL/v1/whoami")" = 401 ] || fail "/v1 without a token must be 401"
  whoami=$(curl -s -H "Authorization: Bearer $TOKEN" "$URL/v1/whoami")
  echo "      /v1/whoami -> $whoami"
  [[ "$whoami" == *'"provider":"mock"'* ]] || fail "/v1/whoami with the token"
  pass "token required and accepted"
  deactivate
  unset TUTOR_ENV TUTOR_AI_PROVIDER TUTOR_TRUSTED_PROXIES  # AI-service settings; Django runs with its own .env

  echo "== 5. Django indexes every published lesson"
  source "$PLATFORM_VENV/bin/activate"
  cd ../backend
  export TUTOR_AI_URL="$URL"
  python manage.py sync_ai_index --all --strict || fail "sync_ai_index"
  indexed=$(curl -s -H "Authorization: Bearer $TOKEN" "$URL/v1/index/status" | python -c \
    "import json,sys; print(len(json.load(sys.stdin)['lessons']))")
  echo "      lessons in the index: $indexed"
  [ "$indexed" -gt 0 ] || fail "no lessons were indexed"
  pass "index in step with published content"

  echo "== 6. a student tutor turn through Django"
  python manage.py shell -c '
import json, sys
from django.test import Client
from apps.catalogue.models import Lesson
from apps.commerce.models import Entitlement
from apps.tutor.models import TutorConversation

ent = (Entitlement.objects.filter(product_type="course", revoked_at__isnull=True,
                                  student__student_profile__status="active").select_related("student").first())
if ent is None:
    sys.exit("FAIL: no active student with a course enrolment in the local database (run seed_demo)")
lesson = Lesson.objects.filter(module__course_id=ent.product_id, versions__status="published").first()
if lesson is None:
    sys.exit("FAIL: the enrolled course has no published lesson")
client = Client(SERVER_NAME="localhost")
client.force_login(ent.student)
started = client.post("/api/v1/tutor/conversations", {"lesson_id": str(lesson.id)}, content_type="application/json")
if started.status_code != 201:
    sys.exit(f"FAIL: starting a chat answered {started.status_code}: {started.content[:300]!r}")
chat = started.json()["id"]
reply = client.post(f"/api/v1/tutor/conversations/{chat}/messages", {"message": "What is this lesson about?"},
                    content_type="application/json")
body = b"".join(reply.streaming_content).decode()
events = [block.split("\n")[0].removeprefix("event: ") for block in body.strip().split("\n\n")]
final = json.loads(body.strip().split("\n\n")[-1].split("data: ", 1)[1])
print("      lesson:", lesson.title)
print("      events:", " ".join(events))
print("      answer:", final.get("text", final)[:200])
TutorConversation.objects.filter(pk=chat).delete()
if events[-1] != "final" or not final.get("text"):
    sys.exit("FAIL: the turn did not end with a final answer")
' || fail "tutor turn"
  pass "streamed answer stored and cleaned up"

  echo "REHEARSAL: PASS"
} 2>&1 | tee .rehearsal.log
