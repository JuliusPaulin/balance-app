"""The model that ships with the app: where it is, fetching it, running it.

The network and the child process are faked. What is being tested is the state
machine the panel switches on — which of the several kinds of "not ready" this
is — because each one asks something different of the user, and telling them
apart wrongly is how you end up asking someone to download a file they already
have.
"""

import os

import pytest

import config
import model_runtime


def write_model(path):
    """A file that passes for a model: the GGUF magic and a plausible size.

    Sparse, so it costs no disk. The size matters because a truncated download
    used to pass as a whole one — see the download tests below.
    """
    with open(path, "wb") as handle:
        handle.write(b"GGUF")
        handle.seek(model_runtime._MIN_MODEL_BYTES)
        handle.write(b"\0")


@pytest.fixture
def models_dir(tmp_path, monkeypatch):
    """A throwaway model directory, and no download in flight."""
    monkeypatch.setattr(config, "SQLITE_PATH", str(tmp_path / "expenses.db"))
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "models" / "model.gguf"))
    os.makedirs(tmp_path / "models", exist_ok=True)
    model_runtime._download.update(state="idle", bytes=0, total=0, error=None)
    model_runtime._reclaimed = False
    return tmp_path / "models"


@pytest.fixture
def no_server(monkeypatch):
    monkeypatch.setattr(model_runtime, "server_responding", lambda host=None: False)


@pytest.fixture
def runtime_present(monkeypatch, tmp_path):
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(model_runtime, "server_binary", lambda: str(binary))


# ── Which "not ready" is it ───────────────────────────────────────────────

def test_a_running_server_with_its_model_on_disk_is_ready(
        models_dir, runtime_present, monkeypatch):
    """Both, and that is the point — see the outliving-its-file test below."""
    write_model(models_dir / "model.gguf")
    monkeypatch.setattr(model_runtime, "server_responding", lambda host=None: True)
    state = model_runtime.runtime_state()
    assert state["state"] == "ready"
    assert state["configured"] is True


def test_a_first_run_asks_for_the_download(models_dir, runtime_present, no_server):
    """The only one of these the user can act on, so the only one with a button."""
    state = model_runtime.runtime_state()
    assert state["state"] == "needs_download"
    assert state["configured"] is False
    assert "GB" in state["detail"]


def test_a_download_in_flight_asks_for_patience(models_dir, runtime_present, no_server):
    model_runtime._download.update(state="downloading", bytes=5, total=10)
    state = model_runtime.runtime_state()
    assert state["state"] == "downloading"
    assert state["progress"]["bytes"] == 5


def test_a_present_model_with_no_server_yet_is_merely_starting(
        models_dir, runtime_present, no_server):
    """Three gigabytes take a few seconds off a cold disk, and that asks nothing
    of anyone — it must not read as a failure or as a second download."""
    write_model(models_dir / "model.gguf")
    state = model_runtime.runtime_state()
    assert state["state"] == "starting"
    assert state["configured"] is False


def test_a_failed_download_says_so_and_keeps_what_it_had(
        models_dir, runtime_present, no_server):
    model_runtime._download.update(state="failed", error="the network went away")
    state = model_runtime.runtime_state()
    assert state["state"] == "download_failed"
    assert "the network went away" in state["detail"]


def test_a_build_with_no_runtime_says_that_rather_than_offering_a_download(
        models_dir, no_server, monkeypatch):
    monkeypatch.setattr(model_runtime, "server_binary", lambda: None)
    assert model_runtime.runtime_state()["state"] == "no_runtime"


# ── The file itself ───────────────────────────────────────────────────────

def test_a_half_finished_download_is_not_a_model(models_dir):
    """The part file is kept on purpose so the next attempt resumes from it, and
    it must never be mistaken for the real thing."""
    (models_dir / "model.gguf.part").write_bytes(b"\0" * 1000)
    assert model_runtime.model_present() is False


def test_the_licence_travels_with_the_weights(models_dir):
    """Apache 2.0 lets us redistribute the model and asks that its terms and the
    attribution go with it. Copied from the bundle, not fetched: the first
    version downloaded them from the model's repository, which declares the
    licence in its metadata and ships no licence file, so it 404'd and the
    obligation quietly went unmet.
    """
    model_runtime._place_licence()
    assert model_runtime.licences_present()
    assert "Apache License" in (models_dir / "Apache-2.0.txt").read_text()
    assert "Qwen" in (models_dir / "NOTICE-model.txt").read_text()


# ── Running it ────────────────────────────────────────────────────────────

def test_a_missing_model_does_not_start_a_server(models_dir, runtime_present, no_server):
    assert model_runtime.ensure_running() is False


def test_an_already_answering_server_is_not_started_twice(models_dir, monkeypatch):
    monkeypatch.setattr(model_runtime, "server_responding", lambda host=None: True)
    monkeypatch.setattr(model_runtime, "_server_process", lambda: None)
    started = []
    monkeypatch.setattr(model_runtime.subprocess, "Popen",
                        lambda *a, **k: started.append(a) or None)
    assert model_runtime.ensure_running() is True
    assert started == []


# ── The server the last session left behind ───────────────────────────────
#
# It is a child process, stopped only when the window closes cleanly. Force-quit
# the app and it stays resident with the port still answering, which the next
# launch reads as "ready" — so a server that fell back to the CPU in an earlier
# session survives every update meant to fix it, and only a reboot clears it.

def test_a_leftover_server_started_our_way_is_kept(models_dir, monkeypatch):
    """No reason to spend twenty seconds reloading a server already right."""
    args = f"llama-server --model {config.model_file()} " + \
        " ".join(model_runtime._fit_args(0))
    monkeypatch.setattr(model_runtime, "server_responding", lambda host=None: True)
    monkeypatch.setattr(model_runtime, "_server_process", lambda: (4242, args))
    killed = []
    monkeypatch.setattr(model_runtime.os, "kill",
                        lambda pid, sig: killed.append(pid))

    assert model_runtime.ensure_running() is True
    assert killed == []


def test_a_leftover_server_started_differently_is_replaced(
        models_dir, runtime_present, monkeypatch):
    """The one that matters: an old fallback on the CPU, outliving its fix."""
    write_model(models_dir / "model.gguf")
    args = f"llama-server --model {config.model_file()} --n-gpu-layers 0"
    monkeypatch.setattr(model_runtime, "_server_process", lambda: (4242, args))
    killed, started = [], []
    monkeypatch.setattr(model_runtime.os, "kill",
                        lambda pid, sig: killed.append(pid))
    # Answering until it is killed, and again once ours is up in its place.
    monkeypatch.setattr(model_runtime, "server_responding",
                        lambda host=None: not killed or bool(started))

    class Alive:
        def poll(self):
            return None
    monkeypatch.setattr(model_runtime.subprocess, "Popen",
                        lambda *a, **k: started.append(a) or Alive())

    try:
        assert model_runtime.ensure_running() is True
        assert killed == [4242]
        assert started, "our own server was never started in its place"
        assert "--n-gpu-layers" in started[0][0]
        assert started[0][0][started[0][0].index("--n-gpu-layers") + 1] == "999"
    finally:
        model_runtime._server = None


def test_a_leftover_is_looked_for_once_not_on_every_poll(models_dir, monkeypatch):
    looks = []
    monkeypatch.setattr(model_runtime, "server_responding", lambda host=None: True)
    monkeypatch.setattr(model_runtime, "_server_process",
                        lambda: looks.append(1) or None)

    model_runtime.ensure_running()
    model_runtime.ensure_running()
    model_runtime.ensure_running()
    assert len(looks) == 1


def test_a_server_this_process_started_is_never_reclaimed(models_dir, monkeypatch):
    """Only a leftover is a candidate — not the one we are already running."""
    monkeypatch.setattr(model_runtime, "server_responding", lambda host=None: True)
    monkeypatch.setattr(model_runtime, "_server_process",
                        lambda: pytest.fail("looked at a server it started itself"))

    class Alive:
        def poll(self):
            return None
    monkeypatch.setattr(model_runtime, "_server", Alive())
    try:
        assert model_runtime.ensure_running() is True
    finally:
        model_runtime._server = None


# ── Whose server is that ──────────────────────────────────────────────────

def test_a_server_serving_a_different_model_is_not_ours(models_dir, monkeypatch):
    """A port is not an identity.

    This checked `/health`, which says yes to anything listening. A server left
    over from an earlier session answered it, so the panel called itself ready
    and offered a chat box with no model on disk at all — `state: ready` beside
    `model_installed: false` in the same reply.
    """
    class Reply:
        ok = True
        @staticmethod
        def json():
            return {"model_path": "/somewhere/else/other-model.gguf"}
    monkeypatch.setattr(model_runtime.requests, "get", lambda *a, **k: Reply())
    assert model_runtime.server_responding() is False


def test_a_server_serving_our_model_is_ours(models_dir, monkeypatch):
    class Reply:
        ok = True
        @staticmethod
        def json():
            return {"model_path": config.model_file()}
    monkeypatch.setattr(model_runtime.requests, "get", lambda *a, **k: Reply())
    assert model_runtime.server_responding() is True


def test_a_server_outliving_its_model_file_is_not_ready(
        models_dir, runtime_present, monkeypatch):
    """A process keeps its handle after the file is moved, so it goes on
    answering for a model that is no longer there."""
    monkeypatch.setattr(model_runtime, "server_responding", lambda host=None: True)
    # Nothing on disk: the file was moved out from under the running server.
    state = model_runtime.runtime_state()
    assert state["state"] != "ready"
    assert state["configured"] is False


# ── Something has to actually start it ────────────────────────────────────

def test_a_starting_state_has_something_starting_behind_it(
        models_dir, runtime_present, no_server, monkeypatch):
    """The panel said "starting up" for ever and nothing was.

    `runtime_state` only reports, and the one thing that called
    `ensure_running` was a question — which the panel will not let you ask
    until it says ready. So with the weights on disk and no server up, every
    poll answered "starting" and nothing acted on it.
    """
    model_runtime._starting = False
    write_model(models_dir / "model.gguf")
    started = []
    monkeypatch.setattr(model_runtime, "ensure_running",
                        lambda: started.append(True))

    assert model_runtime.runtime_state()["state"] == "starting"
    model_runtime.nudge()
    for _ in range(50):
        if started:
            break
        import time
        time.sleep(0.02)
    assert started == [True]


def test_a_start_already_in_flight_is_not_started_again(
        models_dir, runtime_present, no_server, monkeypatch):
    """The panel polls every two seconds; that must not queue a spawn a poll."""
    write_model(models_dir / "model.gguf")
    monkeypatch.setattr(model_runtime, "_starting", True)
    started = []
    monkeypatch.setattr(model_runtime, "ensure_running",
                        lambda: started.append(True))
    model_runtime.nudge()
    assert started == []


# ── A download that stopped early ─────────────────────────────────────────

def test_a_truncated_file_is_not_a_model(models_dir):
    """The failure a user actually hit.

    A stream can end early without raising — a dropped connection, a server
    closing mid-file — and this used to ask only whether the file existed and
    was non-empty. So half a model passed as a whole one, `llama-server` could
    not load it, and the panel said "starting up" for ever.
    """
    (models_dir / "model.gguf").write_bytes(b"GGUF" + b"\0" * 1000)
    assert model_runtime.model_present() is False


def test_a_file_that_is_not_a_gguf_is_not_a_model(models_dir):
    """An error page saved under the model's name is not a model."""
    with open(models_dir / "model.gguf", "wb") as handle:
        handle.write(b"<html>404</html>")
        handle.seek(model_runtime._MIN_MODEL_BYTES)
        handle.write(b"\0")
    assert model_runtime.model_present() is False


# ── A start that keeps failing ────────────────────────────────────────────

def test_a_server_that_will_not_start_stops_saying_it_is_starting(
        models_dir, runtime_present, no_server, monkeypatch):
    """"Starting up" for ever is the least useful thing a screen can say."""
    write_model(models_dir / "model.gguf")
    monkeypatch.setattr(model_runtime, "_failed_starts",
                        model_runtime.MAX_START_ATTEMPTS)
    state = model_runtime.runtime_state()
    assert state["state"] == "start_failed"
    assert "could not start" in state["detail"]


def test_a_first_failure_is_still_just_starting(
        models_dir, runtime_present, no_server, monkeypatch):
    """A cold disk or a machine busy from launch deserves a second try."""
    write_model(models_dir / "model.gguf")
    monkeypatch.setattr(model_runtime, "_failed_starts", 1)
    assert model_runtime.runtime_state()["state"] == "starting"


# ── When the Mac itself is the problem ────────────────────────────────────

def test_an_old_macos_is_told_before_anything_is_downloaded(
        models_dir, runtime_present, no_server, monkeypatch):
    """Balance runs on any Apple silicon Mac; the bundled runtime needs 13.3.

    So there is a range of machines where the app is fine and the assistant
    cannot start. Without this the Mac fetches 2.7 GB it can never use and then
    shows a dyld backtrace, which is a miserable way to learn about a version
    requirement.
    """
    monkeypatch.setattr(model_runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(model_runtime.platform, "mac_ver",
                        lambda: ("12.7.1", ("", "", ""), "arm64"))
    state = model_runtime.runtime_state()
    assert state["state"] == "unsupported_os"
    assert "13.3" in state["detail"] and "12.7.1" in state["detail"]


def test_a_supported_macos_is_left_alone(monkeypatch):
    monkeypatch.setattr(model_runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(model_runtime.platform, "mac_ver",
                        lambda: ("13.3", ("", "", ""), "arm64"))
    assert model_runtime.macos_too_old() is False


def test_an_unreadable_version_is_not_treated_as_too_old(monkeypatch):
    """Never guess a machine out of a feature it might be able to run."""
    monkeypatch.setattr(model_runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(model_runtime.platform, "mac_ver",
                        lambda: ("", ("", "", ""), ""))
    assert model_runtime.macos_too_old() is False


# ── Saying what actually went wrong ───────────────────────────────────────

def test_a_crash_reports_its_cause_not_its_stack(models_dir, monkeypatch):
    """A user was told: "It said: 9 dyld 0x00000001ab9c7f28 start + 2236".

    That is a backtrace frame. It cannot be acted on, and the real cause was
    sitting a few lines above it.
    """
    log = (models_dir / "server.log")
    log.write_text(
        "ggml_metal_init: loaded kernels\n"
        "dyld[4711]: Library not loaded: @rpath/libggml-base.dylib\n"
        "  Referenced from: llama-server\n"
        "0   llama-server  0x0000000104a1c000 main + 12\n"
        "9   dyld          0x00000001ab9c7f28 start + 2236\n")
    assert model_runtime.server_error() == (
        "dyld[4711]: Library not loaded: @rpath/libggml-base.dylib")


def test_an_ordinary_last_line_is_still_used(models_dir):
    """No stack, no known phrase — then the last thing said is the best there is."""
    (models_dir / "server.log").write_text("loading model\nout of memory\n")
    assert model_runtime.server_error() == "out of memory"


# ── A server that will not run one way, run another ───────────────────────

def test_a_crashing_server_is_retried_with_less_of_the_gpu(
        models_dir, runtime_present, monkeypatch):
    """A 16 GB Air died inside Metal — `GGML_ASSERT(buf_dst) failed` — which is
    a precondition of `newBufferWithBytesNoCopy:` failing, not a shortage of
    memory. No argument avoids it reliably, so the last level keeps the model
    off the GPU and leaves that code path unused."""
    write_model(models_dir / "model.gguf")
    monkeypatch.setattr(model_runtime, "_fit", 0)
    monkeypatch.setattr(model_runtime, "server_responding", lambda host=None: False)

    tried = []

    class Dead:
        @staticmethod
        def poll():
            return 1  # exited immediately, as a crash does

    def spawn(args, **kwargs):
        tried.append(args)
        return Dead()

    monkeypatch.setattr(model_runtime.subprocess, "Popen", spawn)
    assert model_runtime.ensure_running() is False

    # Every level was tried, ending with the model off the GPU entirely.
    assert len(tried) == model_runtime._FIT_LEVELS
    assert tried[0][tried[0].index("--n-gpu-layers") + 1] == "999"
    assert tried[-1][tried[-1].index("--n-gpu-layers") + 1] == "0"


def test_the_first_attempt_is_the_one_that_works_everywhere(models_dir):
    """The GPU, with the model read in rather than mapped.

    Mapping it is what Metal objects to, and that used to be the default with
    this as the fallback — which put a crash between an affected Mac and a
    working assistant. One never got past it and ran on its CPU at 26 tokens a
    second, while the same binary with these flags started on its GPU in
    seconds. So the crash is not on the path any more.
    """
    args = model_runtime._fit_args(0)
    assert "--load-mode" in args and args[args.index("--load-mode") + 1] == "none"
    assert args[args.index("--n-gpu-layers") + 1] == "999"


def test_no_fallback_ever_starves_the_context(models_dir):
    """The context is not one of the things that may be given up.

    A turn carries the system prompt, the tool schemas and a tool result —
    about 5 100 tokens at the smallest, and past 10 000 for a whole month read
    at once. The first step-down halved it to 4 096 to save memory, which is
    under the floor: the server started, took a long time, and answered nothing
    at all. It traded a crash for silence, which is worse.
    """
    for level in range(model_runtime._FIT_LEVELS):
        args = model_runtime._fit_args(level)
        ctx = int(args[args.index("--ctx-size") + 1])
        assert ctx >= model_runtime.MIN_CTX, f"level {level} would starve the model"


def test_only_the_gpu_is_negotiable(models_dir):
    """Every level runs the same size of conversation; they differ in where."""
    sizes = {tuple(model_runtime._fit_args(l)[:2])
             for l in range(model_runtime._FIT_LEVELS)}
    assert len(sizes) == 1


def test_the_model_is_never_memory_mapped(models_dir):
    """Not at any level. It is the one thing that crashes Metal here."""
    for level in range(model_runtime._FIT_LEVELS):
        args = model_runtime._fit_args(level)
        assert args[args.index("--load-mode") + 1] == "none"


def test_the_cpu_is_the_last_resort_and_only_that(models_dir):
    """26 tokens a second against 310 — slow enough is the same as broken."""
    on_gpu = [l for l in range(model_runtime._FIT_LEVELS)
              if "999" in model_runtime._fit_args(l)]
    assert on_gpu == [0]
    assert model_runtime._fit_args(model_runtime._FIT_LEVELS - 1)[-1] == "0"
