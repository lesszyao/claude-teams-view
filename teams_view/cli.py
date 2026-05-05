"""teams-view CLI entry: starts proxy + viewer + leader claude.

Usage:
    teams-view [--port AUTO] [--viewer-port AUTO] [--target https://api.anthropic.com]
               [--no-launch] [--output-dir ./.teams-view]
               [-- <claude args...>]

Env contract for the spawned leader (and via shim, every teammate):
    ANTHROPIC_BASE_URL=http://127.0.0.1:<proxy_port>/agent/<name>
    CLAUDE_CODE_TEAMMATE_COMMAND=<absolute path to teams-view-shim>
    _TEAMS_VIEW_PORT=<proxy_port>
    _TEAMS_VIEW_REAL_CLAUDE=<absolute path to claude binary>
    NO_PROXY=127.0.0.1
    (CLAUDECODE / CLAUDE_CODE_SSE_PORT cleared to avoid nesting confusion)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path

import aiohttp

from teams_view import __version__
from teams_view.mailbox_watcher import MailboxWatcher
from teams_view.proxy import start_proxy
from teams_view.team_watcher import TeamWatcher
from teams_view.trace_store import TraceStore
from teams_view.viewer_server import start_viewer

# Force line-buffered UTF-8 stdio so emoji + progress prints work everywhere.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

log = logging.getLogger("teams-view")
LEAD_AGENT_NAME = "team-lead"


def _open_browser_async(url: str) -> None:
    threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()


def _resolve_shim_path() -> str:
    """Locate the teams-view-shim executable.

    Priority:
      1. TEAMS_VIEW_SHIM env var (manual override)
      2. `teams-view-shim` on PATH (installed via pip/uv)
      3. Fallback: `<python> -m teams_view.shim` — but we can't put that as a
         single argv entry. So if PATH lookup fails, we write a tiny launcher
         script next to our session dir.
    """
    env_override = os.environ.get("TEAMS_VIEW_SHIM")
    if env_override and Path(env_override).is_file():
        return env_override
    found = shutil.which("teams-view-shim")
    if found:
        return found
    # Write a launcher script that invokes `python -m teams_view.shim`.
    launcher_dir = Path.home() / ".cache" / "teams-view"
    launcher_dir.mkdir(parents=True, exist_ok=True)
    launcher = launcher_dir / "teams-view-shim"
    launcher.write_text(
        "#!/bin/sh\nexec " + sys.executable + ' -m teams_view.shim "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return str(launcher)


def _resolve_real_claude() -> str | None:
    """Find the real claude binary the leader (and shim) will invoke."""
    return shutil.which("claude")


async def _run_leader(
    *,
    proxy_port: int,
    shim_path: str,
    real_claude: str,
    extra_args: list[str],
    tmux_leader: bool,
) -> int:
    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{proxy_port}/agent/{LEAD_AGENT_NAME}"
    env["CLAUDE_CODE_TEAMMATE_COMMAND"] = shim_path
    env["_TEAMS_VIEW_PORT"] = str(proxy_port)
    env["_TEAMS_VIEW_REAL_CLAUDE"] = real_claude
    no_proxy = env.get("NO_PROXY", "")
    if "127.0.0.1" not in no_proxy:
        env["NO_PROXY"] = (no_proxy + ",127.0.0.1").lstrip(",")
    # Prevent claude from thinking it's nested inside another claude.
    for k in ("CLAUDECODE", "CLAUDE_CODE_SSE_PORT"):
        env.pop(k, None)

    # Detach from the user's current tmux unless they explicitly opted in.
    # When TMUX/TMUX_PANE are unset, Claude Code's TmuxBackend goes down its
    # "outside tmux" path: it spins up a detached swarm session on a private
    # socket (`tmux -L claude-swarm-<pid>`) for teammates instead of splitting
    # the user's current window. With teams-view's web viewer there's no need
    # to see teammate panes in the terminal, so this becomes the right default.
    detached = not tmux_leader
    if detached:
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)

    print(f"\nLaunching: claude {' '.join(extra_args)}")
    print(f"   ANTHROPIC_BASE_URL={env['ANTHROPIC_BASE_URL']}")
    print(f"   CLAUDE_CODE_TEAMMATE_COMMAND={shim_path}")
    if detached:
        print("   tmux mode:    detached swarm (teammates run on private socket; see web viewer)")
    else:
        print("   tmux mode:    leader-attached (teammate panes will split your current window)")
    print()

    use_fg = hasattr(os, "tcsetpgrp") and sys.stdin.isatty()
    proc = await asyncio.create_subprocess_exec(
        real_claude,
        *extra_args,
        env=env,
        stdin=None,
        stdout=None,
        stderr=None,
        **({"process_group": 0} if use_fg else {}),
    )
    if use_fg:
        try:
            os.tcsetpgrp(sys.stdin.fileno(), proc.pid)
        except OSError:
            pass

    # Forward Ctrl+C: first ask politely, second kills hard.
    loop = asyncio.get_running_loop()
    sigint_count = 0

    def _on_sigint() -> None:
        nonlocal sigint_count
        sigint_count += 1
        if proc.returncode is not None:
            return
        if sigint_count == 1:
            proc.terminate()
            print("\nShutting down claude... (Ctrl+C again to force)")
        else:
            proc.kill()

    sigtstp = getattr(signal, "SIGTSTP", None)
    old_sigtstp = signal.signal(sigtstp, signal.SIG_IGN) if sigtstp is not None else None

    try:
        loop.add_signal_handler(signal.SIGINT, _on_sigint)
    except (NotImplementedError, OSError):
        pass

    code = await proc.wait()

    if use_fg:
        old_sigttou = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        try:
            os.tcsetpgrp(sys.stdin.fileno(), os.getpgrp())
        except OSError:
            pass
        signal.signal(signal.SIGTTOU, old_sigttou)
    if sigtstp is not None and old_sigtstp is not None:
        signal.signal(sigtstp, old_sigtstp)
    try:
        loop.remove_signal_handler(signal.SIGINT)
    except (NotImplementedError, OSError):
        pass

    print(f"\nclaude exited with code {code}")
    return code


async def _async_main(args: argparse.Namespace) -> int:
    # Capture wall-clock start *before* anything async — TeamWatcher uses this
    # to filter out stale team configs from previous sessions.
    start_time = time.time()

    output_dir = Path(args.output_dir).expanduser().absolute()
    output_dir.mkdir(parents=True, exist_ok=True)
    store = TraceStore(output_dir)

    # Logging is FILE-ONLY. Anything written to stderr would clobber Claude's
    # TUI (cursor positioning, key sequences, etc). We deliberately do NOT call
    # `logging.basicConfig()` — that adds a StreamHandler to the root logger.
    log_path = store.session_dir / "teams-view.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    )
    file_handler.setLevel(logging.DEBUG)

    teams_log = logging.getLogger("teams-view")
    teams_log.setLevel(logging.DEBUG)
    teams_log.addHandler(file_handler)
    teams_log.propagate = False  # don't bubble to root → never reaches stderr

    # aiohttp + asyncio chatter (broken sockets on shutdown, etc.) goes to file
    # at WARNING+ instead of escaping to stderr.
    for noisy in ("aiohttp", "aiohttp.access", "aiohttp.server", "aiohttp.web", "asyncio"):
        nlog = logging.getLogger(noisy)
        nlog.setLevel(logging.WARNING)
        nlog.addHandler(file_handler)
        nlog.propagate = False

    print(f"\n  teams-view v{__version__}")
    print(f"  Session dir: {store.session_dir}")
    print(f"  Log file:    {log_path}  (tail this if something looks off)")

    session = aiohttp.ClientSession(auto_decompress=False, trust_env=True)

    proxy_runner, proxy_port = await start_proxy(
        host=args.host,
        port=args.port,
        target_url=args.target,
        store=store,
        session=session,
        lead_agent_name=LEAD_AGENT_NAME,
    )
    print(f"  Proxy:       http://{args.host}:{proxy_port}")

    viewer_runner, viewer_port = await start_viewer(
        host=args.host,
        port=args.viewer_port,
        store=store,
    )
    viewer_url = f"http://{args.host}:{viewer_port}"
    print(f"  Viewer:      {viewer_url}")

    team_watcher = TeamWatcher(store, start_time=start_time)
    await team_watcher.start()
    mailbox_watcher = MailboxWatcher(store)
    await mailbox_watcher.start()

    if args.open_browser:
        _open_browser_async(viewer_url)

    exit_code = 0
    try:
        if args.no_launch:
            print("\n--no-launch mode: proxy + viewer running. Press Ctrl+C to stop.")
            print(f"  Set: ANTHROPIC_BASE_URL=http://127.0.0.1:{proxy_port}/agent/{LEAD_AGENT_NAME}")
            print(f"       CLAUDE_CODE_TEAMMATE_COMMAND={_resolve_shim_path()}")
            print(f"       _TEAMS_VIEW_PORT={proxy_port}")
            print("       _TEAMS_VIEW_REAL_CLAUDE=$(which claude)")
            try:
                while True:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass
        else:
            real_claude = _resolve_real_claude()
            if not real_claude:
                print("\nERROR: 'claude' not found on PATH. Install Claude Code first.", file=sys.stderr)
                return 127
            shim_path = _resolve_shim_path()
            exit_code = await _run_leader(
                proxy_port=proxy_port,
                shim_path=shim_path,
                real_claude=real_claude,
                extra_args=args.claude_args,
                tmux_leader=args.tmux_leader,
            )
    finally:
        await mailbox_watcher.stop()
        await team_watcher.stop()
        try:
            await session.close()
        except Exception:
            pass
        try:
            await proxy_runner.cleanup()
        except Exception:
            pass
        try:
            await viewer_runner.cleanup()
        except Exception:
            pass

        # Final summary
        snap = await store.snapshot()
        agents = snap["agents"]
        if agents:
            print(f"\nTrace summary ({len(agents)} agents):")
            for name, m in agents.items():
                tok = (m.get("total_input_tokens") or 0) + (m.get("total_output_tokens") or 0)
                lead = " [LEAD]" if m.get("is_lead") else ""
                print(f"  - {name}{lead}: {m.get('request_count') or 0} requests, {tok} tokens")
        print(f"  Traces: {store.traces_dir}")

    return exit_code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="teams-view",
        description=(
            "Per-teammate visibility for Claude Code agent teams. "
            "Launches `claude` with a reverse proxy that tags each request with "
            "the teammate that made it; shows a sidebar of team members and a "
            "real-time, per-member request stream in your browser."
        ),
        epilog=(
            "Examples:\n"
            "  teams-view                                 # launch claude with default settings\n"
            "  teams-view -- --dangerously-skip-permissions\n"
            "  teams-view --port 8080 --viewer-port 8081\n"
            "  teams-view --no-launch                     # only run proxy+viewer, attach manually\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")

    parser.add_argument("--port", type=int, default=0, help="Proxy port (0 = auto)")
    parser.add_argument("--viewer-port", type=int, default=0, help="Viewer port (0 = auto)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    parser.add_argument(
        "--target",
        default="https://api.anthropic.com",
        help="Upstream API base URL (default: https://api.anthropic.com). "
        "Set this to your custom gateway, e.g. https://api.deepseek.com/anthropic",
    )
    parser.add_argument("--output-dir", default="./.teams-view", help="Trace output dir (default: ./.teams-view)")
    parser.add_argument("--no-launch", action="store_true", help="Don't launch claude; just run proxy+viewer")
    parser.add_argument(
        "--no-open",
        dest="open_browser",
        action="store_false",
        default=True,
        help="Don't auto-open the viewer in browser",
    )
    parser.add_argument(
        "--tmux-leader",
        action="store_true",
        default=False,
        help=(
            "Keep TMUX/TMUX_PANE env so teammate panes split your CURRENT tmux window. "
            "Default (off): clear TMUX, run teammates in a detached swarm session — "
            "you don't need to start tmux yourself, and the web viewer shows everything."
        ),
    )

    args, claude_args = parser.parse_known_args(argv)
    if claude_args and claude_args[0] == "--":
        claude_args = claude_args[1:]
    args.claude_args = claude_args
    return args


def main() -> None:
    args = parse_args()
    try:
        code = asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
