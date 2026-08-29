#!/bin/bash
# Cut a release.
#
#     ./scripts/release.sh 1.3.1
#
# Bumps VERSION, commits it, tags, and pushes. GitHub Actions does the rest:
# it builds Balance.app on an Apple silicon runner, runs the tests, and
# publishes the zip that update.sh downloads.
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION_NEW="${1:-}"
if [ -z "$VERSION_NEW" ]; then
    echo "Usage: ./scripts/release.sh <version>   e.g. ./scripts/release.sh 1.3.1"
    echo "Current version: $(tr -d '[:space:]' < VERSION)"
    exit 1
fi

# Digits and dots only — the tag, the plist and update.sh all depend on this.
if ! printf '%s' "$VERSION_NEW" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "Version must look like 1.3.1 (major.minor.patch)."
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "You have uncommitted changes. Commit or stash them first:"
    git status --short
    exit 1
fi

branch=$(git rev-parse --abbrev-ref HEAD)
if [ "$branch" != "main" ]; then
    echo "You are on '$branch', not main. Switch to main first."
    exit 1
fi

if git rev-parse "v$VERSION_NEW" >/dev/null 2>&1; then
    echo "Tag v$VERSION_NEW already exists."
    exit 1
fi

echo "Checking the tests pass..."
python3 -m pytest tests/ -q

echo "$VERSION_NEW" > VERSION
git add VERSION
# Tolerate a re-run after a failed push: VERSION may already say the target.
if git diff --cached --quiet; then
    echo "VERSION already says $VERSION_NEW — tagging the current commit."
else
    git commit -q -m "Version $VERSION_NEW"
fi
git tag "v$VERSION_NEW"

git push -q origin main
git push -q origin "v$VERSION_NEW"

echo
echo "Tagged v$VERSION_NEW and pushed."
echo "GitHub Actions is building it now — takes about five minutes."
echo "Watch it:  gh run watch"
echo "Then anyone can update with:"
echo "  curl -fsSL https://raw.githubusercontent.com/JuliusPaulin/balance-app/main/update.sh | bash"
