#!/bin/bash
# Check a built Balance.app before anyone ships it.
#
#     ./scripts/check_bundle.sh [dist/Balance.app]
#
# One definition, run in three places: the pull-request build, the release
# build, and scripts/build_app.sh. These checks lived inline in release.yml,
# so a tag was the first place they ever ran.
set -euo pipefail

cd "$(dirname "$0")/.."
app="${1:-dist/Balance.app}"
test -d "$app" || { echo "No app at $app — build it first."; exit 1; }

# Version. Catches tagging v1.3.1 while VERSION or the spec still says 1.3.0,
# which ships an app that misreports itself.
built=$(/usr/libexec/PlistBuddy -c "Print CFBundleShortVersionString" "$app/Contents/Info.plist")
expected="$(tr -d '[:space:]' < VERSION)"
[ "$built" = "$expected" ] || {
    echo "Built app reports $built, VERSION says $expected"; exit 1; }
echo "Bundle reports $built"

# The runtime. A bundle that builds and runs but has no model server in it
# looks healthy until someone opens the panel.
llama=""
for dir in "$app/Contents/Frameworks/vendor/llama" "$app/Contents/Resources/vendor/llama"; do
    if [ -f "$dir/llama-server" ]; then llama="$dir"; break; fi
done
[ -n "$llama" ] || { echo "No llama-server in the bundle — the AI would not run."; exit 1; }
test -f "$app/Contents/Resources/licences/Apache-2.0.txt" \
    || { echo "The model licence is missing from the bundle."; exit 1; }
# And the patched one. Without the patch every Mac on macOS 13 or older falls
# back to its CPU, which nothing in a build log would show.
build="$(cat "$llama/BUILD.txt" 2>/dev/null || true)"
case "$build" in
    *metal-readback*) echo "Runtime is $build" ;;
    *) echo "Runtime '$build' has no Metal readback patch — see"
       echo "patches/metal-readback.patch. Do not ship this."; exit 1 ;;
esac

# Sparkle, its settings and its signatures.
framework="$app/Contents/Frameworks/Sparkle.framework"
test -d "$framework" || { echo "Sparkle.framework is missing."; exit 1; }
test -f "$app/Contents/Resources/Sparkle-LICENSE.txt" \
    || { echo "The Sparkle licence is missing from the bundle."; exit 1; }
python3 - "$app/Contents/Info.plist" services/update_config.json <<'PY'
import json, plistlib, sys
with open(sys.argv[1], "rb") as stream:
    info = plistlib.load(stream)
with open(sys.argv[2], encoding="utf-8") as stream:
    config = json.load(stream)
expected = {
    "SUFeedURL": config["feed_url"],
    "SUEnableAutomaticChecks": True,
    "SUAutomaticallyUpdate": False,
    "SUVerifyUpdateBeforeExtraction": True,
    "SURequireSignedFeed": True,
    "SUEnableSystemProfiling": False,
    "LSMinimumSystemVersion": "12.0.0",
}
for key, value in expected.items():
    if info.get(key) != value:
        raise SystemExit(f"{key} is {info.get(key)!r}, expected {value!r}")
if info.get("SUPublicEDKey") != config["public_key"]:
    raise SystemExit("Embedded Sparkle public key does not match services/update_config.json")
print("Sparkle settings match the release configuration")
PY
# Deep strict verification covers Sparkle.framework and its XPC helpers.
codesign --verify --deep --strict --verbose=2 "$app"
codesign --verify --strict --verbose=2 "$framework"

echo "Bundle checks passed"
