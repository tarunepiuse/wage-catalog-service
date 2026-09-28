"""Pure-ASGI middleware (no BaseHTTPMiddleware: it buffers responses and breaks streaming/disconnect handling)."""

import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import ApiError, problem
from app.core.logging import request_id_var

log = logging.getLogger("wtc.http")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",  # tokens and payroll-derived data must never be cached
}


class RequestContextMiddleware:
    """Request id (accepts a sane inbound X-Request-ID), security headers, access log, last-resort 500."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = inbound if _VALID_REQUEST_ID.match(inbound) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        scope.setdefault("state", {})
        started = time.perf_counter()
        status = {"code": 500, "sent": False}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"], status["sent"] = message["status"], True
                headers = MutableHeaders(scope=message)
                headers["x-request-id"] = request_id
                for k, v in _SECURITY_HEADERS.items():
                    if k not in headers:
                        headers[k] = v
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.exception("Unhandled error on %s %s", scope["method"], scope["path"])
            if status["sent"]:
                raise
            await problem(500, "An unexpected error occurred. Quote the request_id when reporting it.")(
                scope, receive, send_wrapper)
        finally:
            log.info(
                "%s %s -> %s", scope["method"], scope["path"], status["code"],
                extra={"fields": {
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                    "user": scope["state"].get("principal", "-"),
                    "client": (scope.get("client") or ("-",))[0],
                }},
            )
            request_id_var.reset(token)


class BodyTooLarge(ApiError):
    def __init__(self, limit: int):
        super().__init__(413, "payload_too_large", f"Request body exceeds {limit // (1024 * 1024)} MB")


class BodySizeLimitMiddleware:
    """Caps request bodies whether or not Content-Length is sent (chunked uploads included)."""

    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.max_bytes:
            await problem(413, BodyTooLarge(self.max_bytes).detail, code="payload_too_large",
                          headers={"connection": "close"})(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyTooLarge(self.max_bytes)
            return message

        await self.app(scope, limited_receive, send)
