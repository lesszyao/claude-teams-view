"""TraceStore: per-agent in-memory trace + JSONL persistence + SSE fan-out.

Layout on disk (under --output-dir, default ./.teams-view):

    ./.teams-view/
        <session_ts>/
            agents.jsonl              # one record per agent first-seen / meta update
            traces/
                team-lead.jsonl       # one trace record per HTTP request
                css-architect.jsonl
                ...
            mailbox/
                team-lead.jsonl       # one entry per inbox event (in+out)
                css-architect.jsonl
                ...

In-memory state:
    self._traces  : dict[agent_name, list[trace_record]]
    self._mailbox : dict[agent_name, list[mailbox_event]]
    self._agents  : dict[agent_name, AgentMeta]   (AgentMeta includes color, prompt, model, ...)

Broadcast envelope sent over SSE:
    {"type": "trace",      "agent": "<name>", "record": {...}}
    {"type": "mailbox",    "agent": "<name>", "event":  {...}}
    {"type": "agent_seen", "agent": "<name>", "meta":   {...}}
    {"type": "team_meta",  "team_name": "...", "members": [...]}
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("teams-view")


@dataclass
class AgentMeta:
    """Per-teammate identity card shown in the sidebar."""

    name: str
    first_seen: str
    color: str | None = None
    agent_type: str | None = None
    model: str | None = None
    prompt: str | None = None
    cwd: str | None = None
    tmux_pane_id: str | None = None
    backend_type: str | None = None
    plan_mode_required: bool | None = None
    is_lead: bool = False
    request_count: int = 0
    last_seen: str | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class TraceStore:
    """Thread-safe (asyncio-lock) trace store + SSE broadcaster."""

    def __init__(self, output_dir: Path) -> None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = output_dir / ts
        self.traces_dir = self.session_dir / "traces"
        self.mailbox_dir = self.session_dir / "mailbox"
        self.traces_dir.mkdir(parents=True, exist_ok=True)
        self.mailbox_dir.mkdir(parents=True, exist_ok=True)
        self.agents_log = self.session_dir / "agents.jsonl"

        self._traces: dict[str, list[dict]] = {}
        self._mailbox: dict[str, list[dict]] = {}
        self._agents: dict[str, AgentMeta] = {}
        self._team_name: str | None = None
        self._team_members_meta: list[dict] = []  # snapshot from disk team config
        self._team_config_path: str | None = None  # e.g. ~/.claude/teams/<team>/config.json
        self._team_mailbox_dir: str | None = None  # e.g. ~/.claude/teams/<team>/inboxes
        self._lock = asyncio.Lock()
        self._sse_clients: list[Any] = []  # aiohttp StreamResponse list

    # ---------------- public registration API ----------------

    async def note_agent(self, name: str, *, is_lead: bool = False) -> None:
        """Register an agent at first sight (called from proxy on every request)."""
        async with self._lock:
            existing = self._agents.get(name)
            if existing is not None:
                return
            meta = AgentMeta(
                name=name,
                first_seen=datetime.now(timezone.utc).isoformat(),
                is_lead=is_lead,
            )
            self._agents[name] = meta
            self._traces.setdefault(name, [])
        log.info(f"[TraceStore] new agent: {name} (lead={is_lead})")
        self._append_jsonl(self.agents_log, {"event": "agent_seen", "name": name, "is_lead": is_lead})
        await self._broadcast({"type": "agent_seen", "agent": name, "meta": asdict(self._agents[name])})

    async def merge_agent_meta(self, name: str, fields: dict[str, Any]) -> None:
        """Merge metadata fields (typically from team_watcher reading config.json) into an agent."""
        async with self._lock:
            meta = self._agents.get(name)
            if meta is None:
                # Pre-register so meta is visible even before first request.
                meta = AgentMeta(name=name, first_seen=datetime.now(timezone.utc).isoformat())
                self._agents[name] = meta
                self._traces.setdefault(name, [])
            for k, v in fields.items():
                if hasattr(meta, k):
                    setattr(meta, k, v)
                else:
                    meta.extra[k] = v
        await self._broadcast({"type": "agent_seen", "agent": name, "meta": asdict(self._agents[name])})

    async def set_team_meta(
        self,
        team_name: str,
        members: list[dict],
        *,
        config_path: str | None = None,
        mailbox_dir: str | None = None,
    ) -> None:
        async with self._lock:
            self._team_name = team_name
            self._team_members_meta = members
            self._team_config_path = config_path
            self._team_mailbox_dir = mailbox_dir
        await self._broadcast(
            {
                "type": "team_meta",
                "team_name": team_name,
                "members": members,
                "config_path": config_path,
                "mailbox_dir": mailbox_dir,
            }
        )

    # ---------------- trace records ----------------

    async def append_trace(self, agent: str, record: dict) -> None:
        async with self._lock:
            self._traces.setdefault(agent, []).append(record)
            meta = self._agents.get(agent)
            if meta is not None:
                meta.request_count += 1
                meta.last_seen = record.get("timestamp")
                usage = (record.get("response", {}) or {}).get("usage") or {}
                if not usage:
                    body = (record.get("response", {}) or {}).get("body")
                    if isinstance(body, dict):
                        usage = body.get("usage") or {}
                if isinstance(usage, dict):
                    meta.total_input_tokens += int(usage.get("input_tokens") or 0)
                    meta.total_output_tokens += int(usage.get("output_tokens") or 0)
                    record.setdefault("usage_summary", {})
                    record["usage_summary"]["input_tokens"] = int(usage.get("input_tokens") or 0)
                    record["usage_summary"]["output_tokens"] = int(usage.get("output_tokens") or 0)
                    record["usage_summary"]["cache_read_input_tokens"] = int(usage.get("cache_read_input_tokens") or 0)
                    record["usage_summary"]["cache_creation_input_tokens"] = int(
                        usage.get("cache_creation_input_tokens") or 0
                    )

        # Persist outside the lock to avoid holding it during disk I/O.
        self._append_jsonl(self.traces_dir / f"{agent}.jsonl", record)
        await self._broadcast({"type": "trace", "agent": agent, "record": record})
        # Also push an updated agent meta so sidebar counters refresh.
        await self._broadcast({"type": "agent_seen", "agent": agent, "meta": asdict(self._agents[agent])})

    # ---------------- mailbox events ----------------

    async def append_mailbox_event(self, agent: str, event: dict) -> None:
        """Record an inbox event (incoming or outgoing message) for `agent`.

        The same wire-level message is fanned out twice — once to the recipient
        with direction='in' and once to the sender with direction='out' — by the
        watcher, so each agent's timeline tells its own story.
        """
        async with self._lock:
            self._mailbox.setdefault(agent, []).append(event)

        self._append_jsonl(self.mailbox_dir / f"{agent}.jsonl", event)
        await self._broadcast({"type": "mailbox", "agent": agent, "event": event})

    async def get_team_name(self) -> str | None:
        """Race-free read of the currently active team name."""
        async with self._lock:
            return self._team_name

    # ---------------- snapshot for new SSE clients ----------------

    async def snapshot(self) -> dict:
        async with self._lock:
            return {
                "team_name": self._team_name,
                "team_members": self._team_members_meta,
                "team_config_path": self._team_config_path,
                "team_mailbox_dir": self._team_mailbox_dir,
                "agents": {n: asdict(m) for n, m in self._agents.items()},
                "traces": {n: list(t) for n, t in self._traces.items()},
                "mailbox": {n: list(e) for n, e in self._mailbox.items()},
            }

    # ---------------- SSE plumbing ----------------

    def add_sse_client(self, resp: Any) -> None:
        self._sse_clients.append(resp)

    def remove_sse_client(self, resp: Any) -> None:
        if resp in self._sse_clients:
            self._sse_clients.remove(resp)

    async def _broadcast(self, message: dict) -> None:
        if not self._sse_clients:
            return
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        line = f"data: {payload}\n\n".encode("utf-8")
        dead = []
        for client in list(self._sse_clients):
            try:
                await client.write(line)
            except Exception:
                dead.append(client)
        for c in dead:
            self.remove_sse_client(c)

    # ---------------- internal disk helpers ----------------

    @staticmethod
    def _append_jsonl(path: Path, obj: dict) -> None:
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning(f"[TraceStore] failed to append to {path}: {e}")
