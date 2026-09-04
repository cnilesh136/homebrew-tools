# homebrew-tools

Personal Homebrew tap — small macOS CLI tools, installable on any Mac with:

```bash
brew install cnilesh136/tools/<tool>
```

## Tools

| Tool | Commands | Description |
|------|----------|-------------|
| [claude-sessions](tools/claude-sessions/) | `claude-sessions`, `cs` | List, search, resume, watch, and manage Claude Code sessions — interactive picker, full-text search, cost stats, finished-turn notifications (`brew services start claude-sessions`) |

Each tool's own README (in `tools/<tool>/`) documents its usage in full.

## Repo structure

```
Formula/<tool>.rb        one Homebrew formula per tool
tools/<tool>/            the tool's source + its README
scripts/publish.sh       release helper (tags, sha256, formula update)
```

Tools version independently via per-tool git tags: `<tool>-v<version>`
(e.g. `claude-sessions-v0.2.0`). Each formula's `url` points at its own tag
tarball.

## Adding a new tool

1. Put the source in `tools/<name>/` with a `README.md`.
2. Write `Formula/<name>.rb` (copy `claude-sessions.rb` as a template — install
   from `tools/<name>/...`, keep the `test do` block meaningful).
3. Add a row to the Tools table above.
4. Release: `./scripts/publish.sh <name> 0.1.0`

## Releasing an update

```bash
./scripts/publish.sh <tool> <new-version>
```

The script pushes main, force-tags `<tool>-v<version>`, downloads the GitHub
tag tarball to compute its sha256, patches the formula, and pushes again.
Users get it with `brew update && brew upgrade <tool>`.
