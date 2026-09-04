# claude-sessions

CLI to list, search, resume, watch, and manage Claude Code sessions on this
machine — past transcripts and currently running processes — with per-session
token usage and cost estimates.

Stdlib-only Python 3.9+, no dependencies. macOS/Linux.

## Data sources

- `~/.claude/projects/<encoded-project-path>/<session-id>.jsonl` — one transcript
  per session. Records carry `cwd`, `timestamp`, `gitBranch`, `version`; assistant
  records carry `message.model` and `message.usage` token counts; `custom-title`
  records carry the session name shown in the UI.
- `~/.claude/sessions/<pid>.json` — metadata for live CLI processes (sessionId,
  pid, name, busy/idle status). A session is reported RUNNING only if its pid is
  still alive.
- Honors `CLAUDE_CONFIG_DIR` if set.

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

`cs watch` (or the brew service) sends a rich macOS notification when a
running session finishes its turn — Claude's app icon, the session name,
and how long the turn took (via `terminal-notifier`, installed automatically
as a brew dependency; falls back to plain osascript without it).

## Notes

- Token figures are summed from transcript usage records: `input`/`output` plus
  `cache_read`/`cache_creation` (visible in `--json` and `show`). The table shows
  output tokens as the best single proxy for work done.
- Sidechain (subagent) messages are excluded from message/model counts.
- A live session with no transcript yet (nothing written) still appears, marked
  RUNNING with no history.
