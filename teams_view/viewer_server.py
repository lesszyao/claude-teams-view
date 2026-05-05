"""Viewer HTTP/SSE server for the teams-view sidebar UI."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp import web

from teams_view.trace_store import TraceStore

log = logging.getLogger("teams-view")


async def _handle_index(request: web.Request) -> web.Response:
    template = Path(__file__).parent / "viewer.html"
    if not template.exists():
        return web.Response(status=500, text="viewer.html not bundled")
    return web.Response(text=template.read_text(encoding="utf-8"), content_type="text/html")


async def _handle_snapshot(request: web.Request) -> web.Response:
    store: TraceStore = request.app["store"]
    return web.json_response(await store.snapshot())


async def _handle_sse(request: web.Request) -> web.StreamResponse:
    store: TraceStore = request.app["store"]
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)
    # Tell the client to start fresh from snapshot, then receive deltas.
    await resp.write(b'data: {"type":"hello"}\n\n')
    store.add_sse_client(resp)
    try:
        while True:
            await asyncio.sleep(15)
            try:
                await resp.write(b": keepalive\n\n")
            except (ConnectionError, ConnectionResetError, RuntimeError):
                break
    except asyncio.CancelledError:
        pass
    finally:
        store.remove_sse_client(resp)
    return resp


async def start_viewer(*, host: str, port: int, store: TraceStore) -> tuple[web.AppRunner, int]:
    app = web.Application()
    app["store"] = store
    app.router.add_get("/", _handle_index)
    app.router.add_get("/snapshot", _handle_snapshot)
    app.router.add_get("/events", _handle_sse)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    try:
        actual = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    except (AttributeError, IndexError, OSError):
        actual = port
    return runner, actual
