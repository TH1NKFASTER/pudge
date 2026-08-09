#!/usr/bin/env bash
set -euo pipefail

REPO_NAME="${1:-pudge}"
VISIBILITY="${2:-public}"

if [[ "$VISIBILITY" != "private" && "$VISIBILITY" != "public" ]]; then
  echo "Usage: $0 [repo-name] [private|public]" >&2
  exit 2
fi

command -v git >/dev/null 2>&1 || { echo "git is required" >&2; exit 1; }
command -v gh >/dev/null 2>&1 || { echo "Install GitHub CLI: brew install gh" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Run: gh auth login" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="$(awk -F'"' '/^version = "/ {print $2; exit}' pyproject.toml)"

if [[ -z "$VERSION" ]]; then
  echo "Could not read version from pyproject.toml" >&2
  exit 1
fi

if [[ ! -d .git ]]; then
  git init
fi

git branch -M main
git add -A

if ! git diff --cached --quiet; then
  git commit -m "Pudge v${VERSION}"
fi

if git remote get-url origin >/dev/null 2>&1; then
  git push -u origin main
else
  gh repo create "$REPO_NAME" --"$VISIBILITY" --source=. --remote=origin --push
fi

TAG="v${VERSION}"

if ! git rev-parse "$TAG" >/dev/null 2>&1; then
  git tag "$TAG"
fi

git push origin "$TAG"

echo
echo "Done: $(gh repo view --json url --jq .url)"
echo "Release workflow started for $TAG"
