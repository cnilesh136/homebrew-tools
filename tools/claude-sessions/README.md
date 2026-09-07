# claude-sessions

> **Mission control for your Claude Code sessions.**

You're running three Claude sessions in three terminals. Which one is still
working? Which one finished while you were in a meeting? Which one is sitting
at a permission prompt, blocked on you? `cs` answers that at a glance — and
pings you the moment the answer changes.

- **Live picker** — every session on your machine in one screen: busy sessions
  animate, idle ones show a green ●, the preview pane streams the last messages
  of whichever you select. Hit ⏎ to jump back in, right in its project dir.
- **Desktop notifications** — a background service tells you when a session
  **finishes its turn** (with duration) or is **waiting for your input**, so
  you stop babysitting terminals.
- **Total recall** — `cs search "that bug"` full-text searches every
  conversation you've ever had, across all projects, and resumes the match.
- Plus the housekeeping: cost & token stats, Markdown export, prune, JSON
  output for scripting.

Unlike Claude Code's built-in `/resume`, `cs` sees **all projects** — and
unlike anything else, it knows what's running *right now*.

Stdlib-only Python 3.9+, no dependencies. macOS/Linux (notifications macOS).

## Install

```bash
brew install cnilesh136/tools/claude-sessions
```

Installs two global commands: `claude-sessions` and the short alias `cs`.

To get a desktop notification whenever a running Claude session finishes its
turn, enable the background watcher once (persists across reboots):

```bash
brew services start claude-sessions
```

## Usage

```bash
cs                          # interactive picker (when on a TTY)
cs list                     # table, running first then newest, with cost
cs list --json              # full JSON for scripting
cs list --running           # only live sessions
cs list --project omnivox   # filter by project path substring
cs search "route53 salt"    # full-text search across all transcripts
cs show 167d0a93            # full detail for one session (id prefix ok)
cs resume 167d0a93          # cd to the project and exec `claude --resume`
cs delete 167d0a93          # delete transcript (asks; --yes to skip)
cs new ~/some/project       # start a new session there (default: cwd)
cs watch                    # foreground watcher (the brew service runs this)
cs prune                    # delete empty sessions; --older-than 30 for stale
cs export 167d0a93          # render the conversation to Markdown
cs stats                    # tokens + est. cost by project and model
```

Piped/non-TTY invocations default to `list`, so scripts keep working.

## Interactive mode

Full-screen picker (alternate screen, restores your terminal on exit):

| Key            | Action                                          |
|----------------|-------------------------------------------------|
| ↑/↓ or j/k     | move selection (g/G = top/bottom)               |
| Enter or r     | resume selected session in its project dir      |
| /              | full-text search filter across transcripts      |
| n              | start a new session (prompts for directory)     |
| e              | export selected session to Markdown (in cwd)    |
| d              | delete selected session's transcript (confirms) |
| q              | quit (Esc clears an active search filter first) |

The picker is live: busy sessions show an animated spinner (yellow), idle
running sessions a green ●, and status refreshes every second — the preview
pane updates as a running session produces output. The header shows a 14-day
activity sparkline and your lifetime spend; costs are heat-colored (red ≥ $50).

A preview pane at the bottom shows the last few messages of the selected
session. Deleting a running session is refused until you quit it.

## Notifications

`cs watch` (or the background service) sends a macOS notification — session
name, project as subtitle — when a running session **finishes its turn**
(with the turn duration) or **changes state to waiting for your input**.

Delivery uses osascript: it is the one channel modern macOS reliably allows
for CLI tools (third-party notifiers like terminal-notifier are refused
notification permission on macOS 15+, and sender-icon spoofing is silently
dropped), so the notification carries the Script Editor icon by design.

Control the always-on service:

```bash
cs service start     # enable (survives reboots)
cs service stop      # disable anytime
cs service status    # check
```

## How it compares

| | `cs` | Claude Code `/resume` | cc-sessions | ccusage |
|---|---|---|---|---|
| Sessions across all projects | ✅ | ❌ current project only | ✅ | — |
| Live running/busy status | ✅ | ❌ | ❌ | ❌ |
| Finished / needs-input notifications | ✅ | hooks (DIY) | ❌ | ❌ |
| Full-text search of transcripts | ✅ | ❌ | ❌ | ❌ |
| Cost / usage reports | basic | ❌ | ❌ | ✅ deep |

If you want in-depth spend analytics (daily/monthly rollups, live billing-block
dashboards), use [ccusage](https://github.com/ryoppippi/ccusage) alongside —
`cs stats` covers the per-session and per-project basics.

## Data sources

- `~/.claude/projects/<encoded-project-path>/<session-id>.jsonl` — one transcript
  per session. Records carry `cwd`, `timestamp`, `gitBranch`, `version`; assistant
  records carry `message.model` and `message.usage` token counts; `custom-title`
  records carry the session name shown in the UI.
- `~/.claude/sessions/<pid>.json` — metadata for live CLI processes (sessionId,
  pid, name, busy/idle status). A session is reported RUNNING only if its pid is
  still alive.
- Honors `CLAUDE_CONFIG_DIR` if set.

## Notes

- Token figures are summed from transcript usage records: `input`/`output` plus
  `cache_read`/`cache_creation` (visible in `--json` and `show`). The table shows
  output tokens as the best single proxy for work done.
- Sidechain (subagent) messages are excluded from message/model counts.
- A live session with no transcript yet (nothing written) still appears, marked
  RUNNING with no history.
