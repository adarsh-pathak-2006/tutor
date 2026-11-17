"""AI tutor API for students — /api/v1/tutor (docs/architecture/api-reference.md).

Sending a message answers with Server-Sent Events, not JSON: `meta`, then `delta` events with pieces of the reply,
then one `final` with the stored answer (or one `error`). Checks that can fail (consent, entitlement, daily limit,
AI service configured) are made first and answer with the usual JSON error and status code.
"""

from uuid import UUID

from django.http import HttpResponse
from ninja import Router
from ninja.security import django_auth

from apps.core.http import AsyncStreamingHttpResponse
from apps.core.throttling import tutor_throttle

from . import services
from .schemas import ConversationDetailOut, ConversationOut, MessageIn, StartIn

tutor_router = Router(tags=["tutor"], auth=django_auth)

SSE_DOC = {
    200: {
        "description": "text/event-stream: meta {mode, conversation_id} → delta {text}… → final {message_id, text, "
        "mode, blocked, replaced, off_topic, citations[{label, heading}], helplines[], personal_data_hidden} | "
        "error {code, message}",
        "content": {"text/event-stream": {}},
    }
}


def _detail(user, conversation):
    return {
        "id": conversation.id,
        "lesson_id": conversation.lesson_id,
        "lesson_title": conversation.lesson.title,
        "mode": conversation.mode,
        "created_at": conversation.created_at,
        "last_message_at": conversation.last_message_at,
        "messages": list(conversation.messages.all()),
        "usage": services.usage_today(user),
    }


@tutor_router.post("/conversations", response={201: ConversationDetailOut})
def start(request, payload: StartIn):
    """Start a tutor chat on a lesson of a course the student is enrolled in."""
    conversation = services.start_conversation(request.user, payload.lesson_id, payload.mode)
    return 201, _detail(request.user, conversation)


@tutor_router.get("/conversations", response=list[ConversationOut])
def conversations(request, lesson_id: UUID | None = None):
    """The student's recent chats (newest first), optionally for one lesson."""
    return services.list_conversations(request.user, lesson_id)


@tutor_router.get("/conversations/{conversation_id}", response=ConversationDetailOut)
def conversation(request, conversation_id: UUID):
    return _detail(request.user, services.conversation_for(request.user, conversation_id))


@tutor_router.delete("/conversations/{conversation_id}", response={204: None})
def delete(request, conversation_id: UUID):
    services.delete_conversation(request.user, conversation_id)
    return 204, None


@tutor_router.post(
    "/conversations/{conversation_id}/messages",
    throttle=[tutor_throttle],
    openapi_extra={"responses": SSE_DOC},
)
def send(request, conversation_id: UUID, payload: MessageIn) -> HttpResponse:
    """Ask the tutor. The reply streams as Server-Sent Events; nothing is stored until the final event."""
    user = request.user
    conversation = services.conversation_for(user, conversation_id)
    services.tutor_lesson(user, conversation.lesson_id)  # entitlement may have ended since the chat started
    services.require_tutor_available()
    services.reserve_message(user)
    response = AsyncStreamingHttpResponse(
        services.relay_turn(user, conversation, payload.message, payload.mode), content_type="text/event-stream"
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # proxies must not hold the stream back
    return response
