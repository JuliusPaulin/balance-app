#!/bin/bash
# Fetch the pinned official Sparkle distribution, including the framework and tools.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$ROOT/services/update_config.json"
SPARKLE_VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sparkle_version"])' "$CONFIG")"
EXPECTED_SHA256="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sparkle_sha256"])' "$CONFIG")"
URL="https://github.com/sparkle-project/Sparkle/releases/download/${SPARKLE_VERSION}/Sparkle-${SPARKLE_VERSION}.tar.xz"
VENDOR="$ROOT/vendor"
TARGET="$VENDOR/sparkle"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/balance-sparkle.XXXXXX")"
STAGE=""
cleanup() {
    rm -rf "$TMP"
    if [ -n "$STAGE" ] && [ -d "$STAGE" ]; then rm -rf "$STAGE"; fi
}
trap cleanup EXIT

mkdir -p "$VENDOR"
ARCHIVE="$TMP/Sparkle-${SPARKLE_VERSION}.tar.xz"
echo "=== Fetching Sparkle ${SPARKLE_VERSION} ==="
curl --fail --location --retry 3 --silent --show-error "$URL" -o "$ARCHIVE"
ACTUAL_SHA256="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
if [ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]; then
    echo "Sparkle archive checksum mismatch: expected $EXPECTED_SHA256, got $ACTUAL_SHA256" >&2
    exit 1
fi

mkdir -p "$TMP/unpacked"
tar -xJf "$ARCHIVE" -C "$TMP/unpacked"
DIST="$TMP/unpacked"
test -d "$DIST/Sparkle.framework" || { echo "Sparkle.framework is missing from the archive." >&2; exit 1; }
test -x "$DIST/bin/sign_update" || { echo "Sparkle sign_update is missing from the archive." >&2; exit 1; }
test -x "$DIST/bin/generate_appcast" || { echo "Sparkle generate_appcast is missing from the archive." >&2; exit 1; }
test -f "$DIST/LICENSE" || { echo "Sparkle's licence is missing from the archive." >&2; exit 1; }

python3 - "$DIST/Sparkle.framework/Resources/Info.plist" "$SPARKLE_VERSION" <<'PY'
import plistlib
import sys

with open(sys.argv[1], "rb") as stream:
    version = plistlib.load(stream).get("CFBundleShortVersionString")
if version != sys.argv[2]:
    raise SystemExit(f"Sparkle framework reports {version!r}; expected {sys.argv[2]!r}")
PY

# Build the staging tree on the same volume as vendor, preserving framework
# symlinks and executable modes with ditto.
STAGE="$(mktemp -d "$VENDOR/.sparkle.XXXXXX")"
ditto "$DIST/Sparkle.framework" "$STAGE/Sparkle.framework"
ditto "$DIST/bin" "$STAGE/bin"
for NOTICE in LICENSE NOTICE; do
    if [ -f "$DIST/$NOTICE" ]; then ditto "$DIST/$NOTICE" "$STAGE/$NOTICE"; fi
done

OLD="$VENDOR/.sparkle-old.$$"
if [ -e "$TARGET" ]; then mv "$TARGET" "$OLD"; fi
if mv "$STAGE" "$TARGET"; then
    STAGE=""
    rm -rf "$OLD"
else
    [ ! -e "$OLD" ] || mv "$OLD" "$TARGET"
    exit 1
fi

echo "Sparkle ${SPARKLE_VERSION} installed at vendor/sparkle"
