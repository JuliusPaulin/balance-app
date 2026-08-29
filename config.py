"""Central configuration for the expense tracker.

Loads settings from environment variables (optionally from a local ``.env``
file via python-dotenv, if installed). Every value has a safe default so the
module imports cleanly with no environment configured — this keeps the desktop
(pywebview + SQLite) build working without any of the hosted-mode env vars.

Environment variables
----------------------
APP_MODE
    "desktop" (default) or "hosted". Selects the runtime path: desktop uses
    SQLite + pywebview; hosted uses Postgres + gunicorn + OAuth.
DATABASE_URL
    Postgres connection string for hosted mode. Empty in desktop mode (which
    uses local SQLite instead).
FLASK_SECRET_KEY
    Secret used to sign Flask session cookies. Override in production; the
    default is intentionally insecure and only suitable for local dev.
GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
    Google OAuth client credentials (hosted mode auth). Empty until configured.
OAUTH_REDIRECT_BASE
    Base URL the OAuth callback is built from, e.g. the Render URL in prod or
    http://localhost:5050 locally.
ALLOWED_EMAILS
    Comma-separated list of Google emails that are **auto-approved** (skip the
    pending queue). Parsed into a frozenset of lowercased, stripped addresses.
    Empty = new users start as ``pending`` and must be approved by an admin.
ADMIN_EMAILS
    Comma-separated list of Google emails that are **admins** — auto-approved and
    granted ``role='admin'``. Parsed like ``ALLOWED_EMAILS``. Empty allowed.
PORT
    TCP port the Flask server listens on. Defaults to 5050.
FLASK_DEBUG
    "1" enables Flask debug mode; anything else (default "0") disables it.
SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_FROM
    SMTP settings used to email admins when a new access request arrives. All
    optional — when host/user/pass aren't all set, mailing is a no-op (see
    ``SMTP_CONFIGURED``) so local/dev and the current deploy keep working until
    creds are provided. A Gmail App Password works well here: set
    SMTP_HOST=smtp.gmail.com, SMTP_PORT=587, SMTP_USER=<your-gmail-address>,
    SMTP_PASS=<the-16-char-app-password>. SMTP_PORT defaults to 587 and
    SMTP_FROM defaults to SMTP_USER when empty.
"""

import os

# Optionally load a local .env file. python-dotenv is only a hosted/dev
# dependency, so guard both the import and the call — desktop builds may not
# have it installed and won't have a .env file.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


APP_MODE = os.environ.get("APP_MODE", "desktop")
"""Runtime mode: "desktop" or "hosted"."""

IS_HOSTED = APP_MODE == "hosted"
"""True when running in hosted (server) mode."""

USE_SQLITE = not IS_HOSTED
"""True when the local SQLite engine is used (desktop). Hosted mode uses Postgres.

The whole app talks to one of two interchangeable database layers behind
``db.db_conn()`` / ``db.run_sql_script()``: a local SQLite file (desktop) or a
pooled Postgres connection (hosted). This flag selects which one ``db.py`` wires
up at import time. Desktop is single-user and entirely offline — no Supabase, no
OAuth, no network database."""

LOCAL_USER_ID = 1
"""The fixed user id used in desktop (single-user) mode. The app is multi-tenant
under the hood; desktop simply auto-logs-in this one local user so every
user-scoped query keeps working unchanged."""

DATABASE_URL = os.environ.get("DATABASE_URL", "")
"""Postgres connection string; empty/ignored in desktop (SQLite) mode."""

def _default_sqlite_path():
    """Default SQLite location: a writable, persistent per-user app-data folder.

    ``~/Library/Application Support/Balance/expenses.db`` on macOS. This keeps the
    database OUT of the (read-only) app bundle when packaged with PyInstaller and
    makes the data survive app rebuilds/reinstalls. Override with SQLITE_PATH."""
    base = os.path.join(
        os.path.expanduser("~"), "Library", "Application Support", "Balance"
    )
    return os.path.join(base, "expenses.db")


SQLITE_PATH = os.environ.get("SQLITE_PATH", _default_sqlite_path())
"""Filesystem path to the SQLite database file (desktop mode). Defaults to
``~/Library/Application Support/Balance/expenses.db``; override via SQLITE_PATH."""

FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-insecure-secret-change-me")
"""Flask session-signing key. Override in production."""

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
"""Google OAuth client id (hosted mode)."""

GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
"""Google OAuth client secret (hosted mode)."""

OAUTH_REDIRECT_BASE = os.environ.get("OAUTH_REDIRECT_BASE", "http://localhost:5050")
"""Base URL for building the OAuth callback URL."""


def _parse_allowed_emails(raw):
    """Parse a comma-separated email list into a frozenset of normalized addrs."""
    return frozenset(
        email.strip().lower()
        for email in raw.split(",")
        if email.strip()
    )


ALLOWED_EMAILS = _parse_allowed_emails(os.environ.get("ALLOWED_EMAILS", ""))
"""Frozenset of lowercased auto-approved emails; empty = new users start pending."""

ADMIN_EMAILS = _parse_allowed_emails(os.environ.get("ADMIN_EMAILS", ""))
"""Frozenset of lowercased admin emails; admins are auto-approved with role=admin."""

PORT = int(os.environ.get("PORT", 5050))
"""Port the Flask server listens on."""

DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"
"""Flask debug mode flag."""


# ── SMTP (access-request emails) ─────────────────────────────────────────
# Used by mailer.py to notify admins of new access requests. All best-effort:
# when host/user/pass aren't all set, sending is a no-op (SMTP_CONFIGURED is
# False), so local/dev and the current deploy keep working with no SMTP creds.

SMTP_HOST = os.environ.get("SMTP_HOST", "")
"""SMTP server host (e.g. smtp.gmail.com). Empty = mailing disabled."""

SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
"""SMTP server port (STARTTLS submission port; defaults to 587)."""

SMTP_USER = os.environ.get("SMTP_USER", "")
"""SMTP login username (for Gmail, your full gmail address)."""

SMTP_PASS = os.environ.get("SMTP_PASS", "")
"""SMTP login password (for Gmail, a 16-char App Password)."""

SMTP_FROM = os.environ.get("SMTP_FROM", "") or SMTP_USER
"""From address for outgoing mail; defaults to SMTP_USER when empty."""

SMTP_CONFIGURED = bool(SMTP_HOST and SMTP_USER and SMTP_PASS)
"""True only when host, user, and pass are all set; otherwise mailing is a no-op."""


# ── Enable Banking (hosted Open Banking import) ──────────────────────────
# Credentials for the Enable Banking PSD2 API. The private key is stored
# base64-encoded in the env var (so a multi-line PEM survives Render's env UI)
# and decoded at use time in enable_banking.py — it is never written to disk and
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


def enable_banking_configured():
    """True when the EB app id and private key are both present."""
    return bool(ENABLE_BANKING_APP_ID and ENABLE_BANKING_PRIVATE_KEY)
