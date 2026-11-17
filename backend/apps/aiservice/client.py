"""HTTP client for the AI service (docs/architecture/ai-service.md §3).

Every call sends the service token, the current request ID (so both services' logs line up) and, for indexing, an
Idempotency-Key. Failures become AIServiceError with a `retryable` flag: network problems, timeouts, 5xx, 429 and
401 (a token being rotated) are worth retrying later; any other 4xx means the request itself is wrong.

The token is never logged. The client is synchronous (Django views and management commands are).
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
from django.conf import settings

from apps.core.request_context import get_request_id

logger = logging.getLogger("tutor.aiservice")

MIN_TOKEN_LENGTH = 32
RETRYABLE_STATUSES = frozenset({401, 408, 425, 429, 500, 502, 503, 504})
CONNECT_TIMEOUT_SECONDS = 5.0


class AIServiceNotConfigured(RuntimeError):
    """TUTOR_AI_URL or TUTOR_AI_SERVICE_TOKEN is missing: there is no AI service in this environment."""


class AIServiceError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "unavailable",
        status: int | None = None,
        retryable: bool = True,
        fields: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.fields = fields or {}

    def __str__(self) -> str:
        prefix = f"{self.status} " if self.status else ""
        return f"{prefix}{self.code}: {self.args[0]}"


def is_configured() -> bool:
    return bool(settings.TUTOR_AI_URL) and len(settings.TUTOR_AI_SERVICE_TOKEN) >= MIN_TOKEN_LENGTH


class AIServiceClient:
    def __init__(self, base_url: str, token: str, *, timeout: float, transport: httpx.BaseTransport | None = None):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._transport = transport
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "tutor-django"}
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout, connect=min(CONNECT_TIMEOUT_SECONDS, timeout)),
            transport=transport,
            headers=self._headers,
        )
        self._aclient: httpx.AsyncClient | None = None

    def _get_aclient(self) -> httpx.AsyncClient:
        if self._aclient is None or self._aclient.is_closed:
            self._aclient = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout, connect=min(CONNECT_TIMEOUT_SECONDS, self._timeout)),
                transport=self._transport,
                headers=self._headers,
            )
        return self._aclient

    async def aclose(self) -> None:
        if self._aclient is not None and not self._aclient.is_closed:
            await self._aclient.aclose()

    def close(self) -> None:
        self._client.close()
        if self._aclient is not None and not self._aclient.is_closed:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None and loop.is_running():
                loop.create_task(self._aclient.aclose())
            else:
                try:
                    asyncio.run(self._aclient.aclose())
                except RuntimeError:
                    pass

    def __enter__(self) -> "AIServiceClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    async def __aenter__(self) -> "AIServiceClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- plumbing ----------------------------------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        request_id = get_request_id()
        headers = {"X-Request-ID": request_id if request_id != "-" else uuid.uuid4().hex}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._client.request(method, path, json=json, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise AIServiceError(f"{method} {path} timed out", code="timeout") from exc
        except httpx.HTTPError as exc:
            raise AIServiceError(f"{method} {path} failed: {type(exc).__name__}", code="unreachable") from exc
        if response.status_code >= 400:
            raise self._error(method, path, response)
        return response

    @staticmethod
    def _error(method: str, path: str, response: httpx.Response) -> AIServiceError:
        code, message, fields = f"http_{response.status_code}", response.reason_phrase or "error", {}
        try:
            error = response.json().get("error") or {}
            code = str(error.get("code") or code)
            message = str(error.get("message") or message)
            fields = {str(k): str(v) for k, v in (error.get("fields") or {}).items()}
        except (ValueError, AttributeError):
            pass  # not our JSON error shape (a proxy page, say): keep the HTTP status
        return AIServiceError(
            f"{method} {path}: {message}",
            code=code,
            status=response.status_code,
            retryable=response.status_code in RETRYABLE_STATUSES,
            fields=fields,
        )

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise AIServiceError("the AI service returned invalid JSON", code="bad_response") from exc
        if not isinstance(data, dict):
            raise AIServiceError("the AI service returned an unexpected body", code="bad_response")
        return data

    # -- endpoints ---------------------------------------------------------------------------------------------

    def whoami(self) -> dict[str, Any]:
        return self._json(self._request("GET", "/v1/whoami"))

    def index_lesson(self, lesson_id: uuid.UUID, payload: dict[str, Any], *, key: str) -> dict[str, Any]:
        return self._json(self._request("PUT", f"/v1/lessons/{lesson_id}/index", json=payload, idempotency_key=key))

    def delete_lesson(self, lesson_id: uuid.UUID, *, key: str, up_to_version: int | None = None) -> None:
        params = {"up_to_version": up_to_version} if up_to_version else None
        self._request("DELETE", f"/v1/lessons/{lesson_id}/index", params=params, idempotency_key=key)

    def index_status(self) -> dict[str, Any]:
        return self._json(self._request("GET", "/v1/index/status"))

    def stream_turn(self, payload: dict[str, Any], *, timeout: float) -> Iterator[tuple[str, dict[str, Any]]]:
        """POST /v1/tutor/turns and yield its Server-Sent Events as (event, data) pairs, as they arrive.

        Errors before the stream starts (validation, auth, network) raise AIServiceError; a stream cut off halfway
        raises AIServiceError too. The AI service's own `error` events are yielded like any other event. A tutor turn
        is never retried: a retry could double-charge tokens and confuse the student (design §3).
        """
        request_id = get_request_id()
        headers = {"X-Request-ID": request_id if request_id != "-" else uuid.uuid4().hex, "Accept": "text/event-stream"}
        path = "/v1/tutor/turns"
        try:
            with self._client.stream(
                "POST",
                path,
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(timeout, connect=min(CONNECT_TIMEOUT_SECONDS, timeout)),
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise self._error("POST", path, response)
                event: str | None = None
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        event = line[len("event: ") :].strip()
                    elif line.startswith("data: ") and event:
                        data = json.loads(line[len("data: ") :])
                        if not isinstance(data, dict):
                            raise AIServiceError("an event's data is not an object", code="bad_response")
                        yield event, data
                        event = None
        except httpx.TimeoutException as exc:
            raise AIServiceError(f"POST {path} timed out", code="timeout") from exc
        except httpx.HTTPError as exc:
            raise AIServiceError(f"POST {path} failed: {type(exc).__name__}", code="unreachable") from exc
        except ValueError as exc:  # invalid JSON in an event
            raise AIServiceError("the AI service sent an invalid event", code="bad_response") from exc

    async def astream_turn(
        self, payload: dict[str, Any], *, timeout: float
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        request_id = get_request_id()
        headers = {"X-Request-ID": request_id if request_id != "-" else uuid.uuid4().hex, "Accept": "text/event-stream"}
        path = "/v1/tutor/turns"
        aclient = self._get_aclient()
        try:
            async with aclient.stream(
                "POST",
                path,
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(timeout, connect=min(CONNECT_TIMEOUT_SECONDS, timeout)),
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise self._error("POST", path, response)
                event: str | None = None
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        event = line[len("event: ") :].strip()
                    elif line.startswith("data: ") and event:
                        data = json.loads(line[len("data: ") :])
                        if not isinstance(data, dict):
                            raise AIServiceError("an event's data is not an object", code="bad_response")
                        yield event, data
                        event = None
        except httpx.TimeoutException as exc:
            raise AIServiceError(f"POST {path} timed out", code="timeout") from exc
        except httpx.HTTPError as exc:
            raise AIServiceError(f"POST {path} failed: {type(exc).__name__}", code="unreachable") from exc
        except ValueError as exc:
            raise AIServiceError("the AI service sent an invalid event", code="bad_response") from exc


def get_client(transport: httpx.BaseTransport | None = None) -> AIServiceClient:
    if not is_configured():
        raise AIServiceNotConfigured("TUTOR_AI_URL and TUTOR_AI_SERVICE_TOKEN are not both set")
    return AIServiceClient(
        settings.TUTOR_AI_URL,
        settings.TUTOR_AI_SERVICE_TOKEN,
        timeout=settings.TUTOR_AI_INDEX_TIMEOUT_SECONDS,
        transport=transport,
    )
