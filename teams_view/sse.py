"""Minimal SSE re-assembler for Anthropic streaming responses.

We feed raw upstream bytes; it accumulates `data:` payloads and reconstructs
the final message body so the trace record carries something the viewer can
inspect (assistant text, tool calls, usage).

Anthropic SSE event types we care about:
  message_start           : initial Message envelope (model, id, usage)
  content_block_start     : begin a block (text or tool_use)
  content_block_delta     : append text_delta or input_json_delta
  content_block_stop      : end a block
  message_delta           : updates final usage/stop_reason
  message_stop            : terminator

For unknown event types we silently skip — the viewer just shows what we have.
"""

from __future__ import annotations

import json
from typing import Any


class SSEReassembler:
    def __init__(self) -> None:
        self._buf = b""
        self.events: list[dict] = []
        self._message: dict[str, Any] = {}
        self._content: list[dict] = []
        self._partial_json: dict[int, str] = {}  # block index -> accumulating JSON string

    def feed_bytes(self, chunk: bytes) -> None:
        self._buf += chunk
        while b"\n\n" in self._buf:
            block, self._buf = self._buf.split(b"\n\n", 1)
            self._handle_block(block)

    def _handle_block(self, block: bytes) -> None:
        text = block.decode("utf-8", errors="replace")
        event_name = ""
        data_lines: list[str] = []
        for line in text.split("\n"):
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
        if not data_lines:
            return
        data_str = "\n".join(data_lines)
        try:
            data = json.loads(data_str)
        except (json.JSONDecodeError, ValueError):
            self.events.append({"event": event_name, "raw": data_str})
            return

        self.events.append({"event": event_name or data.get("type", ""), "data": data})
        evt_type = data.get("type") or event_name

        if evt_type == "message_start":
            msg = data.get("message", {})
            if isinstance(msg, dict):
                self._message = dict(msg)
                self._content = []
        elif evt_type == "content_block_start":
            idx = data.get("index", len(self._content))
            cb = data.get("content_block", {}) or {}
            while len(self._content) <= idx:
                self._content.append({})
            self._content[idx] = dict(cb)
            if cb.get("type") == "tool_use":
                self._partial_json[idx] = ""
        elif evt_type == "content_block_delta":
            idx = data.get("index", 0)
            delta = data.get("delta", {}) or {}
            if delta.get("type") == "text_delta" and 0 <= idx < len(self._content):
                self._content[idx]["text"] = self._content[idx].get("text", "") + delta.get("text", "")
            elif delta.get("type") == "input_json_delta":
                self._partial_json[idx] = self._partial_json.get(idx, "") + delta.get("partial_json", "")
            elif delta.get("type") == "thinking_delta" and 0 <= idx < len(self._content):
                self._content[idx]["thinking"] = self._content[idx].get("thinking", "") + delta.get("thinking", "")
        elif evt_type == "content_block_stop":
            idx = data.get("index", 0)
            if idx in self._partial_json and 0 <= idx < len(self._content):
                pj = self._partial_json.pop(idx)
                try:
                    self._content[idx]["input"] = json.loads(pj) if pj else {}
                except (json.JSONDecodeError, ValueError):
                    self._content[idx]["input_raw"] = pj
        elif evt_type == "message_delta":
            delta = data.get("delta", {}) or {}
            usage = data.get("usage") or {}
            if delta:
                self._message.setdefault("delta", {}).update(delta)
                if "stop_reason" in delta:
                    self._message["stop_reason"] = delta["stop_reason"]
            if usage:
                self._message.setdefault("usage", {}).update(usage)

    def reconstruct(self) -> dict:
        out = dict(self._message)
        if self._content:
            out["content"] = self._content
        return out
