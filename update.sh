#!/bin/bash
# Update Balance to the newest release.
# Run:  bash update.sh
set -euo pipefail

REPO="JuliusPaulin/balance-app"
APP="/Applications/Balance.app"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if pgrep -x "Balance" >/dev/null 2>&1; then
    echo "Balance is open. Quit it first, then run this again."
    exit 1
fi

echo "Looking up the newest version..."
URL=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
      | grep -o 'https://[^"]*Balance-macOS-arm64\.zip' | head -1)
if [ -z "$URL" ]; then
    echo "Could not find a download. Check your internet connection and try again."
    exit 1
fi

echo "Downloading..."
curl -fSL --progress-bar -o "$TMP/Balance.zip" "$URL"

echo "Unpacking..."
ditto -x -k "$TMP/Balance.zip" "$TMP/out"
if [ ! -d "$TMP/out/Balance.app" ]; then
    echo "That download does not look right. Nothing was changed."
    exit 1
fi

echo "Installing..."
if [ -d "$APP" ]; then
    rm -rf "$APP.old"
    mv "$APP" "$APP.old"
fi
if ditto "$TMP/out/Balance.app" "$APP"; then
    xattr -cr "$APP"
    rm -rf "$APP.old"
    echo
    echo "Done. Balance is up to date. Open it from your Applications folder."
else
    echo "Install failed. Putting the old version back."
    rm -rf "$APP"
    [ -d "$APP.old" ] && mv "$APP.old" "$APP"
    exit 1
fi
