#!/bin/sh
# Smoke test: run teams-view --no-launch against a mock upstream, hit it from
# multiple "agents", verify per-agent partitioning in the viewer snapshot.
set -eu

cd "$(dirname "$0")/.."
. .venv/bin/activate

# 1. Mock upstream (responds 200 with a canned Anthropic-shaped message).
cat > /tmp/teams_view_mock_upstream.py <<'PYEOF'
import asyncio
from aiohttp import web

async def messages(req):
    return web.json_response({
      "id": "msg_smoke",
      "type": "message",
      "role": "assistant",
      "model": "claude-test",
      "content": [{"type":"text","text":"hello from mock"}],
      "stop_reason":"end_turn",
      "usage": {"input_tokens": 11, "output_tokens": 7}
    })

async def main():
    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    print(port, flush=True)
    while True:
        await asyncio.sleep(3600)

asyncio.run(main())
PYEOF

python3 /tmp/teams_view_mock_upstream.py > /tmp/teams_view_mock.port &
MOCK_PID=$!
cleanup() {
  kill -TERM "$TV_PID" 2>/dev/null || true
  kill -TERM "$MOCK_PID" 2>/dev/null || true
  # Give them a moment to exit gracefully, then force.
  sleep 0.3
  kill -KILL "$TV_PID" 2>/dev/null || true
  kill -KILL "$MOCK_PID" 2>/dev/null || true
  [ -n "${TMP_CFG:-}" ] && rm -rf "$TMP_CFG"
}
trap cleanup EXIT INT TERM

# Wait for mock to print its port.
for i in $(seq 1 20); do
  sleep 0.1
  MOCK_PORT=$(cat /tmp/teams_view_mock.port 2>/dev/null || true)
  if [ -n "$MOCK_PORT" ]; then break; fi
done
[ -n "$MOCK_PORT" ] || { echo "FAIL: mock upstream did not start"; exit 1; }
echo "Mock upstream on http://127.0.0.1:$MOCK_PORT"

# 1b. Synthesise a fake CLAUDE_CONFIG_DIR with a team + inboxes so we can exercise
#     team_watcher + mailbox_watcher without touching the user's real .claude.
TMP_CFG=$(mktemp -d)
mkdir -p "$TMP_CFG/teams/smoke-team/inboxes"
cat > "$TMP_CFG/teams/smoke-team/config.json" <<'JSON'
{
  "name": "smoke-team",
  "leadAgentId": "team-lead@smoke-team",
  "members": [
    {"agentId":"team-lead@smoke-team","name":"team-lead","agentType":"team-lead","model":"x","cwd":"/tmp"},
    {"agentId":"alice@smoke-team","name":"alice","agentType":"general-purpose","color":"red","model":"y","prompt":"do alice things","tmuxPaneId":"%1","backendType":"tmux"}
  ]
}
JSON

# Plant a STALE team config (mtime = 1 hour ago). Watcher must not pick this up.
mkdir -p "$TMP_CFG/teams/stale-team/inboxes"
cat > "$TMP_CFG/teams/stale-team/config.json" <<'JSON'
{
  "name": "stale-team",
  "leadAgentId": "team-lead@stale-team",
  "members": [
    {"agentId":"team-lead@stale-team","name":"team-lead","agentType":"team-lead","cwd":"/tmp"},
    {"agentId":"ghost@stale-team","name":"ghost","agentType":"general-purpose","color":"purple","model":"z"}
  ]
}
JSON
# Backdate by an hour — Python so it's portable across BSD/GNU touch.
python3 -c "
import os, time
p='$TMP_CFG/teams/stale-team/config.json'
old=time.time()-3600
os.utime(p,(old,old))
"
cat > "$TMP_CFG/teams/smoke-team/inboxes/alice.json" <<'JSON'
[
  {"from":"team-lead","text":"start working","timestamp":"2026-05-04T10:00:00Z","read":false,"summary":"kickoff"}
]
JSON
cat > "$TMP_CFG/teams/smoke-team/inboxes/team-lead.json" <<'JSON'
[
  {"from":"alice","text":"done","timestamp":"2026-05-04T10:05:00Z","read":false,"summary":"finished"},
  {"from":"alice","text":"{\"type\":\"shutdown_request\",\"requestId\":\"r1\",\"from\":\"alice\",\"timestamp\":\"2026-05-04T10:06:00Z\"}","timestamp":"2026-05-04T10:06:00Z","read":false}
]
JSON
export CLAUDE_CONFIG_DIR="$TMP_CFG"
echo "Fake CLAUDE_CONFIG_DIR=$TMP_CFG"

# 2. teams-view --no-launch.
python3 -m teams_view.cli \
  --no-launch \
  --port 0 \
  --viewer-port 0 \
  --target "http://127.0.0.1:$MOCK_PORT" \
  --no-open \
  --output-dir ./.teams-view-smoke > /tmp/teams_view_smoke.log 2>&1 &
TV_PID=$!

PROXY_LINE=""; VIEWER_LINE=""
for i in $(seq 1 50); do
  sleep 0.2
  PROXY_LINE=$(grep -E "Proxy:" /tmp/teams_view_smoke.log || true)
  VIEWER_LINE=$(grep -E "Viewer:" /tmp/teams_view_smoke.log || true)
  if [ -n "$PROXY_LINE" ] && [ -n "$VIEWER_LINE" ]; then break; fi
done
if [ -z "$PROXY_LINE" ] || [ -z "$VIEWER_LINE" ]; then
  echo "FAIL: teams-view did not start"
  cat /tmp/teams_view_smoke.log
  exit 1
fi

PROXY_PORT=$(echo "$PROXY_LINE" | sed -E 's/.*:([0-9]+).*/\1/')
VIEWER_PORT=$(echo "$VIEWER_LINE" | sed -E 's/.*:([0-9]+).*/\1/')
echo "Proxy: http://127.0.0.1:$PROXY_PORT  | Viewer: http://127.0.0.1:$VIEWER_PORT"

# 3. Fire requests as several "agents".
for who in team-lead alice alice; do
  curl -sS -X POST \
    "http://127.0.0.1:$PROXY_PORT/agent/$who/v1/messages" \
    -H "Content-Type: application/json" \
    -d '{"model":"claude-opus-4-7","stream":false,"messages":[{"role":"user","content":"hi"}]}' \
    > /dev/null
done

# Wait for at least one team_watcher + mailbox_watcher cycle (poll = 2s and 1s).
sleep 2.5

# 4. Verify partitioning + mailbox events.
SNAP=$(curl -sS "http://127.0.0.1:$VIEWER_PORT/snapshot")
python3 - "$SNAP" <<'PYEOF'
import json, sys
snap = json.loads(sys.argv[1])
agents = snap["agents"]; traces = snap["traces"]; mailbox = snap.get("mailbox", {})
errs = []

# --- per-agent trace partitioning ---
want = {"team-lead": 1, "alice": 2}
for name, n in want.items():
    if name not in agents: errs.append(f"missing agent: {name}"); continue
    rc = agents[name].get("request_count", 0)
    if rc != n: errs.append(f"{name}: request_count={rc} want {n}")
    if len(traces.get(name, [])) != n: errs.append(f"{name}: len(traces)={len(traces.get(name,[]))} want {n}")

# --- team_watcher hooked up the right team config ---
if snap.get("team_name") != "smoke-team":
    errs.append(f"team_name={snap.get('team_name')!r} want 'smoke-team'")
if not agents.get("alice", {}).get("prompt"):
    errs.append("alice prompt not propagated from config.json")
if agents.get("team-lead", {}).get("is_lead") is not True:
    errs.append("team-lead.is_lead != True")

# --- team config / mailbox paths surfaced in snapshot for the viewer ---
cfg_path = snap.get("team_config_path") or ""
mb_dir = snap.get("team_mailbox_dir") or ""
if not cfg_path.endswith("/teams/smoke-team/config.json"):
    errs.append(f"team_config_path missing or wrong: {cfg_path!r}")
if not mb_dir.endswith("/teams/smoke-team/inboxes"):
    errs.append(f"team_mailbox_dir missing or wrong: {mb_dir!r}")

# --- watcher must NOT pick the stale team (1-hour-old mtime). ghost member
#     should never appear in agents and must not show up as the active team. ---
if "ghost" in agents:
    errs.append("stale 'ghost' agent leaked from old team config")
if any(m.get("name") == "ghost" for m in snap.get("team_members", [])):
    errs.append("stale-team members leaked into team_members")

# --- mailbox_watcher emitted incoming + outgoing for both agents ---
alice_mb = mailbox.get("alice", [])
lead_mb = mailbox.get("team-lead", [])

# alice receives 1 (from team-lead). alice sent 2 to team-lead (incl. 1 protocol).
alice_in  = [e for e in alice_mb if e["direction"] == "in"]
alice_out = [e for e in alice_mb if e["direction"] == "out"]
if len(alice_in) != 1: errs.append(f"alice incoming={len(alice_in)} want 1")
if len(alice_out) != 2: errs.append(f"alice outgoing={len(alice_out)} want 2")

# team-lead received 2 (incl. 1 protocol). team-lead sent 1 to alice.
lead_in  = [e for e in lead_mb if e["direction"] == "in"]
lead_out = [e for e in lead_mb if e["direction"] == "out"]
if len(lead_in) != 2: errs.append(f"team-lead incoming={len(lead_in)} want 2")
if len(lead_out) != 1: errs.append(f"team-lead outgoing={len(lead_out)} want 1")

# Protocol classification.
proto_in = [e for e in lead_in if e["is_protocol"]]
if len(proto_in) != 1 or proto_in[0]["protocol_type"] != "shutdown_request":
    errs.append(f"protocol classification failed: {proto_in}")

# Historical flag set on first scan.
if not all(e["historical"] for e in alice_mb + lead_mb):
    errs.append("expected all events to be historical=True on initial scan")

if errs:
    for e in errs: print("  - " + e)
    sys.exit(1)

print("PASS: trace partitioning correct (team-lead:1 alice:2)")
print("PASS: team_watcher loaded smoke-team and propagated alice.prompt")
print("PASS: stale team-config (1-hour-old mtime) was correctly ignored")
print(f"PASS: team_config_path + team_mailbox_dir surfaced ({cfg_path!r})")
print(f"PASS: mailbox alice:  {len(alice_in)} in / {len(alice_out)} out")
print(f"PASS: mailbox lead:   {len(lead_in)} in / {len(lead_out)} out")
print(f"PASS: protocol classification ({proto_in[0]['protocol_type']})")
PYEOF

# 5. Verify JSONL files were written per agent (latest session dir).
LATEST_SESSION=$(find ./.teams-view-smoke -mindepth 1 -maxdepth 1 -type d | sort | tail -1)
[ -n "$LATEST_SESSION" ] || { echo "FAIL: no session dir found"; exit 1; }
echo "  latest session: $LATEST_SESSION"
for f in traces/team-lead.jsonl traces/alice.jsonl mailbox/team-lead.jsonl mailbox/alice.jsonl; do
  full="$LATEST_SESSION/$f"
  [ -f "$full" ] || { echo "FAIL: missing $f"; exit 1; }
  lines=$(wc -l < "$full" | tr -d ' ')
  echo "  $f: $lines line(s)"
done
echo "PASS: per-agent traces/ + mailbox/ JSONL files written"
echo "---SMOKE OK---"
cleanup
exit 0
