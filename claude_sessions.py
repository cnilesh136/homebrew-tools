#!/usr/bin/env python3
"""claude-sessions — list, resume, delete, and start Claude Code sessions.

Scans ~/.claude/projects/*/<session-id>.jsonl transcripts, enriches them with
live-process info from ~/.claude/sessions/<pid>.json.

Usage:
  claude_sessions.py                     # interactive picker (when on a TTY)
  claude_sessions.py list                # table of all sessions, newest first
  claude_sessions.py list --json         # machine-readable output
  claude_sessions.py list --running      # only sessions with a live process
  claude_sessions.py list --project X    # filter by project path substring
  claude_sessions.py show <id-prefix>    # full detail for one session (JSON)
  claude_sessions.py resume <id-prefix>  # resume a session (execs `claude --resume`)
  claude_sessions.py delete <id-prefix>  # delete a session transcript (--yes to skip confirm)
  claude_sessions.py new [directory]     # start a new session (execs `claude`)

Interactive keys:
  up/down or j/k  move        Enter/r  resume selected
  n               new session d        delete selected (asks to confirm)
  g/G             top/bottom  q/Esc    quit

Stdlib only; Python 3.9+; macOS/Linux.
"""

import argparse
import json
import os
import select
import shutil
import signal
import sys
import termios
import tty
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
LIVE_DIR = CLAUDE_DIR / "sessions"


# ---------------------------------------------------------------- live sessions

def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load_live_sessions():
    """Map sessionId -> live-process metadata for currently running CLI sessions."""
    live = {}
    if not LIVE_DIR.is_dir():
        return live
    for f in LIVE_DIR.glob("*.json"):
        try:
            meta = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        pid = meta.get("pid")
        sid = meta.get("sessionId")
        if not sid or not isinstance(pid, int) or not pid_alive(pid):
            continue
        live[sid] = {
            "pid": pid,
            "name": meta.get("name"),
            "status": meta.get("status"),
            "kind": meta.get("kind"),
            "cwd": meta.get("cwd"),
            "version": meta.get("version"),
        }
    return live


# ---------------------------------------------------------------- transcript parsing

def first_text(content):
    """Extract displayable text from a message content field (str or blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    return ""


def parse_transcript(path):
    s = {
        "session_id": path.stem,
        "transcript": str(path),
        "project": None,
        "title": None,
        "first_prompt": None,
        "git_branch": None,
        "cli_version": None,
        "started": None,
        "last_activity": None,
        "user_messages": 0,
        "assistant_messages": 0,
        "models": [],
        "tokens": {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_creation": 0,
        },
    }
    models = {}  # model -> assistant message count, insertion-ordered
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError as e:
        s["error"] = str(e)
        return s
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = r.get("type")

            ts = r.get("timestamp")
            if isinstance(ts, str):
                if s["started"] is None:
                    s["started"] = ts
                s["last_activity"] = ts
            if s["project"] is None and r.get("cwd"):
                s["project"] = r["cwd"]
            if r.get("gitBranch"):
                s["git_branch"] = r["gitBranch"]
            if r.get("version"):
                s["cli_version"] = r["version"]

            if rtype == "custom-title" and r.get("customTitle"):
                s["title"] = r["customTitle"]
            elif rtype == "user":
                if r.get("isMeta") or r.get("isSidechain"):
                    continue
                s["user_messages"] += 1
                if s["first_prompt"] is None:
                    msg = r.get("message") or {}
                    text = first_text(msg.get("content")).strip()
                    # skip slash commands and injected XML-ish content
                    if text and not text.startswith(("/", "<")):
                        s["first_prompt"] = text[:200]
            elif rtype == "assistant":
                if r.get("isSidechain"):
                    continue
                s["assistant_messages"] += 1
                msg = r.get("message") or {}
                model = msg.get("model")
                if model and model != "<synthetic>":
                    models[model] = models.get(model, 0) + 1
                usage = msg.get("usage") or {}
                t = s["tokens"]
                t["input"] += usage.get("input_tokens", 0) or 0
                t["output"] += usage.get("output_tokens", 0) or 0
                t["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
                t["cache_creation"] += usage.get("cache_creation_input_tokens", 0) or 0

    s["models"] = list(models)
    return s


def collect_sessions():
    sessions = []
    if PROJECTS_DIR.is_dir():
        for path in PROJECTS_DIR.glob("*/*.jsonl"):
            sessions.append(parse_transcript(path))
    live = load_live_sessions()
    for s in sessions:
        info = live.pop(s["session_id"], None)
        s["running"] = bool(info)
        s["live"] = info
    # live sessions whose transcript we did not find (e.g. brand-new, no writes yet)
    for sid, info in live.items():
        sessions.append({
            "session_id": sid,
            "transcript": None,
            "project": info.get("cwd"),
            "title": info.get("name"),
            "first_prompt": None,
            "started": None,
            "last_activity": None,
            "user_messages": 0,
            "assistant_messages": 0,
            "models": [],
            "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
            "running": True,
            "live": info,
        })
    sessions.sort(key=lambda s: (s["running"], s["last_activity"] or ""), reverse=True)
    return sessions


def find_session(sessions, prefix):
    matches = [s for s in sessions if s["session_id"].startswith(prefix)]
    if not matches:
        print(f"No session matching '{prefix}'", file=sys.stderr)
        return None
    if len(matches) > 1:
        print(f"Ambiguous prefix '{prefix}' matches "
              f"{', '.join(s['session_id'] for s in matches)}", file=sys.stderr)
        return None
    return matches[0]


# ---------------------------------------------------------------- actions

def exec_claude(argv, cwd=None):
    """Replace this process with the claude CLI. Only returns on failure."""
    exe = shutil.which("claude")
    if not exe:
        print("claude CLI not found on PATH", file=sys.stderr)
        return 1
    if cwd:
        try:
            os.chdir(cwd)
        except OSError as e:
            print(f"Cannot chdir to {cwd}: {e}", file=sys.stderr)
            return 1
    os.execv(exe, [exe] + argv)


def resume_session(s):
    project = s.get("project")
    if not project or not Path(project).is_dir():
        print(f"Project directory for session {s['session_id']} not found: {project}",
              file=sys.stderr)
        return 1
    return exec_claude(["--resume", s["session_id"]], cwd=project)


def delete_session(s):
    """Delete a session transcript. Returns (ok, message)."""
    if s["running"]:
        pid = (s.get("live") or {}).get("pid")
        return False, f"Session is running (pid {pid}) — quit it first."
    path = s.get("transcript")
    if not path:
        return False, "Session has no transcript file."
    try:
        os.remove(path)
    except OSError as e:
        return False, f"Delete failed: {e}"
    return True, f"Deleted {s['session_id']}"


# ---------------------------------------------------------------- output helpers

def humanize_time(iso):
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    if secs < 86400 * 30:
        return f"{secs // 86400}d ago"
    return dt.astimezone().strftime("%Y-%m-%d")


def humanize_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def short_model(model):
    return (model or "").removeprefix("claude-")


def display_title(s):
    return (s.get("title") or s.get("first_prompt") or "(no prompt)").replace("\n", " ")


def shorten_project(project):
    if not project:
        return "-"
    home = str(Path.home())
    if project.startswith(home):
        project = "~" + project[len(home):]
    return project


def print_table(sessions):
    if not sessions:
        print("No sessions found.")
        return
    rows = []
    for s in sessions:
        state = "RUNNING" if s["running"] else ""
        if s["running"] and s.get("live", {}).get("status"):
            state = f"RUNNING ({s['live']['status']})"
        rows.append([
            s["session_id"][:8],
            shorten_project(s["project"]),
            display_title(s)[:48],
            humanize_time(s["last_activity"]),
            str(s["user_messages"] + s["assistant_messages"]),
            humanize_tokens(s["tokens"]["output"]),
            ", ".join(short_model(m) for m in s["models"][:2]) or "-",
            state,
        ])
    headers = ["SESSION", "PROJECT", "TITLE", "LAST ACTIVE", "MSGS", "OUT-TOK", "MODEL", "STATE"]
    widths = [max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for r in rows:
        print(fmt.format(*r))
    running = sum(1 for s in sessions if s["running"])
    print(f"\n{len(sessions)} sessions, {running} running")


# ---------------------------------------------------------------- interactive mode

REV, GREEN, DIM, RESET = "\x1b[7m", "\x1b[32m", "\x1b[2m", "\x1b[0m"


def get_key(fd):
    ch = os.read(fd, 1)
    if ch != b"\x1b":
        try:
            return ch.decode()
        except UnicodeDecodeError:
            return ""
    if not select.select([fd], [], [], 0.05)[0]:
        return "ESC"
    seq = os.read(fd, 2)
    if seq in (b"[A", b"OA"):
        return "UP"
    if seq in (b"[B", b"OB"):
        return "DOWN"
    # swallow the rest of longer sequences (PgUp/PgDn/Home/End etc.)
    while select.select([fd], [], [], 0.01)[0]:
        os.read(fd, 1)
    return ""


def draw(sessions, sel, top, status, prompt=None):
    cols, rows = shutil.get_terminal_size()
    visible = max(1, rows - 4)
    out = ["\x1b[H\x1b[2J"]
    running = sum(1 for s in sessions if s["running"])
    out.append(f"{REV} claude-sessions {RESET}  {len(sessions)} sessions, "
               f"{running} running\r\n")
    hdr = f"  {'SESSION':8}  {'LAST':>9}  {'STATE':7}  {'PROJECT':32.32}  TITLE"
    out.append(DIM + hdr[:cols] + RESET + "\r\n")
    for i in range(top, min(top + visible, len(sessions))):
        s = sessions[i]
        state = "RUNNING" if s["running"] else ""
        line = (f"  {s['session_id'][:8]:8}  {humanize_time(s['last_activity']):>9}  "
                f"{state:7}  {shorten_project(s['project']):32.32}  {display_title(s)}")
        line = line[:cols - 1]
        if i == sel:
            out.append(REV + line + RESET + "\r\n")
        elif s["running"]:
            out.append(GREEN + line + RESET + "\r\n")
        else:
            out.append(line + "\r\n")
    if not sessions:
        out.append("  (no sessions)\r\n")
    out.append(f"\x1b[{rows};1H")  # jump to last line
    if prompt:
        out.append(REV + prompt[:cols - 1] + RESET)
    else:
        footer = status or ("Enter resume · n new · d delete · j/k move · q quit")
        out.append(DIM + footer[:cols - 1] + RESET)
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def interactive():
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Interactive mode needs a TTY; use 'list' instead.", file=sys.stderr)
        return 1
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    def raw_on():
        tty.setcbreak(fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        sys.stdout.flush()

    def raw_off():
        sys.stdout.write("\x1b[?1049l\x1b[?25h")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    sessions = collect_sessions()
    sel, top, status = 0, 0, ""
    raw_on()
    try:
        while True:
            visible = max(1, shutil.get_terminal_size().lines - 4)
            sel = max(0, min(sel, len(sessions) - 1))
            top = max(0, min(top, sel))
            if sel >= top + visible:
                top = sel - visible + 1
            draw(sessions, sel, top, status)
            status = ""
            key = get_key(fd)

            if key in ("q", "ESC"):
                return 0
            if key in ("UP", "k"):
                sel -= 1
            elif key in ("DOWN", "j"):
                sel += 1
            elif key == "g":
                sel = 0
            elif key == "G":
                sel = len(sessions) - 1
            elif key in ("\r", "\n", "r") and sessions:
                s = sessions[sel]
                raw_off()
                rc = resume_session(s)  # only returns on failure
                input("Press Enter to continue...")
                raw_on()
                sessions = collect_sessions()
            elif key == "d" and sessions:
                s = sessions[sel]
                draw(sessions, sel, top, "",
                     prompt=f" Delete {s['session_id'][:8]} ({display_title(s)[:40]})? y/N ")
                if get_key(fd) == "y":
                    ok, status = delete_session(s)
                    if ok:
                        sessions = collect_sessions()
                else:
                    status = "Cancelled."
            elif key == "n":
                default = (sessions[sel]["project"] if sessions else None) or os.getcwd()
                raw_off()
                try:
                    path = input(f"Start new session in [{default}]: ").strip() or default
                except (EOFError, KeyboardInterrupt):
                    raw_on()
                    sessions = collect_sessions()
                    continue
                path = os.path.expanduser(path)
                if not Path(path).is_dir():
                    input(f"Not a directory: {path} — press Enter...")
                    raw_on()
                    continue
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
                exec_claude([], cwd=path)  # only returns on failure
                input("Press Enter to continue...")
                raw_on()
    except KeyboardInterrupt:
        return 0
    finally:
        raw_off()


# ---------------------------------------------------------------- main

def main(argv=None):
    p = argparse.ArgumentParser(prog="claude-sessions",
                                description=__doc__.splitlines()[0])
    p.add_argument("command", nargs="?", default=None,
                   choices=["list", "show", "interactive", "resume", "delete", "new"],
                   help="default: interactive on a TTY, else list")
    p.add_argument("arg", nargs="?",
                   help="session id prefix (show/resume/delete) or directory (new)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    p.add_argument("--running", action="store_true", help="only currently running sessions")
    p.add_argument("--project", metavar="SUBSTR", help="filter by project path substring")
    p.add_argument("--limit", type=int, metavar="N", help="max sessions to show")
    p.add_argument("--yes", action="store_true", help="skip confirmation (delete)")
    args = p.parse_args(argv)

    cmd = args.command
    if cmd is None:
        filters = args.json or args.running or args.project or args.limit
        cmd = "interactive" if (not filters and sys.stdout.isatty()
                                and sys.stdin.isatty()) else "list"

    if cmd == "interactive":
        return interactive()

    sessions = collect_sessions()

    if cmd in ("show", "resume", "delete"):
        if not args.arg:
            p.error(f"{cmd} requires a session id (full or prefix)")
        s = find_session(sessions, args.arg)
        if s is None:
            return 1
        if cmd == "show":
            json.dump(s, sys.stdout, indent=2)
            print()
            return 0
        if cmd == "resume":
            return resume_session(s)
        if cmd == "delete":
            if not args.yes:
                reply = input(f"Delete {s['session_id']} "
                              f"({display_title(s)[:60]})? [y/N] ").strip().lower()
                if reply != "y":
                    print("Cancelled.")
                    return 0
            ok, msg = delete_session(s)
            print(msg)
            return 0 if ok else 1

    if cmd == "new":
        path = os.path.expanduser(args.arg or os.getcwd())
        if not Path(path).is_dir():
            print(f"Not a directory: {path}", file=sys.stderr)
            return 1
        return exec_claude([], cwd=path)

    # list
    if args.running:
        sessions = [s for s in sessions if s["running"]]
    if args.project:
        needle = args.project.lower()
        sessions = [s for s in sessions if needle in (s["project"] or "").lower()]
    if args.limit:
        sessions = sessions[: args.limit]

    if args.json:
        json.dump(sessions, sys.stdout, indent=2)
        print()
    else:
        print_table(sessions)
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit(main())
