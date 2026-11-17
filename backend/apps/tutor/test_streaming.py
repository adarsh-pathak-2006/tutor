import asyncio
import json
import time
import uuid
from unittest import mock

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts.models import StudentProfile, User
from apps.aiservice.client import AIServiceClient, AIServiceError
from apps.catalogue.models import Board, ClassLevel, Course, Lesson
from apps.commerce.models import Entitlement
from apps.content import services as content_services
from apps.core.http import AsyncStreamingHttpResponse
from apps.tutor import services
from apps.tutor.models import TutorConversation, TutorMessage, UsageCounter

TOKEN = "s" * 40
AI_ON = {"TUTOR_AI_URL": "http://ai.test", "TUTOR_AI_SERVICE_TOKEN": TOKEN}
BASE = "/api/v1/tutor"


def make_sse_events(text="Answer text"):
    return [
        ("meta", {"mode": "explain", "model": "test-model"}),
        ("delta", {"text": text[:5]}),
        ("delta", {"text": text[5:]}),
        (
            "final",
            {
                "text": text,
                "message": "Question?",
                "blocked": False,
                "replaced": False,
                "off_topic": False,
                "citations": [{"label": "P1", "chunk_id": str(uuid.uuid4()), "heading": "Heading"}],
                "safety": {},
                "flags": [],
                "usage": {"prompt_tokens_estimate": 20, "reply_tokens_estimate": 10},
                "latency_ms": 50,
                "model": "test-model",
                "prompt_version": "v1",
                "safety_version": "v1",
                "mode": "explain",
            },
        ),
    ]


def sse_encode(events):
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


class AsyncStreamingHttpResponseTests(SimpleTestCase):
    def test_async_generator_streaming(self):
        async def agen():
            yield "event: delta\ndata: 1\n\n"
            yield "event: delta\ndata: 2\n\n"

        response = AsyncStreamingHttpResponse(agen(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"

        async def collect():
            items = []
            async for chunk in response.streaming_content:
                items.append(chunk)
            return items

        content = asyncio.run(collect())
        self.assertEqual(content, [b"event: delta\ndata: 1\n\n", b"event: delta\ndata: 2\n\n"])
        self.assertEqual(response["Content-Type"], "text/event-stream")
        self.assertEqual(response["Cache-Control"], "no-cache")
        self.assertEqual(response["X-Accel-Buffering"], "no")

    def test_sync_generator_streaming(self):
        def sgen():
            yield "chunk1"
            yield "chunk2"

        response = AsyncStreamingHttpResponse(sgen(), content_type="text/plain")
        items = list(response.streaming_content)
        self.assertEqual(items, [b"chunk1", b"chunk2"])

    def test_empty_stream(self):
        response = AsyncStreamingHttpResponse([], content_type="text/plain")
        self.assertEqual(list(response.streaming_content), [])


class AIServiceClientAsyncTests(SimpleTestCase):
    def test_aclient_lifecycle(self):
        client = AIServiceClient("http://ai.test", TOKEN, timeout=5)
        aclient = client._get_aclient()
        self.assertIsInstance(aclient, httpx.AsyncClient)
        self.assertFalse(aclient.is_closed)
        asyncio.run(client.aclose())
        self.assertTrue(aclient.is_closed)

    def test_async_context_manager(self):
        async def run():
            async with AIServiceClient("http://ai.test", TOKEN, timeout=5) as client:
                aclient = client._get_aclient()
                self.assertFalse(aclient.is_closed)
            self.assertTrue(aclient.is_closed)

        asyncio.run(run())

    def test_astream_turn_success(self):
        events = make_sse_events("Async streaming works")

        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse_encode(events))

        client = AIServiceClient("http://ai.test", TOKEN, timeout=5, transport=httpx.MockTransport(handler))

        async def run():
            received = []
            async for event, data in client.astream_turn({"message": "test"}, timeout=5):
                received.append((event, data))
            return received

        results = asyncio.run(run())
        self.assertEqual(len(results), 4)
        self.assertEqual(results[0][0], "meta")
        self.assertEqual(results[1][0], "delta")
        self.assertEqual(results[2][0], "delta")
        self.assertEqual(results[3][0], "final")
        self.assertEqual(results[3][1]["text"], "Async streaming works")

    def test_astream_turn_http_error(self):
        def handler(request):
            return httpx.Response(400, json={"error": {"code": "bad_request", "message": "Invalid"}})

        client = AIServiceClient("http://ai.test", TOKEN, timeout=5, transport=httpx.MockTransport(handler))

        async def run():
            async for _ in client.astream_turn({}, timeout=5):
                pass

        with self.assertRaises(AIServiceError) as cm:
            asyncio.run(run())
        self.assertEqual(cm.exception.code, "bad_request")

    def test_astream_turn_timeout(self):
        def handler(request):
            raise httpx.ReadTimeout("Request timed out")

        client = AIServiceClient("http://ai.test", TOKEN, timeout=5, transport=httpx.MockTransport(handler))

        async def run():
            async for _ in client.astream_turn({}, timeout=5):
                pass

        with self.assertRaises(AIServiceError) as cm:
            asyncio.run(run())
        self.assertEqual(cm.exception.code, "timeout")

    def test_astream_turn_bad_response(self):
        def handler(request):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=b"event: delta\ndata: not-json\n\n"
            )

        client = AIServiceClient("http://ai.test", TOKEN, timeout=5, transport=httpx.MockTransport(handler))

        async def run():
            async for _ in client.astream_turn({}, timeout=5):
                pass

        with self.assertRaises(AIServiceError) as cm:
            asyncio.run(run())
        self.assertEqual(cm.exception.code, "bad_response")


@override_settings(**AI_ON)
class RelayStreamAsyncTests(TransactionTestCase):
    def setUp(self):
        call_command("seed_demo_catalogue", verbosity=0)
        self.course = Course.objects.get(slug="ai-foundations")
        self.lesson = Lesson.objects.get(slug="what-is-data")
        self.version = content_services.published_version(self.lesson)
        self.user = User.objects.create_user(email="streamstudent@example.com", password="password123")
        StudentProfile.objects.create(
            user=self.user,
            class_level=ClassLevel.objects.get(number=8),
            board=Board.objects.get(code="CBSE"),
            status="active",
        )
        Entitlement.objects.create(student=self.user, product_type="course", product_id=self.course.id)
        self.conversation = TutorConversation.objects.create(
            student=self.user,
            lesson=self.lesson,
            content_version_id=self.version.pk,
        )

    async def test_relay_stream_aiter_success(self):
        events = make_sse_events("Plants use chlorophyll to make glucose.")

        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse_encode(events))

        client = AIServiceClient("http://ai.test", TOKEN, timeout=5, transport=httpx.MockTransport(handler))
        relay = services.RelayStream(self.user, self.conversation, "What do plants use?", "explain", client=client)

        chunks = []
        async for chunk in relay:
            chunks.append(chunk)

        self.assertEqual(len(chunks), 4)
        self.assertIn("event: meta", chunks[0])
        self.assertIn("event: delta", chunks[1])
        self.assertIn("event: delta", chunks[2])
        self.assertIn("event: final", chunks[3])
        self.assertIn("Plants use chlorophyll", chunks[3])

        message_count = await sync_to_async(TutorMessage.objects.filter(conversation=self.conversation).count)()
        self.assertEqual(message_count, 2)

    async def test_relay_stream_error_releases_reserved_token(self):
        await sync_to_async(services.reserve_message)(self.user)
        counter = await sync_to_async(UsageCounter.objects.get)(student=self.user)
        self.assertEqual(counter.messages, 1)

        def handler(request):
            raise httpx.ConnectError("Network is down")

        client = AIServiceClient("http://ai.test", TOKEN, timeout=5, transport=httpx.MockTransport(handler))
        relay = services.RelayStream(self.user, self.conversation, "Hello", "explain", client=client)

        chunks = []
        async for chunk in relay:
            chunks.append(chunk)

        self.assertEqual(len(chunks), 1)
        self.assertIn("event: error", chunks[0])
        self.assertIn("tutor_unavailable", chunks[0])

        await sync_to_async(counter.refresh_from_db)()
        self.assertEqual(counter.messages, 0)

    def test_relay_stream_iter_sync_fallback(self):
        events = make_sse_events("Sync fallback content")

        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse_encode(events))

        client = AIServiceClient("http://ai.test", TOKEN, timeout=5, transport=httpx.MockTransport(handler))
        relay = services.RelayStream(self.user, self.conversation, "Question", "explain", client=client)

        chunks = list(relay)
        self.assertEqual(len(chunks), 4)
        self.assertIn("event: meta", chunks[0])
        self.assertIn("event: delta", chunks[1])
        self.assertIn("event: delta", chunks[2])
        self.assertIn("event: final", chunks[3])
        self.assertIn("Sync fallback content", chunks[3])


@override_settings(**AI_ON)
class ConcurrencyAndNonBlockingTests(TransactionTestCase):
    def setUp(self):
        call_command("seed_demo_catalogue", verbosity=0)
        self.course = Course.objects.get(slug="ai-foundations")
        self.lesson = Lesson.objects.get(slug="what-is-data")
        self.version = content_services.published_version(self.lesson)

    def _create_students_and_conversations(self, count):
        students = []
        conversations = []
        for i in range(count):
            user = User.objects.create_user(
                email=f"parallel_{i}_{uuid.uuid4().hex[:6]}@example.com", password="password123"
            )
            class_level = ClassLevel.objects.get(number=8)
            board = Board.objects.get(code="CBSE")
            StudentProfile.objects.create(
                user=user,
                class_level=class_level,
                board=board,
                status="active",
            )
            Entitlement.objects.create(student=user, product_type="course", product_id=self.course.id)
            conversation = TutorConversation.objects.create(
                student=user,
                lesson=self.lesson,
                content_version_id=self.version.pk,
            )
            students.append(user)
            conversations.append(conversation)
        return students, conversations

    async def test_multiple_concurrent_streams_run_in_parallel_without_blocking(self):
        num_concurrent = 6
        students, conversations = await sync_to_async(self._create_students_and_conversations)(num_concurrent)

        async def delayed_handler(request):
            await asyncio.sleep(0.06)
            events = make_sse_events(f"Stream result for {request.url.path}")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse_encode(events))

        async def stream_user(idx):
            client = AIServiceClient("http://ai.test", TOKEN, timeout=5, transport=httpx.MockTransport(delayed_handler))
            relay = services.RelayStream(students[idx], conversations[idx], f"Query {idx}", "explain", client=client)
            results = []
            async for chunk in relay:
                results.append(chunk)
            return results

        start_time = time.monotonic()
        all_results = await asyncio.gather(*(stream_user(i) for i in range(num_concurrent)))
        elapsed = time.monotonic() - start_time

        self.assertEqual(len(all_results), num_concurrent)
        for res in all_results:
            self.assertEqual(len(res), 4)
            self.assertIn("event: meta", res[0])
            self.assertIn("event: final", res[3])

        self.assertLess(elapsed, 0.06 * num_concurrent)

        for conversation in conversations:
            msg_count = await sync_to_async(TutorMessage.objects.filter(conversation=conversation).count)()
            self.assertEqual(msg_count, 2)


@override_settings(**AI_ON)
class FunctionalityIntegrityTests(TransactionTestCase):
    def setUp(self):
        call_command("seed_demo_catalogue", verbosity=0)
        self.course = Course.objects.get(slug="ai-foundations")
        self.lesson = Lesson.objects.get(slug="what-is-data")
        self.version = content_services.published_version(self.lesson)
        self.student = User.objects.create_user(email="integrity@example.com", password="password123")
        StudentProfile.objects.create(
            user=self.student,
            class_level=ClassLevel.objects.get(number=8),
            board=Board.objects.get(code="CBSE"),
            status="active",
        )
        self.client.force_login(self.student)

    def test_unentitled_student_is_refused(self):
        conversation = TutorConversation.objects.create(
            student=self.student, lesson=self.lesson, content_version_id=self.version.pk
        )
        response = self.client.post(
            f"{BASE}/conversations/{conversation.pk}/messages",
            {"message": "Hello?"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "not_entitled")

    def test_daily_limit_reached_blocks_message(self):
        Entitlement.objects.create(student=self.student, product_type="course", product_id=self.course.id)
        conversation = TutorConversation.objects.create(
            student=self.student, lesson=self.lesson, content_version_id=self.version.pk
        )
        UsageCounter.objects.create(
            student=self.student, day=timezone.localdate(), messages=settings.TUTOR_TUTOR_DAILY_LIMIT
        )

        response = self.client.post(
            f"{BASE}/conversations/{conversation.pk}/messages",
            {"message": "Can I ask more?"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "daily_limit_reached")

    def test_standard_client_streaming_endpoint_receives_complete_sse(self):
        Entitlement.objects.create(student=self.student, product_type="course", product_id=self.course.id)
        conversation = TutorConversation.objects.create(
            student=self.student, lesson=self.lesson, content_version_id=self.version.pk
        )
        events = make_sse_events("Regular client streaming still works perfectly.")

        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse_encode(events))

        fake_client = AIServiceClient("http://ai.test", TOKEN, timeout=5, transport=httpx.MockTransport(handler))

        with mock.patch("apps.tutor.services.get_client", return_value=fake_client):
            response = self.client.post(
                f"{BASE}/conversations/{conversation.pk}/messages",
                {"message": "What is data?"},
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "text/event-stream")
            body = b"".join(response.streaming_content).decode()
            self.assertIn("event: meta", body)
            self.assertIn("event: delta", body)
            self.assertIn("event: final", body)
            self.assertIn("Regular client streaming still works perfectly.", body)
