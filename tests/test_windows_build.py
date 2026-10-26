"""Downloaded runtimes must be intact and carry their licence notices."""

import hashlib
import io
import zipfile
from types import SimpleNamespace

import pytest

from scripts import fetch_runtime_windows as runtime_fetch


def archive_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return buffer.getvalue()


def mock_download(monkeypatch, archive):
    def get(url, **kwargs):
        return SimpleNamespace(content=archive, text="MIT licence notice",
                               raise_for_status=lambda: None)
    monkeypatch.setattr(runtime_fetch.requests, "get", get)


def test_checksum_failure_preserves_existing_runtime(tmp_path, monkeypatch):
    destination = tmp_path / "llama"
    destination.mkdir()
    existing = destination / "llama-server.exe"
    existing.write_bytes(b"old working runtime")
    mock_download(monkeypatch, b"corrupt download")
    with pytest.raises(RuntimeError, match="checksum"):
        runtime_fetch.fetch_runtime(destination)
    assert existing.read_bytes() == b"old working runtime"


def test_archive_keeps_server_dlls_and_upstream_licences(tmp_path, monkeypatch):
    archive = archive_bytes({"llama-server.exe": b"server", "ggml.dll": b"DLL",
                             "LICENSE-LLVM-OpenMP": b"LLVM notice",
                             "llama-cli.exe": b"unneeded CLI"})
    monkeypatch.setattr(runtime_fetch, "SHA256", hashlib.sha256(archive).hexdigest())
    mock_download(monkeypatch, archive)
    destination = tmp_path / "llama"
    runtime_fetch.fetch_runtime(destination)
    assert (destination / "llama-server.exe").read_bytes() == b"server"
    assert (destination / "ggml.dll").read_bytes() == b"DLL"
    assert (destination / "LICENSE-LLVM-OpenMP").read_bytes() == b"LLVM notice"
    assert (destination / "LICENSE-llama.cpp.txt").read_text() == "MIT licence notice"
    assert not (destination / "llama-cli.exe").exists()


def test_archive_missing_server_preserves_existing_runtime(tmp_path, monkeypatch):
    archive = archive_bytes({"ggml.dll": b"DLL"})
    monkeypatch.setattr(runtime_fetch, "SHA256", hashlib.sha256(archive).hexdigest())
    mock_download(monkeypatch, archive)
    destination = tmp_path / "llama"
    destination.mkdir()
    (destination / "BUILD.txt").write_text("existing build")
    with pytest.raises(RuntimeError, match="no llama-server"):
        runtime_fetch.fetch_runtime(destination)
    assert (destination / "BUILD.txt").read_text() == "existing build"
