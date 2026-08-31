#!/bin/bash
# Build the model runtime that ships inside Balance.app.
#
# llama.cpp's own server, from its own source, pinned to a tag and carrying one
# patch of ours. It is not in git: 56 MB of compiled binaries do not belong in a
# repository. `build_app.sh` runs this before packaging.
#
# Pinned rather than "latest" on purpose. The thing this app depends on is not
# llama.cpp in general but a few behaviours of it — that Qwen's tool calls come
# back parsed, and that `enable_thinking: false` reaches the chat template — and
# a build that quietly changed either would be found by a user, not by us.
#
# This downloaded the official release binary until it turned out that the
# official binary cannot use the GPU on macOS 12 or 13: it aborts on its first
# decode inside Metal's readback path, on every Mac below Ventura's successor,
# and no flag avoids it. See patches/metal-readback.patch for what goes wrong
# and why the fix has to be in the build rather than in how we start it.
#
# So there is a compiler in the release path now, which costs 95 seconds cold on
# eight cores and about four minutes on a GitHub runner's three. That is the
# whole price of the fix, and it buys back about twice the speed on the machines
# that were falling back to their CPU — 2.2x on reading a question and 1.25x on
# writing an answer, measured on one M1 Pro, with reading the half that counts. The patch is a file rather than a fork,
# because a file rebases onto the next pinned tag and a fork has to be merged.
#
# Building only the `llama-server` target saves four seconds of the ninety-five
# — the other tools link against dylibs this has already built — so the build is
# left whole rather than narrowed for nothing.
set -e
cd "$(dirname "$0")/.."

BUILD="${LLAMA_BUILD:-b10715}"
# What BUILD.txt says, and what the app checks for before it trusts the GPU on
# an old Mac. Change the patch and change this with it.
PATCH="metal-readback"
DEST="vendor/llama"
SRC_DIR=".llama-build/llama.cpp"
# Absolute: `git -C` runs from inside the checkout, and a relative path there
# would point at llama.cpp's own tree.
PATCH_FILE="${PWD}/patches/${PATCH}.patch"

if [ -x "${DEST}/llama-server" ] && [ "${1}" != "--force" ]; then
    echo "Runtime already in ${DEST} (--force to replace)"
    exit 0
fi

command -v cmake >/dev/null || {
    echo "cmake is needed to build the runtime now — 'brew install cmake'."
    echo "The GitHub runners already have it; this is only for building here."
    exit 1
}

ARCH="$(uname -m)"
case "$ARCH" in
    arm64)  METAL=ON  ;;
    # Metal on Intel is not what Balance ships — the app is Apple silicon only —
    # and upstream disables it on x64 for the same reason. A build here is for
    # working on the app, not for anyone to run.
    x86_64) METAL=OFF ;;
    *) echo "No llama.cpp build for ${ARCH}"; exit 1 ;;
esac

echo "=== Fetching llama.cpp ${BUILD} source ==="
mkdir -p .llama-build
if [ -d "${SRC_DIR}/.git" ] && \
   [ "$(git -C "${SRC_DIR}" describe --tags --exact-match 2>/dev/null)" = "${BUILD}" ]; then
    # Kept between runs so a second build is incremental. The patch is reapplied
    # from a clean tree every time, so a half-applied one cannot survive.
    git -C "${SRC_DIR}" checkout -- .
else
    rm -rf "${SRC_DIR}"
    git clone --depth 1 --branch "${BUILD}" \
        https://github.com/ggml-org/llama.cpp.git "${SRC_DIR}"
fi

echo "=== Applying ${PATCH} ==="
# --check first: a patch that no longer applies is the thing most likely to go
# wrong at the next version bump, and it must stop the build rather than produce
# a runtime that is quietly back to crashing.
git -C "${SRC_DIR}" apply --check "${PATCH_FILE}" || {
    echo
    echo "patches/${PATCH}.patch does not apply to llama.cpp ${BUILD}."
    echo "Rebase it onto the new tag before bumping LLAMA_BUILD — without it,"
    echo "every Mac on macOS 13 or older loses the GPU and nothing says so."
    exit 1
}
git -C "${SRC_DIR}" apply "${PATCH_FILE}"

echo "=== Building (about 90 seconds on eight cores) ==="
# The flags upstream uses for its own macOS release, less the parts Balance has
# no use for. The deployment target is theirs and is load-bearing: built on a
# current runner without it, the binary would refuse to load on the very Macs
# this patch exists for.
cmake -S "${SRC_DIR}" -B "${SRC_DIR}/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DGGML_METAL="${METAL}" \
    -DGGML_METAL_EMBED_LIBRARY=ON \
    -DCMAKE_OSX_DEPLOYMENT_TARGET=13.3 \
    -DCMAKE_INSTALL_RPATH='@loader_path' \
    -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON \
    -DLLAMA_BUILD_TESTS=OFF \
    -DLLAMA_BUILD_EXAMPLES=OFF \
    -DLLAMA_BUILD_TOOLS=ON \
    -DLLAMA_BUILD_SERVER=ON \
    -DLLAMA_BUILD_APP=OFF \
    -DLLAMA_OPENSSL=OFF
cmake --build "${SRC_DIR}/build" --config Release -j "$(sysctl -n hw.logicalcpu)"

BIN="${SRC_DIR}/build/bin"
test -x "${BIN}/llama-server" || { echo "No llama-server was built"; exit 1; }

rm -rf "${DEST}"
mkdir -p "${DEST}"
# The server and the libraries it opens. The other twenty binaries in there —
# the benchmarks, the quantiser, the CLI — are not what this app runs.
cp "${BIN}/llama-server" "${DEST}/"
cp "${BIN}"/*.dylib "${DEST}/" 2>/dev/null || true
cp "${BIN}"/*.metal "${DEST}/" 2>/dev/null || true

# MIT, and it asks that the notice travel with the binaries.
cp "${SRC_DIR}/LICENSE" "${DEST}/LICENSE-llama.cpp.txt"
# What the running binary is, patch and all. `model_runtime` reads this before
# it will put the model on the GPU on an old Mac, and a test asserts the marker
# is here — a runtime bump that dropped the patch would otherwise put every
# Ventura user back on their CPU with nothing to show for it.
echo "${BUILD}+${PATCH}" > "${DEST}/BUILD.txt"

echo "=== Done: $(du -sh "${DEST}" | cut -f1) in ${DEST} ($(cat "${DEST}/BUILD.txt")) ==="
