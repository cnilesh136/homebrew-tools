# claude-sessions

CLI to list, resume, delete, and start Claude Code sessions on this machine —
past transcripts and currently running processes — with per-session usage stats.

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

## Usage

```bash
cs                          # interactive picker (when on a TTY)
cs list                     # table, running first then newest
cs list --json              # full JSON for scripting
cs list --running           # only live sessions
cs list --project omnivox   # filter by project path substring
cs show 167d0a93            # full detail for one session (id prefix ok)
cs resume 167d0a93          # cd to the project and exec `claude --resume`
cs delete 167d0a93          # delete transcript (asks; --yes to skip)
cs new ~/some/project       # start a new session there (default: cwd)
```

Piped/non-TTY invocations default to `list`, so scripts keep working.

## Interactive mode

Full-screen picker (alternate screen, restores your terminal on exit):

| Key            | Action                                          |
|----------------|-------------------------------------------------|
| ↑/↓ or j/k     | move selection (g/G = top/bottom)               |
| Enter or r     | resume selected session in its project dir      |
| n              | start a new session (prompts for directory)     |
| d              | delete selected session's transcript (confirms) |
| q or Esc       | quit                                            |

Running sessions are shown in green and sorted to the top; deleting a running
session is refused until you quit it.

Table columns: session id (short), project, title (custom title or first prompt),
last activity, message count, output tokens, model(s), running state.

## Notes

- Token figures are summed from transcript usage records: `input`/`output` plus
  `cache_read`/`cache_creation` (visible in `--json` and `show`). The table shows
  output tokens as the best single proxy for work done.
- Sidechain (subagent) messages are excluded from message/model counts.
- A live session with no transcript yet (nothing written) still appears, marked
  RUNNING with no history.
