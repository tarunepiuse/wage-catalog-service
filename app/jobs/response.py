"""One-time file download that only counts as delivered when the whole body was sent.

Why not FileResponse + BackgroundTask(delete):
  * FileResponse honours Range requests and still runs the background task afterwards, so a
    `Range: bytes=0-0` probe (download managers, some browsers) would delete the file after 1 byte.
  * Under uvicorn (ASGI spec 2.3) send() silently no-ops after the client disconnects, so the
    background task also runs for an interrupted transfer and the user loses the file.

This response ignores Range, serves HEAD without consuming the job, watches receive() for
http.disconnect while streaming, and calls exactly one of on_complete / on_abort.

Limit: "complete" means the last byte was handed to the server's socket, not acknowledged by the
client — HTTP gives no delivery receipt. That's the right trade-off for delete-on-download.
"""

from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import anyio
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

CHUNK = 64 * 1024


class OneTimeFileResponse(Response):
    def __init__(self, path: Path, *, filename: str, media_type: str,
                 on_complete: Callable[[], None], on_abort: Callable[[], None]):
        super().__init__(status_code=200, media_type=media_type)
        self.path = path
        self.on_complete = on_complete
        self.on_abort = on_abort
        self.size = path.stat().st_size
        ascii_name = filename.encode("ascii", "ignore").decode() or "download.xlsx"
        self.headers["content-length"] = str(self.size)
        self.headers["content-disposition"] = (
            f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}')
        self.headers["accept-ranges"] = "none"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        start = {"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers}
        if scope["method"] == "HEAD":
            await send(start)
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            await run_in_threadpool(self.on_abort)  # a HEAD is not a download
            return

        disconnected = anyio.Event()
        completed = False

        async def watch_disconnect() -> None:
            while True:
                if (await receive())["type"] == "http.disconnect":
                    disconnected.set()
                    return

        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(watch_disconnect)
                await send(start)
                sent = 0
                async with await anyio.open_file(self.path, "rb") as f:
                    while True:
                        chunk = await f.read(CHUNK)
                        # A disconnect seen *before* a chunk goes out means the client can't have the
                        # whole file. One seen after the last chunk is normal: clients close as soon as
                        # they hold Content-Length bytes, so it must not count as an abort.
                        if disconnected.is_set():
                            break
                        sent += len(chunk)
                        last = not chunk or sent >= self.size
                        await send({"type": "http.response.body", "body": chunk, "more_body": not last})
                        if last:
                            completed = sent == self.size
                            break
                tg.cancel_scope.cancel()
        finally:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(self.on_complete if completed else self.on_abort)
