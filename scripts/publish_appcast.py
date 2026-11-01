#!/usr/bin/env python3
"""Create a version-pinned, EdDSA-signed Sparkle appcast for a release."""

from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "services" / "update_config.json"
VERSION_PATH = ROOT / "VERSION"
SPARKLE_NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"
MINIMUM_MACOS = "12.0.0"
ARCHIVE_NAME = "Balance-macOS-arm64.zip"
ET.register_namespace("sparkle", SPARKLE_NS)


def load_config(path: Path = CONFIG_PATH) -> dict[str, str]:
    config = json.loads(path.read_text(encoding="utf-8"))
    public_key = config.get("public_key")
    if not isinstance(public_key, str) or not public_key:
        raise ValueError("Update configuration is missing public_key")
    return config


def load_keypair(private_key_b64: str, public_key_b64: str) -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    try:
        private_bytes = base64.b64decode(private_key_b64.strip(), validate=True)
        public_bytes = base64.b64decode(public_key_b64.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Sparkle signing keys must be base64 encoded") from exc
    if len(private_bytes) != 32:
        raise ValueError("SPARKLE_PRIVATE_KEY must contain a 32-byte Ed25519 seed")
    if len(public_bytes) != 32:
        raise ValueError("Embedded Sparkle public key must contain 32 bytes")

    private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    derived_public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    if not hmac.compare_digest(derived_public, public_bytes):
        raise ValueError("SPARKLE_PRIVATE_KEY does not match the app's embedded Sparkle public key")
    return private_key, Ed25519PublicKey.from_public_bytes(public_bytes)


def validate_release_version(version: str, tag: str, expected_version: str) -> None:
    expected_version = expected_version.strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"Release version must be major.minor.patch, got {version!r}")
    if version != expected_version:
        raise ValueError(f"Release version {version!r} does not match VERSION {expected_version!r}")
    if tag != f"v{version}":
        raise ValueError(f"Release tag {tag!r} does not match version {version!r}")


def verify_archive_signature(
    archive: Path,
    signature_b64: str,
    public_key: Ed25519PublicKey,
) -> None:
    try:
        signature = base64.b64decode(signature_b64, validate=True)
        public_key.verify(signature, archive.read_bytes())
    except (binascii.Error, InvalidSignature) as exc:
        raise ValueError("Sparkle archive signature does not verify with the embedded public key") from exc


def create_appcast(
    *,
    version: str,
    tag: str,
    repo: str,
    archive_name: str,
    archive_length: int,
    signature_b64: str,
    pub_date: datetime | None = None,
) -> bytes:
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = "Balance"
    ET.SubElement(channel, "link").text = f"https://github.com/{repo}/releases/latest"
    ET.SubElement(channel, "description").text = "Balance for macOS updates"
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = f"Balance {version}"
    ET.SubElement(item, "link").text = f"https://github.com/{repo}/releases/tag/{tag}"
    ET.SubElement(item, f"{{{SPARKLE_NS}}}version").text = version
    ET.SubElement(item, f"{{{SPARKLE_NS}}}minimumSystemVersion").text = MINIMUM_MACOS
    date = pub_date or datetime.now(timezone.utc)
    if date.tzinfo is None:
        raise ValueError("Appcast publish date must include a timezone")
    ET.SubElement(item, "pubDate").text = format_datetime(date.astimezone(timezone.utc))
    ET.SubElement(item, "enclosure", {
        "url": f"https://github.com/{repo}/releases/download/{tag}/{archive_name}",
        f"{{{SPARKLE_NS}}}edSignature": signature_b64,
        "length": str(archive_length),
        "type": "application/octet-stream",
    })
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _run_sign_update(
    sign_update: Path,
    private_key_b64: str,
    args: list[str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(sign_update), "--ed-key-file", "-", *args],
        input=private_key_b64.strip() + "\n",
        text=True,
        capture_output=True,
        check=True,
    )


def archive_signature(
    archive: Path,
    sign_update: Path,
    private_key_b64: str,
    private_key: Ed25519PrivateKey,
    public_key: Ed25519PublicKey,
) -> str:
    result = _run_sign_update(sign_update, private_key_b64, ["-p", str(archive)])
    signature_b64 = result.stdout.strip()
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Sparkle sign_update did not return a base64 archive signature") from exc
    expected = private_key.sign(archive.read_bytes())
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Sparkle sign_update returned a signature that does not match the release key")
    verify_archive_signature(archive, signature_b64, public_key)
    return signature_b64


def publish(
    *,
    archive: Path,
    version: str,
    tag: str,
    repo: str,
    sign_update: Path,
    output: Path,
    private_key_b64: str,
) -> None:
    expected_version = VERSION_PATH.read_text(encoding="utf-8").strip()
    validate_release_version(version, tag, expected_version)
    if not archive.is_file() or archive.stat().st_size == 0:
        raise ValueError(f"Release archive is missing or empty: {archive}")
    config = load_config()
    private_key, public_key = load_keypair(private_key_b64, config["public_key"])
    signature = archive_signature(archive, sign_update, private_key_b64, private_key, public_key)
    xml_bytes = create_appcast(
        version=version,
        tag=tag,
        repo=repo,
        archive_name=ARCHIVE_NAME,
        archive_length=archive.stat().st_size,
        signature_b64=signature,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(xml_bytes)

    # Sparkle 2.9+ validates signed feeds before showing update information.
    _run_sign_update(sign_update, private_key_b64, [str(output)])
    _run_sign_update(sign_update, private_key_b64, ["--verify", str(output)])
    ET.parse(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="Final signed app archive")
    parser.add_argument("--version", default=VERSION_PATH.read_text(encoding="utf-8").strip())
    parser.add_argument("--tag", default=os.environ.get("GITHUB_REF_NAME"))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "JuliusPaulin/balance-app"))
    parser.add_argument("--sign-update", type=Path, default=ROOT / "vendor/sparkle/bin/sign_update")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/appcast.xml")
    parser.add_argument(
        "--validate-key-only", action="store_true",
        help="Check that SPARKLE_PRIVATE_KEY matches the embedded public key, then exit",
    )
    args = parser.parse_args()
    secret = os.environ.get("SPARKLE_PRIVATE_KEY", "").strip()
    if not secret:
        raise SystemExit("SPARKLE_PRIVATE_KEY is required")
    config = load_config()
    load_keypair(secret, config["public_key"])
    if args.validate_key_only:
        print("Sparkle signing key matches the app's embedded public key")
        return
    if args.archive is None:
        parser.error("--archive is required unless --validate-key-only is used")
    tag = args.tag or f"v{args.version}"
    publish(
        archive=args.archive.resolve(),
        version=args.version,
        tag=tag,
        repo=args.repo,
        sign_update=args.sign_update.resolve(),
        output=args.output.resolve(),
        private_key_b64=secret,
    )
    print(f"Signed appcast written to {args.output}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Appcast publishing failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
