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

START_MAXIMIZED = os.environ.get("START_MAXIMIZED", "1") != "0"
"""Open the desktop window filling the screen instead of a 1200x800 box.

A 1200x800 window is a starting size, not a choice: every session began by
dragging the corner out, and the Transactions rail plus its table wants the
width. Set START_MAXIMIZED=0 for the old windowed default.
"""

START_FULLSCREEN = os.environ.get("START_FULLSCREEN", "0") == "1"
"""Open in true OS fullscreen (macOS Spaces / no title bar).

Off by default: fullscreen hides the window chrome, and this window's only
close affordance is that chrome. START_MAXIMIZED gives the whole screen and
keeps it.
"""


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

AI_BACKEND = os.environ.get("AI_BACKEND", "bundled").strip().lower()
"""Which backend answers.

"bundled" (the default) is llama.cpp travelling inside the app, against a model
this app downloads on first use — nothing else to install. "local" is Ollama,
kept because swapping models is one `ollama pull` rather than a rebuild.
"anthropic" is the control: the same loop and tools with a frontier model, which
is the only way to tell a bad answer caused by the model from one caused by the
prompt.
"""

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").strip()
"""Where the local Ollama server listens."""

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:4b").strip()
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

# ── The model that ships with the app ─────────────────────────────────────
# Ollama is a separate thing to install, and nobody installs a second app to
# try a side panel. So the shipped default runs a copy of llama.cpp's own
# server that travels in the bundle, against one model file this app downloads
# and owns. Ollama stays as a backend because swapping models is one
# `ollama pull` rather than a rebuild.

LLAMACPP_PORT = int(os.environ.get("LLAMACPP_PORT", 5051))
"""Port the bundled model server listens on. Next to the app's own 5050, and
deliberately not Ollama's 11434 — someone may be running that too."""

LLAMACPP_HOST = os.environ.get(
    "LLAMACPP_HOST", f"http://127.0.0.1:{LLAMACPP_PORT}").strip()
"""Where the bundled model server listens."""

MODEL_NAME = os.environ.get("MODEL_NAME", "Qwen3.5-4B-Q4_K_M.gguf").strip()
"""The one model file the app downloads and runs. Qwen 3.5 4B at 4-bit: the
smallest size that still picks the right tool every time, measured against the
9B on the same eleven questions."""

MODEL_URL = os.environ.get(
    "MODEL_URL",
    "https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/"
    "Qwen3.5-4B-Q4_K_M.gguf").strip()
"""Where to fetch it on first launch. Apache 2.0, so it may be redistributed —
and the terms and attribution travel in `licences/`, copied beside the weights
when they land. The model's own repository declares the licence in its metadata
but ships no licence file, so fetching one from there 404s."""

MODEL_BYTES = int(os.environ.get("MODEL_BYTES", 2_740_000_000))
"""Roughly what to expect, for the progress bar before the server says. The
download believes the Content-Length it is actually given; this is only so the
first frame of the bar is not empty."""


def model_dir():
    """Beside the database, for the same reason: out of the read-only bundle."""
    return os.path.join(os.path.dirname(SQLITE_PATH), "models")


def model_file():
    """The model file this app runs, downloaded or not."""
    return os.environ.get("MODEL_PATH", "").strip() or os.path.join(
        model_dir(), MODEL_NAME)


OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m").strip()
"""How long Ollama holds the model in memory after a question.

Its own default is five minutes, after which the next question pays to read
several gigabytes off disk again — which is most of the difference between an
answer in eight seconds and one in fifteen. Someone dipping into a side panel
while they look at their spending is exactly the person who waits longer than
five minutes between questions. Set "0" to hand the memory back at once.
"""

OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", 420))
"""How long to wait for one model call.

180 was generous for a Mac running the model on its GPU and not enough for one
reduced to its CPU: 26 tokens a second there, so a month's analysis spends over
four minutes reading the prompt before it writes a word, and the request was
abandoned before the answer existed. A wait that ends in an answer beats one
that ends in nothing.
"""

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
    chat, not whether it can answer this second. Whether the model is
    downloaded, loading or running is a live question, and /api/chat/status is
    where it gets asked.

    The bundled backend is therefore always configured — the runtime ships with
    the app and the weights are its own to fetch, so there is nothing for anyone
    to set up. Falling through to the Anthropic key here, as this used to, made
    the whole panel report itself unconfigured the moment the default changed.
    """
    if AI_BACKEND == "bundled":
        return True
    if AI_BACKEND == "local":
        return bool(OLLAMA_HOST and OLLAMA_MODEL)
    return bool(ANTHROPIC_API_KEY)
