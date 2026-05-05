"""MailboxWatcher: poll ~/.claude/teams/<team>/inboxes/*.json and stream new messages.

Each inbox file is a JSON array; entries look like:
    {"from": "team-lead", "text": "...", "timestamp": "...", "read": false,
     "color": "blue", "summary": "kick off the css rewrite"}

We diff by length: every tick we read each inbox, slice [already_emitted:], and
push every new entry to the store as a `mailbox` event. Each entry is fanned
out to TWO timelines:
  - the recipient (file owner)  with direction='in',  peer=sender
  - the sender                  with direction='out', peer=recipient

so when the user clicks a teammate, they see *that teammate's* sent + received
messages on a single timeline, even though we only ever read from inbox files.

Protocol JSON messages (permission_request/response, shutdown_*, plan_*,
mode_set_request, team_permission_update, idle_notification, ...) are tagged
with `is_protocol=true` and `protocol_type` so the viewer can hide them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from teams_view.trace_store import TraceStore

log = logging.getLogger("teams-view")

POLL_INTERVAL_SECONDS = 1.0


def _get_teams_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override) / "teams"
    return Path.home() / ".claude" / "teams"


def _classify_protocol(text: object) -> tuple[bool, str | None]:
    """If `text` is a JSON object with a `type` field, treat as protocol message."""
    if not isinstance(text, str):
        return False, None
    s = text.strip()
    if not s or s[0] != "{":
        return False, None
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return False, None
    if isinstance(obj, dict) and isinstance(obj.get("type"), str):
        return True, obj["type"]
    return False, None


class MailboxWatcher:
    """Polls inbox files and forwards new messages to the store as mailbox events."""

    def __init__(self, store: TraceStore) -> None:
        self.store = store
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        # agent_name -> count of messages we've already emitted from this file
        self._processed: dict[str, int] = {}
        self._initial_scan_done = False

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _loop(self) -> None:
        # Don't tick immediately — give TeamWatcher one cycle to discover the active team
        # so the first emit batch goes to the right inbox dir.
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass

        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as e:
                log.warning(f"[MailboxWatcher] tick failed: {e}")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        team_name = await self.store.get_team_name()
        if not team_name:
            return

        inbox_dir = _get_teams_dir() / team_name / "inboxes"
        if not inbox_dir.is_dir():
            return

        for inbox_file in sorted(inbox_dir.glob("*.json")):
            agent_name = inbox_file.stem
            try:
                content = inbox_file.read_text(encoding="utf-8")
                messages = json.loads(content)
            except (OSError, ValueError):
                # Concurrent writer may race; we'll catch up next tick.
                continue
            if not isinstance(messages, list):
                continue

            already = self._processed.get(agent_name, 0)
            if len(messages) < already:
                # File was rewritten / cleared; reset counter conservatively.
                already = 0
            new_messages = messages[already:]

            for offset, msg in enumerate(new_messages):
                if not isinstance(msg, dict):
                    continue
                seq = already + offset
                await self._emit(agent_name, msg, seq, historical=not self._initial_scan_done)

            self._processed[agent_name] = len(messages)

        self._initial_scan_done = True

    async def _emit(self, recipient: str, msg: dict, seq: int, *, historical: bool) -> None:
        sender = msg.get("from") or "unknown"
        text = msg.get("text", "")
        is_protocol, protocol_type = _classify_protocol(text)

        # Truncation guard for very long texts (the leader's spawn-time prompt
        # can be many KB). Always include the full length so the viewer can
        # offer "show more".
        text_str = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
        full_len = len(text_str)
        truncated = False
        max_inline = 8000
        if full_len > max_inline:
            text_str = text_str[:max_inline]
            truncated = True

        base_event = {
            "seq": seq,
            "timestamp": msg.get("timestamp"),
            "from": sender,
            "to": recipient,
            "text": text_str,
            "text_truncated": truncated,
            "text_full_length": full_len,
            "summary": msg.get("summary"),
            "color": msg.get("color"),
            "read": bool(msg.get("read", False)),
            "is_protocol": is_protocol,
            "protocol_type": protocol_type,
            "historical": historical,
        }

        # Recipient view: this is an "incoming" message.
        await self.store.append_mailbox_event(recipient, {**base_event, "direction": "in", "peer": sender})

        # Sender view: same logical message, "outgoing". Skip for self-sends and
        # for the obvious "system" pseudo-sender (some protocol replies use it).
        if sender and sender != recipient and sender != "system":
            await self.store.append_mailbox_event(sender, {**base_event, "direction": "out", "peer": recipient})
