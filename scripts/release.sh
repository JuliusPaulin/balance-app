#!/bin/bash
# Cut a release: tag a commit CI has already proven, and push the tag.
#
#     ./scripts/release.sh 1.3.1
#
# GitHub Actions does the rest (.github/workflows/release.yml): it builds
# Balance.app on an Apple silicon runner, signs the appcast and publishes the
# zip that update.sh and Sparkle download.
#
# Bump VERSION and add the "## <version>" section to CHANGELOG.md in a pull
# request first. This script used to commit the bump itself and tag it at
# once, which meant the tagged commit was one no CI had ever run on. It now
# refuses unless:
#   - VERSION already says <version>, and CHANGELOG.md has its section
#   - you are on main, clean, and level with origin/main
#   - the tag does not exist here or on GitHub
#   - the "macos" and "verify" checks passed on that exact commit
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

current="$(tr -d '[:space:]' < VERSION)"
if [ "$current" != "$VERSION_NEW" ]; then
    echo "VERSION says $current, not $VERSION_NEW."
    echo "Bump VERSION and CHANGELOG.md in a pull request, merge it once CI is"
    echo "green, then run this again from main."
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

git fetch -q origin main --tags
sha=$(git rev-parse HEAD)
if [ "$sha" != "$(git rev-parse origin/main)" ]; then
    echo "main is not level with origin/main. Pull (or push) first, so the"
    echo "commit you tag is the one CI ran on."
    exit 1
fi

if git rev-parse -q --verify "refs/tags/v$VERSION_NEW" >/dev/null \
   || git ls-remote --exit-code --tags origin "refs/tags/v$VERSION_NEW" >/dev/null 2>&1; then
    echo "Tag v$VERSION_NEW already exists."
    exit 1
fi

if ! python3 scripts/changelog.py "$VERSION_NEW" >/dev/null; then
    echo "CHANGELOG.md needs a '## $VERSION_NEW' section before this can be tagged."
    exit 1
fi

command -v gh >/dev/null || {
    echo "The GitHub CLI (gh) is needed to check CI for this commit."; exit 1; }

# The checks the release depends on, by name. "macos" is build.yml — the same
# build release.yml runs — and "verify" is lint plus the suite on Linux. The
# newest run of each wins, so a re-run that passed counts.
REQUIRED_CHECKS="macos verify"
echo "Checking CI for ${sha:0:7}..."
results=$(gh api "repos/{owner}/{repo}/commits/$sha/check-runs?per_page=100" --jq '
    .check_runs | group_by(.name) | map(max_by(.id))
    | .[] | "\(.name) \(.status) \(.conclusion)"')
failed=0
for check in $REQUIRED_CHECKS; do
    line=$(printf '%s\n' "$results" | awk -v n="$check" '$1 == n' | head -1)
    status=$(printf '%s' "$line" | awk '{print $2}')
    conclusion=$(printf '%s' "$line" | awk '{print $3}')
    if [ -z "$line" ]; then
        echo "  $check: has not run on this commit"; failed=1
    elif [ "$status" != "completed" ]; then
        echo "  $check: still $status"; failed=1
    elif [ "$conclusion" != "success" ]; then
        echo "  $check: $conclusion"; failed=1
    else
        echo "  $check: passed"
    fi
done
if [ "$failed" -ne 0 ]; then
    echo "CI is not green on ${sha:0:7}. Not tagging."
    echo "Watch it:  gh run list --commit $sha"
    exit 1
fi

git tag "v$VERSION_NEW"
git push -q origin "v$VERSION_NEW"

echo
echo "Tagged v$VERSION_NEW at ${sha:0:7} and pushed the tag."
echo "GitHub Actions is building it now — about twenty minutes."
echo "Watch it:  gh run watch"
echo "Then anyone can update with:"
echo "  curl -fsSL https://raw.githubusercontent.com/JuliusPaulin/balance-app/main/update.sh | bash"
