#!/usr/bin/env python3
"""Read one version's notes out of CHANGELOG.md.

The release workflow publishes this text. Exit status 2 means the version
has no section, which the workflow treats as "use the commit subjects".
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHANGELOG_PATH = ROOT / "CHANGELOG.md"
_VERSION = re.compile(r"\d+\.\d+\.\d+")
_HEADING = re.compile(r"^## (\d+\.\d+\.\d+)\s*$", re.MULTILINE)


class NoSection(Exception):
    """CHANGELOG.md has no heading for this version."""


def section(text: str, version: str) -> str:
    """Return the body under ``## version``, without the heading."""
    if not _VERSION.fullmatch(version):
        raise ValueError(f"Version must look like 1.16.0, got {version!r}")
    matches = list(_HEADING.finditer(text))
    for index, match in enumerate(matches):
        if match.group(1) != version:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if not body:
            raise ValueError(f"CHANGELOG.md section {version} is empty")
        return body
    raise NoSection(f"CHANGELOG.md has no section for {version}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("Usage: changelog.py <version>", file=sys.stderr)
        return 1
    try:
        body = section(CHANGELOG_PATH.read_text(encoding="utf-8"), args[0])
    except NoSection as exc:
        print(exc, file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
