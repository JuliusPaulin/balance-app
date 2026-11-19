#!/bin/bash
# Build Balance as a standalone macOS .app and package as DMG.
# The app runs entirely locally on SQLite (data lives in
# ~/Library/Application Support/Balance/expenses.db) — no server, no Postgres.
set -e

cd "$(dirname "$0")/.."
APP_NAME="Balance"
DIST_DIR="dist"
BUILD_DIR="build"

echo "=== Installing build dependencies ==="
pip3 install pyinstaller --quiet

# The model runtime travels inside the app; the weights do not, and are fetched
# on first use. 56 MB of compiled binaries are not in git, so they are pulled
# here — see scripts/fetch_runtime.sh for why the build is pinned.
echo "=== Fetching the model runtime ==="
./scripts/fetch_runtime.sh

echo "=== Fetching Sparkle ==="
./scripts/fetch_sparkle.sh

echo "=== Building ${APP_NAME}.app ==="
# From Balance.spec, which is the build. This used to pass the whole
# configuration on the command line instead, and PyInstaller writes a spec out
# of whatever it is given — so every local build silently overwrote the real
# one. That is how the bundle identifier, the plist and `version=VERSION` were
# lost: a build here, `git add -A`, and the released app no longer knew what
# version it was. Release CI builds from the spec, so this does too, and there
# is one definition of the app rather than two that drift.
python3 -m PyInstaller Balance.spec --noconfirm --clean

echo "=== Checking the bundle ==="
# The same script the pull-request and release builds run, so a local build
# fails here rather than in CI ten minutes later.
./scripts/check_bundle.sh "${DIST_DIR}/${APP_NAME}.app"

echo "=== Creating DMG ==="
DMG_NAME="${APP_NAME}.dmg"
DMG_PATH="${DIST_DIR}/${DMG_NAME}"

# Remove old DMG if exists
rm -f "${DMG_PATH}"

# Create DMG with Applications symlink for drag-install
STAGING="${BUILD_DIR}/dmg_staging"
rm -rf "${STAGING}"
mkdir -p "${STAGING}"
ditto "${DIST_DIR}/${APP_NAME}.app" "${STAGING}/${APP_NAME}.app"
ln -s /Applications "${STAGING}/Applications"

hdiutil create -volname "${APP_NAME}" \
    -srcfolder "${STAGING}" \
    -ov -format UDZO \
    "${DMG_PATH}"

rm -rf "${STAGING}"

echo ""
echo "=== Done ==="
echo "App:  ${DIST_DIR}/${APP_NAME}.app"
echo "DMG:  ${DMG_PATH}"
echo ""
echo "Share the DMG file with your friends."
echo "They open it, drag to Applications, and run."
