"""Reverse proxy: per-teammate identity routing via URL path prefix.

Routes:
    POST /agent/<name>/v1/messages    -> forwarded to <upstream>/v1/messages
    GET  /agent/<name>/v1/models      -> forwarded to <upstream>/v1/models
    *    /agent/<name>/<rest>         -> forwarded to <upstream>/<rest>

Anything outside `/agent/<name>/` is rejected (404). The path prefix is the
sole identity carrier; we never inspect headers for agent identification.

Records are appended to TraceStore keyed by <name>, which fans out via SSE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import aiohttp
from aiohttp import web

from teams_view.sse import SSEReassembler
from teams_view.trace_store import TraceStore

log = logging.getLogger("teams-view")

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

_AGENT_PATH_RE = re.compile(r"^/agent/([A-Za-z0-9_.\-]+)(/.*)$")
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _filter_headers(headers: dict[str, str], *, redact: bool = False) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in HOP_BY_HOP:
            continue
        if redact and k.lower() in ("x-api-key", "authorization"):
            out[k] = (v[:12] + "...") if isinstance(v, str) and len(v) > 12 else "***"
        else:
            out[k] = v
    return out


def _build_record(
    *,
    agent: str,
    req_id: str,
    duration_ms: int,
    method: str,
    path: str,
    upstream_url: str,
    req_headers: Any,
    req_body: Any,
    status: int,
    resp_headers: Any,
    resp_body: Any,
    sse_events: list[dict] | None = None,
) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "request_id": req_id,
        "duration_ms": duration_ms,
        "request": {
            "method": method,
            "path": path,
            "upstream_url": upstream_url,
            "headers": _filter_headers(dict(req_headers), redact=True),
            "body": req_body,
        },
        "response": {
            "status": status,
            "headers": _filter_headers(dict(resp_headers)),
            "body": resp_body,
            **({"sse_events": sse_events} if sse_events else {}),
        },
    }


async def _proxy_handler(request: web.Request) -> web.StreamResponse:
    ctx: dict = request.app["ctx"]
    target: str = ctx["target_url"]
    session: aiohttp.ClientSession = ctx["session"]
    store: TraceStore = ctx["store"]
    lead_agent_name: str = ctx["lead_agent_name"]

    m = _AGENT_PATH_RE.match(request.path)
    if not m:
        return web.Response(status=404, text="teams-view: path must be /agent/<name>/...")
    agent = m.group(1)
    if not _AGENT_NAME_RE.match(agent):
        return web.Response(status=400, text="teams-view: invalid agent name")
    real_path = m.group(2)
    upstream_url = target.rstrip("/") + real_path

    is_lead = agent == lead_agent_name
    await store.note_agent(agent, is_lead=is_lead)

    body = await request.read()
    fwd_headers = _filter_headers(dict(request.headers))
    fwd_headers.pop("Host", None)
    fwd_headers.pop("host", None)
    fwd_headers["Accept-Encoding"] = "identity"

    try:
        req_body = json.loads(body) if body else None
    except (json.JSONDecodeError, ValueError):
        req_body = body.decode("utf-8", errors="replace") if body else None

    is_streaming = isinstance(req_body, dict) and bool(req_body.get("stream"))
    model = req_body.get("model", "") if isinstance(req_body, dict) else ""
    req_id = f"req_{uuid.uuid4().hex[:12]}"
    t0 = time.monotonic()

    log.info(f"[{agent}] -> {request.method} {real_path} (model={model}, stream={is_streaming})")

    try:
        upstream_resp = await session.request(
            method=request.method,
            url=upstream_url,
            headers=fwd_headers,
            data=body,
            timeout=aiohttp.ClientTimeout(total=600, sock_read=300),
        )
    except Exception as exc:
        log.error(f"[{agent}] upstream error: {exc}")
        record = _build_record(
            agent=agent,
            req_id=req_id,
            duration_ms=int((time.monotonic() - t0) * 1000),
            method=request.method,
            path=real_path,
            upstream_url=upstream_url,
            req_headers=request.headers,
            req_body=req_body,
            status=502,
            resp_headers={},
            resp_body={"error": str(exc)},
        )
        await store.append_trace(agent, record)
        return web.Response(status=502, text=str(exc))

    if is_streaming and upstream_resp.status == 200:
        return await _handle_stream(request, upstream_resp, agent, req_id, t0, real_path, upstream_url, req_body, store)
    return await _handle_buffered(request, upstream_resp, agent, req_id, t0, real_path, upstream_url, req_body, store)


async def _handle_stream(
    request: web.Request,
    upstream_resp: aiohttp.ClientResponse,
    agent: str,
    req_id: str,
    t0: float,
    path: str,
    upstream_url: str,
    req_body: Any,
    store: TraceStore,
) -> web.StreamResponse:
    resp = web.StreamResponse(
        status=upstream_resp.status,
        headers={k: v for k, v in upstream_resp.headers.items() if k.lower() not in HOP_BY_HOP},
    )
    await resp.prepare(request)
    reassembler = SSEReassembler()

    try:
        async for chunk in upstream_resp.content.iter_any():
            await resp.write(chunk)
            reassembler.feed_bytes(chunk)
    except (ConnectionError, asyncio.CancelledError):
        pass

    try:
        await resp.write_eof()
    except (ConnectionError, ConnectionResetError, Exception):
        pass

    duration_ms = int((time.monotonic() - t0) * 1000)
    reconstructed = reassembler.reconstruct()
    log.info(f"[{agent}] <- 200 stream done ({duration_ms}ms)")

    record = _build_record(
        agent=agent,
        req_id=req_id,
        duration_ms=duration_ms,
        method=request.method,
        path=path,
        upstream_url=upstream_url,
        req_headers=request.headers,
        req_body=req_body,
        status=upstream_resp.status,
        resp_headers=upstream_resp.headers,
        resp_body=reconstructed,
        sse_events=reassembler.events,
    )
    await store.append_trace(agent, record)
    return resp


async def _handle_buffered(
    request: web.Request,
    upstream_resp: aiohttp.ClientResponse,
    agent: str,
    req_id: str,
    t0: float,
    path: str,
    upstream_url: str,
    req_body: Any,
    store: TraceStore,
) -> web.Response:
    resp_bytes = await upstream_resp.read()
    duration_ms = int((time.monotonic() - t0) * 1000)
    try:
        resp_body = json.loads(resp_bytes) if resp_bytes else None
    except (json.JSONDecodeError, ValueError):
        resp_body = resp_bytes.decode("utf-8", errors="replace") if resp_bytes else None
    log.info(f"[{agent}] <- {upstream_resp.status} ({duration_ms}ms, {len(resp_bytes)} bytes)")

    record = _build_record(
        agent=agent,
        req_id=req_id,
        duration_ms=duration_ms,
        method=request.method,
        path=path,
        upstream_url=upstream_url,
        req_headers=request.headers,
        req_body=req_body,
        status=upstream_resp.status,
        resp_headers=upstream_resp.headers,
        resp_body=resp_body,
    )
    await store.append_trace(agent, record)

    return web.Response(
        status=upstream_resp.status,
        headers={k: v for k, v in upstream_resp.headers.items() if k.lower() not in HOP_BY_HOP},
        body=resp_bytes,
    )


async def start_proxy(
    *,
    host: str,
    port: int,
    target_url: str,
    store: TraceStore,
    session: aiohttp.ClientSession,
    lead_agent_name: str,
) -> tuple[web.AppRunner, int]:
    app = web.Application(client_max_size=0)
    app["ctx"] = {
        "target_url": target_url,
        "session": session,
        "store": store,
        "lead_agent_name": lead_agent_name,
    }
    app.router.add_route("*", "/agent/{tail:.*}", _proxy_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    try:
        actual = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    except (AttributeError, IndexError, OSError):
        actual = port
    return runner, actual
