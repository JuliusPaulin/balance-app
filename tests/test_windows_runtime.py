"""Windows-specific runtime behavior, exercised without a live model server."""

import json
import os
import signal
import subprocess
from types import SimpleNamespace

import pytest

import config
from ai import runtime as model_runtime


@pytest.fixture
def windows_runtime(tmp_path, monkeypatch):
    """A fake Windows bundle and model path, isolated from the user's data."""
    root = tmp_path / "bundle"
    runtime_dir = root / "vendor" / "llama"
    runtime_dir.mkdir(parents=True)
    binary = runtime_dir / "llama-server.exe"
    binary.write_bytes(b"fake executable")
    (runtime_dir / "BUILD.txt").write_text("b10715+windows-cpu-x64\n")

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    model = model_dir / "model.gguf"
    with model.open("wb") as handle:
        handle.write(b"GGUF")
        handle.seek(model_runtime._MIN_MODEL_BYTES)
        handle.write(b"\0")

    monkeypatch.setattr(model_runtime.platform, "system", lambda: "Windows")
    monkeypatch.setattr(model_runtime, "_bundle_root", lambda: str(root))
    monkeypatch.setattr(config, "SQLITE_PATH", str(tmp_path / "expenses.db"))
    monkeypatch.setattr(config, "model_dir", lambda: str(model_dir))
    monkeypatch.setattr(config, "model_file", lambda: str(model))
    model_runtime._server = None
    model_runtime._fit = None
    model_runtime._fell_back = None
    model_runtime._reclaimed = False
    model_runtime._download.update(state="idle", bytes=0, total=0, error=None)
    yield root, runtime_dir, binary, model
    model_runtime._server = None
    model_runtime._fit = None
    model_runtime._fell_back = None
    model_runtime._reclaimed = False


def test_windows_finds_exe_without_touching_posix_permissions(
        windows_runtime, monkeypatch):
    _, _, binary, _ = windows_runtime
    monkeypatch.setattr(model_runtime.os, "access", lambda *args: False)
    monkeypatch.setattr(model_runtime.os, "chmod",
                        lambda *args: pytest.fail("Windows must not chmod the exe"))

    assert model_runtime.server_binary() == str(binary)
    assert model_runtime.runtime_build() == "b10715+windows-cpu-x64"


def test_windows_starts_the_cpu_build_with_its_dll_directory_and_no_console(
        windows_runtime, monkeypatch):
    _, runtime_dir, binary, _ = windows_runtime
    monkeypatch.delenv("DYLD_LIBRARY_PATH", raising=False)
    launched = []
    responding = {"value": False}

    class Alive:
        @staticmethod
        def poll():
            return None

    def spawn(args, **kwargs):
        launched.append((args, kwargs))
        responding["value"] = True
        return Alive()

    monkeypatch.setattr(model_runtime.subprocess, "Popen", spawn)
    monkeypatch.setattr(model_runtime, "server_responding",
                        lambda host=None: responding["value"])

    try:
        assert model_runtime.ensure_running() is True
    finally:
        model_runtime._server = None

    args, kwargs = launched[0]
    assert args[0] == str(binary)
    assert args[args.index("--n-gpu-layers") + 1] == "0"
    assert kwargs["cwd"] == str(runtime_dir)
    assert kwargs["creationflags"] == model_runtime._windows_creation_flags()
    assert kwargs["env"]["PATH"].split(os.pathsep)[0] == str(runtime_dir)
    assert "DYLD_LIBRARY_PATH" not in kwargs["env"]


def test_windows_uses_cpu_and_explains_it_without_mac_claims(windows_runtime):
    accelerator = model_runtime._accelerator()

    assert accelerator[0] == "cpu"
    assert "Windows" in accelerator[1]
    assert "Mac" not in accelerator[1]
    assert "macOS" not in accelerator[1]
    assert model_runtime._start_level() == 1


def test_windows_first_download_message_is_platform_neutral(
        windows_runtime, monkeypatch):
    _, _, _, model = windows_runtime
    model.unlink()
    monkeypatch.setattr(model_runtime, "server_responding", lambda host=None: False)

    state = model_runtime.runtime_state()

    assert state["state"] == "needs_download"
    assert "this computer" in state["detail"]
    assert "Mac" not in state["detail"]


def test_windows_process_lookup_uses_exact_name_and_model_path(
        windows_runtime, monkeypatch):
    _, _, _, model = windows_runtime
    rows = [
        {"ProcessId": 1, "Name": "llama-server-helper.exe", "CommandLine": "helper"},
        {"ProcessId": 2, "Name": "llama-server.exe", "CommandLine": "other-model"},
        {"ProcessId": 3, "Name": "llama-server.exe", "CommandLine": "our-model"},
    ]
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(rows))

    def fake_argv(command_line):
        if command_line == "other-model":
            return [r"C:\Balance\llama-server.exe", "--model",
                    str(model) + ".backup"]
        if command_line == "our-model":
            return [r"C:\Balance\llama-server.exe", "--model", str(model)]
        return [r"C:\Balance\llama-server-helper.exe", "--model", str(model)]

    monkeypatch.setattr(model_runtime.subprocess, "run", fake_run)
    monkeypatch.setattr(model_runtime, "_windows_argv", fake_argv)

    assert model_runtime._server_process() == (3, "our-model")
    args, kwargs = calls[0]
    assert args[0] == "powershell.exe"
    assert kwargs["creationflags"] == model_runtime._windows_creation_flags()
    assert kwargs["encoding"] == "utf-8"


def test_windows_reclaim_uses_only_sigterm_for_the_verified_process(
        windows_runtime, monkeypatch):
    _, _, _, model = windows_runtime
    monkeypatch.setattr(model_runtime, "_server_process",
                        lambda: (4242, f"llama-server.exe --model {model} --n-gpu-layers 999"))
    sent = []
    monkeypatch.setattr(model_runtime.os, "kill",
                        lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(model_runtime, "server_responding",
                        lambda host=None: not sent)
    monkeypatch.setattr(model_runtime, "_log", lambda line: None)

    assert model_runtime._reclaim() is True
    assert sent == [(4242, signal.SIGTERM)]


def test_stop_waits_after_forcing_the_server_to_exit(monkeypatch):
    events = []

    class StubbornServer:
        def poll(self):
            return None

        def terminate(self):
            events.append("terminate")

        def kill(self):
            events.append("kill")

        def wait(self, timeout):
            events.append("wait")
            if events.count("wait") == 1:
                raise subprocess.TimeoutExpired("llama-server", timeout)

    server = StubbornServer()
    monkeypatch.setattr(model_runtime, "_log", lambda line: None)
    monkeypatch.setattr(model_runtime, "_server", server)

    model_runtime.stop()

    assert events == ["terminate", "wait", "kill", "wait"]
    assert model_runtime._server is None
