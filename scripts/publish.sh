#!/bin/bash
# Publish one tool from this tap to GitHub.
#
#   ./scripts/publish.sh <tool> <version>
#   ./scripts/publish.sh claude-sessions 0.2.0
#
# Expects: Formula/<tool>.rb and tools/<tool>/ to exist.
# Tags per tool:  <tool>-v<version>   (so tools version independently)
# Prereqs: `gh auth login` done once, `git` and `gh` installed.

set -euo pipefail
cd "$(dirname "$0")/.."

TOOL="${1:?usage: publish.sh <tool> <version>}"
VERSION="${2:?usage: publish.sh <tool> <version>}"
REPO_NAME="homebrew-tools"
FORMULA="Formula/${TOOL}.rb"
TAG="${TOOL}-v${VERSION}"

[ -f "$FORMULA" ] || { echo "Missing $FORMULA" >&2; exit 1; }
[ -d "tools/$TOOL" ] || { echo "Missing tools/$TOOL/" >&2; exit 1; }

GH_USER=$(gh api user -q .login) || {
  echo "GitHub auth is not working. Run: gh auth login -h github.com" >&2
  exit 1
}
echo "==> Publishing ${TOOL} v${VERSION} to github.com/${GH_USER}/${REPO_NAME}"

# Point the formula at this GitHub user and tag (idempotent)
sed -i '' \
  -e "s|github.com/[^/\"]*/homebrew-tools|github.com/${GH_USER}/${REPO_NAME}|g" \
  -e "s|refs/tags/[^\"]*\.tar\.gz|refs/tags/${TAG}.tar.gz|" \
  "$FORMULA"

if [ ! -d .git ]; then
  git init -b main
  printf '__pycache__/\n*.pyc\n' > .gitignore
fi
git add -A
git commit -m "${TOOL} v${VERSION}" || true   # ok if nothing to commit

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "==> Creating public repo ${GH_USER}/${REPO_NAME}"
  gh repo create "${REPO_NAME}" --public --source . --push \
    --description "Personal Homebrew tap"
else
  git push -u origin main
fi

echo "==> Tagging ${TAG}"
git tag -f "$TAG"
git push -f origin "$TAG"

echo "==> Computing tarball sha256"
TARBALL_URL="https://github.com/${GH_USER}/${REPO_NAME}/archive/refs/tags/${TAG}.tar.gz"
SHA=$(curl -fsSL "$TARBALL_URL" | shasum -a 256 | awk '{print $1}')
echo "    ${SHA}"

sed -i '' -e "s|sha256 \".*\"|sha256 \"${SHA}\"|" "$FORMULA"
git add "$FORMULA"
git commit -m "${TOOL}: formula sha256 for v${VERSION}"
git push origin main

cat <<DONE

Published. Install on any Mac with:

  brew install ${GH_USER}/tools/${TOOL}

Upgraders run:  brew update && brew upgrade ${TOOL}
DONE
