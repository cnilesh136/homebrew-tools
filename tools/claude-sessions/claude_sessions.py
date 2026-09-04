#!/usr/bin/env python3
"""claude-sessions — list, search, resume, watch, and manage Claude Code sessions.

Scans ~/.claude/projects/*/<session-id>.jsonl transcripts, enriches them with
live-process info from ~/.claude/sessions/<pid>.json.

Usage (installed as `claude-sessions`, short alias `cs`):
  cs                          # interactive picker (when on a TTY)
  cs list                     # table of all sessions, newest first
  cs list --json              # machine-readable output
  cs list --running           # only sessions with a live process
  cs list --project X         # filter by project path substring
  cs search <query>           # full-text search across all transcripts
  cs show <id-prefix>         # full detail for one session (JSON)
  cs resume <id-prefix>       # resume a session (execs `claude --resume`)
  cs delete <id-prefix>       # delete a session transcript (--yes to skip confirm)
  cs new [directory]          # start a new session (execs `claude`)
  cs watch                    # notify when a running session finishes its turn
  cs prune                    # delete empty sessions (--older-than N for stale ones)
  cs export <id-prefix>       # render a transcript to Markdown
  cs stats                    # usage and cost summary by project and model

Interactive keys:
  up/down or j/k  move        Enter/r  resume selected
  /               search      n        new session
  d               delete      g/G      top/bottom
  q               quit        Esc      clear search, then quit

Stdlib only; Python 3.9+; macOS/Linux (notifications are macOS).
"""

import argparse
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import termios
import textwrap
import time
import tty
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
LIVE_DIR = CLAUDE_DIR / "sessions"

# $/MTok (input, output). Cache read is billed ~0.1x input, cache write ~1.25x.
PRICES = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-3": (0.25, 1.25),
}
CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25


def model_price(model):
    best = None
    for prefix, price in PRICES.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, price)
    return best[1] if best else None


def session_cost(model_usage):
    total = 0.0
    for model, u in (model_usage or {}).items():
        price = model_price(model)
        if not price:
            continue
        p_in, p_out = price
        total += (u["input"] * p_in
                  + u["output"] * p_out
                  + u["cache_read"] * p_in * CACHE_READ_MULT
                  + u["cache_creation"] * p_in * CACHE_WRITE_MULT) / 1e6
    return total


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
        "model_usage": {},
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
    }
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
                usage = msg.get("usage") or {}
                inc = {
                    "input": usage.get("input_tokens", 0) or 0,
                    "output": usage.get("output_tokens", 0) or 0,
                    "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
                    "cache_creation": usage.get("cache_creation_input_tokens", 0) or 0,
                }
                for k, v in inc.items():
                    s["tokens"][k] += v
                model = msg.get("model")
                if model and model != "<synthetic>":
                    mu = s["model_usage"].setdefault(
                        model, {"input": 0, "output": 0,
                                "cache_read": 0, "cache_creation": 0})
                    for k, v in inc.items():
                        mu[k] += v

    s["models"] = list(s["model_usage"])
    s["cost_usd"] = round(session_cost(s["model_usage"]), 4)
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
            "model_usage": {},
            "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
            "cost_usd": 0.0,
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


def iter_message_texts(path):
    """Yield (role, text) for every main-thread user/assistant message."""
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("type") not in ("user", "assistant"):
                continue
            if r.get("isMeta") or r.get("isSidechain"):
                continue
            text = first_text((r.get("message") or {}).get("content"))
            if text.strip():
                yield r["type"], text


# ---------------------------------------------------------------- search

def search_sessions(sessions, query):
    """Full-text search across transcripts. Returns matching sessions with
    _hits and _snippet attached, sorted by hit count."""
    q = query.lower()
    results = []
    for s in sessions:
        hits, snippet = 0, None
        path = s.get("transcript")
        meta_text = f"{s.get('title') or ''} {s.get('project') or ''}".lower()
        if q in meta_text:
            hits += 1
            snippet = s.get("title") or s.get("project")
        if path:
            for _, text in iter_message_texts(path):
                low = text.lower()
                idx = low.find(q)
                if idx < 0:
                    continue
                hits += 1
                if snippet is None or hits == 1:
                    start = max(0, idx - 40)
                    snippet = " ".join(text[start:idx + len(q) + 60].split())
        if hits:
            s = dict(s)
            s["_hits"] = hits
            s["_snippet"] = snippet or ""
            results.append(s)
    results.sort(key=lambda s: s["_hits"], reverse=True)
    return results


def print_search(results, query):
    if not results:
        print(f"No sessions matching '{query}'.")
        return
    rows = [[s["session_id"][:8], shorten_project(s["project"]),
             str(s["_hits"]), s["_snippet"][:70]] for s in results]
    headers = ["SESSION", "PROJECT", "HITS", "MATCH"]
    widths = [max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for r in rows:
        print(fmt.format(*r))
    print(f"\n{len(results)} sessions match. Resume with: cs resume <session>")


# ---------------------------------------------------------------- watch

def humanize_duration(secs):
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"


def notify_mac(title, message, subtitle="", sound="Glass"):
    # osascript is the one channel modern macOS reliably allows for CLI tools:
    # third-party notifiers, sender spoofing, and even AppleScript applets are
    # refused notification permission on macOS 15+ (verified empirically).
    if sys.platform != "darwin":
        return
    def esc(t):
        return (t or "").replace("\\", "\\\\").replace('"', '\\"')
    script = (f'display notification "{esc(message)}" with title "{esc(title)}" '
              f'subtitle "{esc(subtitle)}" sound name "{esc(sound)}"')
    subprocess.run(["osascript", "-e", script], capture_output=True)


def watch(interval=3.0, quiet=False):
    def log(msg):
        print(f"{datetime.now().strftime('%H:%M:%S')}  {msg}", flush=True)

    log(f"watching {LIVE_DIR} every {interval:g}s (Ctrl-C to stop)")
    prev = load_live_sessions()
    busy_since = {sid: time.time() for sid, i in prev.items()
                  if i.get("status") == "busy"}
    while True:
        time.sleep(interval)
        live = load_live_sessions()
        for sid, info in live.items():
            old = prev.get(sid)
            label = info.get("name") or shorten_project(info.get("cwd")) or sid[:8]
            new_st = info.get("status")
            old_st = old.get("status") if old else None
            if new_st == "busy":
                busy_since.setdefault(sid, time.time())
            if old is None:
                log(f"session started: {label} ({new_st})")
                continue
            if old_st == new_st:
                continue
            if old_st == "busy" and new_st == "idle":
                took = time.time() - busy_since.pop(sid, time.time())
                log(f"finished: {label} (turn took {humanize_duration(took)})")
                if not quiet:
                    emoji = "⚡" if took < 10 else ("✅" if took < 300 else "🏁")
                    others = sum(1 for o_sid, o in live.items()
                                 if o_sid != sid and o.get("status") == "busy")
                    tail = (f" · {others} still busy" if others
                            else " — all quiet, ready for you")
                    notify_mac(f"{emoji} {label} finished",
                               f"Turn took {humanize_duration(took)}{tail}",
                               subtitle=f"in {shorten_project(info.get('cwd'))}")
            elif new_st not in ("busy", "idle", "shell", None):
                # any other state (permission prompt, question, plan approval…);
                # "shell" is the user themselves at the keyboard — don't ping
                log(f"waiting for input: {label} ({new_st})")
                if not quiet:
                    notify_mac(f"⌨️ {label} needs you",
                               "Claude is waiting on a prompt — "
                               "it can't continue until you answer",
                               subtitle=f"in {shorten_project(info.get('cwd'))}",
                               sound="Ping")
        for sid, old in prev.items():
            if sid not in live:
                busy_since.pop(sid, None)
                label = old.get("name") or shorten_project(old.get("cwd")) or sid[:8]
                log(f"session ended: {label}")
        prev = live


def service_cmd(action):
    """Control the background notification watcher (brew service)."""
    action = action or "status"
    if action not in ("start", "stop", "restart", "status"):
        print(f"Unknown service action '{action}' — "
              f"use start, stop, restart, or status", file=sys.stderr)
        return 2
    brew = shutil.which("brew") or next(
        (p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
         if os.path.exists(p)), None)
    if not brew:
        print("Homebrew not found — the watcher service is managed by "
              "`brew services`.", file=sys.stderr)
        return 1
    verb = "info" if action == "status" else action
    rc = subprocess.run([brew, "services", verb, "claude-sessions"]).returncode
    if rc == 0 and action == "stop":
        print("Notifications off. Re-enable anytime: cs service start")
    if rc == 0 and action in ("start", "restart"):
        print("You'll get a desktop ping when a session finishes or needs input.")
    return rc


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


# ---------------------------------------------------------------- prune

def prune(sessions, older_than=None, assume_yes=False):
    now = datetime.now(timezone.utc)
    candidates = []
    for s in sessions:
        if s["running"] or not s.get("transcript"):
            continue
        if s["user_messages"] + s["assistant_messages"] == 0:
            candidates.append((s, "empty"))
        elif older_than is not None and s["last_activity"]:
            try:
                dt = datetime.fromisoformat(s["last_activity"].replace("Z", "+00:00"))
            except ValueError:
                continue
            age = (now - dt).days
            if age >= older_than:
                candidates.append((s, f"{age}d old"))
    if not candidates:
        print("Nothing to prune.")
        return 0
    total = 0
    for s, reason in candidates:
        size = Path(s["transcript"]).stat().st_size if Path(s["transcript"]).exists() else 0
        total += size
        print(f"  {s['session_id'][:8]}  {shorten_project(s['project']):40.40}  "
              f"{reason:10}  {display_title(s)[:40]}")
    print(f"\n{len(candidates)} sessions, {total / 1024:.0f} KB")
    if not assume_yes:
        reply = input("Delete these? [y/N] ").strip().lower()
        if reply != "y":
            print("Cancelled.")
            return 0
    for s, _ in candidates:
        ok, msg = delete_session(s)
        print(msg)
    return 0


# ---------------------------------------------------------------- export

def export_markdown(s):
    lines = [f"# {display_title(s)}", ""]
    lines.append(f"- **Session:** `{s['session_id']}`")
    lines.append(f"- **Project:** {s.get('project') or '-'}")
    lines.append(f"- **Period:** {s.get('started') or '-'} → {s.get('last_activity') or '-'}")
    lines.append(f"- **Model(s):** {', '.join(s['models']) or '-'}")
    t = s["tokens"]
    lines.append(f"- **Tokens:** {humanize_tokens(t['output'])} out, "
                 f"{humanize_tokens(t['input'] + t['cache_read'] + t['cache_creation'])} in "
                 f"(~${s.get('cost_usd', 0):.2f})")
    lines += ["", "---", ""]
    path = s.get("transcript")
    if not path:
        lines.append("_No transcript available._")
        return "\n".join(lines) + "\n"

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = r.get("type")
            if rtype not in ("user", "assistant"):
                continue
            if r.get("isMeta") or r.get("isSidechain"):
                continue
            content = (r.get("message") or {}).get("content")
            if rtype == "user":
                text = first_text(content).strip()
                if not text:
                    continue  # pure tool_result turns
                lines += ["## 👤 User", "", text, ""]
            else:
                text_parts, tools = [], {}
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text" and block.get("text", "").strip():
                            text_parts.append(block["text"].strip())
                        elif block.get("type") == "tool_use":
                            name = block.get("name", "?")
                            tools[name] = tools.get(name, 0) + 1
                elif isinstance(content, str) and content.strip():
                    text_parts.append(content.strip())
                if not text_parts and not tools:
                    continue
                lines.append("## 🤖 Claude")
                lines.append("")
                lines += text_parts
                if tools:
                    pretty = ", ".join(n if c == 1 else f"{n} ×{c}"
                                       for n, c in tools.items())
                    lines.append(f"\n> 🔧 {pretty}")
                lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- stats

def print_stats(sessions):
    by_project, by_model = {}, {}
    for s in sessions:
        proj = shorten_project(s["project"])
        p = by_project.setdefault(proj, {"sessions": 0, "msgs": 0, "out": 0, "cost": 0.0})
        p["sessions"] += 1
        p["msgs"] += s["user_messages"] + s["assistant_messages"]
        p["out"] += s["tokens"]["output"]
        p["cost"] += s.get("cost_usd", 0)
        for model, u in (s.get("model_usage") or {}).items():
            m = by_model.setdefault(model, {"sessions": 0, "out": 0, "cost": 0.0})
            m["sessions"] += 1
            m["out"] += u["output"]
            m["cost"] += session_cost({model: u})

    def table(title, headers, rows):
        print(title)
        widths = [max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))]
        fmt = "  ".join(f"{{:<{w}}}" for w in widths)
        print(fmt.format(*headers))
        for r in rows:
            print(fmt.format(*r))
        print()

    rows = [[p, str(v["sessions"]), str(v["msgs"]), humanize_tokens(v["out"]),
             f"${v['cost']:.2f}"]
            for p, v in sorted(by_project.items(), key=lambda kv: -kv[1]["cost"])]
    table("By project:", ["PROJECT", "SESSIONS", "MSGS", "OUT-TOK", "COST"], rows)

    rows = [[m, str(v["sessions"]), humanize_tokens(v["out"]), f"${v['cost']:.2f}"]
            for m, v in sorted(by_model.items(), key=lambda kv: -kv[1]["cost"])]
    if rows:
        table("By model:", ["MODEL", "SESSIONS", "OUT-TOK", "COST"], rows)

    total_cost = sum(s.get("cost_usd", 0) for s in sessions)
    total_out = sum(s["tokens"]["output"] for s in sessions)
    print(f"Total: {len(sessions)} sessions, {humanize_tokens(total_out)} output tokens, "
          f"~${total_cost:.2f} (cache write ~1.25x / read ~0.1x input price, approximate)")


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


def humanize_cost(c):
    if c == 0:
        return "-"
    if c < 0.01:
        return "<$0.01"
    return f"${c:.2f}"


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
            display_title(s)[:44],
            humanize_time(s["last_activity"]),
            str(s["user_messages"] + s["assistant_messages"]),
            humanize_tokens(s["tokens"]["output"]),
            humanize_cost(s.get("cost_usd", 0)),
            ", ".join(short_model(m) for m in s["models"][:2]) or "-",
            state,
        ])
    headers = ["SESSION", "PROJECT", "TITLE", "LAST ACTIVE", "MSGS",
               "OUT-TOK", "COST", "MODEL", "STATE"]
    widths = [max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    tty = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    print((DIM if tty else "") + fmt.format(*headers) + (RESET if tty else ""))
    print((DIM if tty else "") + fmt.format(*("-" * w for w in widths))
          + (RESET if tty else ""))
    for s, r in zip(sessions, rows):
        line = fmt.format(*r)
        if tty and s["running"]:
            line = GREEN + line + RESET
        print(line)
    running = sum(1 for s in sessions if s["running"])
    total_cost = sum(s.get("cost_usd", 0) for s in sessions)
    print(f"\n{len(sessions)} sessions, {running} running, ~${total_cost:.2f} total"
          + ((DIM + "   (try `cs` for the interactive picker)" + RESET)
             if tty else ""))


# ---------------------------------------------------------------- colors & help

RESET, BOLD, DIM, REV = "\x1b[0m", "\x1b[1m", "\x1b[2m", "\x1b[7m"
RED, GREEN, YELLOW = "\x1b[31m", "\x1b[32m", "\x1b[33m"
BLUE, MAGENTA, CYAN = "\x1b[34m", "\x1b[35m", "\x1b[36m"

COMMANDS = {
    "list":        "table of all sessions — cost, tokens, running state",
    "search":      "full-text search every conversation you ever had",
    "show":        "full JSON detail for one session",
    "resume":      "jump back into a session, right in its project dir",
    "new":         "start a fresh Claude session in any directory",
    "delete":      "remove a session transcript (asks first)",
    "watch":       "get notified when a session finishes or needs input",
    "service":     "notifications always-on: cs service start|stop|status",
    "prune":       "sweep away empty or stale sessions",
    "export":      "turn a conversation into shareable Markdown",
    "stats":       "tokens + cost breakdown by project and model",
    "interactive": "the full-screen picker (same as bare `cs`)",
    "help":        "this screen",
}


def print_help():
    t = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def c(text, *codes):
        return ("".join(codes) + text + RESET) if t else text

    print()
    print("  " + c(" ◆ claude-sessions ", REV, BOLD)
          + "  " + c("mission control for your Claude Code sessions", DIM))
    print()
    print("  " + c("USAGE", BOLD, YELLOW))
    print(f"    {c('cs', BOLD, CYAN)}                        "
          + c("open the interactive picker", DIM))
    print(f"    {c('cs', BOLD, CYAN)} {c('<command> [arg] [flags]', CYAN)}")
    print()
    print("  " + c("COMMANDS", BOLD, YELLOW))
    for cmd, desc in COMMANDS.items():
        print(f"    {c(f'{cmd:<12}', BOLD, CYAN)}{desc}")
    print()
    print("  " + c("POPULAR FLAGS", BOLD, YELLOW))
    for flag, desc in [
        ("--json", "machine-readable output (list/search/show)"),
        ("--running", "only live sessions"),
        ("--project X", "filter by project path substring"),
        ("--older-than N", "prune sessions idle for N days"),
        ("--yes", "skip confirmations (delete/prune)"),
        ("-o FILE", "export destination ('-' for stdout)"),
    ]:
        print(f"    {c(f'{flag:<16}', CYAN)}{c(desc, DIM)}")
    print()
    print("  " + c("TRY THIS", BOLD, YELLOW))
    for ex, desc in [
        ("cs", "browse & resume — green dot = running"),
        ('cs search "that bug"', "find any conversation, ever"),
        ("cs stats", "see what your sessions really cost"),
        ("cs service start",
         "desktop ping when Claude finishes or needs you"),
    ]:
        print(f"    {c(f'{ex:<38}', GREEN)}{c(desc, DIM)}")
    print()
    print("  " + c("PICKER KEYS", BOLD, YELLOW) + "   "
          + "   ".join(c(k, BOLD, CYAN) + c(" " + v, DIM) for k, v in [
              ("⏎", "resume"), ("/", "search"), ("n", "new"),
              ("d", "delete"), ("j/k", "move"), ("q", "quit")]))
    print()


# ---------------------------------------------------------------- interactive mode

_preview_cache = {}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def cost_color(c):
    if c >= 50:
        return RED
    if c >= 1:
        return YELLOW
    return DIM


def activity_spark(days=14):
    """Prompts-per-day sparkline for the last `days` days, from history.jsonl."""
    hist = CLAUDE_DIR / "history.jsonl"
    counts = [0] * days
    now = time.time()
    try:
        with open(hist, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    ts = json.loads(line).get("timestamp", 0) / 1000
                except (json.JSONDecodeError, TypeError):
                    continue
                age = int((now - ts) // 86400)
                if 0 <= age < days:
                    counts[days - 1 - age] += 1
    except OSError:
        return ""
    if not any(counts):
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    mx = max(counts)
    return "".join(blocks[min(7, round(c / mx * 7))] for c in counts)


def get_key(fd, timeout=None):
    if timeout is not None and not select.select([fd], [], [], timeout)[0]:
        return None  # tick — no key pressed
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


def tail_messages(path, want=6, chunk=262_144):
    """Last `want` (role, text) messages, reading only the file tail."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - chunk))
            data = fh.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if size > chunk:
        lines = lines[1:]  # drop partial first line
    out = []
    for raw in reversed(lines):
        if len(out) >= want:
            break
        try:
            r = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if r.get("type") not in ("user", "assistant"):
            continue
        if r.get("isMeta") or r.get("isSidechain"):
            continue
        text = first_text((r.get("message") or {}).get("content")).strip()
        if text:
            out.append((r["type"], " ".join(text.split())))
    return list(reversed(out))


def preview_lines(s, width, height):
    """Wrapped preview of the selected session's last messages as
    (role, is_first_line, text) tuples."""
    path = s.get("transcript")
    if not path:
        return [("", True, "(no transcript yet — brand-new session)")]
    key = (s["session_id"], os.path.getsize(path) if os.path.exists(path) else 0)
    if key not in _preview_cache:
        _preview_cache.clear()  # keep at most one entry
        _preview_cache[key] = tail_messages(path)
    msgs = _preview_cache[key]
    if not msgs:
        return [("", True, "(no messages yet)")]
    lines = []
    for role, text in msgs:
        wrapped = textwrap.wrap(text, width=max(20, width),
                                max_lines=3, placeholder=" …")
        for i, w in enumerate(wrapped):
            lines.append((role, i == 0, w))
    return lines[-height:]


def draw(sessions, sel, top, status, query="", prompt=None, tick=0, spark=""):
    cols, rows = shutil.get_terminal_size()
    show_preview = rows >= 18 and sessions
    ph = min(12, rows // 3) if show_preview else 0
    visible = max(1, rows - 4 - (ph + 1 if show_preview else 0))
    out = ["\x1b[H\x1b[2J"]

    running = sum(1 for s in sessions if s["running"])
    total_cost = sum(s.get("cost_usd", 0) or 0 for s in sessions)
    head = (f"{REV}{BOLD} ◆ claude-sessions {RESET}  "
            f"{BOLD}{len(sessions)}{RESET}{DIM} sessions{RESET}   "
            f"{GREEN}●{RESET} {BOLD}{running}{RESET}{DIM} running{RESET}   "
            f"{YELLOW}~${total_cost:,.0f}{RESET}{DIM} lifetime{RESET}")
    if spark:
        head += f"   {CYAN}{spark}{RESET}{DIM} 14d{RESET}"
    if query:
        head += f"   {MAGENTA}⌕ {query}{RESET}{DIM}  Esc clears{RESET}"
    out.append(head + "\r\n")

    # fixed columns take 61 chars; the title gets the rest
    title_w = max(10, cols - 62)
    hdr = (f"   {'SESSION':<8}  {'LAST':>9}  {'COST':>7}  "
           f"{'PROJECT':<26.26}  TITLE")
    out.append(DIM + hdr[:cols - 1] + RESET + "\r\n")

    for i in range(top, min(top + visible, len(sessions))):
        s = sessions[i]
        busy = s["running"] and (s.get("live") or {}).get("status") == "busy"
        mark = SPINNER[tick % len(SPINNER)] if busy else ("●" if s["running"] else "·")
        sid = f"{s['session_id'][:8]:<8}"
        when = f"{humanize_time(s['last_activity']):>9}"
        cost_val = s.get("cost_usd", 0) or 0
        cost = f"{humanize_cost(cost_val):>7}"
        proj = f"{shorten_project(s['project']):<26.26}"
        title = display_title(s)[:title_w]
        if i == sel:
            line = f" {mark} {sid}  {when}  {cost}  {proj}  {title}"
            out.append(REV + BOLD + line[:cols - 1] + RESET + "\r\n")
        else:
            mcol = (YELLOW if busy else GREEN) if s["running"] else DIM
            tcol = BOLD if s["running"] else ""
            out.append(f" {mcol}{mark}{RESET} {DIM}{sid}{RESET}  {DIM}{when}{RESET}  "
                       f"{cost_color(cost_val)}{cost}{RESET}  {CYAN}{proj}{RESET}  "
                       f"{tcol}{title}{RESET}\r\n")
    if not sessions:
        out.append(f"\r\n   {DIM}Nothing here yet.{RESET}\r\n"
                   f"   Press {BOLD}{CYAN}n{RESET} to start your first "
                   f"Claude session ✨\r\n")

    if show_preview and 0 <= sel < len(sessions):
        out.append(f"\x1b[{rows - ph - 1};1H")
        cap = f" {display_title(sessions[sel])[:cols - 12]} "
        bar = "───" + cap + "─" * max(0, cols - 5 - len(cap))
        out.append(DIM + CYAN + bar[:cols - 1] + RESET + "\r\n")
        for role, first, text in preview_lines(sessions[sel], cols - 12, ph):
            if first and role == "user":
                lab = f"{BOLD}{BLUE}   You{RESET} {DIM}▏{RESET}"
            elif first and role == "assistant":
                lab = f"{BOLD}{MAGENTA}Claude{RESET} {DIM}▏{RESET}"
            else:
                lab = f"       {DIM}▏{RESET}"
            body = (DIM + text + RESET) if role == "user" else text
            out.append(" " + lab + " " + body + "\r\n")

    out.append(f"\x1b[{rows};1H")
    if prompt:
        out.append(RED + REV + BOLD + prompt[:cols - 1] + RESET)
    elif status:
        out.append(" " + status + RESET)
    else:
        keys = "   ".join(f"{BOLD}{CYAN}{k}{RESET}{DIM} {v}{RESET}" for k, v in [
            ("⏎", "resume"), ("/", "search"), ("n", "new"), ("e", "export"),
            ("d", "delete"), ("q", "quit")])
        pos = f"{DIM}{min(sel + 1, len(sessions))}/{len(sessions)}{RESET}" \
            if sessions else ""
        out.append(" " + keys + "   " + pos)
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

    all_sessions = collect_sessions()
    view = all_sessions
    query = ""
    sel, top, status = 0, 0, ""
    tick = 0
    spark = activity_spark()
    flag = Path.home() / ".config" / "claude-sessions" / "welcomed"
    if not flag.exists():
        try:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.touch()
        except OSError:
            pass
        status = (f"👋 {BOLD}Welcome!{RESET} {BOLD}{CYAN}⏎{RESET} resumes a session"
                  f" · {BOLD}{CYAN}/{RESET} searches everything you ever discussed"
                  f" · {CYAN}cs help{RESET}{DIM} shows the full tour{RESET}")
    raw_on()
    try:
        while True:
            cols, term_rows = shutil.get_terminal_size()
            ph = min(12, term_rows // 3) if term_rows >= 18 and view else 0
            visible = max(1, term_rows - 4 - (ph + 1 if ph else 0))
            sel = max(0, min(sel, len(view) - 1))
            top = max(0, min(top, sel))
            if sel >= top + visible:
                top = sel - visible + 1
            draw(view, sel, top, status, query, tick=tick, spark=spark)
            key = get_key(fd, timeout=1.0)

            if key is None:  # tick: animate spinners, refresh live status
                tick += 1
                live = load_live_sessions()
                for s in all_sessions:
                    info = live.get(s["session_id"])
                    s["running"] = bool(info)
                    s["live"] = info
                continue
            status = ""

            if key == "q":
                return 0
            if key == "ESC":
                if query:
                    query, view, sel, top = "", all_sessions, 0, 0
                    continue
                return 0
            if key in ("UP", "k"):
                sel -= 1
            elif key in ("DOWN", "j"):
                sel += 1
            elif key == "g":
                sel = 0
            elif key == "G":
                sel = len(view) - 1
            elif key == "/":
                raw_off()
                try:
                    q = input("Search: ").strip()
                except (EOFError, KeyboardInterrupt):
                    q = ""
                raw_on()
                if q:
                    query = q
                    view = search_sessions(all_sessions, q)
                    status = (f"{MAGENTA}⌕{RESET} {BOLD}{len(view)}{RESET} "
                              f"sessions match '{q}'" if view else
                              f"{MAGENTA}⌕{RESET} no matches for '{q}' — "
                              f"{DIM}Esc to clear{RESET}")
                else:
                    query, view = "", all_sessions
                sel, top = 0, 0
            elif key in ("\r", "\n", "r") and view:
                s = view[sel]
                raw_off()
                resume_session(s)  # only returns on failure
                input("Press Enter to continue...")
                raw_on()
                all_sessions = collect_sessions()
                view = search_sessions(all_sessions, query) if query else all_sessions
            elif key == "e" and view:
                s = view[sel]
                out_path = Path.cwd() / f"claude-session-{s['session_id'][:8]}.md"
                try:
                    out_path.write_text(export_markdown(s))
                    status = f"{GREEN}✓{RESET} Exported → {out_path}"
                except OSError as exc:
                    status = f"{RED}✗{RESET} Export failed: {exc}"
            elif key == "d" and view:
                s = view[sel]
                draw(view, sel, top, "", query, tick=tick, spark=spark,
                     prompt=f" Delete {s['session_id'][:8]} ({display_title(s)[:40]})? y/N ")
                if get_key(fd) == "y":
                    ok, msg = delete_session(s)
                    status = (f"{GREEN}✓{RESET} {msg}" if ok
                              else f"{RED}✗{RESET} {msg}")
                    if ok:
                        all_sessions = collect_sessions()
                        view = search_sessions(all_sessions, query) if query else all_sessions
                else:
                    status = f"{DIM}Cancelled — nothing deleted.{RESET}"
            elif key == "n":
                default = (view[sel]["project"] if view else None) or os.getcwd()
                raw_off()
                try:
                    path = input(f"Start new session in [{default}]: ").strip() or default
                except (EOFError, KeyboardInterrupt):
                    raw_on()
                    continue
                path = os.path.expanduser(path)
                if not Path(path).is_dir():
                    input(f"Not a directory: {path} — press Enter...")
                    raw_on()
                    continue
                exec_claude([], cwd=path)  # only returns on failure
                input("Press Enter to continue...")
                raw_on()
    except KeyboardInterrupt:
        return 0
    finally:
        raw_off()


# ---------------------------------------------------------------- main

def main(argv=None):
    argv = list(sys.argv[1:]) if argv is None else list(argv)

    # friendly help + typo suggestions, before argparse gets a chance to shout
    if any(a in ("-h", "--help") for a in argv) or (argv and argv[0] == "help"):
        print_help()
        return 0
    first = next((a for a in argv if not a.startswith("-")), None)
    valid = [c for c in COMMANDS if c != "help"]
    if first is not None and argv.index(first) == 0 and first not in valid:
        import difflib
        guess = difflib.get_close_matches(first, list(COMMANDS), n=1)
        hint = f" — did you mean {BOLD}cs {guess[0]}{RESET}?" if guess else ""
        print(f"{RED}✗{RESET} Unknown command '{first}'{hint}   "
              f"{DIM}(cs help shows everything){RESET}", file=sys.stderr)
        return 2

    p = argparse.ArgumentParser(prog="claude-sessions", add_help=False,
                                description=__doc__.splitlines()[0])
    p.add_argument("command", nargs="?", default=None, choices=valid,
                   help="default: interactive on a TTY, else list")
    p.add_argument("arg", nargs="?",
                   help="session id prefix (show/resume/delete/export), "
                        "query (search), or directory (new)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    p.add_argument("--running", action="store_true", help="only currently running sessions")
    p.add_argument("--project", metavar="SUBSTR", help="filter by project path substring")
    p.add_argument("--limit", type=int, metavar="N", help="max sessions to show")
    p.add_argument("--yes", action="store_true", help="skip confirmation (delete/prune)")
    p.add_argument("--older-than", type=int, metavar="DAYS",
                   help="prune: also delete sessions idle for this many days")
    p.add_argument("--interval", type=float, default=3.0, metavar="SEC",
                   help="watch: poll interval (default 3)")
    p.add_argument("--quiet", action="store_true",
                   help="watch: log only, no desktop notifications")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="export: output path ('-' for stdout)")
    args = p.parse_args(argv)

    cmd = args.command
    if cmd is None:
        filters = args.json or args.running or args.project or args.limit
        cmd = "interactive" if (not filters and sys.stdout.isatty()
                                and sys.stdin.isatty()) else "list"

    if cmd == "interactive":
        return interactive()
    if cmd == "service":
        return service_cmd(args.arg)
    if cmd == "watch":
        try:
            return watch(interval=max(1.0, args.interval), quiet=args.quiet)
        except KeyboardInterrupt:
            return 0

    sessions = collect_sessions()

    if cmd == "search":
        if not args.arg:
            p.error("search requires a query")
        results = search_sessions(sessions, args.arg)
        if args.json:
            json.dump(results, sys.stdout, indent=2)
            print()
        else:
            print_search(results, args.arg)
        return 0

    if cmd == "stats":
        print_stats(sessions)
        return 0

    if cmd == "prune":
        return prune(sessions, older_than=args.older_than, assume_yes=args.yes)

    if cmd in ("show", "resume", "delete", "export"):
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
        if cmd == "export":
            md = export_markdown(s)
            if args.output == "-":
                sys.stdout.write(md)
            else:
                out = Path(args.output or f"claude-session-{s['session_id'][:8]}.md")
                out.write_text(md)
                print(f"Wrote {out}")
            return 0
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
