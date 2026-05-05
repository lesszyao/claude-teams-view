"""teams-view-shim: thin wrapper invoked by Claude Code's `CLAUDE_CODE_TEAMMATE_COMMAND` hook.

The leader spawns each teammate via `tmux send-keys` with a command like:
    cd <cwd> && env <inherited_env> <SHIM> --agent-id ... --agent-name css-architect --team-name X ...

`<inherited_env>` is whatever the leader's `buildInheritedEnvVars()` deems
forwardable (a hard-coded white-list inside Claude Code; see
`src/utils/swarm/spawnUtils.ts:TEAMMATE_ENV_VARS`). It contains
`ANTHROPIC_BASE_URL` but **does NOT** contain teams-view's private vars like
`_TEAMS_VIEW_PORT`. Worse, tmux server is a singleton: panes inherit the env
that existed when the user *first* started tmux, NOT the env of the leader
process. So we cannot rely on env propagation for those private vars.

The good news: the leader's `ANTHROPIC_BASE_URL` already encodes the proxy
port and host (e.g. `http://127.0.0.1:62523/agent/team-lead`). The shim parses
it, swaps the `team-lead` segment for its own `--agent-name`, and execs claude.

This shim:
  1. Parses `--agent-name` from argv.
  2. Reads `ANTHROPIC_BASE_URL` (set by the leader) and rewrites the
     `/agent/<old>/...` segment to `/agent/<my-name>/...`.
  3. Re-asserts CLAUDE_CODE_TEAMMATE_COMMAND so any deeper spawn also goes
     through us (defensive — agent teams currently only spawn one level deep).
  4. `os.execvpe`s the real claude binary with original argv (minus argv[0]).

The shim never sees HTTP traffic; identity flows entirely through the URL path.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from urllib.parse import urlparse, urlunparse

# Match the first /agent/<name> segment of a URL path.
_AGENT_PATH_RE = re.compile(r"^(/agent/)([^/]+)(.*)$")


def _extract_arg(argv: list[str], flag: str) -> str | None:
    """Return the value following `flag` in argv, or None if absent.

    Supports both `--flag value` and `--flag=value` forms.
    """
    for i, a in enumerate(argv):
        if a == flag:
            if i + 1 < len(argv):
                return argv[i + 1]
            return None
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def _sanitize_agent_name(name: str) -> str:
    """Allow only safe URL-path characters; fall back to 'unknown' if empty."""
    safe = "".join(c for c in name if c.isalnum() or c in "-_.")
    return safe or "unknown"


def _rewrite_base_url(base_url: str, agent_name: str) -> str | None:
    """Replace the /agent/<old> segment in `base_url` with /agent/<agent_name>.

    Returns the rewritten URL, or None if the URL is not a teams-view URL.
    """
    if not base_url:
        return None
    try:
        parts = urlparse(base_url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    m = _AGENT_PATH_RE.match(parts.path or "")
    if not m:
        return None
    new_path = f"/agent/{agent_name}{m.group(3)}"
    return urlunparse(parts._replace(path=new_path))


def _resolve_real_claude() -> str | None:
    """Locate the real claude binary, avoiding self-reference (the shim itself)."""
    explicit = os.environ.get("_TEAMS_VIEW_REAL_CLAUDE")
    if explicit and os.path.isfile(explicit):
        return explicit
    candidate = shutil.which("claude")
    if candidate:
        # Defensive: refuse to exec ourselves if `claude` somehow points at the shim.
        try:
            same = os.path.samefile(candidate, sys.argv[0])
        except OSError:
            same = False
        if not same:
            return candidate
    return None


def main() -> int:
    raw_name = _extract_arg(sys.argv, "--agent-name") or "unknown"
    agent_name = _sanitize_agent_name(raw_name)

    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    new_base_url = _rewrite_base_url(base_url, agent_name)

    real_claude = _resolve_real_claude()
    if not real_claude:
        print(
            "[teams-view-shim] ERROR: cannot find a real `claude` binary on PATH; "
            "set _TEAMS_VIEW_REAL_CLAUDE or install claude.",
            file=sys.stderr,
        )
        return 127

    if new_base_url is None:
        # Not invoked under teams-view (or BASE_URL doesn't carry our prefix).
        # Pass through to claude so we don't break unrelated agent-teams setups.
        print(
            "[teams-view-shim] WARNING: ANTHROPIC_BASE_URL does not look like a "
            "teams-view URL; running claude without identity routing.",
            file=sys.stderr,
        )
        os.execvp(real_claude, [real_claude, *sys.argv[1:]])
        return 0

    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = new_base_url
    # Defensive: NO_PROXY so loopback isn't routed through an upstream HTTP proxy.
    no_proxy = env.get("NO_PROXY", "")
    if "127.0.0.1" not in no_proxy:
        env["NO_PROXY"] = (no_proxy + ",127.0.0.1").lstrip(",")
    # Keep deeper spawns routed through us.
    env["CLAUDE_CODE_TEAMMATE_COMMAND"] = sys.argv[0]

    os.execvpe(real_claude, [real_claude, *sys.argv[1:]], env)
    return 0  # unreachable


if __name__ == "__main__":
    sys.exit(main())
