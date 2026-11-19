"""The release notes the tag workflow publishes."""

from pathlib import Path

import pytest

from scripts.changelog import NoSection, section


ROOT = Path(__file__).resolve().parents[1]

_SAMPLE = """# Changelog

## 1.16.0

Hello from this Mac.

## 1.15.0

Older notes.
"""


def test_section_returns_one_version():
    assert section(_SAMPLE, "1.16.0") == "Hello from this Mac."
    assert section(_SAMPLE, "1.15.0") == "Older notes."


def test_missing_section_is_an_error():
    with pytest.raises(NoSection, match="1.2.3"):
        section(_SAMPLE, "1.2.3")


def test_empty_section_is_refused():
    with pytest.raises(ValueError, match="empty"):
        section("# Changelog\n\n## 1.16.0\n\n## 1.15.0\n\nOlder.\n", "1.16.0")


def test_version_must_be_major_minor_patch():
    with pytest.raises(ValueError, match="1.16"):
        section(_SAMPLE, "1.16")


def test_checked_in_notes_match_the_version_file():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    notes = section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    assert notes


def test_v1_16_0_notes_cover_the_local_mcp_server():
    notes = section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), "1.16.0")
    assert "this Mac" in notes
    assert "README.md#other-assistants-on-this-mac" in notes
    assert "Undo removes that batch." in notes
    assert "scripts/verify.py" in notes
    for name in (
        "list_accounts",
        "net_worth",
        "search_transactions",
        "spending_by_category",
        "cash_flow",
        "list_categories",
        "list_category_rules",
        "preview_bank_import",
        "commit_bank_import",
        "set_transaction_category",
        "create_category",
        "rename_category",
        "create_category_rule",
        "preview_recategorise",
        "apply_recategorise",
        "suggest_categories",
        "apply_category_suggestions",
        "list_audit_log",
        "undo_change",
    ):
        assert name in notes
