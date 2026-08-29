import os
import csv
import io
import re
import secrets
import difflib
import hashlib
from datetime import datetime, date, timedelta
from dateutil import parser as date_parser
from flask import Flask, request, jsonify, render_template, send_from_directory, session, abort, redirect
import db
from database import get_db, db_conn, init_db, backup_db, list_backups
from recurring import detect_recurring
import networth
import investment_import
import config
import enable_banking
from enable_banking import (BankAuthError, BankConfigMissing, BankError,
                            SessionExpired)

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


@app.route("/healthz")
def healthz():
    """Liveness probe for Render health checks. No auth, minimal work (no DB)."""
    return jsonify({"status": "ok"})


@app.route("/healthz/db")
def healthz_db():
    """DB keep-alive probe: runs a trivial query so a periodic ping keeps the
    Supabase free-tier project from pausing after ~7 days idle. Kept separate
    from /healthz so Render's frequent liveness checks stay DB-free."""
    try:
        with db_conn() as conn:
            conn.execute("SELECT 1")
        return jsonify({"status": "ok", "db": True})
    except Exception:
        return jsonify({"status": "error", "db": False}), 503


# ── Pages ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/me")
def me():
    """Minimal app-state endpoint for the SPA.

    Single-user local app — no login, so there is no identity to report. This
    carries the per-session CSRF token the fetch wrapper echoes back, and the
    version, so Settings can show which build you are on.
    """
    return jsonify({
        "csrf_token": _ensure_csrf_token(),
        "version": config.APP_VERSION,
    })


# ── Categories API ─────────────────────────────────────────────────────

@app.route("/api/categories")
def get_categories():
    """Categories with usage info (transaction count + last used date)."""
    uid = current_user_id()
    cat_type = request.args.get("type")
    base = """
        SELECT c.*, COUNT(t.id) AS tx_count, MAX(t.date) AS last_used
        FROM categories c
        LEFT JOIN transactions t ON t.category_id = c.id AND t.user_id = c.user_id
        WHERE c.user_id = %s {extra}
        GROUP BY c.id
        ORDER BY {order}
    """
    with db_conn() as conn:
        if cat_type:
            rows = conn.execute(base.format(extra="AND c.type = %s", order="c.name"),
                                (uid, cat_type)).fetchall()
        else:
            rows = conn.execute(base.format(extra="", order="c.type, c.name"),
                                (uid,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/categories", methods=["POST"])
def create_category():
    uid = current_user_id()
    data = request.json
    with db_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO categories (user_id, name, type, is_default, color) "
            "VALUES (%s, %s, %s, 0, %s) RETURNING id",
            (uid, data["name"], data["type"], data.get("color")),
        )
        new_id = cursor.fetchone()["id"]
        cat = conn.execute(
            "SELECT * FROM categories WHERE id = %s AND user_id = %s",
            (new_id, uid),
        ).fetchone()
    return jsonify(dict(cat)), 201


@app.route("/api/categories/<int:cat_id>", methods=["PUT"])
def update_category(cat_id):
    uid = current_user_id()
    data = request.json
    # Partial update: only fields present in the body change. "color": null
    # clears a stored color (falls back to the palette-derived one).
    sets, params = [], []
    if "name" in data:
        sets.append("name = %s"); params.append(data["name"])
    if "color" in data:
        sets.append("color = %s"); params.append(data["color"])
    if not sets:
        return jsonify({"error": "Nothing to update"}), 400
    with db_conn() as conn:
        conn.execute(
            f"UPDATE categories SET {', '.join(sets)}, updated_at = now() "
            "WHERE id = %s AND user_id = %s",
            (*params, cat_id, uid),
        )
        cat = conn.execute(
            "SELECT * FROM categories WHERE id = %s AND user_id = %s",
            (cat_id, uid),
        ).fetchone()
    if not cat:
        return jsonify({"error": "Category not found"}), 404
    return jsonify(dict(cat))


@app.route("/api/categories/<int:cat_id>", methods=["DELETE"])
def delete_category(cat_id):
    uid = current_user_id()
    reassign_to = request.args.get("reassign_to", type=int)
    try:
        with db_conn() as conn:
            if reassign_to:
                # The reassignment target must also belong to this user.
                target = conn.execute(
                    "SELECT 1 FROM categories WHERE id = %s AND user_id = %s",
                    (reassign_to, uid),
                ).fetchone()
                if not target:
                    return jsonify({"error": "Reassignment target not found"}), 400
                conn.execute(
                    "UPDATE transactions SET category_id = %s "
                    "WHERE category_id = %s AND user_id = %s",
                    (reassign_to, cat_id, uid),
                )
                conn.execute(
                    "UPDATE import_staging SET final_category_id = %s "
                    "WHERE final_category_id = %s AND user_id = %s",
                    (reassign_to, cat_id, uid),
                )
            conn.execute(
                "DELETE FROM merchant_rules WHERE category_id = %s AND user_id = %s",
                (cat_id, uid),
            )
            cursor = conn.execute(
                "DELETE FROM categories WHERE id = %s AND user_id = %s",
                (cat_id, uid),
            )
            if cursor.rowcount == 0:
                return jsonify({"error": "Category not found"}), 404
            _bump_data_version()
    except db.IntegrityError:
        # Category is still referenced by transactions/staging and no valid
        # reassignment target was supplied (or it left dangling references).
        return jsonify({
            "error": "Category is still in use. Reassign its transactions to "
                     "another category before deleting it."
        }), 409
    return "", 204


# ── Merchant Rules API ─────────────────────────────────────────────────

@app.route("/api/merchant-rules")
def get_merchant_rules():
    uid = current_user_id()
    with db_conn() as conn:
        rows = conn.execute("""
            SELECT mr.*, c.name as category_name
            FROM merchant_rules mr
            JOIN categories c ON mr.category_id = c.id
            WHERE mr.user_id = %s
            ORDER BY mr.pattern
        """, (uid,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/merchant-rules", methods=["POST"])
def create_merchant_rule():
    uid = current_user_id()
    data = request.json
    with db_conn() as conn:
        # The category the rule points at must belong to this user.
        cat = conn.execute(
            "SELECT 1 FROM categories WHERE id = %s AND user_id = %s",
            (data["category_id"], uid),
        ).fetchone()
        if not cat:
            return jsonify({"error": "Category not found"}), 400
        cursor = conn.execute(
            "INSERT INTO merchant_rules (user_id, pattern, category_id, match_type) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (uid, data["pattern"], data["category_id"], data.get("match_type", "exact")),
        )
        new_id = cursor.fetchone()["id"]
        row = conn.execute("""
            SELECT mr.*, c.name as category_name FROM merchant_rules mr
            JOIN categories c ON mr.category_id = c.id
            WHERE mr.id = %s AND mr.user_id = %s
        """, (new_id, uid)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/merchant-rules/<int:rule_id>", methods=["PUT"])
def update_merchant_rule(rule_id):
    uid = current_user_id()
    data = request.json
    with db_conn() as conn:
        cat = conn.execute(
            "SELECT 1 FROM categories WHERE id = %s AND user_id = %s",
            (data["category_id"], uid),
        ).fetchone()
        if not cat:
            return jsonify({"error": "Category not found"}), 400
        conn.execute(
            "UPDATE merchant_rules SET pattern = %s, category_id = %s, match_type = %s "
            "WHERE id = %s AND user_id = %s",
            (data["pattern"], data["category_id"], data.get("match_type", "exact"),
             rule_id, uid),
        )
        row = conn.execute("""
            SELECT mr.*, c.name as category_name FROM merchant_rules mr
            JOIN categories c ON mr.category_id = c.id
            WHERE mr.id = %s AND mr.user_id = %s
        """, (rule_id, uid)).fetchone()
    if not row:
        return jsonify({"error": "Rule not found"}), 404
    return jsonify(dict(row))


@app.route("/api/merchant-rules/<int:rule_id>", methods=["DELETE"])
def delete_merchant_rule(rule_id):
    uid = current_user_id()
    with db_conn() as conn:
        conn.execute(
            "DELETE FROM merchant_rules WHERE id = %s AND user_id = %s",
            (rule_id, uid),
        )
    return "", 204


def _matches_rule(store: str, pattern: str, match_type: str) -> bool:
    if not store or not pattern:
        return False
    s = store.strip().lower()
    p = pattern.strip().lower()
    if match_type == "exact":
        return s == p
    if match_type == "contains":
        return p in s
    if match_type == "smart":
        return difflib.SequenceMatcher(None, s, p).ratio() >= 0.72
    return False


@app.route("/api/merchant-rules/preview")
def preview_merchant_rule():
    """Return transactions that would match a candidate rule. Used live in the modal."""
    uid = current_user_id()
    pattern = (request.args.get("pattern") or "").strip()
    match_type = request.args.get("match_type", "exact")
    limit = request.args.get("limit", default=20, type=int)
    if not pattern:
        return jsonify({"matches": [], "match_count": 0, "distinct_stores": 0})

    with db_conn() as conn:
        rows = conn.execute("""
            SELECT t.id, t.date, t.store, t.amount, t.type, c.name as category_name
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.store != ''
            ORDER BY t.date DESC
        """, (uid,)).fetchall()

    matches = [dict(r) for r in rows if _matches_rule(r["store"], pattern, match_type)]
    distinct = len({m["store"].strip().lower() for m in matches})
    return jsonify({
        "match_count": len(matches),
        "distinct_stores": distinct,
        "matches": matches[:limit],
    })


@app.route("/api/merchant-rules/<int:rule_id>/apply", methods=["POST"])
def apply_rule_to_history(rule_id):
    """Re-categorize all historical transactions whose store matches this rule."""
    uid = current_user_id()
    with db_conn() as conn:
        rule = conn.execute("""
            SELECT mr.*, c.name as category_name, c.type as category_type
            FROM merchant_rules mr
            JOIN categories c ON mr.category_id = c.id
            WHERE mr.id = %s AND mr.user_id = %s
        """, (rule_id, uid)).fetchone()
        if not rule:
            return jsonify({"error": "Rule not found"}), 404

        rows = conn.execute("""
            SELECT id, store, category_id, type FROM transactions
            WHERE user_id = %s AND store != ''
        """, (uid,)).fetchall()

        updated = 0
        for r in rows:
            if r["category_id"] == rule["category_id"]:
                continue
            if rule["category_type"] != r["type"]:
                # Skip cross-type updates — don't reassign income tx to expense category, etc.
                continue
            if _matches_rule(r["store"], rule["pattern"], rule["match_type"]):
                conn.execute(
                    "UPDATE transactions SET category_id = %s, updated_at = now() "
                    "WHERE id = %s AND user_id = %s",
                    (rule["category_id"], r["id"], uid),
                )
                updated += 1

    _bump_data_version()
    return jsonify({"updated": updated, "rule_id": rule_id})


@app.route("/api/merchant-rules/stats")
def merchant_rule_stats():
    """Per-rule hit counts and last-match date — basis for dead-rule report."""
    uid = current_user_id()
    with db_conn() as conn:
        rules = conn.execute("""
            SELECT mr.*, c.name as category_name, c.type as category_type
            FROM merchant_rules mr
            JOIN categories c ON mr.category_id = c.id
            WHERE mr.user_id = %s
        """, (uid,)).fetchall()

        rows = conn.execute("""
            SELECT id, store, date, type FROM transactions
            WHERE user_id = %s AND store != ''
        """, (uid,)).fetchall()

    stats = []
    for rule in rules:
        hit_count = 0
        last_match = None
        for r in rows:
            if rule["category_type"] != r["type"]:
                continue
            if _matches_rule(r["store"], rule["pattern"], rule["match_type"]):
                hit_count += 1
                if last_match is None or r["date"] > last_match:
                    last_match = r["date"]
        stats.append({
            "rule_id": rule["id"],
            "hit_count": hit_count,
            "last_match": last_match,
        })

    return jsonify(stats)


def _rebuild_merchant_rules(conn, uid):
    """Rebuild all merchant rules from transaction history.

    Shared by the manual Start Training endpoint and the auto-retrain that runs
    after an import is confirmed. Returns (inserted, total_stores).
    """
    NOISE_STORES = {"Other", "Rent", "Missing info", "Monthly fee", "Korko", ""}
    CONFIDENCE_THRESHOLD = 70.0

    cur = conn.cursor()

    # Compute dominant category per store
    cur.execute("""
        SELECT
            t.store,
            t.category_id,
            c.name as category_name,
            c.type as category_type,
            COUNT(*) as cnt,
            total_counts.total,
            ROUND(100.0 * COUNT(*) / total_counts.total, 1) as confidence
        FROM transactions t
        JOIN categories c ON t.category_id = c.id
        JOIN (
            SELECT store, COUNT(*) as total
            FROM transactions
            WHERE user_id = %s AND store != '' AND store IS NOT NULL
            GROUP BY store
        ) total_counts ON t.store = total_counts.store
        WHERE t.user_id = %s AND t.store != '' AND t.store IS NOT NULL
        GROUP BY t.store, t.category_id, c.name, c.type, total_counts.total
        ORDER BY t.store, cnt DESC
    """, (uid, uid))
    store_data = {}
    for r in cur.fetchall():
        store = r["store"]
        if store not in store_data:
            store_data[store] = {
                "category_id": r["category_id"],
                "category_name": r["category_name"],
                "confidence": r["confidence"],
                "total": r["total"],
            }

    eligible = {
        s: d for s, d in store_data.items()
        if s not in NOISE_STORES and d["confidence"] >= CONFIDENCE_THRESHOLD
    }

    # Find contains-pattern groups
    stores_by_len = sorted(eligible.keys(), key=len)
    used = set()
    all_rules = []

    for base in stores_by_len:
        if base in used or len(base) < 4:
            continue
        base_lower = base.lower()
        base_cat = eligible[base]["category_id"]
        group = [base]
        for other in stores_by_len:
            if other == base or other in used:
                continue
            if base_lower in other.lower() and eligible[other]["category_id"] == base_cat:
                group.append(other)
        if len(group) > 1:
            all_rules.append({
                "pattern": base,
                "category_id": base_cat,
                "match_type": "contains",
            })
            used.update(group)

    # Exact rules for remaining stores
    for store, d in store_data.items():
        if store in used or store in NOISE_STORES or d["confidence"] < CONFIDENCE_THRESHOLD:
            continue
        all_rules.append({
            "pattern": store,
            "category_id": d["category_id"],
            "match_type": "exact",
        })

    cur.execute("DELETE FROM merchant_rules WHERE user_id = %s", (uid,))
    inserted = 0
    for rule in all_rules:
        cur.execute(
            "INSERT INTO merchant_rules (user_id, pattern, category_id, match_type) "
            "VALUES (%s, %s, %s, %s)",
            (uid, rule["pattern"], rule["category_id"], rule["match_type"])
        )
        inserted += 1

    return inserted, len(store_data)


@app.route("/api/merchant-rules/train", methods=["POST"])
def train_merchant_rules():
    """Rebuild all merchant rules from transaction history (manual trigger)."""
    uid = current_user_id()
    with db_conn() as conn:
        inserted, total_stores = _rebuild_merchant_rules(conn, uid)
    return jsonify({"inserted": inserted, "total_stores": total_stores})


# ── Month Notes API ────────────────────────────────────────────────────

@app.route("/api/notes/<month>")
def get_note(month):
    uid = current_user_id()
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM month_notes WHERE user_id = %s AND month = %s",
            (uid, month),
        ).fetchone()
    return jsonify(dict(row) if row else {"month": month, "note": ""})


@app.route("/api/notes/<month>", methods=["PUT"])
def save_note(month):
    uid = current_user_id()
    data = request.json
    with db_conn() as conn:
        conn.execute("""
            INSERT INTO month_notes (user_id, month, note) VALUES (%s, %s, %s)
            ON CONFLICT (user_id, month)
            DO UPDATE SET note = excluded.note, updated_at = now()
        """, (uid, month, data.get("note", "")))
        row = conn.execute(
            "SELECT * FROM month_notes WHERE user_id = %s AND month = %s",
            (uid, month),
        ).fetchone()
    return jsonify(dict(row))


@app.route("/api/notes")
def get_all_notes():
    uid = current_user_id()
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT month FROM month_notes WHERE user_id = %s AND note != ''",
            (uid,),
        ).fetchall()
    return jsonify([r["month"] for r in rows])


# ── Transactions API ──────────────────────────────────────────────────

@app.route("/api/transactions")
def get_transactions():
    uid       = current_user_id()
    page      = request.args.get("page", 1, type=int)
    per_page  = request.args.get("per_page", 50, type=int)
    t_type    = request.args.get("type")
    month     = request.args.get("month")        # YYYY-MM
    q         = request.args.get("q", "").strip()
    date_from = request.args.get("date_from")    # YYYY-MM-DD
    date_to   = request.args.get("date_to")      # YYYY-MM-DD
    cat_ids   = request.args.get("category_ids") # comma-separated
    amt_min   = request.args.get("amount_min", type=float)
    amt_max   = request.args.get("amount_max", type=float)
    sort_col  = request.args.get("sort", "date")
    sort_dir  = request.args.get("dir", "desc")

    # Dates are stored as ISO YYYY-MM-DD strings, which sort chronologically as
    # plain strings on both engines. (CAST(t.date AS date) is NOT safe here:
    # SQLite has no date type, so the cast collapses every value to the integer
    # year and the id tiebreak ends up deciding the order.)
    SORT_COLS = {
        "date": "t.date",
        "amount": "t.amount",
        "store": "t.store",
        "category": "c.name",
    }
    dir_sql = "ASC" if sort_dir == "asc" else "DESC"
    order = f"{SORT_COLS.get(sort_col, 'CAST(t.date AS date)')} {dir_sql}, t.id {dir_sql}"

    # The driving table (transactions) is always scoped to the requesting user.
    conditions, params = ["t.user_id = %s"], [uid]

    if t_type:
        conditions.append("t.type = %s"); params.append(t_type)
    if month:
        conditions.append("substr(t.date, 1, 7) = %s"); params.append(month)
    if q:
        # Postgres LIKE is case-sensitive (SQLite's was not) — use ILIKE to
        # preserve the original case-insensitive search behaviour.
        conditions.append("(t.store ILIKE %s OR c.name ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    if date_from:
        conditions.append("t.date >= %s"); params.append(date_from)
    if date_to:
        conditions.append("t.date <= %s"); params.append(date_to)
    if cat_ids:
        ids = [x.strip() for x in cat_ids.split(",") if x.strip()]
        conditions.append(f"t.category_id IN ({','.join(['%s']*len(ids))})"); params += ids
    months_filter = request.args.get("months")
    if months_filter:
        ml = [m.strip() for m in months_filter.split(",") if m.strip()]
        if ml:
            conditions.append(f"substr(t.date, 1, 7) IN ({','.join(['%s']*len(ml))})"); params += ml
    if amt_min is not None:
        conditions.append("t.amount >= %s"); params.append(amt_min)
    if amt_max is not None:
        conditions.append("t.amount <= %s"); params.append(amt_max)

    where = " WHERE " + " AND ".join(conditions)
    base  = "FROM transactions t JOIN categories c ON t.category_id = c.id" + where

    with db_conn() as conn:
        # One aggregate pass gives the count AND the filtered money totals, so
        # the list header can answer "how much?" for the current filter.
        agg = conn.execute(
            f"""SELECT COUNT(*) AS n,
                       COALESCE(SUM(CASE WHEN t.type = 'expense' THEN t.amount END), 0) AS sum_expense,
                       COALESCE(SUM(CASE WHEN t.type = 'income'  THEN t.amount END), 0) AS sum_income
                {base}""", params).fetchone()
        rows = conn.execute(
            f"SELECT t.*, c.name as category_name {base} ORDER BY {order} LIMIT %s OFFSET %s",
            params + [per_page, (page - 1) * per_page]
        ).fetchall()

    return jsonify({
        "items": [dict(r) for r in rows],
        "total": agg["n"],
        "sum_expense": float(agg["sum_expense"]),
        "sum_income": float(agg["sum_income"]),
        "page": page,
    })


@app.route("/api/transactions", methods=["POST"])
def create_transaction():
    uid = current_user_id()
    data = request.json
    with db_conn() as conn:
        # The category must belong to this user.
        cat = conn.execute(
            "SELECT 1 FROM categories WHERE id = %s AND user_id = %s",
            (data["category_id"], uid),
        ).fetchone()
        if not cat:
            return jsonify({"error": "Category not found"}), 400
        cursor = conn.execute(
            """INSERT INTO transactions (user_id, date, store, category_id, amount, type)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (uid, data["date"], data.get("store", ""), data["category_id"],
             data["amount"], data["type"]),
        )
        new_id = cursor.fetchone()["id"]
        row = conn.execute(
            """SELECT t.*, c.name as category_name FROM transactions t
               JOIN categories c ON t.category_id = c.id
               WHERE t.id = %s AND t.user_id = %s""",
            (new_id, uid),
        ).fetchone()
    _bump_data_version()
    return jsonify(dict(row)), 201


@app.route("/api/transactions/<int:t_id>", methods=["PUT"])
def update_transaction(t_id):
    uid = current_user_id()
    data = request.json
    with db_conn() as conn:
        cat = conn.execute(
            "SELECT 1 FROM categories WHERE id = %s AND user_id = %s",
            (data["category_id"], uid),
        ).fetchone()
        if not cat:
            return jsonify({"error": "Category not found"}), 400
        cursor = conn.execute(
            """UPDATE transactions
               SET date = %s, store = %s, category_id = %s, amount = %s, type = %s,
                   updated_at = now()
               WHERE id = %s AND user_id = %s""",
            (data["date"], data.get("store", ""), data["category_id"],
             data["amount"], data["type"], t_id, uid),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "Transaction not found"}), 404
        row = conn.execute(
            """SELECT t.*, c.name as category_name FROM transactions t
               JOIN categories c ON t.category_id = c.id
               WHERE t.id = %s AND t.user_id = %s""",
            (t_id, uid),
        ).fetchone()
    _bump_data_version()
    return jsonify(dict(row))


@app.route("/api/transactions/<int:t_id>", methods=["DELETE"])
def delete_transaction(t_id):
    uid = current_user_id()
    with db_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM transactions WHERE id = %s AND user_id = %s",
            (t_id, uid),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "Transaction not found"}), 404
    _bump_data_version()
    return "", 204


# ── Dashboard API ─────────────────────────────────────────────────────

@app.route("/api/dashboard/monthly-summary")
def monthly_summary():
    uid = current_user_id()
    with db_conn() as conn:
        rows = conn.execute("""
            SELECT substr(date, 1, 7) as month,
                   type,
                   SUM(amount) as total
            FROM transactions
            WHERE user_id = %s
            GROUP BY substr(date, 1, 7), type
            UNION ALL
            SELECT substr(t.date, 1, 7) as month,
                   'investment' as type,
                   SUM(t.amount) as total
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = 'expense' AND c.name = 'Investments'
            GROUP BY substr(t.date, 1, 7)
            ORDER BY month
        """, (uid, uid)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/dashboard/top-expenses")
def top_expenses():
    uid = current_user_id()
    with db_conn() as conn:
        latest = conn.execute("""
            SELECT substr(date, 1, 7) as month
            FROM transactions WHERE user_id = %s AND type = 'expense'
            ORDER BY date DESC LIMIT 1
        """, (uid,)).fetchone()

        if not latest:
            return jsonify({"latest_month": None, "categories": [], "trends": []})

        latest_month = latest["month"]

        top_cats = conn.execute("""
            SELECT c.id, c.name, SUM(t.amount) as total
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = 'expense'
              AND substr(t.date, 1, 7) = %s
            GROUP BY c.id, c.name
            ORDER BY total DESC
            LIMIT 5
        """, (uid, latest_month)).fetchall()

        cat_ids = [r["id"] for r in top_cats]
        if not cat_ids:
            return jsonify({"latest_month": latest_month, "categories": [], "trends": []})

        placeholders = ",".join(["%s"] * len(cat_ids))
        trends = conn.execute(f"""
            SELECT substr(t.date, 1, 7) as month,
                   c.id as category_id, c.name as category_name,
                   SUM(t.amount) as total
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = 'expense' AND c.id IN ({placeholders})
            GROUP BY substr(t.date, 1, 7), c.id, c.name
            ORDER BY month
        """, [uid] + cat_ids).fetchall()

    return jsonify({
        "latest_month": latest_month,
        "categories": [dict(r) for r in top_cats],
        "trends": [dict(r) for r in trends],
    })


@app.route("/api/dashboard/category-trends")
def category_trends():
    uid = current_user_id()
    cat_ids_str = request.args.get("ids", "")
    if not cat_ids_str:
        return jsonify({"categories": [], "trends": []})

    try:
        cat_ids = [int(x) for x in cat_ids_str.split(",") if x.strip()]
    except ValueError:
        return jsonify({"error": "category_ids must be integers"}), 400
    if not cat_ids:
        return jsonify({"categories": [], "trends": []})

    placeholders = ",".join(["%s"] * len(cat_ids))

    with db_conn() as conn:
        cats = conn.execute(
            f"SELECT id, name FROM categories "
            f"WHERE user_id = %s AND id IN ({placeholders})",
            [uid] + cat_ids,
        ).fetchall()

        trends = conn.execute(f"""
            SELECT substr(t.date, 1, 7) as month,
                   c.id as category_id, c.name as category_name,
                   SUM(t.amount) as total
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND c.id IN ({placeholders})
            GROUP BY substr(t.date, 1, 7), c.id, c.name
            ORDER BY month
        """, [uid] + cat_ids).fetchall()

    return jsonify({
        "categories": [dict(r) for r in cats],
        "trends": [dict(r) for r in trends],
    })


@app.route("/api/dashboard/category-breakdown")
def category_breakdown():
    uid = current_user_id()
    month = request.args.get("month")
    year  = request.args.get("year")
    # "expense" (default) or "income" — the dashboard draws one card of each.
    txn_type = request.args.get("type", "expense")
    if txn_type not in ("expense", "income"):
        txn_type = "expense"

    with db_conn() as conn:
        months_param = request.args.get("months")  # comma-separated YYYY-MM
        if months_param:
            months_list = [m.strip() for m in months_param.split(",") if m.strip()]
            if months_list:
                placeholders = ",".join(["%s"] * len(months_list))
                rows = conn.execute(f"""
                    SELECT c.name, SUM(t.amount) as total
                    FROM transactions t
                    JOIN categories c ON t.category_id = c.id
                    WHERE t.user_id = %s AND t.type = %s
                      AND substr(t.date, 1, 7) IN ({placeholders})
                    GROUP BY c.id, c.name
                    ORDER BY total DESC
                """, [uid, txn_type] + months_list).fetchall()
                return jsonify({"type": txn_type, "months": months_list, "items": [dict(r) for r in rows]})

        if year:
            rows = conn.execute("""
                SELECT c.name, SUM(t.amount) as total
                FROM transactions t
                JOIN categories c ON t.category_id = c.id
                WHERE t.user_id = %s AND t.type = %s
                  AND substr(t.date, 1, 4) = %s
                GROUP BY c.id, c.name
                ORDER BY total DESC
            """, (uid, txn_type, year)).fetchall()
            return jsonify({"type": txn_type, "year": year, "items": [dict(r) for r in rows]})

        if not month:
            latest = conn.execute("""
                SELECT substr(date, 1, 7) as month
                FROM transactions WHERE user_id = %s AND type = %s
                ORDER BY date DESC LIMIT 1
            """, (uid, txn_type)).fetchone()
            month = latest["month"] if latest else None

        if not month:
            return jsonify({"type": txn_type, "month": None, "items": []})

        rows = conn.execute("""
            SELECT c.name, SUM(t.amount) as total
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = %s
              AND substr(t.date, 1, 7) = %s
            GROUP BY c.id, c.name
            ORDER BY total DESC
        """, (uid, txn_type, month)).fetchall()

    return jsonify({"type": txn_type, "month": month, "items": [dict(r) for r in rows]})


@app.route("/api/dashboard/heatmap")
def dashboard_heatmap():
    """Daily expense totals for a year — drives the GitHub-style heatmap."""
    uid = current_user_id()
    year = request.args.get("year", type=int)

    with db_conn() as conn:
        if not year:
            latest = conn.execute(
                "SELECT substr(date, 1, 4) as year FROM transactions "
                "WHERE user_id = %s ORDER BY date DESC LIMIT 1",
                (uid,),
            ).fetchone()
            year = int(latest["year"]) if latest else datetime.now().year

        rows = conn.execute("""
            SELECT date, SUM(amount) as total
            FROM transactions
            WHERE user_id = %s AND type = 'expense' AND substr(date, 1, 4) = %s
            GROUP BY date
        """, (uid, str(year))).fetchall()

        available_years = conn.execute("""
            SELECT DISTINCT substr(date, 1, 4) as year
            FROM transactions WHERE user_id = %s ORDER BY year DESC
        """, (uid,)).fetchall()

    return jsonify({
        "year": year,
        "items": [dict(r) for r in rows],
        "available_years": [int(r["year"]) for r in available_years],
    })


# ── Annual Report API ──────────────────────────────────────────────────

@app.route("/api/reports/annual")
def annual_report():
    uid = current_user_id()
    year = request.args.get("year", type=int) or datetime.now().year
    year_str = str(year)
    prev_year_str = str(year - 1)

    with db_conn() as conn:
        totals = conn.execute("""
            SELECT type, SUM(amount) as total
            FROM transactions WHERE user_id = %s AND substr(date, 1, 4) = %s
            GROUP BY type
        """, (uid, year_str)).fetchall()

        prev_totals = conn.execute("""
            SELECT type, SUM(amount) as total
            FROM transactions WHERE user_id = %s AND substr(date, 1, 4) = %s
            GROUP BY type
        """, (uid, prev_year_str)).fetchall()

        categories_data = conn.execute("""
            SELECT c.name, SUM(t.amount) as total, COUNT(*) as count
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = 'expense'
              AND substr(t.date, 1, 4) = %s
            GROUP BY c.id, c.name
            ORDER BY total DESC
        """, (uid, year_str)).fetchall()

        income_categories = conn.execute("""
            SELECT c.name, SUM(t.amount) as total, COUNT(*) as count
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = 'income'
              AND substr(t.date, 1, 4) = %s
            GROUP BY c.id, c.name
            ORDER BY total DESC
        """, (uid, year_str)).fetchall()

        # Previous-year expense totals per category, for YoY deltas in the
        # category breakdown (design change #16).
        prev_categories = conn.execute("""
            SELECT c.name, SUM(t.amount) as total
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = 'expense'
              AND substr(t.date, 1, 4) = %s
            GROUP BY c.id, c.name
        """, (uid, prev_year_str)).fetchall()

        top_transactions = conn.execute("""
            SELECT t.*, c.name as category_name
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = 'expense'
              AND substr(t.date, 1, 4) = %s
            ORDER BY t.amount DESC
            LIMIT 10
        """, (uid, year_str)).fetchall()

        top_income = conn.execute("""
            SELECT t.*, c.name as category_name
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = 'income'
              AND substr(t.date, 1, 4) = %s
            ORDER BY t.amount DESC
            LIMIT 10
        """, (uid, year_str)).fetchall()

        monthly = conn.execute("""
            SELECT substr(date, 1, 7) as month, type, SUM(amount) as total
            FROM transactions
            WHERE user_id = %s AND substr(date, 1, 4) = %s
            GROUP BY substr(date, 1, 7), type
            ORDER BY month
        """, (uid, year_str)).fetchall()

        prev_monthly = conn.execute("""
            SELECT substr(date, 1, 7) as month, type, SUM(amount) as total
            FROM transactions
            WHERE user_id = %s AND substr(date, 1, 4) = %s
            GROUP BY substr(date, 1, 7), type
            ORDER BY month
        """, (uid, prev_year_str)).fetchall()

        transaction_count = conn.execute("""
            SELECT type, COUNT(*) as count
            FROM transactions WHERE user_id = %s AND substr(date, 1, 4) = %s
            GROUP BY type
        """, (uid, year_str)).fetchall()

        available_years = conn.execute("""
            SELECT DISTINCT substr(date, 1, 4) as year
            FROM transactions WHERE user_id = %s ORDER BY year DESC
        """, (uid,)).fetchall()

    return jsonify({
        "year": year,
        "totals": {r["type"]: r["total"] for r in totals},
        "prev_totals": {r["type"]: r["total"] for r in prev_totals},
        "categories": [dict(r) for r in categories_data],
        "prev_categories": {r["name"]: r["total"] for r in prev_categories},
        "income_categories": [dict(r) for r in income_categories],
        "top_transactions": [dict(r) for r in top_transactions],
        "top_income": [dict(r) for r in top_income],
        "monthly": [dict(r) for r in monthly],
        "prev_monthly": [dict(r) for r in prev_monthly],
        "transaction_count": {r["type"]: r["count"] for r in transaction_count},
        "available_years": [r["year"] for r in available_years],
    })


# ── Trends API ────────────────────────────────────────────────────────

@app.route("/api/trends/category")
def trends_category():
    uid         = current_user_id()
    ids_raw     = request.args.get("category_ids") or request.args.get("category_id", "")
    try:
        cat_ids = [int(x) for x in ids_raw.split(",") if x.strip()]
    except ValueError:
        return jsonify({"error": "category_ids must be integers"}), 400
    months_back = request.args.get("months", type=int, default=12)

    if not cat_ids:
        return jsonify({"error": "category_ids required"}), 400

    ph   = ",".join(["%s"] * len(cat_ids))

    if months_back > 0:
        now   = datetime.now()
        month = now.month - months_back
        year  = now.year
        while month <= 0:
            month += 12
            year  -= 1
        from_ym     = f"{year:04d}-{month:02d}"
        date_cond   = "AND substr(date, 1, 7) >= %s"
        date_params = [from_ym]
    else:
        date_cond   = ""
        date_params = []

    with db_conn() as conn:
        cats = conn.execute(
            f"SELECT id, name, type FROM categories "
            f"WHERE user_id = %s AND id IN ({ph})",
            [uid] + cat_ids,
        ).fetchall()
        if not cats:
            return jsonify({"error": "category not found"}), 404

        if len(cats) == 1:
            category = dict(cats[0])
        else:
            names    = " + ".join(c["name"] for c in cats[:3])
            if len(cats) > 3: names += f" +{len(cats)-3}"
            cat_type = "income" if all(c["type"] == "income" for c in cats) else "expense"
            category = {"id": None, "name": names, "type": cat_type}

        # user_id first, then category ids, then optional date param.
        monthly_params = [uid] + cat_ids + date_params
        monthly = conn.execute(f"""
            SELECT substr(date, 1, 7) as month,
                   SUM(amount) as total,
                   COUNT(*) as count
            FROM transactions
            WHERE user_id = %s AND category_id IN ({ph}) {date_cond}
            GROUP BY substr(date, 1, 7)
            ORDER BY month
        """, monthly_params).fetchall()

        merchant_params = [uid] + cat_ids + date_params
        top_merchants = conn.execute(f"""
            SELECT COALESCE(NULLIF(store,''), '(no name)') as store,
                   SUM(amount) as total,
                   COUNT(*) as count
            FROM transactions
            WHERE user_id = %s AND category_id IN ({ph})
              AND store != '' {date_cond}
            GROUP BY store
            ORDER BY total DESC
            LIMIT 12
        """, merchant_params).fetchall()

        monthly_list = [dict(r) for r in monthly]

    # Fill gaps so every month in the range has an entry (total=0, count=0)
    now_dt     = datetime.now()
    current_ym = now_dt.strftime("%Y-%m")
    if monthly_list:
        start_ym = from_ym if months_back > 0 else monthly_list[0]["month"]
        monthly_dict = {r["month"]: r for r in monthly_list}
        filled = []
        fy, fm = int(start_ym[:4]), int(start_ym[5:7])
        while True:
            ym = f"{fy:04d}-{fm:02d}"
            if ym > current_ym:
                break
            filled.append(monthly_dict.get(ym, {"month": ym, "total": 0.0, "count": 0}))
            fm += 1
            if fm > 12:
                fm = 1
                fy += 1
        monthly_list = filled

    total    = sum(r["total"] for r in monthly_list)
    tx_count = sum(r["count"] for r in monthly_list)
    months_n = len(monthly_list)

    return jsonify({
        "category":      category,
        "monthly":       monthly_list,
        "top_merchants": [dict(r) for r in top_merchants],
        "stats": {
            "total":            total,
            "avg_monthly":      total / months_n  if months_n  else 0,
            "tx_count":         tx_count,
            "avg_per_tx":       total / tx_count  if tx_count  else 0,
            "months_with_data": sum(1 for r in monthly_list if r["count"] > 0),
        },
    })


# ── Recurring / subscriptions ─────────────────────────────────────────

# Recurring detection walks the whole transaction history (~seconds on a large
# DB), so cache the result in memory and recompute only after the underlying
# data changed. Any route that writes transactions or reassigns categories
# calls _bump_data_version(); dismiss/manual-subscription routes do too since
# they feed the same view.
_data_version = 0
_recurring_cache = {}   # (uid, lookback, min_occ) -> (version, result)


def _bump_data_version():
    global _data_version
    _data_version += 1


@app.route("/api/recurring")
def recurring():
    """Detected recurring charges & subscriptions (see recurring.py)."""
    uid = current_user_id()

    def _int_arg(name, default):
        try:
            return int(request.args.get(name, default))
        except (TypeError, ValueError):
            return default

    lookback = _int_arg("lookback_months", 18)
    min_occ = _int_arg("min_occurrences", 3)

    key = (uid, lookback, min_occ)
    cached = _recurring_cache.get(key)
    if cached and cached[0] == _data_version:
        return jsonify(cached[1])

    with db_conn() as conn:
        result = detect_recurring(conn, uid, lookback_months=lookback,
                                  min_occurrences=min_occ)
    _recurring_cache[key] = (_data_version, result)
    return jsonify(result)


@app.route("/api/recurring/dismiss", methods=["POST"])
def dismiss_recurring():
    """Hide a recurring series. Body: {"signature": "<normalized>|<cadence>"}."""
    uid = current_user_id()
    data = request.json or {}
    sig = (data.get("signature") or "").strip()
    if not sig:
        return jsonify({"error": "signature is required"}), 400
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO recurring_dismissed (user_id, signature) VALUES (%s, %s) "
            "ON CONFLICT (user_id, signature) DO NOTHING",
            (uid, sig),
        )
    _bump_data_version()
    return jsonify({"dismissed": sig}), 201


@app.route("/api/recurring/dismiss/<path:sig>", methods=["DELETE"])
def undismiss_recurring(sig):
    """Un-hide a previously dismissed recurring series."""
    uid = current_user_id()
    with db_conn() as conn:
        conn.execute(
            "DELETE FROM recurring_dismissed WHERE user_id = %s AND signature = %s",
            (uid, sig),
        )
    _bump_data_version()
    return "", 204


_SUB_CADENCES = ("monthly", "quarterly", "yearly")


@app.route("/api/subscriptions", methods=["POST"])
def add_subscription():
    """Add a manual subscription (one detection missed).

    Body: {store, amount, cadence, category?, type?}. Stored in
    manual_subscriptions and folded into /api/recurring on the next load.
    """
    uid = current_user_id()
    data = request.json or {}
    store = (data.get("store") or "").strip()
    cadence = (data.get("cadence") or "monthly").strip().lower()
    sub_type = (data.get("type") or "expense").strip().lower()
    category = (data.get("category") or "").strip() or None
    try:
        amount = abs(float(data.get("amount")))
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a number"}), 400
    if not store:
        return jsonify({"error": "store is required"}), 400
    if amount <= 0:
        return jsonify({"error": "amount must be greater than 0"}), 400
    if cadence not in _SUB_CADENCES:
        return jsonify({"error": "cadence must be monthly, quarterly or yearly"}), 400
    if sub_type not in ("expense", "income"):
        return jsonify({"error": "type must be expense or income"}), 400

    with db_conn() as conn:
        row = conn.execute(
            "INSERT INTO manual_subscriptions (user_id, store, amount, cadence, "
            "category, type) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (uid, store, amount, cadence, category, sub_type),
        ).fetchone()
    _bump_data_version()
    return jsonify({"id": row["id"]}), 201


@app.route("/api/subscriptions/<int:sub_id>", methods=["DELETE"])
def delete_subscription(sub_id):
    """Delete a manual subscription owned by the current user."""
    uid = current_user_id()
    with db_conn() as conn:
        conn.execute(
            "DELETE FROM manual_subscriptions WHERE id = %s AND user_id = %s",
            (sub_id, uid),
        )
    _bump_data_version()
    return "", 204


# ── Net Worth API ─────────────────────────────────────────────────────

@app.route("/api/accounts")
def get_accounts():
    uid = current_user_id()
    include_archived = request.args.get("include_archived")
    with db_conn() as conn:
        if include_archived:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE user_id = %s "
                "ORDER BY is_archived, type, sort_order, name",
                (uid,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE user_id = %s AND is_archived = 0 "
                "ORDER BY type, sort_order, name",
                (uid,),
            ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/accounts", methods=["POST"])
def create_account():
    uid = current_user_id()
    data = request.json or {}
    name = (data.get("name") or "").strip()
    acc_type = data.get("type")
    if not name or acc_type not in ("asset", "liability"):
        return jsonify({"error": "name and type ('asset'|'liability') are required"}), 400
    with db_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO accounts (user_id, name, type, sort_order) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (uid, name, acc_type, int(data.get("sort_order", 0))),
        )
        new_id = cursor.fetchone()["id"]
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id = %s AND user_id = %s",
            (new_id, uid),
        ).fetchone()
    return jsonify(dict(acc)), 201


@app.route("/api/accounts/<int:account_id>", methods=["PUT"])
def update_account(account_id):
    uid = current_user_id()
    data = request.json or {}
    fields, params = [], []
    if "name" in data:
        fields.append("name = %s"); params.append((data.get("name") or "").strip())
    if "type" in data:
        if data["type"] not in ("asset", "liability"):
            return jsonify({"error": "invalid type"}), 400
        fields.append("type = %s"); params.append(data["type"])
    if "sort_order" in data:
        fields.append("sort_order = %s"); params.append(int(data["sort_order"]))
    if "is_archived" in data:
        fields.append("is_archived = %s"); params.append(1 if data["is_archived"] else 0)
    if not fields:
        return jsonify({"error": "no fields to update"}), 400
    params.extend([account_id, uid])
    with db_conn() as conn:
        conn.execute(
            f"UPDATE accounts SET {', '.join(fields)} WHERE id = %s AND user_id = %s",
            params,
        )
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, uid),
        ).fetchone()
    if not acc:
        return jsonify({"error": "account not found"}), 404
    return jsonify(dict(acc))


@app.route("/api/accounts/<int:account_id>/close", methods=["POST"])
def close_account(account_id):
    """Close an account you sold or paid off, keeping its history.

    Writes a zero balance at ``as_of`` and marks the account closed. Carry-forward
    then leaves it out of every total from that date on, while earlier months
    still count what you held. Deleting the account instead would erase the past.
    """
    uid = current_user_id()
    data = request.json or {}
    as_of = (data.get("as_of") or "").strip()
    if not _DATE_RE.match(as_of):
        return jsonify({"error": "as_of must be YYYY-MM-DD"}), 400
    with db_conn() as conn:
        if not conn.execute(
            "SELECT 1 FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, uid),
        ).fetchone():
            return jsonify({"error": "account not found"}), 404
        conn.execute(
            """
            INSERT INTO account_balances (user_id, account_id, as_of, balance)
            VALUES (%s, %s, %s, 0)
            ON CONFLICT (account_id, as_of) DO UPDATE SET balance = excluded.balance
            """,
            (uid, account_id, as_of),
        )
        conn.execute(
            "UPDATE accounts SET is_archived = 1 WHERE id = %s AND user_id = %s",
            (account_id, uid),
        )
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, uid),
        ).fetchone()
    return jsonify(dict(acc))


@app.route("/api/accounts/<int:account_id>/reopen", methods=["POST"])
def reopen_account(account_id):
    """Undo a close: the account is listed again. Its zero balance stays, so the
    total only moves once a new balance is entered."""
    uid = current_user_id()
    with db_conn() as conn:
        cursor = conn.execute(
            "UPDATE accounts SET is_archived = 0 WHERE id = %s AND user_id = %s",
            (account_id, uid),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "account not found"}), 404
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, uid),
        ).fetchone()
    return jsonify(dict(acc))


@app.route("/api/accounts/<int:account_id>", methods=["DELETE"])
def delete_account(account_id):
    uid = current_user_id()
    # account_balances rows cascade-delete (FK ON DELETE CASCADE).
    with db_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, uid),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "account not found"}), 404
    return "", 204


@app.route("/api/accounts/<int:account_id>/balances")
def get_balances(account_id):
    uid = current_user_id()
    with db_conn() as conn:
        # Confirm the account belongs to this user before returning balances.
        if not conn.execute(
            "SELECT 1 FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, uid),
        ).fetchone():
            return jsonify({"error": "account not found"}), 404
        rows = conn.execute(
            "SELECT * FROM account_balances "
            "WHERE account_id = %s AND user_id = %s ORDER BY as_of",
            (account_id, uid),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/accounts/<int:account_id>/balances", methods=["POST"])
def set_balance(account_id):
    uid = current_user_id()
    data = request.json or {}
    as_of = (data.get("as_of") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", as_of):
        return jsonify({"error": "as_of must be YYYY-MM-DD"}), 400
    try:
        balance = float(data["balance"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "balance must be a number"}), 400
    with db_conn() as conn:
        if not conn.execute(
            "SELECT 1 FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, uid),
        ).fetchone():
            return jsonify({"error": "account not found"}), 404
        conn.execute(
            """
            INSERT INTO account_balances (user_id, account_id, as_of, balance)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (account_id, as_of) DO UPDATE SET balance = excluded.balance
            """,
            (uid, account_id, as_of, balance),
        )
        row = conn.execute(
            "SELECT * FROM account_balances "
            "WHERE account_id = %s AND as_of = %s AND user_id = %s",
            (account_id, as_of, uid),
        ).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/balances/<int:balance_id>", methods=["DELETE"])
def delete_balance(balance_id):
    uid = current_user_id()
    with db_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM account_balances WHERE id = %s AND user_id = %s",
            (balance_id, uid),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "balance not found"}), 404
    return "", 204


@app.route("/api/networth/history")
def networth_history():
    uid = current_user_id()
    months = request.args.get("months", default=12, type=int) or 12
    with db_conn() as conn:
        series = networth.compute_history(conn, uid, months=months)
    return jsonify({"series": series})


@app.route("/api/networth/summary")
def networth_summary():
    uid = current_user_id()
    with db_conn() as conn:
        result = networth.summary(conn, uid)
    return jsonify(result)


# ── Investment holdings + import (CSV / xlsx → Net Worth) ──────────────

@app.route("/api/networth/holdings")
def networth_holdings():
    """Latest (or a specific as_of) holdings for one account, for the drill-down."""
    uid = current_user_id()
    account_id = request.args.get("account_id", type=int)
    if not account_id:
        return jsonify({"error": "account_id is required"}), 400
    as_of = (request.args.get("as_of") or "").strip()
    with db_conn() as conn:
        # The account must belong to this user before exposing its holdings.
        owns = conn.execute(
            "SELECT 1 FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, uid),
        ).fetchone()
        if not owns:
            return jsonify({"error": "account not found"}), 404
        if not as_of:
            row = conn.execute(
                "SELECT MAX(as_of) AS m FROM holdings WHERE account_id = %s",
                (account_id,),
            ).fetchone()
            as_of = row["m"] if row else None
        if not as_of:
            return jsonify({"account_id": account_id, "as_of": None, "holdings": []})
        rows = conn.execute(
            """
            SELECT id, name, isin, units, value_eur, return_pct, return_eur, currency
            FROM holdings
            WHERE account_id = %s AND as_of = %s
            ORDER BY value_eur DESC, name
            """,
            (account_id, as_of),
        ).fetchall()
    return jsonify({
        "account_id": account_id,
        "as_of": as_of,
        "holdings": [dict(r) for r in rows],
    })


@app.route("/api/networth/holdings/<int:holding_id>", methods=["DELETE"])
def delete_holding(holding_id):
    """Remove one holding from a snapshot — the "I sold this" action.

    The account total for that snapshot date was written as the sum of its
    holdings, so it is recomputed here. Earlier snapshots keep the holding, so
    the net-worth history stays true to what was held at the time.
    """
    uid = current_user_id()
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT h.account_id, h.as_of
            FROM holdings h
            JOIN accounts a ON a.id = h.account_id
            WHERE h.id = %s AND a.user_id = %s
            """,
            (holding_id, uid),
        ).fetchone()
        if not row:
            return jsonify({"error": "holding not found"}), 404
        account_id, as_of = row["account_id"], row["as_of"]
        conn.execute("DELETE FROM holdings WHERE id = %s", (holding_id,))
        agg = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(value_eur), 0) AS t "
            "FROM holdings WHERE account_id = %s AND as_of = %s",
            (account_id, as_of),
        ).fetchone()
        total = round(agg["t"], 2)
        # Only touch a balance that already exists for this date — never invent
        # one, or deleting a holding from an old snapshot would add a data point.
        if conn.execute(
            "SELECT 1 FROM account_balances WHERE account_id = %s AND as_of = %s",
            (account_id, as_of),
        ).fetchone():
            conn.execute(
                "UPDATE account_balances SET balance = %s "
                "WHERE account_id = %s AND as_of = %s",
                (total, account_id, as_of),
            )
    return jsonify({
        "account_id": account_id,
        "as_of": as_of,
        "holdings_left": agg["c"],
        "total": total,
    })


def _match_existing_account(conn, uid, acct):
    """Suggest an existing accounts row a parsed account could map onto (user-scoped).

    Order: exact external_id, then (cash only) IBAN parsed from the label, then a
    case-insensitive name match. Returns (account_id, by) or (None, None). Never
    auto-merges — only a dedupe *suggestion* for the review UI.
    """
    row = conn.execute(
        "SELECT id FROM accounts WHERE external_id = %s AND user_id = %s",
        (acct.external_id, uid),
    ).fetchone()
    if row:
        return row["id"], "external_id"

    if acct.kind == "cash":
        m = re.search(r"\b([A-Z]{2}\d{2}[\d ]{6,})", acct.label or "")
        if m:
            iban = re.sub(r"\s", "", m.group(1))
            row = conn.execute(
                "SELECT id FROM accounts WHERE external_id LIKE %s AND user_id = %s",
                (f"%{iban}%", uid),
            ).fetchone()
            if row:
                return row["id"], "iban"

    label = (acct.label or "").strip()
    if label:
        lead = label.split()[0] if label.split() else label
        row = conn.execute(
            "SELECT id FROM accounts WHERE user_id = %s AND "
            "(LOWER(name) = LOWER(%s) OR LOWER(name) = LOWER(%s)) LIMIT 1",
            (uid, label, lead),
        ).fetchone()
        if row:
            return row["id"], "name"
    return None, None


def _account_to_dict(acct, match_id=None, match_by=None):
    return {
        "broker": acct.broker,
        "label": acct.label,
        "external_id": acct.external_id,
        "kind": acct.kind,
        "total_eur": acct.total_eur,
        "holdings": [
            {
                "name": h.name,
                "units": h.units,
                "value_eur": h.value_eur,
                "return_pct": h.return_pct,
                "return_eur": h.return_eur,
                "isin": h.isin,
                "currency": h.currency,
            }
            for h in acct.holdings
        ],
        "match": {"existing_account_id": match_id, "by": match_by},
    }


@app.route("/api/networth/import-investments/preview", methods=["POST"])
def import_investments_preview():
    """Parse 1+ uploaded broker exports and return the hierarchy + dedupe hints.

    NO DB writes. Each file is parsed independently; an unreadable/unknown file
    yields a 400 naming the file and why.
    """
    uid = current_user_id()
    files = request.files.getlist("files") or request.files.getlist("file")
    if not files:
        return jsonify({"error": "No files provided"}), 400

    out_files = []
    with db_conn() as conn:
        for f in files:
            filename = f.filename or "(unnamed)"
            try:
                parsed = investment_import.detect_and_parse(filename, f.read())
            except investment_import.ImportError_ as e:
                return jsonify({"error": f"Could not read {filename}: {e}",
                                "filename": filename}), 400
            except Exception as e:
                return jsonify({"error": f"Could not read {filename}: {e}",
                                "filename": filename}), 400

            accounts = []
            for acct in parsed.accounts:
                match_id, match_by = _match_existing_account(conn, uid, acct)
                accounts.append(_account_to_dict(acct, match_id, match_by))

            out_files.append({
                "filename": filename,
                "source": parsed.source,
                "as_of": parsed.as_of,
                "warnings": parsed.warnings,
                "accounts": accounts,
            })

    return jsonify({"files": out_files})


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@app.route("/api/networth/import-investments/confirm", methods=["POST"])
def import_investments_confirm():
    """Write the user-reviewed selection: upsert accounts, holdings, balances.

    For each included account, resolve / create the target accounts row (scoped to
    the local user), replace/union its holdings for (account_id, as_of), and upsert
    the account total into account_balances. Idempotent on (account_id, as_of).
    """
    uid = current_user_id()
    data = request.json or {}
    incoming = data.get("accounts") or []
    selected = [a for a in incoming if a.get("include", True)]
    if not selected:
        return jsonify({"error": "No accounts selected to import"}), 400

    for a in selected:
        as_of = (a.get("as_of") or "").strip()
        if not _DATE_RE.match(as_of):
            label = a.get("name") or a.get("external_id") or "(account)"
            return jsonify({"error": f"{label}: as_of must be YYYY-MM-DD"}), 400

    # Merge selected accounts that resolve to the same target + as_of so multiple
    # files for one account UNION their holdings instead of overwriting.
    _merged, _order = {}, []
    for a in selected:
        key = (
            a.get("target_account_id") or (a.get("external_id") or "").strip() or id(a),
            (a.get("as_of") or "").strip(),
        )
        if key not in _merged:
            m = dict(a)
            m["holdings"] = list(a.get("holdings") or [])
            _merged[key] = m
            _order.append(key)
        elif a.get("kind") != "cash":
            _merged[key]["holdings"].extend(a.get("holdings") or [])
            _merged[key]["group_name"] = _merged[key].get("group_name") or a.get("group_name")
    selected = [_merged[k] for k in _order]

    backup_db("invest-import")

    results = []
    as_of_seen = None
    with db_conn() as conn:
        for a in selected:
            as_of = a["as_of"].strip()
            as_of_seen = as_of
            external_id = (a.get("external_id") or "").strip() or None
            name = (a.get("name") or external_id or "Investment").strip()
            group_name = (a.get("group_name") or "").strip() or None
            kind = a.get("kind") or "investment"
            acc_type = a.get("type") or "asset"
            if acc_type not in ("asset", "liability"):
                acc_type = "asset"
            target_id = a.get("target_account_id")
            holdings = a.get("holdings") or []

            # ── Resolve the target accounts row (user-scoped) ────────────
            if target_id:
                row = conn.execute(
                    "SELECT id FROM accounts WHERE id = %s AND user_id = %s",
                    (target_id, uid),
                ).fetchone()
                if not row:
                    return jsonify({"error": f"target_account_id {target_id} not found"}), 400
                account_id = row["id"]
                conn.execute(
                    "UPDATE accounts SET external_id = COALESCE(%s, external_id), "
                    "group_name = COALESCE(%s, group_name) WHERE id = %s AND user_id = %s",
                    (external_id, group_name, account_id, uid),
                )
                matched = "adopted"
            else:
                row = (
                    conn.execute(
                        "SELECT id FROM accounts WHERE external_id = %s AND user_id = %s",
                        (external_id, uid),
                    ).fetchone()
                    if external_id
                    else None
                )
                if row:
                    account_id = row["id"]
                    conn.execute(
                        "UPDATE accounts SET group_name = COALESCE(%s, group_name) "
                        "WHERE id = %s AND user_id = %s",
                        (group_name, account_id, uid),
                    )
                    matched = "existing"
                else:
                    cursor = conn.execute(
                        "INSERT INTO accounts (user_id, name, type, external_id, group_name, is_archived) "
                        "VALUES (%s, %s, %s, %s, %s, 0) RETURNING id",
                        (uid, name, acc_type, external_id, group_name),
                    )
                    account_id = cursor.fetchone()["id"]
                    matched = "created"

            # ── Holdings + total ─────────────────────────────────────────
            if kind == "cash":
                try:
                    total = round(float(a.get("total_eur", 0) or 0), 2)
                except (TypeError, ValueError):
                    total = 0.0
                conn.execute(
                    "DELETE FROM holdings WHERE account_id = %s AND as_of = %s",
                    (account_id, as_of),
                )
                holdings_count = 0
            else:
                seen_names = set()
                for h in holdings:
                    hname = (h.get("name") or "").strip()
                    if not hname or hname in seen_names:
                        continue
                    try:
                        value_eur = float(h.get("value_eur", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    seen_names.add(hname)

                    def _num(v):
                        try:
                            return float(v) if v is not None and v != "" else None
                        except (TypeError, ValueError):
                            return None

                    conn.execute(
                        """
                        INSERT INTO holdings
                          (account_id, as_of, name, isin, units, value_eur,
                           return_pct, return_eur, currency)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT(account_id, as_of, name) DO UPDATE SET
                          isin = excluded.isin, units = excluded.units,
                          value_eur = excluded.value_eur, return_pct = excluded.return_pct,
                          return_eur = excluded.return_eur, currency = excluded.currency
                        """,
                        (
                            account_id, as_of, hname,
                            (h.get("isin") or None),
                            _num(h.get("units")),
                            value_eur,
                            _num(h.get("return_pct")),
                            _num(h.get("return_eur")),
                            (h.get("currency") or None),
                        ),
                    )
                agg = conn.execute(
                    "SELECT COUNT(*) AS c, COALESCE(SUM(value_eur), 0) AS t "
                    "FROM holdings WHERE account_id = %s AND as_of = %s",
                    (account_id, as_of),
                ).fetchone()
                holdings_count = agg["c"]
                total = round(agg["t"], 2)

            # ── Account total → account_balances (carry-forward source) ──
            conn.execute(
                """
                INSERT INTO account_balances (user_id, account_id, as_of, balance)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(account_id, as_of) DO UPDATE SET balance = excluded.balance
                """,
                (uid, account_id, as_of, total),
            )

            acc_row = conn.execute(
                "SELECT name, group_name FROM accounts WHERE id = %s", (account_id,)
            ).fetchone()
            results.append({
                "id": account_id,
                "name": acc_row["name"],
                "group_name": acc_row["group_name"],
                "kind": kind,
                "total": total,
                "holdings_count": holdings_count,
                "matched": matched,
            })

    return jsonify({
        "updated": len(results),
        "as_of": as_of_seen,
        "accounts": results,
    })


# ── CSV Import API ────────────────────────────────────────────────────

def parse_date(date_str):
    """Parse a date string into ISO 'YYYY-MM-DD'.

    The three supported export formats are matched explicitly first so we never
    fall into dateutil's ambiguous month/day guessing:
      * YYYY-MM-DD and YYYY/MM/DD (ISO and Finnish bank statement)
      * D.M.YYYY (Nordea Platinum / Finnish dot format)
    Only genuinely unrecognized formats reach the dateutil fallback, which uses
    dayfirst=True because all inputs here are European (day-first) dates.
    """
    if not date_str:
        return None
    s = date_str.strip()

    # YYYY-MM-DD or YYYY/MM/DD — unambiguous (4-digit year leads), parse directly
    m = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$', s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # DD.MM.YYYY or D.M.YYYY — Finnish dot format (day first, 4-digit year)
    m = re.match(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Fallback: dateutil with explicit dayfirst for any other European format
    try:
        return date_parser.parse(s, dayfirst=True).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OverflowError):
        return None


def parse_amount(amount_str):
    """Parse a monetary amount string into a positive float (sign is dropped).

    Handles the separator conventions seen in the supported exports:
      * fi-FI decimal comma with optional space/dot thousands: "1 234,56", "1.234,56"
      * en-US decimal dot with comma thousands: "1,234.56"
      * plain integers/decimals: "1234", "12.50"
    Non-breaking and thin spaces (common fi-FI thousands separators) are stripped
    alongside ASCII spaces so they don't break parsing.
    """
    if not amount_str:
        return None
    # Strip currency symbols and all whitespace variants (ASCII, NBSP, thin space)
    cleaned = re.sub(r"[€$£¥\s]", "", amount_str.strip())

    if "," in cleaned and "." in cleaned:
        # Both present: the right-most separator is the decimal mark.
        if cleaned.rindex(",") > cleaned.rindex("."):
            # fi-FI: "." groups thousands, "," is decimal
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            # en-US: "," groups thousands, "." is decimal
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts) == 2 and len(parts[1]) <= 2:
            # Single comma with ≤2 trailing digits → decimal comma
            cleaned = cleaned.replace(",", ".")
        else:
            # Multiple commas or long trailing group → thousands separators
            cleaned = cleaned.replace(",", "")
    elif cleaned.count(".") > 1:
        # Multiple dots with no comma → "." is a thousands separator (e.g. "1.234.567")
        cleaned = cleaned.replace(".", "")

    try:
        return abs(float(cleaned))
    except ValueError:
        return None


def suggest_category(store_name, conn, user_id):
    if not store_name:
        return None

    store_lower = store_name.strip().lower()

    # Check merchant rules first: exact → contains → smart. Scoped to the user.
    rules = conn.execute("""
        SELECT mr.match_type, mr.pattern, c.name as category_name
        FROM merchant_rules mr
        JOIN categories c ON mr.category_id = c.id
        WHERE mr.user_id = %s
        ORDER BY CASE mr.match_type WHEN 'exact' THEN 1 WHEN 'contains' THEN 2 ELSE 3 END
    """, (user_id,)).fetchall()

    smart_candidates = []
    for rule in rules:
        pattern_lower = rule["pattern"].lower()
        mt = rule["match_type"]
        if mt == "exact" and store_lower == pattern_lower:
            return rule["category_name"]
        elif mt == "contains" and pattern_lower in store_lower:
            return rule["category_name"]
        elif mt == "smart":
            ratio = difflib.SequenceMatcher(None, store_lower, pattern_lower).ratio()
            if ratio >= 0.72:
                smart_candidates.append((ratio, rule["category_name"]))

    if smart_candidates:
        smart_candidates.sort(reverse=True)
        return smart_candidates[0][1]

    # Fall back to past transactions with same store (case-insensitive match;
    # Postgres '=' is case-sensitive, so compare lowered values to preserve the
    # original SQLite LOWER(...) = LOWER(...) behaviour).
    row = conn.execute("""
        SELECT c.name FROM transactions t
        JOIN categories c ON t.category_id = c.id
        WHERE t.user_id = %s AND LOWER(t.store) = LOWER(%s)
        GROUP BY c.id, c.name
        ORDER BY COUNT(*) DESC
        LIMIT 1
    """, (user_id, store_name.strip())).fetchone()
    return row["name"] if row else None


COLUMN_ALIASES = {
    "date": ["date", "datum", "päivä", "pvm", "transaction date", "trans date", "booking date",
             "date of payment", "tapahtumapäivä", "kirjauspäivä"],
    "store": ["store", "merchant", "description", "payee", "kauppa", "saaja", "memo", "name",
              "recipient", "location of purchase", "otsikko", "nimi"],
    "category": ["category", "kategoria", "luokka", "type", "group"],
    "amount": ["amount", "sum", "summa", "määrä", "value", "total", "debit"],
    "message": ["viesti", "message", "note", "memo2"],
    # Payment reference. Only read to tell a card purchase from a transfer when
    # deciding whether the message column may stand in as the store name.
    "reference": ["viitenumero", "reference", "reference number", "arkistointitunnus"],
}


def detect_columns(headers):
    mapping = {}
    normalized = [h.strip().lower() for h in headers]
    for field, aliases in COLUMN_ALIASES.items():
        for i, header in enumerate(normalized):
            if header in aliases:
                mapping[field] = i
                break
    return mapping


def _detect_delimiter(content):
    """Pick the most likely CSV delimiter from the header line.

    Counts ';' vs ',' in the first non-empty line. Ties (or a header with no
    commas) resolve to ';' because two of the three supported export formats
    (Finnish bank statement, Nordea Platinum) are semicolon-delimited.
    """
    first_line = ""
    for line in content.splitlines():
        if line.strip():
            first_line = line
            break
    semis = first_line.count(";")
    commas = first_line.count(",")
    if semis == 0 and commas == 0:
        return ";"
    return ";" if semis >= commas else ","


# Finnair credit-card exports are comma-delimited and carry the purchase-currency
# amount in a generic column, while the EUR amount lives in a fixed column.
# Per CLAUDE.md the EUR amount is column index 8. Detect the format by its
# signature headers so multi-currency rows import with the correct EUR amount.
FINNAIR_EUR_AMOUNT_COL = 8
FINNAIR_SIGNATURE_HEADERS = {"date of payment", "location of purchase"}


def _is_finnair_format(headers):
    normalized = {h.strip().lower() for h in headers}
    return FINNAIR_SIGNATURE_HEADERS.issubset(normalized)


def _decode_csv_bytes(raw):
    """Decode uploaded CSV bytes, trying UTF-8 (BOM-aware) then common fallbacks.

    Raises UnicodeDecodeError only if none of the candidate encodings work.
    """
    last_error = None
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError as e:
            last_error = e
    # latin-1 decodes any byte string, so this is effectively unreachable,
    # but re-raise to be explicit if the candidate list ever changes.
    raise last_error


# Card rows in Nordea exports put the terminal receipt line in Viesti, e.g.
# "EUR          10,76 CORK" or "SEK          15,00 Solna". It starts with a
# 3-letter currency code and an amount, and is never a merchant name.
CARD_DETAIL_RE = re.compile(r"^[A-Z]{3}\s+[\d\s.,]*\d[.,]\d{2}\b")

# Boilerplate a bank writes into Viesti in place of a real message.
VIESTI_BOILERPLATE = {
    "tiedot mobilepay-sovelluksessa",
}


def _viesti_as_store(viesti):
    """Return viesti string as the store name if it looks like a real merchant name."""
    if not viesti or not viesti.strip():
        return None
    v = viesti.strip()
    if len(v) > 50:
        return None
    if v[0].isdigit():          # reference numbers start with digits
        return None
    if re.match(r"^[A-Z]{2}\d{2}", v):  # IBAN pattern
        return None
    if CARD_DETAIL_RE.match(v):  # card receipt line, not a merchant
        return None
    if v.lower() in VIESTI_BOILERPLATE:
        return None
    return v


def format_signature(headers, delimiter):
    """Stable fingerprint of a CSV layout (header names + delimiter).

    Lowercases and trims each header, joins with '|', appends the delimiter, and
    SHA-1s the result. The same bank export (same header row + delimiter) always
    yields the same signature, so a learned column mapping can be looked up by it.
    """
    joined = "|".join(h.strip().lower() for h in headers)
    return hashlib.sha1((joined + "||" + delimiter).encode("utf-8")).hexdigest()


def _read_headers(reader):
    """Read + normalize the header row from a csv.reader (drop trailing empties)."""
    headers = next(reader, None)
    if not headers:
        return None
    while headers and not headers[-1].strip():
        headers = headers[:-1]
    return headers


def _stage_rows(conn, uid, batch_id, reader, col_map, amount_sign="neg_expense"):
    """Parse the remaining rows of ``reader`` and INSERT them into import_staging.

    ``col_map`` maps field name -> column index and may contain: ``date`` and
    ``amount`` (required), and optionally ``store``, ``message``, ``category``.

    ``amount_sign`` controls the expense/income convention:
      * ``'neg_expense'`` (default, the historical behaviour): a leading '-' means
        expense, otherwise income.
      * ``'pos_expense'``: a positive amount means expense, a leading '-' income.

    Returns the number of rows staged.
    """
    staged = 0
    for row in reader:
        # Drop trailing empty cells
        while row and not row[-1].strip():
            row = row[:-1]

        if not row or all(c.strip() == "" for c in row):
            continue

        if col_map["date"] >= len(row):
            continue
        parsed_date = parse_date(row[col_map["date"]])
        if not parsed_date:
            continue

        raw_amount = row[col_map["amount"]].strip() if col_map["amount"] < len(row) else ""
        is_negative = raw_amount.startswith("-")
        if amount_sign == "pos_expense":
            txn_type = "income" if is_negative else "expense"
        else:
            txn_type = "expense" if is_negative else "income"
        amount = parse_amount(raw_amount)
        if amount is None or amount == 0:
            continue

        store = row[col_map["store"]].strip() if "store" in col_map and col_map["store"] is not None and col_map["store"] < len(row) else ""

        # Use viesti as store override when it's a meaningful merchant name.
        # Skip it on rows that carry a payment reference: those are card
        # purchases and direct debits, where the bank writes the receipt line
        # or the city into viesti and the real merchant sits in the name
        # column. Transfers have no reference, and there viesti is the message
        # the sender typed ("Palkka"), which names the transaction better.
        has_reference = (
            "reference" in col_map
            and col_map["reference"] is not None
            and col_map["reference"] < len(row)
            and row[col_map["reference"]].strip() != ""
        )
        if (not has_reference and "message" in col_map
                and col_map["message"] is not None and col_map["message"] < len(row)):
            override = _viesti_as_store(row[col_map["message"]])
            if override:
                store = override

        csv_category = row[col_map["category"]].strip() if "category" in col_map and col_map["category"] is not None and col_map["category"] < len(row) else ""
        suggested = suggest_category(store, conn, uid) or csv_category or None

        conn.execute("""
            INSERT INTO import_staging (user_id, date, store, suggested_category, amount, type, import_batch_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (uid, parsed_date, store, suggested, amount, txn_type, batch_id))
        staged += 1
    return staged


def _staging_response(conn, uid, batch_id):
    """Build the standard {batch_id, count, items} response for a staged batch."""
    rows = conn.execute(
        "SELECT * FROM import_staging "
        "WHERE import_batch_id = %s AND user_id = %s ORDER BY date DESC",
        (batch_id, uid),
    ).fetchall()
    return jsonify({
        "batch_id": batch_id,
        "count": len(rows),
        "items": [dict(r) for r in rows],
    })


@app.route("/api/import/upload", methods=["POST"])
@limiter.limit("30/hour")
def upload_csv():
    uid = current_user_id()
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    filename = file.filename or ""
    if not filename.lower().endswith(".csv"):
        return jsonify({"error": "File must be CSV"}), 400

    backup_db("pre-import")
    conn = get_db()
    batch_id = None
    try:
        # Decode first so an encoding problem fails before we create a batch.
        try:
            content = _decode_csv_bytes(file.read())
        except UnicodeDecodeError:
            return jsonify({
                "error": "Could not decode CSV file. Please save it as UTF-8."
            }), 400

        delimiter = _detect_delimiter(content)
        reader = csv.reader(io.StringIO(content), delimiter=delimiter)

        headers = _read_headers(reader)
        if not headers:
            return jsonify({"error": "Empty CSV file"}), 400

        col_map = detect_columns(headers)

        # Finnair multi-currency rows carry their EUR amount in a fixed column;
        # honor that instead of the generic 'amount' alias (which may be the
        # purchase-currency amount).
        if _is_finnair_format(headers):
            col_map["amount"] = FINNAIR_EUR_AMOUNT_COL

        amount_sign = "neg_expense"

        # If the auto-detector can't find the required columns, fall back to a
        # format this user has previously taught us (keyed by layout signature).
        if "date" not in col_map or "amount" not in col_map:
            signature = format_signature(headers, delimiter)
            learned = conn.execute(
                "SELECT * FROM import_formats WHERE user_id = %s AND signature = %s",
                (uid, signature),
            ).fetchone()
            if learned:
                col_map = {"date": learned["date_col"], "amount": learned["amount_col"]}
                if learned["store_col"] is not None:
                    col_map["store"] = learned["store_col"]
                amount_sign = learned["amount_sign"]
            else:
                # Still unmapped: don't 400 and don't create an orphan batch.
                # Return a mapping preview so the UI can ask the user to map columns.
                sample_rows = []
                for row in reader:
                    while row and not row[-1].strip():
                        row = row[:-1]
                    if not row or all(c.strip() == "" for c in row):
                        continue
                    sample_rows.append(row)
                    if len(sample_rows) >= 5:
                        break
                guess = detect_columns(headers)
                return jsonify({
                    "needs_mapping": True,
                    "signature": signature,
                    "delimiter": delimiter,
                    "headers": headers,
                    "sample_rows": sample_rows,
                    "guess": {
                        "date": guess.get("date"),
                        "amount": guess.get("amount"),
                        "store": guess.get("store"),
                    },
                })

        # We can parse — create the batch now and stage every row.
        cursor = conn.execute(
            "INSERT INTO import_batches (user_id, filename) VALUES (%s, %s) RETURNING id",
            (uid, filename),
        )
        batch_id = cursor.fetchone()["id"]

        _stage_rows(conn, uid, batch_id, reader, col_map, amount_sign)
        conn.commit()

        return _staging_response(conn, uid, batch_id)
    except Exception as e:
        # Roll back the in-flight transaction, then remove any partially-created
        # batch + staging rows so a failed import leaves no half-written state.
        conn.rollback()
        if batch_id is not None:
            try:
                _cleanup_import_batch(conn, batch_id, uid)
                conn.commit()
            except db.DatabaseError:
                conn.rollback()
        status = 400 if isinstance(e, (csv.Error, ValueError)) else 500
        return jsonify({"error": f"Import failed: {e}"}), status
    finally:
        conn.close()


def _cleanup_import_batch(conn, batch_id, user_id):
    """Remove staging rows and the batch record for a failed/empty import."""
    conn.execute(
        "DELETE FROM import_staging WHERE import_batch_id = %s AND user_id = %s",
        (batch_id, user_id),
    )
    conn.execute(
        "DELETE FROM import_batches WHERE id = %s AND user_id = %s",
        (batch_id, user_id),
    )


def _parse_col_index(value, required=False):
    """Coerce a form field to a non-negative int column index.

    Blank/missing → None (only allowed when ``required`` is False). Returns
    ``(index_or_None, error_or_None)``.
    """
    if value is None or str(value).strip() == "":
        if required:
            return None, "missing required column index"
        return None, None
    try:
        idx = int(value)
    except (TypeError, ValueError):
        return None, "column index must be an integer"
    if idx < 0:
        return None, "column index out of range"
    return idx, None


@app.route("/api/import/upload-mapped", methods=["POST"])
@limiter.limit("30/hour")
def upload_mapped_csv():
    """Import a CSV using a user-supplied column mapping (the "learn" path).

    Accepts the re-uploaded file (multipart, same field name as /upload) plus
    form fields:
      * date_col      (required int)  — column index of the date
      * amount_col    (required int)  — column index of the amount
      * store_col     (optional int / blank) — column index of the merchant
      * amount_sign   ('neg_expense' | 'pos_expense')
      * remember      ('1' | '0')     — persist this mapping for the layout

    The signature is recomputed server-side from the file's headers + delimiter
    (a client-sent signature is never trusted). Returns the same shape as
    /api/import/upload so the UI flows into the existing review table.
    """
    uid = current_user_id()
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    filename = file.filename or ""
    if not filename.lower().endswith(".csv"):
        return jsonify({"error": "File must be CSV"}), 400

    date_col, err = _parse_col_index(request.form.get("date_col"), required=True)
    if err:
        return jsonify({"error": f"date_col: {err}"}), 400
    amount_col, err = _parse_col_index(request.form.get("amount_col"), required=True)
    if err:
        return jsonify({"error": f"amount_col: {err}"}), 400
    store_col, err = _parse_col_index(request.form.get("store_col"), required=False)
    if err:
        return jsonify({"error": f"store_col: {err}"}), 400

    amount_sign = (request.form.get("amount_sign") or "neg_expense").strip()
    if amount_sign not in ("neg_expense", "pos_expense"):
        return jsonify({"error": "amount_sign must be 'neg_expense' or 'pos_expense'"}), 400
    remember = request.form.get("remember") == "1"

    backup_db("pre-import")
    conn = get_db()
    batch_id = None
    try:
        try:
            content = _decode_csv_bytes(file.read())
        except UnicodeDecodeError:
            return jsonify({
                "error": "Could not decode CSV file. Please save it as UTF-8."
            }), 400

        delimiter = _detect_delimiter(content)
        reader = csv.reader(io.StringIO(content), delimiter=delimiter)

        headers = _read_headers(reader)
        if not headers:
            return jsonify({"error": "Empty CSV file"}), 400

        # Validate indices against the actual header width.
        ncols = len(headers)
        for label, idx in (("date_col", date_col), ("amount_col", amount_col)):
            if idx >= ncols:
                return jsonify({"error": f"{label} out of range"}), 400
        if store_col is not None and store_col >= ncols:
            return jsonify({"error": "store_col out of range"}), 400

        signature = format_signature(headers, delimiter)
        col_map = {"date": date_col, "amount": amount_col}
        if store_col is not None:
            col_map["store"] = store_col

        cursor = conn.execute(
            "INSERT INTO import_batches (user_id, filename) VALUES (%s, %s) RETURNING id",
            (uid, filename),
        )
        batch_id = cursor.fetchone()["id"]

        _stage_rows(conn, uid, batch_id, reader, col_map, amount_sign)

        if remember:
            conn.execute("""
                INSERT INTO import_formats
                    (user_id, signature, delimiter, date_col, amount_col, store_col, amount_sign)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, signature) DO UPDATE SET
                    delimiter   = EXCLUDED.delimiter,
                    date_col    = EXCLUDED.date_col,
                    amount_col  = EXCLUDED.amount_col,
                    store_col   = EXCLUDED.store_col,
                    amount_sign = EXCLUDED.amount_sign
            """, (uid, signature, delimiter, date_col, amount_col, store_col, amount_sign))

        conn.commit()
        return _staging_response(conn, uid, batch_id)
    except Exception as e:
        conn.rollback()
        if batch_id is not None:
            try:
                _cleanup_import_batch(conn, batch_id, uid)
                conn.commit()
            except db.DatabaseError:
                conn.rollback()
        status = 400 if isinstance(e, (csv.Error, ValueError)) else 500
        return jsonify({"error": f"Import failed: {e}"}), status
    finally:
        conn.close()


@app.route("/api/import/formats", methods=["GET"])
def list_import_formats():
    """Return this user's saved CSV-format mappings (for a Settings/Import list)."""
    uid = current_user_id()
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id, signature, delimiter, date_col, amount_col, store_col, "
            "amount_sign, created_at FROM import_formats "
            "WHERE user_id = %s ORDER BY created_at DESC, id DESC",
            (uid,),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/import/formats/<int:format_id>", methods=["DELETE"])
def delete_import_format(format_id):
    """Delete one saved CSV-format mapping. 404 if it isn't this user's."""
    uid = current_user_id()
    with db_conn() as conn:
        row = conn.execute(
            "DELETE FROM import_formats WHERE id = %s AND user_id = %s RETURNING id",
            (format_id, uid),
        ).fetchone()
    if not row:
        return jsonify({"error": "Format not found"}), 404
    return "", 204


@app.route("/api/import/staging/<int:batch_id>")
def get_staging(batch_id):
    uid = current_user_id()
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM import_staging "
            "WHERE import_batch_id = %s AND user_id = %s ORDER BY date",
            (batch_id, uid),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/import/confirm", methods=["POST"])
def confirm_imports():
    """Confirm staged imports. Handles normal items and splits (staging_id + amount override).

    The whole confirm is atomic: either every staged row is committed together
    or — if any item is malformed or a write fails — nothing is committed.
    """
    uid = current_user_id()
    data = request.json or {}
    items = data.get("items")
    if not isinstance(items, list):
        return jsonify({"error": "Missing or invalid 'items' list"}), 400

    try:
        with db_conn() as conn:
            confirmed_staging_ids = set()

            for item in items:
                # splits pass staging_id; normal items pass id
                staging_id = item.get("staging_id") or item.get("id")
                if staging_id is None:
                    raise ValueError("Item is missing a staging id")
                staging = conn.execute(
                    "SELECT * FROM import_staging WHERE id = %s AND user_id = %s",
                    (staging_id, uid),
                ).fetchone()
                if not staging:
                    continue

                amount = item.get("amount", staging["amount"])
                store  = item.get("store",  staging["store"])
                # Normalize whatever the client sent (ISO or D.M.YYYY) to ISO;
                # never let a raw display string reach the transactions table.
                date = parse_date(str(item.get("date", staging["date"])))
                if not date:
                    raise ValueError(f"Unrecognized date on item {staging_id}")

                # The target category must belong to this user.
                cat = conn.execute(
                    "SELECT 1 FROM categories WHERE id = %s AND user_id = %s",
                    (item["category_id"], uid),
                ).fetchone()
                if not cat:
                    raise ValueError("Invalid category for import item")

                conn.execute("""
                    INSERT INTO transactions (user_id, date, store, category_id, amount, type)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (uid, date, store, item["category_id"], amount,
                      item.get("type", "expense")))

                if staging_id not in confirmed_staging_ids:
                    conn.execute("""
                        UPDATE import_staging SET confirmed = 1, final_category_id = %s
                        WHERE id = %s AND user_id = %s
                    """, (item["category_id"], staging_id, uid))
                    confirmed_staging_ids.add(staging_id)

            batch_id = data.get("batch_id")
            if batch_id:
                conn.execute(
                    "UPDATE import_batches SET status = 'completed' "
                    "WHERE id = %s AND user_id = %s",
                    (batch_id, uid),
                )
    except (KeyError, ValueError, TypeError) as e:
        # Malformed item payload — db_conn() already rolled back.
        return jsonify({"error": f"Invalid import item: {e}"}), 400
    except db.DatabaseError as e:
        return jsonify({"error": f"Database error during import: {e}"}), 500

    _bump_data_version()
    # Auto-retrain merchant rules so the corrections made during this review
    # feed the next import's suggestions. Runs after the confirm committed, in
    # its own transaction — a training failure must never undo the import.
    retrained = None
    try:
        with db_conn() as conn:
            retrained, _ = _rebuild_merchant_rules(conn, uid)
    except db.DatabaseError:
        pass

    return jsonify({"status": "ok", "rules_retrained": retrained})


@app.route("/api/import/staging/<int:item_id>", methods=["DELETE"])
def delete_staging_item(item_id):
    uid = current_user_id()
    with db_conn() as conn:
        conn.execute(
            "DELETE FROM import_staging WHERE id = %s AND user_id = %s",
            (item_id, uid),
        )
    return "", 204


# ── Hosted Open Banking import (Enable Banking) ─────────────────────
# In-app PSD2 consent + per-user session storage. The desktop build bootstraps
# its bank connection from a local config file; on the hosted server there is no
# such file, so the user consents in-browser (connect → bank → callback) and the
# resulting session is stored per-user in bank_sessions. Every route below is
# behind the normal auth/status guard and scoped by current_user_id(), so one
# user can never see or use another user's bank session.

# Session key holding the per-flow CSRF nonce for the bank consent redirect. The
# bank echoes ``state`` back to the callback; we compare it to this stored value
# to reject forged/replayed callbacks (in addition to the standard CSRF on the
# mutating routes — connect/callback are top-level GET navigations).
_BANK_STATE_KEY = "bank_oauth_state"


def _bank_failed(reason, detail):
    """Send a failed consent step back to the Import tab with a reason code.

    /connect and /callback are full-page browser navigations, so returning JSON
    would leave the user staring at a raw error object. Instead we log the detail
    for whoever runs the server and bounce the browser to /#import?bank=<reason>,
    where the UI turns the code into a sentence. ``reason`` is one of
    ``not_configured``, ``auth_error``, ``error`` or ``cancelled``.
    """
    app.logger.warning("Bank consent step failed (%s): %s", reason, detail)
    return redirect(f"/#import?bank={reason}")


def _load_bank_session(uid):
    """Return the user's bank_sessions row as a dict, or None."""
    with db_conn() as conn:
        return conn.execute(
            "SELECT id, session_id, aspsp_name, aspsp_country, valid_until, accounts "
            "FROM bank_sessions WHERE user_id = %s",
            (uid,),
        ).fetchone()


@app.route("/api/import/bank/status")
def bank_status():
    """Report this user's bank-connection state for the Import UI.

    Returns ``{connected, valid_until, accounts}``. ``connected`` is True only
    when a session row exists AND its consent has not expired; an expired session
    reports ``connected=False`` but still surfaces ``valid_until`` so the UI can
    prompt a reconnect. ``configured`` tells the UI whether the server even has
    Enable Banking credentials (so it can hide the card when not).
    """
    uid = current_user_id()
    row = _load_bank_session(uid)
    if not row:
        return jsonify({
            "connected": False,
            "configured": config.enable_banking_configured(),
            "valid_until": None,
            "accounts": [],
        })
    valid = enable_banking.session_valid(row["valid_until"])
    vu = row["valid_until"]
    return jsonify({
        "connected": bool(valid),
        "expired": not valid,
        "configured": config.enable_banking_configured(),
        "valid_until": vu.isoformat() if hasattr(vu, "isoformat") else vu,
        "aspsp_name": row["aspsp_name"],
        "accounts": db.load_json(row["accounts"]) or [],
    })


@app.route("/api/import/bank/connect")
def bank_connect():
    """Start the bank consent flow: mint a CSRF state, 302 to the bank.

    A random ``state`` nonce is stored in the Flask session and passed to Enable
    Banking; the callback verifies the echoed value matches. The redirect target
    is our own callback under BANK_REDIRECT_BASE. A full-page browser redirect
    (302) is returned so the user lands on their bank's authorization page.
    """
    current_user_id()  # enforce auth (raises 401 otherwise)
    if not config.enable_banking_configured():
        return _bank_failed("not_configured", "Bank import is not configured.")

    state = secrets.token_urlsafe(32)
    session[_BANK_STATE_KEY] = state
    redirect_url = config.BANK_REDIRECT_BASE + "/api/import/bank/callback"
    try:
        auth_url = enable_banking.start_auth(
            config.ENABLE_BANKING_ASPSP,
            config.ENABLE_BANKING_COUNTRY,
            redirect_url,
            state,
        )
    except BankConfigMissing as e:
        return _bank_failed("not_configured", e)
    except BankAuthError as e:
        return _bank_failed("auth_error", e)
    except BankError as e:
        return _bank_failed("error", e)
    return redirect(auth_url)


@app.route("/api/import/bank/callback")
def bank_callback():
    """Complete the consent: verify state, create a session, store it per-user.

    The bank redirects here with ``code`` + ``state``. We reject any callback
    whose ``state`` doesn't match the value we stashed in :func:`bank_connect`
    (CSRF defence). On success we exchange the code for a session and upsert the
    user's single ``bank_sessions`` row (delete-then-insert under UNIQUE(user_id)),
    then bounce back to the Import page.
    """
    uid = current_user_id()

    expected = session.pop(_BANK_STATE_KEY, None)
    got = request.args.get("state")
    if not expected or not got or not secrets.compare_digest(str(got), str(expected)):
        return jsonify({"error": "Invalid or missing state (CSRF check failed)."}), 400

    # The bank may redirect back with an error instead of a code (user declined).
    if request.args.get("error") or not request.args.get("code"):
        return redirect("/#import?bank=cancelled")

    code = request.args.get("code")
    try:
        result = enable_banking.create_session(code)
    except BankConfigMissing as e:
        return _bank_failed("not_configured", e)
    except BankAuthError as e:
        return _bank_failed("auth_error", e)
    except BankError as e:
        return _bank_failed("error", e)

    with db_conn() as conn:
        # Delete-then-insert keeps the UNIQUE(user_id) "one active connection"
        # invariant on reconnect without relying on ON CONFLICT semantics.
        conn.execute("DELETE FROM bank_sessions WHERE user_id = %s", (uid,))
        conn.execute(
            "INSERT INTO bank_sessions "
            "(user_id, session_id, aspsp_name, aspsp_country, valid_until, accounts) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                uid,
                result["session_id"],
                config.ENABLE_BANKING_ASPSP,
                config.ENABLE_BANKING_COUNTRY,
                result.get("valid_until"),
                db.Json(result.get("accounts") or []),
            ),
        )
    return redirect("/#import?bank=connected")


@app.route("/api/import/bank/fetch", methods=["POST"])
@limiter.limit("30/hour")
def bank_fetch():
    """Fetch transactions from the bank into the standard import review pipeline.

    Body: ``{account_uid, date_from, date_to}``. Loads the user's bank session
    (401 ``not_connected`` / ``session_expired`` when missing/expired), validates
    the dates, then creates an ``import_batches`` row and stages each normalized
    transaction into ``import_staging`` using the existing per-user
    ``suggest_category``. Returns ``{batch_id, count, items}`` — the SAME shape as
    /api/import/upload so the frontend reuses the review table.
    """
    uid = current_user_id()
    data = request.json or {}
    account_uid = data.get("account_uid")
    date_from = data.get("date_from")
    date_to = data.get("date_to")

    if not account_uid:
        return jsonify({"error": "account_uid is required"}), 400
    if not _valid_iso_date(date_from) or not _valid_iso_date(date_to):
        return jsonify({"error": "date_from and date_to must be YYYY-MM-DD"}), 400
    if date_from > date_to:
        return jsonify({"error": "date_from must be on or before date_to"}), 400

    row = _load_bank_session(uid)
    if not row:
        return jsonify({"error": "not_connected"}), 401
    if not enable_banking.session_valid(row["valid_until"]):
        return jsonify({"error": "session_expired"}), 401

    # Guard: the requested account must belong to this user's stored session.
    account_uids = {a.get("uid") for a in (db.load_json(row["accounts"]) or []) if isinstance(a, dict)}
    if account_uids and account_uid not in account_uids:
        return jsonify({"error": "unknown account for this connection"}), 400

    try:
        txns = enable_banking.get_transactions(
            row["session_id"], account_uid, date_from, date_to
        )
    except SessionExpired:
        return jsonify({"error": "session_expired"}), 401
    except BankConfigMissing as e:
        return jsonify({"error": str(e)}), 400
    except BankAuthError as e:
        app.logger.warning("Enable Banking refused the app credentials: %s", e)
        return jsonify({"error": "bank_auth"}), 502
    except BankError as e:
        return jsonify({"error": f"Bank error: {e}"}), 502

    aspsp = row["aspsp_name"] or "Bank"
    filename = f"Bank · {aspsp} · {date_from}..{date_to}"

    conn = get_db()
    batch_id = None
    try:
        cursor = conn.execute(
            "INSERT INTO import_batches (user_id, filename) VALUES (%s, %s) RETURNING id",
            (uid, filename),
        )
        batch_id = cursor.fetchone()["id"]

        for txn in txns:
            suggested = suggest_category(txn["store"], conn, uid)
            conn.execute(
                "INSERT INTO import_staging "
                "(user_id, date, store, suggested_category, amount, type, import_batch_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (uid, txn["date"], txn["store"], suggested, txn["amount"],
                 txn["type"], batch_id),
            )
        conn.commit()
        return _staging_response(conn, uid, batch_id)
    except Exception as e:
        conn.rollback()
        if batch_id is not None:
            try:
                _cleanup_import_batch(conn, batch_id, uid)
                conn.commit()
            except db.DatabaseError:
                conn.rollback()
        return jsonify({"error": f"Import failed: {e}"}), 500
    finally:
        conn.close()


@app.route("/api/import/bank/disconnect", methods=["POST"])
def bank_disconnect():
    """Forget this user's bank connection (delete their bank_sessions row)."""
    uid = current_user_id()
    with db_conn() as conn:
        conn.execute("DELETE FROM bank_sessions WHERE user_id = %s", (uid,))
    return jsonify({"status": "ok"})


def _valid_iso_date(value):
    """True when ``value`` is a 'YYYY-MM-DD' string parseable as a real date."""
    if not isinstance(value, str):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


# ── Backups ────────────────────────────────────────────────────────

@app.route("/api/backups")
def get_backups():
    current_user_id()
    return jsonify(list_backups())


@app.route("/api/backups", methods=["POST"])
def create_backup():
    current_user_id()
    reason = (request.json or {}).get("reason", "manual")
    path = backup_db(reason)
    return jsonify({"path": path, "ok": path is not None})


# ── Quit ──────────────────────────────────────────────────────────

@app.route("/api/quit", methods=["POST"])
def quit_app():
    # Quitting kills the local process — there is only ever the one.
    import os, signal
    backup_db("on-quit")
    os.kill(os.getpid(), signal.SIGTERM)
    return "", 204


# ── Main ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app.run(debug=DEBUG, port=SERVER_PORT)
