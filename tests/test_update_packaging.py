import base64
import plistlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import bundle_sparkle, publish_appcast


SPARKLE_NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"


def keypair():
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return (
        private_key,
        base64.b64encode(private_bytes).decode("ascii"),
        base64.b64encode(public_bytes).decode("ascii"),
    )


def test_appcast_pins_tagged_archive_and_verifies_its_signature(tmp_path):
    private_key, private_b64, public_b64 = keypair()
    loaded_private, public_key = publish_appcast.load_keypair(private_b64, public_b64)
    assert loaded_private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    ) == public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )

    archive = tmp_path / "Balance-macOS-arm64.zip"
    archive.write_bytes(b"signed Balance archive")
    signature = base64.b64encode(private_key.sign(archive.read_bytes())).decode("ascii")
    publish_appcast.verify_archive_signature(archive, signature, public_key)

    feed = publish_appcast.create_appcast(
        version="1.15.0",
        tag="v1.15.0",
        repo="JuliusPaulin/balance-app",
        archive_name="Balance-macOS-arm64.zip",
        archive_length=archive.stat().st_size,
        signature_b64=signature,
        pub_date=datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc),
    )
    root = ET.fromstring(feed)
    item = root.find("./channel/item")
    assert item is not None
    assert item.findtext(f"{{{SPARKLE_NS}}}version") == "1.15.0"
    assert item.findtext(f"{{{SPARKLE_NS}}}minimumSystemVersion") == "12.0.0"
    enclosure = item.find("enclosure")
    assert enclosure is not None
    assert enclosure.attrib["url"] == (
        "https://github.com/JuliusPaulin/balance-app/releases/download/"
        "v1.15.0/Balance-macOS-arm64.zip"
    )
    assert enclosure.attrib[f"{{{SPARKLE_NS}}}edSignature"] == signature
    assert enclosure.attrib["length"] == str(archive.stat().st_size)


def test_private_key_must_match_the_embedded_public_key():
    _, private_b64, _ = keypair()
    _, _, other_public_b64 = keypair()
    with pytest.raises(ValueError, match="does not match"):
        publish_appcast.load_keypair(private_b64, other_public_b64)


def test_release_tag_and_version_must_match_version_file():
    publish_appcast.validate_release_version("1.15.0", "v1.15.0", "1.15.0\n")
    with pytest.raises(ValueError, match="does not match VERSION"):
        publish_appcast.validate_release_version("1.15.0", "v1.15.0", "1.14.0")
    with pytest.raises(ValueError, match="does not match version"):
        publish_appcast.validate_release_version("1.15.0", "v1.14.0", "1.15.0")


def test_archive_signature_mismatch_is_rejected(tmp_path):
    private_key, _, public_b64 = keypair()
    public_key = publish_appcast.load_keypair(
        base64.b64encode(
            private_key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ).decode("ascii"),
        public_b64,
    )[1]
    archive = tmp_path / "Balance.zip"
    archive.write_bytes(b"release archive")
    wrong_signature = base64.b64encode(Ed25519PrivateKey.generate().sign(b"release archive")).decode("ascii")
    with pytest.raises(ValueError, match="does not verify"):
        publish_appcast.verify_archive_signature(archive, wrong_signature, public_key)


def test_bundle_copy_keeps_sparkle_symlinks_and_writes_update_settings(tmp_path):
    source = tmp_path / "source" / "Sparkle.framework"
    version_dir = source / "Versions" / "B"
    resources = version_dir / "Resources"
    resources.mkdir(parents=True)
    with (resources / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleShortVersionString": "2.10.1"}, stream)
    (resources / "framework-resource.txt").write_text("resource")
    try:
        (source / "Versions" / "Current").symlink_to("B")
        (source / "Resources").symlink_to("Versions/Current/Resources")
    except (NotImplementedError, OSError):
        pytest.skip("This platform does not permit creating symlinks")

    destination = tmp_path / "app" / "Contents" / "Frameworks" / "Sparkle.framework"
    bundle_sparkle.copy_framework(source, destination, "2.10.1")
    assert (destination / "Resources").is_symlink()
    assert (destination / "Versions" / "Current").is_symlink()
    assert (destination / "Resources" / "framework-resource.txt").read_text() == "resource"

    app = destination.parents[2]
    plist_path = app / "Contents" / "Info.plist"
    with plist_path.open("wb") as stream:
        plistlib.dump({"CFBundleIdentifier": "example.balance.test"}, stream)
    config = {
        "feed_url": "https://example.test/appcast.xml",
        "public_key": "public-key",
    }
    bundle_sparkle.write_update_settings(app, config)
    with plist_path.open("rb") as stream:
        info = plistlib.load(stream)
    assert info["CFBundleIdentifier"] == "example.balance.test"
    assert info["SUFeedURL"] == config["feed_url"]
    assert info["SUPublicEDKey"] == config["public_key"]
    assert info["SUEnableAutomaticChecks"] is True
    assert info["SUAutomaticallyUpdate"] is False
    assert info["SUVerifyUpdateBeforeExtraction"] is True
    assert info["SURequireSignedFeed"] is True
    assert info["SUEnableSystemProfiling"] is False


def test_bundle_rejects_wrong_sparkle_framework_version(tmp_path):
    source = tmp_path / "Sparkle.framework"
    resources = source / "Resources"
    resources.mkdir(parents=True)
    with (resources / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleShortVersionString": "2.9.0"}, stream)
    with pytest.raises(ValueError, match="expected '2.10.1'"):
        bundle_sparkle.copy_framework(source, tmp_path / "copy", "2.10.1")
