#!/usr/bin/env python3
"""Copy Sparkle into a completed PyInstaller app and seal the outer bundle."""

from __future__ import annotations

import argparse
import json
import plistlib
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "services" / "update_config.json"
MINIMUM_MACOS = "12.0.0"


def load_update_config(path: Path = CONFIG_PATH) -> dict[str, str]:
    config = json.loads(path.read_text(encoding="utf-8"))
    for name in ("feed_url", "public_key", "sparkle_version"):
        if not isinstance(config.get(name), str) or not config[name].strip():
            raise ValueError(f"Update configuration is missing {name!r}")
    return config


def copy_framework(source: Path, destination: Path, expected_version: str) -> None:
    info_path = source / "Resources" / "Info.plist"
    with info_path.open("rb") as stream:
        framework_version = plistlib.load(stream).get("CFBundleShortVersionString")
    if framework_version != expected_version:
        raise ValueError(
            f"Sparkle framework reports {framework_version!r}; expected {expected_version!r}"
        )
    if destination.exists() or destination.is_symlink():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    if shutil.which("ditto"):
        subprocess.run(["ditto", str(source), str(destination)], check=True)
    else:
        shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)


def write_update_settings(app: Path, config: dict[str, str]) -> None:
    plist_path = app / "Contents" / "Info.plist"
    with plist_path.open("rb") as stream:
        info = plistlib.load(stream)
    info.update({
        "LSMinimumSystemVersion": MINIMUM_MACOS,
        "SUFeedURL": config["feed_url"],
        "SUPublicEDKey": config["public_key"],
        "SUEnableAutomaticChecks": True,
        "SUAutomaticallyUpdate": False,
        "SUVerifyUpdateBeforeExtraction": True,
        "SURequireSignedFeed": True,
        "SUEnableSystemProfiling": False,
    })
    with plist_path.open("wb") as stream:
        plistlib.dump(info, stream, fmt=plistlib.FMT_BINARY, sort_keys=True)


def copy_notices(distribution: Path, resources: Path) -> None:
    license_path = distribution / "LICENSE"
    if not license_path.is_file():
        raise FileNotFoundError(f"Sparkle licence is missing from {distribution}")
    resources.mkdir(parents=True, exist_ok=True)
    for name in ("LICENSE", "NOTICE"):
        source = distribution / name
        if source.is_file():
            shutil.copy2(source, resources / f"Sparkle-{name}.txt")


def sign_and_verify(app: Path, framework: Path) -> None:
    # The framework carries valid signatures on its XPC services and helpers.
    # Signing the host without --deep keeps those signatures intact.
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
    subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
        check=True,
    )
    subprocess.run(
        ["codesign", "--verify", "--strict", "--verbose=2", str(framework)],
        check=True,
    )


def bundle_sparkle(app: Path, sparkle_root: Path = ROOT / "vendor" / "sparkle") -> None:
    config = load_update_config()
    framework_source = sparkle_root / "Sparkle.framework"
    framework_destination = app / "Contents" / "Frameworks" / "Sparkle.framework"
    if not framework_source.is_dir():
        raise FileNotFoundError(
            f"{framework_source} is missing. Run scripts/fetch_sparkle.sh first."
        )
    copy_framework(framework_source, framework_destination, config["sparkle_version"])
    write_update_settings(app, config)
    copy_notices(sparkle_root, app / "Contents" / "Resources")
    sign_and_verify(app, framework_destination)
    print(f"Sparkle {config['sparkle_version']} bundled and verified in {app}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path, help="Completed Balance.app bundle")
    parser.add_argument(
        "--sparkle-root", type=Path, default=ROOT / "vendor" / "sparkle",
        help="Fetched Sparkle distribution directory",
    )
    args = parser.parse_args()
    bundle_sparkle(args.app.resolve(), args.sparkle_root.resolve())


if __name__ == "__main__":
    main()
