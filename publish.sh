#!/bin/bash
# One-shot publisher: turns this folder into a public Homebrew tap on GitHub.
#
#   ./publish.sh            # first release (v0.1.0)
#   ./publish.sh 0.2.0      # subsequent releases
#
# Prereqs: `gh auth login` done once, `git` and `gh` installed.
# After it finishes, install on any Mac with:
#   brew install <github-user>/tools/claude-sessions

set -euo pipefail
cd "$(dirname "$0")"

VERSION="${1:-0.1.0}"
REPO_NAME="homebrew-tools"
FORMULA="Formula/claude-sessions.rb"

GH_USER=$(gh api user -q .login) || {
  echo "GitHub auth is not working. Run: gh auth login -h github.com" >&2
  exit 1
}
echo "==> Publishing as github.com/${GH_USER}/${REPO_NAME} v${VERSION}"

# Point the formula at this GitHub user/tag (idempotent)
sed -i '' \
  -e "s|github.com/[^/\"]*/homebrew-tools|github.com/${GH_USER}/${REPO_NAME}|g" \
  -e "s|refs/tags/v[0-9.]*\.tar\.gz|refs/tags/v${VERSION}.tar.gz|" \
  "$FORMULA"

if [ ! -d .git ]; then
  git init -b main
  printf '__pycache__/\n' > .gitignore
fi
git add -A
git commit -m "claude-sessions v${VERSION}" || true   # ok if nothing to commit

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "==> Creating public repo ${GH_USER}/${REPO_NAME}"
  gh repo create "${REPO_NAME}" --public --source . --push \
    --description "Homebrew tap: claude-sessions (Claude Code session manager)"
else
  git push -u origin main
fi

echo "==> Tagging v${VERSION}"
git tag -f "v${VERSION}"
git push -f origin "v${VERSION}"

echo "==> Computing tarball sha256"
TARBALL_URL="https://github.com/${GH_USER}/${REPO_NAME}/archive/refs/tags/v${VERSION}.tar.gz"
SHA=$(curl -fsSL "$TARBALL_URL" | shasum -a 256 | awk '{print $1}')
echo "    ${SHA}"

sed -i '' -e "s|sha256 \".*\"|sha256 \"${SHA}\"|" "$FORMULA"
git add "$FORMULA"
git commit -m "formula: sha256 for v${VERSION}"
git push origin main

cat <<DONE

Published. Install on any Mac with:

  brew install ${GH_USER}/tools/claude-sessions

Then use:  claude-sessions   (or the short alias:  cs)

To ship a new version later: edit the code, then  ./publish.sh <new-version>
Upgraders run:  brew update && brew upgrade claude-sessions
DONE
