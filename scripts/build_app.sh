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

echo "=== Building ${APP_NAME}.app ==="
# Exclude drivers left over from an old hosted build so PyInstaller neither
# looks for them nor bundles them.
python3 -m PyInstaller \
    --name "${APP_NAME}" \
    --windowed \
    --onedir \
    --icon static/icon.icns \
    --add-data "templates:templates" \
    --add-data "static:static" \
    --add-data "vendor/llama:vendor/llama" \
    --add-data "licences:licences" \
    --hidden-import model_runtime \
    --hidden-import webview \
    --hidden-import webview.platforms.cocoa \
    --hidden-import flask \
    --hidden-import flask_limiter \
    --hidden-import dateutil \
    --hidden-import db_sqlite \
    --hidden-import database \
    --hidden-import investment_import \
    --hidden-import openpyxl \
    --exclude-module psycopg \
    --exclude-module psycopg_pool \
    --exclude-module authlib \
    --collect-all webview \
    --noconfirm \
    --clean \
    main.py

echo "=== Creating DMG ==="
DMG_NAME="${APP_NAME}.dmg"
DMG_PATH="${DIST_DIR}/${DMG_NAME}"

# Remove old DMG if exists
rm -f "${DMG_PATH}"

# Create DMG with Applications symlink for drag-install
STAGING="${BUILD_DIR}/dmg_staging"
rm -rf "${STAGING}"
mkdir -p "${STAGING}"
cp -R "${DIST_DIR}/${APP_NAME}.app" "${STAGING}/"
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
