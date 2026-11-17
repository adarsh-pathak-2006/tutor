"""Rules for the AI tutor (docs/architecture/ai-service.md §2–§3, §7; decisions D13, D28, D40).

Before a message reaches the AI service: a logged-in student with active consent (D10), entitled to the course
(the free module does not include the tutor, FR-CAT-3), an AI service configured here (D28), and a message left in
today's limit. The AI service receives a pseudonymous learner_ref, never who the student is.

While the reply streams, meta and delta events are relayed to the browser; only the final event is stored. A turn
that fails gives the student's message back to the daily limit and stores nothing. A turn is never retried.
"""

import json
import logging
from collections.abc import AsyncIterator, Iterator
from functools import cache
from pathlib import Path
from typing import Any

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.aiservice.client import AIServiceClient, AIServiceError, get_client, is_configured
from apps.assessment.models import Attempt
from apps.catalogue.models import Lesson, PublishStatus
from apps.content import services as content_services
from apps.core.errors import ApiError
from apps.learning.access import entitled_course_ids, require_active_student

from .models import Mode, SafetyFlag, TutorConversation, TutorMessage, UsageCounter

logger = logging.getLogger("tutor.tutor")

HISTORY_MESSAGES = 12  # the AI service trims further to its own limits
LIST_LIMIT = 20
HELPLINES_FILE = Path(__file__).resolve().parent / "content" / "helplines.json"
PARENT_VISIBLE = frozenset({SafetyFlag.Severity.HIGH, SafetyFlag.Severity.CRITICAL})


# ---------------------------------------------------------------------------------------------------------------
# Who may use the tutor
# ---------------------------------------------------------------------------------------------------------------


def learner_ref(user) -> str:
    """A stable pseudonym for the AI service: the same student always gets the same value, which can't be turned back
    into an id without the secret key."""
    return "lr_" + salted_hmac("tutor.learner_ref", str(user.pk)).hexdigest()[:40]


def require_tutor_available() -> None:
    if not is_configured():
        raise ApiError(503, "feature_unavailable", "The AI tutor isn't available here yet.")


def tutor_lesson(user, lesson_id) -> Lesson:
    """The lesson, if this student may use the tutor on it."""
    require_active_student(user)
    lesson = (
        Lesson.objects.select_related("module__course__subject", "module__course__class_level")
        .filter(pk=lesson_id)
        .first()
    )
    if lesson is None:
        raise ApiError(404, "not_found", "Lesson not found.")
    course = lesson.module.course
    if course.status != PublishStatus.PUBLISHED or course.id not in entitled_course_ids(user):
        raise ApiError(403, "not_entitled", "The AI tutor comes with the full course. Enrol to use it.")
    return lesson


def conversation_for(user, conversation_id) -> TutorConversation:
    require_active_student(user)
    conversation = (
        TutorConversation.objects.select_related(
            "lesson__module__course__subject", "lesson__module__course__class_level"
        )
        .filter(pk=conversation_id, student=user, hidden_by_student=False)
        .first()
    )
    if conversation is None:
        raise ApiError(404, "not_found", "Conversation not found.")
    return conversation


def start_conversation(user, lesson_id, mode: str = Mode.EXPLAIN) -> TutorConversation:
    lesson = tutor_lesson(user, lesson_id)
    require_tutor_available()
    version = content_services.published_version(lesson)
    if version is None:
        raise ApiError(409, "lesson_not_ready", "This lesson isn't ready for the tutor yet.")
    return TutorConversation.objects.create(student=user, lesson=lesson, content_version_id=version.pk, mode=mode)


def list_conversations(user, lesson_id=None):
    require_active_student(user)
    conversations = TutorConversation.objects.filter(student=user, hidden_by_student=False)
    if lesson_id is not None:
        conversations = conversations.filter(lesson_id=lesson_id)
    return list(conversations.select_related("lesson")[:LIST_LIMIT])


def delete_conversation(user, conversation_id) -> None:
    """The student deletes a chat. One with a safety flag is only hidden from them: Operations and the parent still
    need it until the flag is closed (D40)."""
    conversation = conversation_for(user, conversation_id)
    if conversation.flagged:
        conversation.hidden_by_student = True
        conversation.save(update_fields=["hidden_by_student"])
    else:
        conversation.delete()


# ---------------------------------------------------------------------------------------------------------------
# Daily limit
# ---------------------------------------------------------------------------------------------------------------


def reserve_message(user) -> None:
    """Count one message against today's limit (IST), atomically; refuse when the limit is reached."""
    limit = settings.TUTOR_TUTOR_DAILY_LIMIT
    today = timezone.localdate()
    try:
        with transaction.atomic():
            UsageCounter.objects.get_or_create(student=user, day=today)
    except IntegrityError:  # created by a concurrent request between our lookup and insert
        pass
    reserved = UsageCounter.objects.filter(student=user, day=today, messages__lt=limit).update(
        messages=F("messages") + 1
    )
    if not reserved:
        raise ApiError(429, "daily_limit_reached", f"You've used today's {limit} tutor messages. Come back tomorrow!")


def release_message(user) -> None:
    UsageCounter.objects.filter(student=user, day=timezone.localdate(), messages__gt=0).update(
        messages=F("messages") - 1
    )


def usage_today(user) -> dict[str, int]:
    used = UsageCounter.objects.filter(student=user, day=timezone.localdate()).values_list("messages", flat=True)
    limit = settings.TUTOR_TUTOR_DAILY_LIMIT
    count = next(iter(used), 0)
    return {"used": count, "limit": limit, "left": max(limit - count, 0)}


# ---------------------------------------------------------------------------------------------------------------
# Building the request
# ---------------------------------------------------------------------------------------------------------------


def quiz_guard(user, lesson) -> dict[str, Any]:
    """While a quiz on this lesson is open, the tutor gives hints only and must never state the correct options of
    the questions still to answer (design §6)."""
    attempt = (
        Attempt.objects.filter(student=user, lesson=lesson, kind=Attempt.Kind.LESSON_QUIZ, submitted_at__isnull=True)
        .prefetch_related("items__question_version", "items__answer")
        .first()
    )
    if attempt is None:
        return {"open_quiz": False, "protected_answers": []}
    protected = []
    for item in attempt.items.all():
        if hasattr(item, "answer"):
            continue
        body = item.question_version.body or {}
        options, index = body.get("options") or [], body.get("answer_index")
        if isinstance(index, int) and 0 <= index < len(options) and str(options[index]).strip():
            protected.append(str(options[index]).strip()[:300])
    return {"open_quiz": True, "protected_answers": protected[:20]}


def history_for(conversation) -> list[dict[str, str]]:
    """Recent turns for the model, oldest first. Blocked turns are left out: fixed safety replies and the messages
    that caused them are not conversation the model should continue."""
    recent = list(conversation.messages.filter(blocked=False).order_by("-created_at", "-id")[:HISTORY_MESSAGES])
    return [{"role": m.role, "text": m.text[:4000]} for m in reversed(recent)]


def build_turn(user, conversation, message: str, mode: str) -> dict[str, Any]:
    lesson = conversation.lesson
    course = lesson.module.course
    return {
        "learner_ref": learner_ref(user),
        "chat_id": str(conversation.pk),
        "class_number": course.class_level.number if course.class_level_id else None,
        "lesson": {
            "id": str(lesson.pk),
            "version_id": str(conversation.content_version_id),
            "course_id": str(course.pk),
            "title": lesson.title[:150],
        },
        "mode": mode,
        "message": message,
        "history": history_for(conversation),
        "pinned_chunk_ids": [str(i) for i in conversation.pinned_chunk_ids or []][:50],
        "guard": quiz_guard(user, lesson),
        "limits": {"max_reply_tokens": settings.TUTOR_AI_REPLY_TOKENS},
    }


# ---------------------------------------------------------------------------------------------------------------
# Storing the answer
# ---------------------------------------------------------------------------------------------------------------


@cache
def helplines() -> dict[str, list[dict[str, str]]]:
    data = json.loads(HELPLINES_FILE.read_text(encoding="utf-8"))
    return {key: value for key, value in data.items() if not key.startswith("_")}


def _helplines_for(flags: list[SafetyFlag]) -> list[dict[str, str]]:
    severities = {f.severity for f in flags}
    if SafetyFlag.Severity.CRITICAL in severities:
        return helplines().get("critical", []) + helplines().get("high", [])
    if SafetyFlag.Severity.HIGH in severities:
        return helplines().get("high", [])
    return []


@transaction.atomic
def record_final(conversation, mode: str, final: dict[str, Any]) -> dict[str, Any]:
    """Store the student's message, the tutor's reply and any flags; return what the browser receives."""
    safety = final.get("safety") or {}
    usage = final.get("usage") or {}
    blocked = bool(final.get("blocked"))
    student = TutorMessage.objects.create(
        conversation=conversation,
        role=TutorMessage.Role.STUDENT,
        text=str(final.get("message") or ""),
        mode=mode,
        blocked=blocked,
    )
    reply = TutorMessage.objects.create(
        conversation=conversation,
        role=TutorMessage.Role.TUTOR,
        text=str(final.get("text") or ""),
        mode=str(final.get("mode") or mode),
        blocked=blocked,
        replaced=bool(final.get("replaced")),
        off_topic=bool(final.get("off_topic")),
        citations=[
            {
                "label": str(c.get("label", "")),
                "chunk_id": str(c.get("chunk_id", "")),
                "heading": str(c.get("heading", "")),
            }
            for c in final.get("citations") or []
        ],
        model=str(final.get("model") or ""),
        prompt_version=str(final.get("prompt_version") or ""),
        safety_version=str(final.get("safety_version") or ""),
        latency_ms=final.get("latency_ms") if isinstance(final.get("latency_ms"), int) else None,
        prompt_tokens=int(usage.get("prompt_tokens_estimate") or 0),
        reply_tokens=int(usage.get("reply_tokens_estimate") or 0),
    )
    flags = []
    for raw in final.get("flags") or []:
        if not raw.get("flagged"):
            continue  # low-severity notes (e.g. "ungrounded") stay in the message metadata only
        severity = raw.get("severity")
        if severity not in SafetyFlag.Severity.values:
            continue
        flags.append(
            SafetyFlag.objects.create(
                conversation=conversation,
                message=student if raw.get("stage") == "input" else reply,
                stage=str(raw.get("stage") or ""),
                category=str(raw.get("category") or "")[:40],
                severity=severity,
                parent_visible=severity in PARENT_VISIBLE,
            )
        )
    pinned = (final.get("context") or {}).get("pinned_chunk_ids")
    if pinned:
        conversation.pinned_chunk_ids = [str(i) for i in pinned][:50]
    conversation.mode = mode
    conversation.last_message_at = timezone.now()
    conversation.flagged = conversation.flagged or bool(flags)
    conversation.save(update_fields=["pinned_chunk_ids", "mode", "last_message_at", "flagged"])
    for flag in flags:
        if flag.severity == SafetyFlag.Severity.CRITICAL:
            # The Operations alert (D15). What happens next follows the written escalation procedure.
            logger.error(
                "tutor safety alert",
                extra={"flag_id": flag.pk, "conversation_id": str(conversation.pk), "category": flag.category},
            )
    return {
        "message_id": reply.pk,
        "text": reply.text,
        "mode": reply.mode,
        "blocked": reply.blocked,
        "replaced": reply.replaced,
        "off_topic": reply.off_topic,
        "citations": [{"label": c["label"], "heading": c["heading"]} for c in reply.citations],
        "helplines": _helplines_for(flags),
        "personal_data_hidden": bool(safety.get("personal_data")),
    }


# ---------------------------------------------------------------------------------------------------------------
# Relaying a turn
# ---------------------------------------------------------------------------------------------------------------

_CLIENT_ERRORS = {
    "lesson_not_indexed": ("lesson_not_ready", "This lesson isn't ready for the tutor yet."),
    "timeout": ("tutor_timeout", "The tutor took too long to answer. Please try again."),
}
_UNAVAILABLE = ("tutor_unavailable", "The tutor is unavailable right now. Please try again in a little while.")


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _client_error(code: str | None) -> dict[str, str]:
    client_code, message = _CLIENT_ERRORS.get(code or "", _UNAVAILABLE)
    return {"code": client_code, "message": message}


class RelayStream:
    def __init__(self, user, conversation, message: str, mode: str, client: AIServiceClient | None = None):
        self.user = user
        self.conversation = conversation
        self.message = message
        self.mode = mode
        self.client = client

    def __iter__(self) -> Iterator[str]:
        payload = build_turn(self.user, self.conversation, self.message, self.mode)
        answered = False
        client = self.client
        own_client = client is None
        try:
            client = client or get_client()
            for event, data in client.stream_turn(payload, timeout=settings.TUTOR_AI_TURN_TIMEOUT_SECONDS):
                if event == "meta":
                    yield sse(
                        "meta", {"mode": data.get("mode") or self.mode, "conversation_id": str(self.conversation.pk)}
                    )
                elif event == "delta":
                    yield sse("delta", {"text": str(data.get("text") or "")})
                elif event == "final":
                    answer = record_final(self.conversation, self.mode, data)
                    answered = True
                    yield sse("final", answer)
                    return
                elif event == "error":
                    logger.warning(
                        "tutor turn failed",
                        extra={"conversation_id": str(self.conversation.pk), "code": data.get("code")},
                    )
                    yield sse("error", _client_error(data.get("code")))
                    return
            yield sse("error", _client_error(None))
        except AIServiceError as exc:
            logger.warning("tutor turn failed", extra={"conversation_id": str(self.conversation.pk), "code": exc.code})
            yield sse("error", _client_error(exc.code))
        except Exception:
            logger.exception("tutor turn crashed", extra={"conversation_id": str(self.conversation.pk)})
            yield sse("error", {"code": "server_error", "message": "Something went wrong on our side."})
        finally:
            if not answered:
                release_message(self.user)
            if own_client and client is not None:
                client.close()

    async def __aiter__(self) -> AsyncIterator[str]:
        payload = await sync_to_async(build_turn)(self.user, self.conversation, self.message, self.mode)
        answered = False
        client = self.client
        own_client = client is None
        try:
            client = client or get_client()
            async for event, data in client.astream_turn(payload, timeout=settings.TUTOR_AI_TURN_TIMEOUT_SECONDS):
                if event == "meta":
                    yield sse(
                        "meta", {"mode": data.get("mode") or self.mode, "conversation_id": str(self.conversation.pk)}
                    )
                elif event == "delta":
                    yield sse("delta", {"text": str(data.get("text") or "")})
                elif event == "final":
                    answer = await sync_to_async(record_final)(self.conversation, self.mode, data)
                    answered = True
                    yield sse("final", answer)
                    return
                elif event == "error":
                    logger.warning(
                        "tutor turn failed",
                        extra={"conversation_id": str(self.conversation.pk), "code": data.get("code")},
                    )
                    yield sse("error", _client_error(data.get("code")))
                    return
            yield sse("error", _client_error(None))
        except AIServiceError as exc:
            logger.warning("tutor turn failed", extra={"conversation_id": str(self.conversation.pk), "code": exc.code})
            yield sse("error", _client_error(exc.code))
        except Exception:
            logger.exception("tutor turn crashed", extra={"conversation_id": str(self.conversation.pk)})
            yield sse("error", {"code": "server_error", "message": "Something went wrong on our side."})
        finally:
            if not answered:
                await sync_to_async(release_message)(self.user)
            if own_client and client is not None:
                await client.aclose()


def relay_turn(user, conversation, message: str, mode: str, client: AIServiceClient | None = None) -> RelayStream:
    return RelayStream(user, conversation, message, mode, client)
