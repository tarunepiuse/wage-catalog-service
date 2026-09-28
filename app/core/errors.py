"""Uniform error responses (RFC 9457 problem+json) for every failure path.

Every error body has the same shape, so clients can branch on the stable `code` field:

    {"type": "about:blank", "title": "Not Found", "status": 404, "code": "job_not_found",
     "detail": "...", "request_id": "...", "errors": [...]}
"""

import logging
from http import HTTPStatus

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_var

log = logging.getLogger(__name__)

PROBLEM_JSON = "application/problem+json"

_DEFAULT_CODES = {
    400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
    409: "conflict", 413: "payload_too_large", 415: "unsupported_media_type", 422: "invalid_input",
    429: "rate_limited", 500: "internal_error", 503: "service_unavailable",
}


class ApiError(HTTPException):
    """Raise for any expected failure. Subclasses FastAPI's HTTPException so it also passes through
    FastAPI's own body-parsing error wrapper unchanged."""

    def __init__(self, status: int, code: str, detail: str, *, errors: list | None = None,
                 headers: dict[str, str] | None = None):
        super().__init__(status_code=status, detail=detail, headers=headers)
        self.code = code
        self.errors = errors


def problem(status: int, detail: str | None = None, *, code: str | None = None,
            errors: list | None = None, headers: dict[str, str] | None = None) -> JSONResponse:
    body = {
        "type": "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "code": code or _DEFAULT_CODES.get(status, "error"),
        "detail": detail or HTTPStatus(status).description,
        "request_id": request_id_var.get(),
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, headers=headers, media_type=PROBLEM_JSON)


async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else None
    return problem(exc.status_code, detail, code=getattr(exc, "code", None),
                   errors=getattr(exc, "errors", None), headers=getattr(exc, "headers", None))


async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", "")} for e in exc.errors()]
    return problem(422, "Request validation failed", code="validation_failed", errors=errors)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    # Unhandled exceptions are rendered by RequestContextMiddleware, which still has the request id.
