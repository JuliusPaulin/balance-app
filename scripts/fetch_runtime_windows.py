"""Fetch the pinned Windows x64 CPU runtime and verify it before extraction."""

import hashlib
import io
from pathlib import Path
import shutil
import tempfile
import zipfile

import requests


ROOT = Path(__file__).resolve().parents[1]
BUILD = "b10715"
MARKER = f"{BUILD}+windows-cpu-x64"
SHA256 = "b01268e9d933477f5c12a0b89d1ecde69e4b914ec4a1e0ce52ed1cfcf563d19e"
URL = (f"https://github.com/ggml-org/llama.cpp/releases/download/{BUILD}/"
       f"llama-{BUILD}-bin-win-cpu-x64.zip")
SOURCE_COMMIT = "662a0b0121a53c23b825a71e64ab6eff59b7f4d8"


def fetch_runtime(destination=None):
    destination = Path(destination or ROOT / "vendor" / "llama")
    marker = destination / "BUILD.txt"
    if (destination.joinpath("llama-server.exe").is_file()
            and marker.is_file() and marker.read_text().strip() == MARKER):
        print(f"Runtime already in {destination} ({MARKER})")
        return

    response = requests.get(URL, timeout=120)
    response.raise_for_status()
    if hashlib.sha256(response.content).hexdigest() != SHA256:
        raise RuntimeError("Windows runtime checksum mismatch; refusing to package it")
    licence = requests.get(
        f"https://raw.githubusercontent.com/ggml-org/llama.cpp/{SOURCE_COMMIT}/LICENSE",
        timeout=30)
    licence.raise_for_status()

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
        stage = Path(scratch) / "runtime"
        stage.mkdir()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            # Keep the server and every adjacent DLL, including CPU backends.
            # Flatten only regular binary files; never trust archive paths.
            for entry in archive.infolist():
                name = Path(entry.filename).name
                if (name == "llama-server.exe" or name.lower().endswith(".dll")
                        or name.startswith("LICENSE")):
                    target = stage / name
                    if target.exists():
                        raise RuntimeError(f"Duplicate runtime file: {name}")
                    target.write_bytes(archive.read(entry))
        if not (stage / "llama-server.exe").is_file():
            raise RuntimeError("The runtime archive has no llama-server.exe")
        (stage / "LICENSE-llama.cpp.txt").write_text(licence.text, encoding="utf-8")
        (stage / "BUILD.txt").write_text(MARKER + "\n", encoding="utf-8")
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(stage), str(destination))
    print(f"Verified {MARKER} in {destination}")


if __name__ == "__main__":
    fetch_runtime()
