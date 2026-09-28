"""ASGI-level tests for OneTimeFileResponse: exactly one of on_complete / on_abort must fire."""

import anyio
import anyio.lowlevel
import pytest

from app.jobs.response import CHUNK, OneTimeFileResponse


def _run(tmp_path, *, method="GET", disconnect_after_chunks=None, hang_up_when_complete=False,
         size=CHUNK * 3 + 10):
    path = tmp_path / "out.xlsx"
    path.write_bytes(b"z" * size)
    outcome, sent = [], []

    resp = OneTimeFileResponse(path, filename="Wage Catalog é.xlsx", media_type="application/x",
                               on_complete=lambda: outcome.append("complete"),
                               on_abort=lambda: outcome.append("abort"))
    gone = anyio.Event()

    async def receive():
        if not sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        body_chunks = sum(1 for m in sent if m["type"] == "http.response.body" and m.get("body"))
        received = sum(len(m.get("body", b"")) for m in sent if m["type"] == "http.response.body")
        if disconnect_after_chunks is not None and body_chunks >= disconnect_after_chunks:
            gone.set()
        if hang_up_when_complete and received >= size:
            gone.set()  # like urllib/curl: close the socket the moment Content-Length bytes arrived
        await anyio.lowlevel.checkpoint()

    anyio.run(resp, {"type": "http", "method": method, "headers": []}, receive, send)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return outcome, body, dict(sent[0]["headers"])


def test_complete_transfer_fires_on_complete(tmp_path):
    outcome, body, headers = _run(tmp_path)
    assert outcome == ["complete"] and len(body) == CHUNK * 3 + 10
    assert headers[b"accept-ranges"] == b"none"
    assert b"filename*=UTF-8''Wage%20Catalog%20%C3%A9.xlsx" in headers[b"content-disposition"]


def test_client_disconnect_mid_transfer_fires_on_abort(tmp_path):
    outcome, body, _ = _run(tmp_path, disconnect_after_chunks=1)
    assert outcome == ["abort"] and len(body) < CHUNK * 3 + 10


@pytest.mark.parametrize("size", [0, 1, CHUNK, CHUNK * 3 + 10])
def test_sizes_complete(tmp_path, size):
    assert _run(tmp_path, size=size)[0] == ["complete"]


@pytest.mark.parametrize("size", [6779, CHUNK * 3 + 10])
def test_client_hanging_up_after_full_body_is_complete(tmp_path, size):
    # Regression: found live against uvicorn — a finished download was misread as interrupted.
    outcome, body, _ = _run(tmp_path, hang_up_when_complete=True, size=size)
    assert outcome == ["complete"] and len(body) == size


def test_head_does_not_consume(tmp_path):
    outcome, body, _ = _run(tmp_path, method="HEAD")
    assert outcome == ["abort"] and body == b""
