#!/bin/sh
# Verify teams-view does NOT spew log output to stderr/stdout once running
# (would otherwise interleave with Claude's TUI). All operational logs must
# land in <session>/teams-view.log instead.
set -eu

cd "$(dirname "$0")/.."
. .venv/bin/activate

# Mock upstream so the proxy has somewhere to forward to.
cat > /tmp/teams_view_mock_upstream.py <<'PYEOF'
import asyncio
from aiohttp import web
async def messages(req):
    return web.json_response({"id":"m","type":"message","role":"assistant","content":[{"type":"text","text":"ok"}],"usage":{"input_tokens":1,"output_tokens":1}})
async def main():
    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
    print(site._server.sockets[0].getsockname()[1], flush=True)
    while True: await asyncio.sleep(3600)
asyncio.run(main())
PYEOF
python3 /tmp/teams_view_mock_upstream.py > /tmp/tv_mock.port &
MOCK_PID=$!
trap 'kill -KILL $MOCK_PID $TV_PID 2>/dev/null || true; rm -rf "$TMP_CFG" 2>/dev/null' EXIT
sleep 0.4
MOCK_PORT=$(cat /tmp/tv_mock.port)

# Fake CLAUDE_CONFIG_DIR with a fresh team config so team_watcher activates.
TMP_CFG=$(mktemp -d)
mkdir -p "$TMP_CFG/teams/loud-team/inboxes"
cat > "$TMP_CFG/teams/loud-team/config.json" <<'JSON'
{"name":"loud-team","leadAgentId":"team-lead@x","members":[
  {"agentId":"team-lead@x","name":"team-lead","agentType":"team-lead","cwd":"/tmp"},
  {"agentId":"bob@x","name":"bob","agentType":"general-purpose","prompt":"...","color":"red","model":"y"}
]}
JSON
# Plant a malformed inbox file so MailboxWatcher's logger DOES get exercised.
echo "this is not json" > "$TMP_CFG/teams/loud-team/inboxes/bob.json"
export CLAUDE_CONFIG_DIR="$TMP_CFG"

rm -rf .teams-view-stderr
python3 -m teams_view.cli \
  --no-launch \
  --port 0 \
  --viewer-port 0 \
  --target "http://127.0.0.1:$MOCK_PORT" \
  --no-open \
  --output-dir ./.teams-view-stderr \
  > /tmp/tv_stdout.txt 2> /tmp/tv_stderr.txt &
TV_PID=$!

# Wait for proxy to be ready.
for i in $(seq 1 50); do
  sleep 0.2
  PROXY_LINE=$(grep "Proxy:" /tmp/tv_stdout.txt 2>/dev/null || true)
  [ -n "$PROXY_LINE" ] && break
done
[ -n "$PROXY_LINE" ] || { echo "FAIL: teams-view didn't start"; cat /tmp/tv_stdout.txt /tmp/tv_stderr.txt; exit 1; }
PROXY_PORT=$(echo "$PROXY_LINE" | sed -E 's/.*:([0-9]+).*/\1/')

# Make several requests so proxy logs every time.
for who in team-lead bob bob bob; do
  curl -sS -X POST "http://127.0.0.1:$PROXY_PORT/agent/$who/v1/messages" \
    -H "Content-Type: application/json" \
    -d '{"model":"x","stream":false,"messages":[{"role":"user","content":"hi"}]}' \
    > /dev/null
done

# Force a couple watcher cycles + write a corrupted inbox to provoke a warning log.
sleep 3.0
echo "still not json" > "$TMP_CFG/teams/loud-team/inboxes/bob.json"
sleep 1.5

# stderr MUST be empty (or nearly so). stdout should have exactly the banner +
# the --no-launch hint, nothing repeating per request.
STDERR_BYTES=$(wc -c < /tmp/tv_stderr.txt | tr -d ' ')
STDERR_LINES=$(wc -l < /tmp/tv_stderr.txt | tr -d ' ')
STDOUT_LINES=$(wc -l < /tmp/tv_stdout.txt | tr -d ' ')

echo "stderr bytes: $STDERR_BYTES (lines: $STDERR_LINES)"
echo "stdout lines: $STDOUT_LINES"

# Surface contents on failure for debugging.
if [ "$STDERR_BYTES" != "0" ]; then
  echo "=== /tmp/tv_stderr.txt ==="
  cat /tmp/tv_stderr.txt
  echo "=== end ==="
  echo "FAIL: stderr is not empty"
  exit 1
fi

# Verify the file log DID receive the proxy + watcher chatter (negative would
# mean we accidentally killed all logging, not just the stderr stream).
LOG_FILE=$(find .teams-view-stderr -name "teams-view.log" | head -1)
[ -f "$LOG_FILE" ] || { echo "FAIL: teams-view.log not found"; exit 1; }
LOG_LINES=$(wc -l < "$LOG_FILE" | tr -d ' ')
echo "log file: $LOG_FILE  ($LOG_LINES lines)"
if [ "$LOG_LINES" -lt 5 ]; then
  echo "FAIL: log file suspiciously empty — did we kill all logging?"
  cat "$LOG_FILE"
  exit 1
fi
# Sanity: the log should mention proxy traffic.
grep -q "POST /v1/messages" "$LOG_FILE" || { echo "FAIL: proxy logs missing from file"; cat "$LOG_FILE"; exit 1; }

echo "PASS: stderr is silent, all chatter went to teams-view.log"
echo "---STDERR TEST OK---"

# Silence bash's "Terminated"/"Killed" job-control messages from the trap
# cleanup — they're noise on a green test, not a real failure.
{
  kill -TERM "$TV_PID" 2>/dev/null || true
  sleep 0.2
  kill -KILL "$TV_PID" 2>/dev/null || true
  rm -rf .teams-view-stderr
} >/dev/null 2>&1
trap - EXIT
{ kill -KILL "$MOCK_PID" 2>/dev/null; rm -rf "$TMP_CFG"; } >/dev/null 2>&1
exit 0
