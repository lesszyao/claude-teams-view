"""Watch ~/.claude/teams/<team>/config.json to enrich agent metadata.

The team config (written by Claude Code) contains, per member:
    name, agentType, model, color, prompt (the kickoff task), cwd,
    tmuxPaneId, backendType, planModeRequired, ...

We poll periodically and merge those fields into TraceStore.AgentMeta so the
sidebar shows useful context immediately, even before the teammate makes its
first API request.

We pick the "active" team using two signals, in priority order:

  1. **Strong**: a non-lead member name from the config has already shown up
     in proxy traffic (i.e. that teammate hit our reverse proxy).
  2. **Medium**: the team config's mtime is newer than teams-view's start time
     (with a small clock-skew margin). Only the leader writes team configs,
     and the leader is our child process, so a fresh mtime means "this team
     was created/touched in the current teams-view session".

We deliberately **do not** fall back to "most recently modified team on disk":
that picks up unrelated teams from past sessions (e.g. a `saturn` team in
`~/.claude/teams/` from yesterday) and pollutes the sidebar.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from teams_view.trace_store import TraceStore

log = logging.getLogger("teams-view")

POLL_INTERVAL_SECONDS = 2.0
# Clock-skew margin: a team config touched up to MTIME_SKEW_SECONDS *before*
# teams-view started is still considered "this session" (covers the case
# where the leader writes config.json a hair before our start_time is set).
MTIME_SKEW_SECONDS = 5.0


def _get_teams_dir() -> Path:
    """Return the directory holding per-team config (mirrors getTeamsDir() in claude-code)."""
    import os

    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override) / "teams"
    return Path.home() / ".claude" / "teams"


def _load_json(path: Path) -> dict[str, Any] | None:
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _member_to_meta_fields(member: dict[str, Any], is_lead: bool) -> dict[str, Any]:
    """Map team-config member fields into AgentMeta field names."""
    return {
        "color": member.get("color"),
        "agent_type": member.get("agentType"),
        "model": member.get("model"),
        "prompt": member.get("prompt"),
        "cwd": member.get("cwd"),
        "tmux_pane_id": member.get("tmuxPaneId"),
        "backend_type": member.get("backendType"),
        "plan_mode_required": member.get("planModeRequired"),
        "is_lead": is_lead,
    }


class TeamWatcher:
    """Polls team config files and forwards member metadata to the store."""

    def __init__(self, store: TraceStore, *, start_time: float | None = None) -> None:
        self.store = store
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._teams_dir = _get_teams_dir()
        self._last_active_team: str | None = None
        # Anything older than this is a stale team from a previous session.
        self._start_time = start_time if start_time is not None else time.time()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _loop(self) -> None:
        # Tick once immediately so the sidebar populates fast.
        await self._tick()
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
            if self._stop_event.is_set():
                return
            try:
                await self._tick()
            except Exception as e:
                log.warning(f"[TeamWatcher] tick failed: {e}")

    async def _tick(self) -> None:
        if not self._teams_dir.exists():
            return

        # Collect every (cfg_path, mtime, data) on disk first; we'll filter below.
        all_teams: list[tuple[Path, float, dict[str, Any]]] = []
        for entry in self._teams_dir.iterdir():
            if not entry.is_dir():
                continue
            cfg = entry / "config.json"
            if not cfg.is_file():
                continue
            try:
                mtime = cfg.stat().st_mtime
            except OSError:
                continue
            data = _load_json(cfg)
            if not isinstance(data, dict) or "members" not in data:
                continue
            all_teams.append((cfg, mtime, data))

        if not all_teams:
            return

        snap = await self.store.snapshot()
        seen_names = set(snap["agents"].keys())
        # Lead name is generic ("team-lead"); only non-lead names are reliable signals.
        proxy_signal_names = seen_names - {"team-lead"}

        cutoff = self._start_time - MTIME_SKEW_SECONDS

        # Pass 1 — strongest signal: a non-lead member name from the config has
        # actually been seen on the proxy. Pick the most-recently-modified such
        # team (handles users iterating through multiple teams in one session).
        candidates = sorted(all_teams, key=lambda x: x[1], reverse=True)
        active_team: dict[str, Any] | None = None
        for _cfg, _mt, data in candidates:
            members = data.get("members") or []
            member_names = {m.get("name") for m in members if isinstance(m, dict) and m.get("name")}
            if proxy_signal_names & member_names:
                active_team = data
                break

        # Pass 2 — medium signal: any team config touched after teams-view started.
        # Only the leader writes team configs, and the leader is our child, so a
        # fresh mtime means "this team belongs to this session".
        if active_team is None:
            for _cfg, mt, data in candidates:
                if mt >= cutoff:
                    active_team = data
                    break

        if active_team is None:
            # No team has been created during this session yet — leave sidebar
            # empty rather than picking a stale config from disk.
            if self._last_active_team is not None:
                # User switched away from a team; clear team-level meta.
                await self.store.set_team_meta("", [])
                self._last_active_team = None
            return

        team_name = active_team.get("name") or "unknown-team"
        lead_agent_id = active_team.get("leadAgentId")
        members = active_team.get("members") or []

        # Publish team-level meta whenever active team changes.
        if team_name != self._last_active_team:
            await self.store.set_team_meta(team_name, members)
            self._last_active_team = team_name
            log.info(f"[TeamWatcher] active team: {team_name} ({len(members)} members)")

        # Merge per-member fields into AgentMeta.
        for member in members:
            if not isinstance(member, dict):
                continue
            name = member.get("name")
            if not name:
                continue
            is_lead = member.get("agentId") == lead_agent_id or member.get("agentType") == "team-lead"
            await self.store.merge_agent_meta(name, _member_to_meta_fields(member, is_lead))
