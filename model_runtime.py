"""The model that ships with the app: finding it, fetching it, running it.

Balance has always been one file on your own Mac. The assistant kept that true
by talking to Ollama, but Ollama is a separate thing to install, and nobody
installs a second app to try a side panel. So the app carries llama.cpp's own
server in its bundle and one model file it downloads on first use.

Three jobs live here and nothing else does:

* **Where things are.** The server binary travels inside the bundle; the model
  sits beside the database in Application Support, because the bundle is
  read-only and gets replaced on every update.
* **Fetching the model once.** 2.7 GB is too much for a release asset and far
  too much to re-download after a failure, so it streams to a `.part` file and
  resumes.
* **Running the server.** Started on demand, stopped with the app.

What is deliberately *not* here: anything about prompts, tools or answers. This
module knows a file path and a port.
"""

import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

import requests

import config

# One server, one download, for the life of the process.
_server = None
_download = {"state": "idle", "bytes": 0, "total": 0, "error": None}
# Whether a start is already in flight, so a panel polling every two seconds
# does not queue up a spawn per poll, and how many have failed in a row.
_starting = False
_failed_starts = 0
# Whether this process has already looked for a server left behind by an
# earlier one. Once is enough, and it must not happen on every poll.
_reclaimed = False

# How many attempts before the panel stops saying "starting up" and says what
# went wrong. Two, because the first failure can be a cold disk or a machine
# still busy from launch, and a third try helps nobody: a server that will not
# start does not start on the fifth attempt either, and "starting up" for ever
# is the least useful thing a screen can say.
MAX_START_ATTEMPTS = 2
_lock = threading.Lock()

# How long to let the server boot before calling it stuck. Loading three
# gigabytes off a cold disk is slow the first time and quick afterwards.
STARTUP_TIMEOUT = 180


# ── Where things are ──────────────────────────────────────────────────────

# What the bundled llama.cpp is built against — `otool -l` reports minos 13.3.
# Balance itself runs on any Apple silicon Mac, which is macOS 11 and up, so
# there is a range of machines where the app is fine and the assistant cannot
# start. dyld refuses the binary and the process dies with a backtrace, which is
# a miserable way to learn about a version requirement.
MIN_MACOS = (13, 3)


# Where llama.cpp stops needing our patch to read its own results back off the
# GPU. Below this, `newBufferWithBytesNoCopy:` refuses the logits pointer — 32
# bytes aligned, and Metal wants a page — and an unpatched server aborts on its
# first decode. macOS 14 and later take the same pointer without complaint.
MIN_MACOS_GPU = (14, 0)

# What `scripts/fetch_runtime.sh` writes into BUILD.txt once it has applied
# `patches/metal-readback.patch`. Read rather than assumed, because the failure
# worth catching is a runtime bump that dropped the patch: the app would go on
# putting the model on the GPU on an old Mac and go on crashing there.
RUNTIME_PATCH = "metal-readback"


def _macos_version():
    """This Mac's version as (major, minor), or None when it cannot be read."""
    if platform.system() != "Darwin":
        return None
    release = platform.mac_ver()[0]
    if not release:
        return None
    try:
        return tuple(int(n) for n in release.split(".")[:2])
    except ValueError:
        return None


def macos_too_old():
    """True when this Mac cannot run the bundled runtime. Never guesses."""
    version = _macos_version()
    return version is not None and version < MIN_MACOS


def runtime_build():
    """What the bundled runtime says it is — `b10715+metal-readback`, or ""."""
    try:
        with open(os.path.join(_bundle_root(), "vendor", "llama",
                               "BUILD.txt")) as handle:
            return handle.read().strip()
    except OSError:
        return ""


def runtime_patched():
    """Whether this build's llama.cpp can read off the GPU on an old Mac."""
    return RUNTIME_PATCH in runtime_build()


# The context is not one of the things that may be given up. A turn carries the
# system prompt, the tool schemas and a tool result — about 5 100 tokens at the
# smallest, and past 10 000 for a whole month read at once. An early step-down
# halved it to 4 096 to save memory, which is under the floor: the server
# started, took a long time, and answered nothing at all. It traded a crash for
# silence, which is worse, because a crash says something.
MIN_CTX = 8192

# Two ways to run:
#
#   0  the GPU
#   1  the CPU — the last resort, and a poor one, at 23 tokens a second against
#      310. Slow enough is the same as broken: a simple question takes over two
#      minutes and a month's analysis cannot finish inside the request timeout.
#
# Only the GPU is negotiable, and now it should almost never have to be.
#
# The crash that made this a ladder is `ggml-metal-context.m:361:
# GGML_ASSERT(buf_dst) failed`, and three explanations of it were wrong before
# the fourth was right. It is not memory: that line wraps a pointer with
# `newBufferWithBytesNoCopy:`, and Metal returns nil — not an error — when the
# address is not page-aligned. It is not the memory-mapped model either, which
# is what the previous version of this comment claimed and what six releases
# were built on. The pointer is llama.cpp's *logits* buffer, the destination it
# reads results back into, allocated by the CPU backend and aligned to 32 bytes.
# No flag reaches it, which is why every one of them was tried and every one of
# them aborted: -ngl 1 as surely as -ngl 999, every context size, every load
# mode. What separates the machines is the OS. macOS 14 and later take the
# pointer; 12 and 13 refuse it.
#
# So it was never something to configure around, and the fix is in the build:
# `patches/metal-readback.patch` guards that one call, and
# `scripts/fetch_runtime.sh` compiles it in. With a patched runtime every Mac
# starts at level 0 and stays there. Level 1 remains for the machine nobody has
# tested on yet.
_FIT_LEVELS = 2
# Resolved the first time a server is started, then stepped down if it dies.
# Not a constant 0 any more: on a Mac the runtime cannot use the GPU on, level 0
# is a crash with a known cause, and spending one on every launch teaches
# nobody anything.
_fit = None
# What the GPU attempt said before it was given up on. The fallback used to be
# silent to everything but the log file, so the panel could report a working
# assistant and never mention that it was twelve times slower than it should be.
_fell_back = None


def _start_level():
    """The first level worth trying on this Mac.

    Level 0 puts the model on the GPU, and an unpatched llama.cpp cannot read
    the results back off it below macOS 14 — see `patches/metal-readback.patch`.
    A patched runtime guards that call and level 0 works, which is the whole
    reason the runtime is built rather than downloaded. So this steps down only
    when the runtime in the bundle is not the patched one, and the crash stops
    being part of every launch.
    """
    version = _macos_version()
    if version is not None and version < MIN_MACOS_GPU and not runtime_patched():
        return 1
    return 0


def _level():
    """The level to run at now, decided once and then only ever stepped down."""
    global _fit
    if _fit is None:
        _fit = _start_level()
    return _fit


def _fit_args(level):
    """The flags for a level. The context is the same at every one of them.

    `--load-mode none` does turn the memory map off, and that is all it does
    here: it was added believing it avoided the Metal crash, and it does not.
    Kept because every measurement in this app was taken with it and reading
    the model in costs about twenty seconds of a launch, once — but it is no
    longer holding anything up, and dropping it is worth measuring.
    """
    ctx = max(config.OLLAMA_NUM_CTX, MIN_CTX)
    return ["--ctx-size", str(ctx), "--load-mode", "none",
            "--n-gpu-layers", "999" if level <= 0 else "0"]


def _bundle_root():
    """Where the app's own files are: the unpacked bundle, or the checkout."""
    return getattr(sys, "_MEIPASS", None) or os.path.dirname(
        os.path.abspath(__file__))


def server_binary():
    """The bundled `llama-server`, or None if this build has no runtime.

    Same path either side of packaging — `vendor/llama/` in the checkout, and
    `vendor/llama/` inside the unpacked bundle, because that is what
    `--add-data "vendor/llama:vendor/llama"` writes. Getting those two out of
    step is a bug that cannot happen in development and always happens in the
    shipped app.
    """
    candidate = os.path.join(_bundle_root(), "vendor", "llama", "llama-server")
    if not os.path.isfile(candidate):
        return None
    # PyInstaller copies data files without their permissions, so the binary can
    # arrive in the bundle unable to run. Cheap to put right, and the failure it
    # prevents reads as "the model server would not start".
    if not os.access(candidate, os.X_OK):
        try:
            os.chmod(candidate, 0o755)
        except OSError:
            return None
    return candidate


# Smaller than any real quantisation of this model, and enough to reject a
# truncation that happened to land on a byte boundary.
_MIN_MODEL_BYTES = 100_000_000


def model_present():
    """Whether a usable model is on disk.

    A part-file is not a model, and neither is a truncated one. This asked only
    whether the file existed and was non-empty, so a download that stopped
    early — which can happen without any error being raised — passed as a whole
    model and left the panel starting up for ever.
    """
    path = config.model_file()
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < _MIN_MODEL_BYTES:
            return False
        with open(path, "rb") as handle:
            return handle.read(4) == b"GGUF"
    except OSError:
        return False


# ── Fetching it once ──────────────────────────────────────────────────────

def _part_file():
    return config.model_file() + ".part"


def download_state():
    with _lock:
        return dict(_download)


def start_download():
    """Begin (or resume) the download. Returns at once; watch `download_state`."""
    with _lock:
        if _download["state"] == "downloading":
            return dict(_download)
        _download.update(state="downloading", error=None,
                         total=config.MODEL_BYTES)
    threading.Thread(target=_download_worker, daemon=True).start()
    return download_state()


def _download_worker():
    path, part = config.model_file(), _part_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        # Resume from whatever survived a previous attempt. 2.7 GB is far too
        # much to start again because a laptop lid closed.
        done = os.path.getsize(part) if os.path.exists(part) else 0
        headers = {"Range": f"bytes={done}-"} if done else {}
        with requests.get(config.MODEL_URL, headers=headers, stream=True,
                          timeout=60) as response:
            if response.status_code == 416:
                # The server says there is nothing left to send: it is complete.
                done = os.path.getsize(part)
                response.close()
            elif not response.ok:
                raise RuntimeError(f"the download returned {response.status_code}")
            else:
                length = int(response.headers.get("Content-Length") or 0)
                total = done + length if length else config.MODEL_BYTES
                with _lock:
                    _download["total"] = total
                mode = "ab" if done and response.status_code == 206 else "wb"
                if mode == "wb":
                    done = 0
                with open(part, mode) as handle:
                    for block in response.iter_content(chunk_size=1024 * 1024):
                        handle.write(block)
                        done += len(block)
                        with _lock:
                            _download["bytes"] = done

        # A stream can end early without raising — a dropped connection, a
        # server closing mid-file — and the half a model that leaves behind
        # looks exactly like a whole one to anything that only checks the size
        # is above zero. It gets renamed, `llama-server` cannot load it, and the
        # panel says "starting up" until somebody gives up.
        expected = _download.get("total") or 0
        actual = os.path.getsize(part)
        if expected and actual < expected:
            raise RuntimeError(
                f"the download stopped early — {actual / 1e9:.2f} GB of "
                f"{expected / 1e9:.2f} GB. Trying again resumes from here.")
        os.replace(part, path)
        _place_licence()
        with _lock:
            _download.update(state="done", bytes=os.path.getsize(path))
    except Exception as exc:
        # The part file is kept on purpose: the next attempt resumes from it.
        with _lock:
            _download.update(state="failed", error=str(exc))


def _place_licence():
    """Put the model's licence and attribution beside the weights.

    Apache 2.0 lets us redistribute them and asks two things back: that its
    terms travel with the work, and that the attribution is preserved. Both are
    copied from the app's own bundle rather than downloaded — the first attempt
    fetched them from the model's repository, which declares Apache 2.0 in its
    metadata and ships no licence file, so the request 404'd and the obligation
    quietly went unmet. A licence that depends on a download succeeding is not a
    licence you have shipped.
    """
    for name in ("Apache-2.0.txt", "NOTICE-model.txt"):
        source = os.path.join(_bundle_root(), "licences", name)
        target = os.path.join(config.model_dir(), name)
        if os.path.isfile(source) and not os.path.exists(target):
            shutil.copyfile(source, target)


def licences_present():
    """Whether the notices actually landed. Surfaced in the status, because a
    silent failure here is the one that matters."""
    return all(os.path.exists(os.path.join(config.model_dir(), name))
               for name in ("Apache-2.0.txt", "NOTICE-model.txt"))


def _check_room(needed):
    free = shutil.disk_usage(config.model_dir()).free
    if free < needed:
        raise RuntimeError(
            f"not enough room — {needed / 1e9:.1f} GB needed, "
            f"{free / 1e9:.1f} GB free")


# ── Running the server ────────────────────────────────────────────────────

def server_responding(host=None):
    """Whether *our* model server is answering — not merely that something is.

    This asked `/health`, which says yes to anything listening on the port. It
    said yes to a server left running from an earlier session, so the panel
    reported itself ready, offered a chat box, and had no model on disk at all:
    `state: ready` sitting next to `model_installed: false` in the same reply.
    A port is not an identity. `/props` names the file the server has open, and
    that is the thing worth agreeing on — it also means another program on 5051
    cannot be mistaken for this one.
    """
    try:
        response = requests.get(f"{(host or config.LLAMACPP_HOST)}/props",
                                timeout=2)
        if not response.ok:
            return False
        loaded = (response.json() or {}).get("model_path") or ""
    except (requests.RequestException, ValueError):
        return False
    try:
        return os.path.realpath(loaded) == os.path.realpath(config.model_file())
    except OSError:
        return False


def _log(line):
    """One line into the server log, beside whatever the server itself wrote."""
    try:
        with open(os.path.join(config.model_dir(), "server.log"), "a") as handle:
            handle.write(f"\n=== {line} ===\n")
    except OSError:
        pass


def _server_process():
    """A `llama-server` running against our weights, whoever started it.

    Matched on the model path, not the port: that path is inside Balance's own
    Application Support folder, so a process holding it is ours by definition
    and cannot be somebody else's llama.cpp.
    """
    try:
        listing = subprocess.run(["ps", "-Ao", "pid=,args="], capture_output=True,
                                 text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    model = config.model_file()
    for line in listing.splitlines():
        line = line.strip()
        if "llama-server" in line and model in line:
            pid, _, args = line.partition(" ")
            try:
                return int(pid), args.strip()
            except ValueError:
                return None
    return None


def _reclaim():
    """Take the port back from a server this app left behind.

    The model server is a child process, and it only gets stopped when the
    window closes cleanly. Force-quit the app, or lose it to a crash, and three
    gigabytes stay resident with the port still answering — so the next launch
    finds something running against the right model file, calls that ready, and
    never starts its own.

    That is harmless when the leftover is configured the way this version wants.
    It is not harmless otherwise: a server that fell back to the CPU in an
    earlier session outlives the update meant to fix it, and no release can
    reach it. The user reinstalls, sees no change, and the only cure is a
    reboot nobody would think to try.

    So a leftover is kept when its flags match, and replaced when they do not.
    """
    global _reclaimed
    if _reclaimed:
        return
    _reclaimed = True

    found = _server_process()
    if not found:
        return
    pid, args = found
    wanted = " ".join(_fit_args(_level()))
    if wanted in args:
        _log(f"kept a server already running (pid {pid}): {args}")
        return

    _log(f"replacing a leftover server (pid {pid}) started as: {args}")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    # Give it a moment to let go of the port before we bind it.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not server_responding():
            return
        time.sleep(0.3)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def ensure_running():
    """Start the model server if it is not already answering.

    Returns True once it responds. Safe to call on every request: the common
    case is one HTTP call to /health against a process already up.
    """
    global _server, _fit, _fell_back
    if server_responding():
        # Answering, but not necessarily started by us and not necessarily
        # started the way this version starts one.
        if _server is None:
            _reclaim()
        if server_responding():
            return True
    binary = server_binary()
    if not binary or not model_present():
        return False

    with _lock:
        if _server and _server.poll() is None:
            pass  # Someone else started it; fall through and wait.
        else:
            # Kept, not discarded. When this failed to start there was nothing
            # anywhere to say why — the panel said "starting up" and the only
            # way to learn otherwise was to run the binary by hand.
            # Appended, not truncated. Opening this "w" meant each attempt
            # erased the one before it, so the log held the fallback and never
            # the crash that caused the fallback — which is the half worth
            # having.
            log = open(os.path.join(config.model_dir(), "server.log"), "a")
            # Which llama.cpp, and why this level. On a Mac that skipped the
            # GPU on purpose the log otherwise shows a CPU start with nothing
            # to say what chose it, which is the opacity this whole thing is
            # about: the only record of a machine running twelve times slower
            # than it should was a file with no reason in it.
            log.write(f"\n=== starting: {' '.join(_fit_args(_level()))} ===\n")
            log.write(f"=== runtime {runtime_build() or 'unknown'}, "
                      f"macOS {platform.mac_ver()[0] or 'unknown'}, "
                      f"level {_level()} of {_FIT_LEVELS - 1} ===\n")
            if _level() > 0 and not _fell_back:
                log.write("=== the GPU was not tried: this macOS needs the "
                          "metal-readback patch and this runtime has not got "
                          "it ===\n")
            log.flush()
            _server = subprocess.Popen(
                [binary,
                 "--model", config.model_file(),
                 "--host", "127.0.0.1",
                 "--port", str(config.LLAMACPP_PORT),
                 *_fit_args(_level())],
                stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ,
                     "DYLD_LIBRARY_PATH": os.path.dirname(binary)})

    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if server_responding():
            return True
        if _server.poll() is not None:
            # It died. If there is a less demanding way to run, take it and say
            # so in the log beside the crash that prompted it — and keep the
            # line, because the panel has to be able to say the assistant is
            # running on the CPU and what put it there.
            if _level() < _FIT_LEVELS - 1:
                _fell_back = server_error()
                _fit += 1
                return ensure_running()
            return False
        time.sleep(0.5)
    return False


# A line of a crash backtrace: "9   dyld   0x000000018ab9c7f28 start + 2236".
# Informative to a debugger and to nobody else.
_STACK_FRAME = re.compile(r"^\s*\d+\s+\S+\s+0x[0-9a-f]+", re.I)

# What a failure to start actually looks like, in the order worth reporting.
_TELLING = ("built for newer", "incompatible", "Library not loaded",
            "Symbol not found", "no such file", "error", "failed", "cannot")


def server_log_tail(lines=40):
    """The last of what the model server said, for when it will not start."""
    try:
        with open(os.path.join(config.model_dir(), "server.log"),
                  errors="replace") as handle:
            return "".join(handle.readlines()[-lines:]).strip()
    except OSError:
        return ""


def server_error():
    """The one line worth showing a person out of a crashing server's output.

    Taking the last line is what this did, and on a crash the last line is a
    backtrace frame — a user was told "Balance AI could not start. It said: 9
    dyld 0x000000018ab9c7f28 start + 2236", which tells them nothing and cannot
    be acted on. The real cause is further up, above the stack.
    """
    lines = [l.strip() for l in server_log_tail().splitlines() if l.strip()]
    lines = [l for l in lines if not _STACK_FRAME.match(l)]
    for needle in _TELLING:
        for line in reversed(lines):
            if needle.lower() in line.lower():
                return line
    return lines[-1] if lines else ""


def nudge():
    """Start the server in the background if it should be running.

    `runtime_state` only reports, and for a while nothing acted on what it
    reported: with the weights on disk and no server up it answered "starting"
    to every poll, forever, because the only thing that ever called
    `ensure_running` was a question — and the panel will not let you ask one
    until it says ready. A status of "starting" now has something starting
    behind it.
    """
    global _starting
    if _starting or server_responding() or not model_present():
        return
    if not server_binary():
        return
    _starting = True

    def run():
        global _starting, _failed_starts
        try:
            if ensure_running():
                _failed_starts = 0
            else:
                _failed_starts += 1
        finally:
            _starting = False

    threading.Thread(target=run, daemon=True).start()


def stop():
    """Stop the model server. Called when the app window closes."""
    global _server
    with _lock:
        process, _server = _server, None
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


# ── What the panel is told ────────────────────────────────────────────────

def runtime_state(host=None, model_path=None):
    """One dict describing exactly which of the several "not yet" this is.

    Ollama had two failure modes and the panel could name both. Owning the
    weights adds more, and they call for different things from the user: a
    download asks permission, a download in flight asks for patience, and a
    server still loading asks for nothing at all. `state` is what the panel
    switches on; `detail` is the sentence when there is nothing better to say.
    """
    path = model_path or config.model_file()
    progress = download_state()

    # Both, and in this order. A server can outlive the file it loaded — the
    # process keeps the handle open after the file is moved or deleted — so
    # "something is answering" is not enough to call this ready.
    if os.path.isfile(path) and server_responding(host):
        return _state("ready", path, configured=True,
                      detail=None, progress=progress)
    if not server_binary():
        return _state("no_runtime", path, detail=(
            "This build of Balance has no model runtime in it."), progress=progress)
    if macos_too_old():
        # Said plainly and before anything is downloaded. Otherwise this Mac
        # fetches 2.7 GB it can never use and then shows a crash backtrace.
        return _state("unsupported_os", path, progress=progress, detail=(
            f"Balance AI needs macOS {MIN_MACOS[0]}.{MIN_MACOS[1]} or later. "
            f"This Mac is on {platform.mac_ver()[0]}. The rest of Balance works "
            "as it always has."))
    if progress["state"] == "downloading":
        return _state("downloading", path, detail="Downloading the model…",
                      progress=progress)
    if progress["state"] == "failed":
        return _state("download_failed", path, progress=progress,
                      detail=f"The download stopped: {progress['error']}")
    if not model_present():
        return _state("needs_download", path, progress=progress, detail=(
            "Balance AI needs a one-off "
            f"{config.MODEL_BYTES / 1e9:.1f} GB download to run on this Mac."))
    if _failed_starts >= MAX_START_ATTEMPTS:
        # It has been tried and it is not coming up. Saying "starting up" past
        # this point is not optimism, it is a screen that never changes.
        said = server_error()
        return _state("start_failed", path, progress=progress, detail=(
            "Balance AI could not start." + (f" It said: {said}" if said else "")))

    # The file is there and the server is not answering: it is still loading,
    # which on a cold disk takes a while and asks nothing of anyone.
    return _state("starting", path, detail="Balance AI is starting up…",
                  progress=progress)


def _accelerator():
    """Where the model runs, and what put it there. Returns (what, why).

    Invisible until now, and it is the difference between an answer in fifteen
    seconds and one in two minutes. `runtime_state` said `ready` either way, so
    a Mac quietly down on its CPU looked exactly like a Mac that was fine —
    which is how one stayed that way through six releases, with the only
    evidence in a log file nobody had reason to open.
    """
    if _level() <= 0:
        return "gpu", None
    # The cause only. That it is on the CPU, and what that costs, is the
    # panel's to say — repeating it here put the same sentence on screen twice.
    if _fell_back:
        # The GPU was tried on this machine and died. Carry the line, because
        # it is the one thing that says whether this is the known crash or
        # something nobody has seen yet.
        return "cpu", f"Starting it on the graphics chip failed. It said: {_fell_back}"
    return "cpu", (f"This Mac is on macOS {platform.mac_ver()[0]}, and the model "
                   "in this build of Balance cannot use a graphics chip below "
                   f"macOS {MIN_MACOS_GPU[0]}.{MIN_MACOS_GPU[1]}. A newer "
                   "Balance carries one that can.")


def _state(state, path, configured=False, detail=None, progress=None):
    accelerator, why = _accelerator()
    return {
        "backend": "bundled",
        "state": state,
        # Which of the two ways it is running. The panel says so out loud when
        # it is the slow one: an assistant that takes two minutes and does not
        # explain itself reads as one that has hung.
        "accelerator": accelerator,
        "accelerator_detail": why,
        "runtime_build": runtime_build(),
        "configured": configured,
        # The two the panel used to switch on, kept so the old set-up card and
        # every existing test still read a truthful answer out of this.
        "reachable": state in ("ready", "starting"),
        "model_installed": os.path.isfile(path),
        "installed_models": [os.path.basename(path)] if os.path.isfile(path) else [],
        "model": config.MODEL_NAME,
        "detail": detail,
        "progress": progress or download_state(),
    }
