#!/usr/bin/env python3
"""One command before a pull request.

Lints, then runs the whole pytest suite. The suite includes the property
checks, the schema check, and the end-to-end checks. There is no separate
type checker in this repository; ruff's pyflakes rules are the lint pass.
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

STEPS = [
    ("lint", [PY, "-m", "ruff", "check", "."]),
    ("tests", [PY, "-m", "pytest", "tests", "-q"]),
]


def main() -> int:
    started = time.monotonic()
    for name, command in STEPS:
        step_started = time.monotonic()
        print(f"\n▶ {name}", flush=True)
        result = subprocess.run(command, cwd=ROOT)
        seconds = time.monotonic() - step_started
        if result.returncode != 0:
            print(f"\n✗ {name} failed after {seconds:.1f}s", file=sys.stderr)
            print(
                f"verify failed in {time.monotonic() - started:.1f}s",
                file=sys.stderr,
            )
            return result.returncode or 1
        print(f"✓ {name} ({seconds:.1f}s)", flush=True)
    print(f"\nverify passed in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
