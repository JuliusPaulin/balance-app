"""Central configuration for Balance.

Loads settings from environment variables (optionally from a local ``.env``
file via python-dotenv, if installed). Every value has a safe default, so the
module imports cleanly with nothing configured at all.

This is a local, single-user Mac app: one SQLite file on your own disk, no
server, no accounts, no network database.

Environment variables
----------------------
SQLITE_PATH
    Where the database file lives. Defaults to
    ``~/Library/Application Support/Balance/expenses.db``.
FLASK_SECRET_KEY
    Secret used to sign Flask session cookies.
PORT
    TCP port the local Flask server listens on. Defaults to 5050.
FLASK_DEBUG
    "1" enables Flask debug mode; anything else (default "0") disables it.
ENABLE_BANKING_*
    Optional Open Banking import credentials — see the block at the bottom.
AI_BACKEND, OLLAMA_*, ANTHROPIC_API_KEY
    AI chat assistant settings — local model by default. See the bottom block.
"""

import os
import sys

# Optionally load a local .env file. python-dotenv is a dev-only dependency, so
# guard both the import and the call — a packaged build may not have it
# installed and won't have a .env file.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


def _read_version():
    """The app's version, from the ``VERSION`` file next to this module.

    ``VERSION`` is the single source of truth: ``scripts/release.sh`` bumps it,
    the git tag is cut from it, and the release workflow refuses to build if the
    two disagree. PyInstaller bundles the file, so this works the same whether
    you run from source or from Balance.app.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    # In a PyInstaller bundle the data files land in sys._MEIPASS, not next to
    # this file, so look there first when it exists.
    for base in (getattr(sys, "_MEIPASS", None), here):
        if not base:
            continue
        try:
            with open(os.path.join(base, "VERSION"), encoding="utf-8") as fh:
                text = fh.read().strip()
            if text:
                return text
        except OSError:
            continue
    return "dev"


APP_VERSION = _read_version()
"""Version string, e.g. "1.3.0" — or "dev" when running from an unbuilt tree."""

LOCAL_USER_ID = 1
"""The id of the one and only user.

Every row is owned by this fixed user. The ``users`` table and the ``user_id``
columns survive from an earlier multi-user port and are kept purely as an
internal anchor, so the query layer needs no rewrite — there is no login, no
second user, and no way to become one."""

def _default_sqlite_path():
    """Default SQLite location: a writable, persistent per-user app-data folder.

    ``~/Library/Application Support/Balance/expenses.db`` on macOS. This keeps the
    database OUT of the (read-only) app bundle when packaged with PyInstaller and
    makes the data survive app rebuilds/reinstalls. Override with SQLITE_PATH."""
    base = os.path.join(
        os.path.expanduser("~"), "Library", "Application Support", "Balance"
    )
    return os.path.join(base, "expenses.db")


# `or` rather than a default argument: an empty SQLITE_PATH= line in a .env file
# would otherwise blank the path instead of meaning "use the default".
SQLITE_PATH = os.environ.get("SQLITE_PATH", "").strip() or _default_sqlite_path()
"""Filesystem path to the SQLite database file (desktop mode). Defaults to
``~/Library/Application Support/Balance/expenses.db``; override via SQLITE_PATH."""

FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-insecure-secret-change-me")
"""Flask session-signing key. Only signs a local cookie on 127.0.0.1."""

PORT = int(os.environ.get("PORT", 5050))
"""Port the Flask server listens on."""

DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"
"""Flask debug mode flag."""


# ── Enable Banking (optional Open Banking import) ────────────────────────
# Credentials for the Enable Banking PSD2 API. The private key is stored
# base64-encoded in the env var (so a multi-line PEM survives being pasted
# around) and decoded at use time in enable_banking.py — never written to disk and
# never logged/returned. All optional: when app id + key aren't both set,
# enable_banking_configured() is False and the bank-import routes report 400.

ENABLE_BANKING_APP_ID = os.environ.get("ENABLE_BANKING_APP_ID", "")
"""Enable Banking application id (used as the JWT ``kid``). Empty = not configured."""

ENABLE_BANKING_PRIVATE_KEY = os.environ.get("ENABLE_BANKING_PRIVATE_KEY", "")
"""Base64-encoded PEM private key for the EB app's RS256 JWT. Decoded at load."""

ENABLE_BANKING_ASPSP = os.environ.get("ENABLE_BANKING_ASPSP", "Nordea")
"""Default bank (ASPSP) name to request consent for."""

ENABLE_BANKING_COUNTRY = os.environ.get("ENABLE_BANKING_COUNTRY", "FI")
"""Default ASPSP country code (ISO 3166-1 alpha-2)."""

BANK_REDIRECT_BASE = (
    os.environ.get("BANK_REDIRECT_BASE", "").strip().rstrip("/")
    or f"http://localhost:{PORT}"
)
"""Base URL the bank sends the browser back to after PSD2 consent.

The callback lands on ``<base>/api/import/bank/callback``, which must be
registered as a redirect URI in the Enable Banking app config. Defaults to this
app's own local address; override only if you run it somewhere else."""


def enable_banking_configured():
    """True when the EB app id and private key are both present."""
    return bool(ENABLE_BANKING_APP_ID and ENABLE_BANKING_PRIVATE_KEY)


# ── AI chat assistant ────────────────────────────────────────────────────
# The chat panel runs against a LOCAL model by default: Ollama on this machine,
# nothing leaving the disk. That is the point of the feature — the app has
# always been one SQLite file on your own Mac, and a panel that posted a
# transaction history to somebody's API would be the first thing it ever did
# that contradicts that. See docs/LOCAL_AI_RESEARCH.md.
#
# The cloud backend is kept as a control, not a destination: same loop, same
# tools, different model, so a bad answer can be blamed on the right thing.

AI_BACKEND = os.environ.get("AI_BACKEND", "local").strip().lower()
"""Which backend answers: "local" (Ollama, the default) or "anthropic"."""

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").strip()
"""Where the local Ollama server listens."""

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:9b").strip()
"""The local model to ask. Must be pulled: ``ollama pull <model>``.

Defaulted to a ~9B at 4-bit, which is about 6 GB resident — the comfortable
size on a 16 GB Mac once the app and its webview are already running. On 8 GB,
set this to a 4B."""

OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", 8192))
"""Context window for the local model.

8k is ample here and deliberately not more: the prompt, the tool schemas and one
tool result come to roughly 2k, and every extra token of window is memory taken
from a machine that is also running the app."""

OLLAMA_TEMPERATURE = float(os.environ.get("OLLAMA_TEMPERATURE", "0.1"))
"""Low on purpose. This is a routing task — picking one of six tools and
quoting a figure back. Creativity here shows up as invented category names."""

OLLAMA_THINK = os.environ.get("OLLAMA_THINK", "0") == "1"
"""Whether to let a thinking model reason before answering.

Off by default: the deliberation costs seconds in a side panel and buys little
when the task is choosing between six tools. Models with no thinking mode
ignore this — the backend notices the rejection and stops sending it."""

OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", 180))
"""Seconds to wait for a local reply. The first call after a cold start pays
for loading several gigabytes off disk, which is slow and not a failure."""

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
"""API key for the cloud backend. Only consulted when AI_BACKEND=anthropic."""

AI_MODEL = os.environ.get("AI_MODEL", "claude-opus-5")
"""Model for the cloud backend."""

AI_EFFORT = os.environ.get("AI_EFFORT", "medium")
"""Thinking effort for the cloud backend: low | medium | high | xhigh | max."""


def ai_configured():
    """True when the assistant has a backend it could use.

    Deliberately cheap and offline: it says whether the app is *set up* for
    chat, not whether the server is up right now. Whether Ollama is actually
    running is a live question, and /api/chat/status is where it gets asked.
    """
    if AI_BACKEND == "local":
        return bool(OLLAMA_HOST and OLLAMA_MODEL)
    return bool(ANTHROPIC_API_KEY)
