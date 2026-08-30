"""The Flask app itself, and the request plumbing every route shares.

Split out from ``app.py`` so the route modules in ``routes/`` can import the app,
the rate limiter and :func:`current_user_id` without importing ``app`` — which
imports them. ``app.py`` is now only wiring: it registers the blueprints and
runs the server.
"""

import os
import secrets
from datetime import timedelta
from flask import Flask, request, jsonify, session
from database import init_db
import config

# Single source of truth for the local server port. Now sourced from config
# (which reads the PORT env var). main.py (the pywebview desktop shell) imports
# SERVER_PORT so the window and the Flask server can never drift apart, so we
# keep the name as an alias for config.PORT.
SERVER_PORT = config.PORT

# Debug mode is off by default for the packaged desktop app (the Werkzeug
# debugger/reloader must never ship to end users). Opt in for local dev with
# FLASK_DEBUG=1. Sourced from config.
DEBUG = config.DEBUG

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

# ── Cookie hardening (Step 4.2) ────────────────────────────────────────
# The session cookie carries nothing but the CSRF token — there is no login.
# httpOnly keeps it away from JS; SameSite=Lax is the sane default. Not Secure:
# the app is served over plain http on 127.0.0.1, where TLS does not apply.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = False
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

# ── Schema bootstrap ──────────────────────────────────────────────────
# Create the schema and ensure the local user (+ its default categories) exist,
# so the app is usable on first launch and every user-scoped query has a valid
# user to attach to. Both are idempotent, and both run at import so the app is
# ready however it was started.
from database import seed_local_user

try:
    init_db()
    seed_local_user()
except Exception as _e:  # pragma: no cover
    app.logger.warning("Startup schema init/seed failed: %s", _e)

# ── CSRF enable flag (Step 4.1d) ───────────────────────────────────────
# On by default so production is protected. The test suite flips this False in
# its ``client`` fixture so the existing tests (which mutate without a token)
# keep passing; the dedicated CSRF tests flip it back True. An env override
# (CSRF_ENABLED=0) is available for emergencies but defaults to enabled.
app.config["CSRF_ENABLED"] = os.environ.get("CSRF_ENABLED", "1") != "0"

# Paths exempt from CSRF enforcement. /static/ is read-only; /healthz is an
# unauthenticated liveness probe.
_CSRF_EXEMPT_PREFIXES = ("/static/",)
_CSRF_EXEMPT_PATHS = {"/healthz"}
_CSRF_PROTECTED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _ensure_csrf_token():
    """Return the per-session CSRF token, minting one on first use."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _csrf_exempt(path):
    return path in _CSRF_EXEMPT_PATHS or path.startswith(_CSRF_EXEMPT_PREFIXES)


def current_user_id():
    """Return the id of the single local user.

    This is a single-user local app: there is no login. Every data row is owned
    by one fixed local user (``config.LOCAL_USER_ID``), seeded at startup, and
    every query in this module funnels through this helper. (The ``user_id``
    columns are kept purely as an internal anchor so the query layer is unchanged.)
    """
    return config.LOCAL_USER_ID


@app.after_request
def security_headers(response):
    """No-store caching + security headers + the readable CSRF cookie.

    Runs on every response. Besides the original no-store policy it sets the
    Step 4.2 hardening headers and mirrors the per-session CSRF token into a
    NON-httpOnly cookie so the SPA's fetch wrapper can read it and echo it back
    in the ``X-CSRF-Token`` header (double-submit).
    """
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"

    # Security headers (Step 4.2).
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # CSP is pragmatic, NOT strict: the UI relies on inline onclick= handlers and
    # inline style= attributes, so 'unsafe-inline' is required for script/style or
    # the app breaks. Chart.js loads from the jsdelivr CDN; the Google avatar is a
    # remote https image. Tightening script-src to nonces/hashes is a future
    # refactor of the inline-handler markup. Enforcing (not report-only) because
    # this policy was verified to keep Chart.js + inline handlers working.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "connect-src 'self'"
    )

    # Mirror the session CSRF token into a JS-readable cookie. Only set it when a
    # session token exists (it's minted lazily in before_request for normal
    # browsing); SameSite=Lax, Secure in hosted mode, httponly=False on purpose.
    token = session.get("csrf_token")
    if token:
        response.set_cookie(
            "csrf_token",
            token,
            samesite="Lax",
            secure=False,   # served over plain http on 127.0.0.1
            httponly=False,
        )
    return response


# ── Rate limiting (Step 4.3) ───────────────────────────────────────────
# Light, in-memory rate limiting. The in-memory store is per-process: it resets
# on restart and is NOT shared across instances — fine for a single free Render
# instance, but would need a shared backend (e.g. Redis) if we ever scale out.
# Limits are disabled under TESTING so they never interfere with the suite.
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri="memory://",
    enabled=not app.config.get("TESTING", False),
)


@app.before_request
def _sync_limiter_enabled():
    """Honor TESTING flipped on after import (the test client sets it post-init)."""
    limiter.enabled = not app.config.get("TESTING", False)


@app.before_request
def before_request_guard():
    """Mint the CSRF token and enforce it on mutating requests.

    This is a single-user local app with no login, so there is no auth/access
    gate — only CSRF protection on state-changing methods (kept so the SPA's
    double-submit fetch wrapper keeps working unchanged).
    """
    # Always let CORS/preflight through.
    if request.method == "OPTIONS":
        return None

    # Ensure a CSRF token exists so it can be surfaced to the SPA (cookie +
    # /api/me) on the very first request. Cheap and idempotent.
    _ensure_csrf_token()

    path = request.path
    if path.startswith("/static/"):
        return None

    csrf_error = _check_csrf(request, path)
    if csrf_error is not None:
        return csrf_error

    return None


def _check_csrf(req, path):
    """Validate the CSRF token on mutating requests; return a 403 response or None.

    Double-submit synchronizer token: the request must carry a token (in the
    ``X-CSRF-Token`` header, or a ``csrf_token`` form field for the logout form)
    that matches the per-session ``session["csrf_token"]``. Disabled when
    ``CSRF_ENABLED`` is False (the test suite) or for exempt paths/methods.
    """
    if not app.config.get("CSRF_ENABLED", True):
        return None
    if req.method not in _CSRF_PROTECTED_METHODS or _csrf_exempt(path):
        return None

    expected = session.get("csrf_token")
    sent = req.headers.get("X-CSRF-Token") or req.form.get("csrf_token")
    if not expected or not sent or not secrets.compare_digest(str(sent), str(expected)):
        return jsonify({"error": "CSRF validation failed"}), 403
    return None


# Recurring detection walks the whole transaction history (~seconds on a large
# DB), so cache the result in memory and recompute only after the underlying
# data changed. Any route that writes transactions or reassigns categories
# calls bump_data_version(); dismiss/manual-subscription routes do too since
# they feed the same view.
data_version = 0
recurring_cache = {}   # (uid, lookback, min_occ) -> (version, result)


def bump_data_version():
    global data_version
    data_version += 1


def cached_recurring(conn, user_id, lookback_months=18, min_occurrences=3):
    """Detected recurring series, from the cache when the data has not moved.

    Two pages ask for the same detection — the Subscriptions table and the
    Dashboard's cash-flow forecast — so they go through here and share one
    result rather than each walking the history.
    """
    from recurring import detect_recurring

    key = (user_id, lookback_months, min_occurrences)
    cached = recurring_cache.get(key)
    if cached and cached[0] == data_version:
        return cached[1]
    result = detect_recurring(conn, user_id, lookback_months=lookback_months,
                              min_occurrences=min_occurrences)
    recurring_cache[key] = (data_version, result)
    return result
