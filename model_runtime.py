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
import shutil
import subprocess
import sys
import threading
import time

import requests

import config

# One server, one download, for the life of the process.
_server = None
_download = {"state": "idle", "bytes": 0, "total": 0, "error": None}
_lock = threading.Lock()

# How long to let the server boot before calling it stuck. Loading three
# gigabytes off a cold disk is slow the first time and quick afterwards.
STARTUP_TIMEOUT = 180


# ── Where things are ──────────────────────────────────────────────────────

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


def model_present():
    """Whether the whole model is on disk. A part-file is not a model."""
    path = config.model_file()
    return os.path.isfile(path) and os.path.getsize(path) > 0


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

        _check_room(0)
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


def ensure_running():
    """Start the model server if it is not already answering.

    Returns True once it responds. Safe to call on every request: the common
    case is one HTTP call to /health against a process already up.
    """
    global _server
    if server_responding():
        return True
    binary = server_binary()
    if not binary or not model_present():
        return False

    with _lock:
        if _server and _server.poll() is None:
            pass  # Someone else started it; fall through and wait.
        else:
            _server = subprocess.Popen(
                [binary,
                 "--model", config.model_file(),
                 "--host", "127.0.0.1",
                 "--port", str(config.LLAMACPP_PORT),
                 "--ctx-size", str(config.OLLAMA_NUM_CTX),
                 # Everything on the GPU it can be. On a Mac that is Metal, and
                 # llama.cpp quietly puts back what will not fit.
                 "--n-gpu-layers", "999"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ,
                     "DYLD_LIBRARY_PATH": os.path.dirname(binary)})

    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if server_responding():
            return True
        if _server.poll() is not None:
            return False
        time.sleep(0.5)
    return False


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
    # The file is there and the server is not answering: it is still loading,
    # which on a cold disk takes a while and asks nothing of anyone.
    return _state("starting", path, detail="Balance AI is starting up…",
                  progress=progress)


def _state(state, path, configured=False, detail=None, progress=None):
    return {
        "backend": "bundled",
        "state": state,
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
