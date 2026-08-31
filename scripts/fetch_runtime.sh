#!/bin/bash
# Fetch the model runtime that ships inside Balance.app.
#
# llama.cpp's own server, from an official release, pinned to a build. It is not
# in git: 30 MB of compiled binaries do not belong in a repository, and this
# takes ten seconds. `build_app.sh` runs it before packaging.
#
# Pinned rather than "latest" on purpose. The thing this app depends on is not
# llama.cpp in general but one behaviour of it — that Qwen's tool calls come
# back parsed, and that `enable_thinking: false` reaches the chat template — and
# a build that quietly changed either would be found by a user, not by us.
set -e
cd "$(dirname "$0")/.."

BUILD="${LLAMA_BUILD:-b10715}"
ARCH="$(uname -m)"
case "$ARCH" in
    arm64) ASSET="llama-${BUILD}-bin-macos-arm64.tar.gz" ;;
    x86_64) ASSET="llama-${BUILD}-bin-macos-x64.tar.gz" ;;
    *) echo "No llama.cpp build for ${ARCH}"; exit 1 ;;
esac

DEST="vendor/llama"
if [ -x "${DEST}/llama-server" ] && [ "${1}" != "--force" ]; then
    echo "Runtime already in ${DEST} (--force to replace)"
    exit 0
fi

echo "=== Fetching llama.cpp ${BUILD} for ${ARCH} ==="
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
curl -fL --progress-bar \
    "https://github.com/ggml-org/llama.cpp/releases/download/${BUILD}/${ASSET}" \
    -o "${TMP}/llama.tar.gz"
tar xzf "${TMP}/llama.tar.gz" -C "${TMP}"

rm -rf "${DEST}"
mkdir -p "${DEST}"
# The server and the libraries it opens. The other twenty binaries in the
# archive — the benchmarks, the quantiser, the CLI — are not what this app runs.
SRC="$(find "${TMP}" -name llama-server -maxdepth 2 | head -1 | xargs dirname)"
cp "${SRC}/llama-server" "${DEST}/"
cp "${SRC}"/*.dylib "${DEST}/" 2>/dev/null || true
cp "${SRC}"/*.metal "${DEST}/" 2>/dev/null || true

# MIT, and it asks that the notice travel with the binaries.
find "${TMP}" -iname "LICENSE*" -maxdepth 3 -exec cp {} "${DEST}/LICENSE-llama.cpp.txt" \; 2>/dev/null || true
echo "${BUILD}" > "${DEST}/BUILD.txt"

echo "=== Done: $(du -sh "${DEST}" | cut -f1) in ${DEST} ==="
