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


@pytest.fixture
def models_dir(tmp_path, monkeypatch):
    """A throwaway model directory, and no download in flight."""
    monkeypatch.setattr(config, "SQLITE_PATH", str(tmp_path / "expenses.db"))
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "models" / "model.gguf"))
    os.makedirs(tmp_path / "models", exist_ok=True)
    model_runtime._download.update(state="idle", bytes=0, total=0, error=None)
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
    (models_dir / "model.gguf").write_bytes(b"GGUF" + b"\0" * 100)
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
    (models_dir / "model.gguf").write_bytes(b"GGUF" + b"\0" * 100)
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
    started = []
    monkeypatch.setattr(model_runtime.subprocess, "Popen",
                        lambda *a, **k: started.append(a) or None)
    assert model_runtime.ensure_running() is True
    assert started == []


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
    (models_dir / "model.gguf").write_bytes(b"GGUF" + b"\0" * 100)
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
    (models_dir / "model.gguf").write_bytes(b"GGUF" + b"\0" * 100)
    monkeypatch.setattr(model_runtime, "_starting", True)
    started = []
    monkeypatch.setattr(model_runtime, "ensure_running",
                        lambda: started.append(True))
    model_runtime.nudge()
    assert started == []
