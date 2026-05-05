#!/bin/sh
# Verify shim correctly rewrites ANTHROPIC_BASE_URL even when _TEAMS_VIEW_PORT
# is absent (the actual production scenario inside tmux). We replace the real
# claude with a tiny script that prints its argv + the inherited base URL,
# then check shim's exec'd output.
set -eu

cd "$(dirname "$0")/.."
. .venv/bin/activate

FAKE_DIR=$(mktemp -d)
FAKE_CLAUDE="$FAKE_DIR/claude"
cat > "$FAKE_CLAUDE" <<'PYEOF'
#!/usr/bin/env python3
import os, sys
print("ARGS:", " ".join(sys.argv[1:]))
print("BASE_URL:", os.environ.get("ANTHROPIC_BASE_URL", "<UNSET>"))
print("CTC:", os.environ.get("CLAUDE_CODE_TEAMMATE_COMMAND", "<UNSET>"))
print("NO_PROXY:", os.environ.get("NO_PROXY", "<UNSET>"))
PYEOF
chmod +x "$FAKE_CLAUDE"

trap 'rm -rf "$FAKE_DIR"' EXIT

run_case() {
  desc="$1"; expected_url="$2"; shift 2
  out=$("$@")
  echo "--- $desc ---"
  echo "$out"
  got=$(echo "$out" | sed -n 's/^BASE_URL: //p')
  if [ "$got" != "$expected_url" ]; then
    echo "FAIL: BASE_URL mismatch"
    echo "  want: $expected_url"
    echo "  got : $got"
    exit 1
  fi
  echo "PASS"
  echo
}

# Case 1: production-like — only ANTHROPIC_BASE_URL is propagated through tmux,
# _TEAMS_VIEW_PORT is *missing*. Shim must derive port from BASE_URL.
env -i \
  PATH="$PATH" \
  ANTHROPIC_BASE_URL="http://127.0.0.1:62523/agent/team-lead" \
  _TEAMS_VIEW_REAL_CLAUDE="$FAKE_CLAUDE" \
  bash -c 'teams-view-shim --agent-id "css-architect@x" --agent-name "css-architect" --team-name x' \
  > /tmp/shim_case1.out
run_case "no _TEAMS_VIEW_PORT, only BASE_URL inherited" \
  "http://127.0.0.1:62523/agent/css-architect" \
  cat /tmp/shim_case1.out

# Case 2: with custom host/port and equals-form arg.
env -i \
  PATH="$PATH" \
  ANTHROPIC_BASE_URL="http://localhost:8080/agent/team-lead" \
  _TEAMS_VIEW_REAL_CLAUDE="$FAKE_CLAUDE" \
  bash -c 'teams-view-shim --agent-name=layout-designer --team-name=x' \
  > /tmp/shim_case2.out
run_case "equals-form --agent-name, localhost host" \
  "http://localhost:8080/agent/layout-designer" \
  cat /tmp/shim_case2.out

# Case 3: BASE_URL unrelated to teams-view (e.g. user runs shim by accident).
# Shim should pass through without rewriting and without crashing.
env -i \
  PATH="$PATH" \
  ANTHROPIC_BASE_URL="https://api.anthropic.com" \
  _TEAMS_VIEW_REAL_CLAUDE="$FAKE_CLAUDE" \
  bash -c 'teams-view-shim --agent-name=foo --team-name=x' \
  > /tmp/shim_case3.out
run_case "unrelated BASE_URL — passthrough without rewrite" \
  "https://api.anthropic.com" \
  cat /tmp/shim_case3.out

# Case 4: argv passthrough — every CLI flag the leader passed must reach claude.
env -i \
  PATH="$PATH" \
  ANTHROPIC_BASE_URL="http://127.0.0.1:9000/agent/team-lead" \
  _TEAMS_VIEW_REAL_CLAUDE="$FAKE_CLAUDE" \
  bash -c 'teams-view-shim --agent-id z --agent-name alice --team-name x --agent-color red --plan-mode-required' \
  > /tmp/shim_case4.out
echo "--- argv passthrough ---"
cat /tmp/shim_case4.out
got_args=$(grep ^ARGS: /tmp/shim_case4.out | sed 's/^ARGS: //')
expected="--agent-id z --agent-name alice --team-name x --agent-color red --plan-mode-required"
if [ "$got_args" != "$expected" ]; then
  echo "FAIL: argv mismatch"
  echo "  want: $expected"
  echo "  got : $got_args"
  exit 1
fi
echo "PASS: argv passthrough"
echo

echo "---SHIM TEST OK---"
