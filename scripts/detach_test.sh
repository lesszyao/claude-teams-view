#!/bin/sh
# Verify teams-view's tmux-detach default: TMUX/TMUX_PANE must be unset in the
# leader's environment unless --tmux-leader is passed.
set -eu
cd "$(dirname "$0")/.."
. .venv/bin/activate

# Fake claude that just prints the env vars we care about, then exits 0.
FAKE_DIR=$(mktemp -d)
FAKE_CLAUDE="$FAKE_DIR/claude"
cat > "$FAKE_CLAUDE" <<'PYEOF'
#!/usr/bin/env python3
import os, sys
print("TMUX:", os.environ.get("TMUX", "<UNSET>"))
print("TMUX_PANE:", os.environ.get("TMUX_PANE", "<UNSET>"))
print("BASE_URL:", os.environ.get("ANTHROPIC_BASE_URL", "<UNSET>"))
sys.exit(0)
PYEOF
chmod +x "$FAKE_CLAUDE"
trap 'rm -rf "$FAKE_DIR"' EXIT

run_one() {
  desc="$1"; shift
  flags="$1"; shift
  expected_tmux="$1"; shift
  echo "--- $desc ---"
  TMUX="/tmp/fake-tmux-socket,1234,5" \
  TMUX_PANE="%99" \
  PATH="$FAKE_DIR:$PATH" \
  python3 -m teams_view.cli \
    --no-open \
    --port 0 --viewer-port 0 \
    --target "http://example.invalid" \
    --output-dir ./.teams-view-detach \
    $flags \
    > /tmp/detach_run.out 2>&1 || true
  cat /tmp/detach_run.out
  got=$(grep '^TMUX:' /tmp/detach_run.out | head -1 | sed 's/^TMUX: //')
  if [ "$got" != "$expected_tmux" ]; then
    echo "FAIL: TMUX mismatch — want '$expected_tmux', got '$got'"
    exit 1
  fi
  echo "PASS"
  echo
}

# Default behavior: TMUX must be cleared.
run_one "default detached mode → TMUX must be UNSET" "" "<UNSET>"

# --tmux-leader: TMUX must be preserved.
run_one "--tmux-leader → TMUX must be PRESERVED" "--tmux-leader" "/tmp/fake-tmux-socket,1234,5"

rm -rf .teams-view-detach
echo "---DETACH TEST OK---"
