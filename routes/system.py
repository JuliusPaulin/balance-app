"""The app's own endpoints: the page itself, who the user is, health checks,
backups and quit."""

from flask import Blueprint, request, jsonify, render_template
from data.schema import db_conn, backup_db, list_backups
import config
from core import current_user_id, _ensure_csrf_token

bp = Blueprint("system", __name__)


@bp.route("/healthz")
def healthz():
    """Liveness probe for Render health checks. No auth, minimal work (no DB)."""
    return jsonify({"status": "ok"})


@bp.route("/healthz/db")
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

@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/api/me")
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


# ── Backups ────────────────────────────────────────────────────────

@bp.route("/api/backups")
def get_backups():
    current_user_id()
    return jsonify(list_backups())


@bp.route("/api/backups", methods=["POST"])
def create_backup():
    current_user_id()
    reason = (request.json or {}).get("reason", "manual")
    path = backup_db(reason)
    return jsonify({"path": path, "ok": path is not None})


# ── Quit ──────────────────────────────────────────────────────────

@bp.route("/api/quit", methods=["POST"])
def quit_app():
    # Quitting kills the local process — there is only ever the one.
    import os, signal
    backup_db("on-quit")
    os.kill(os.getpid(), signal.SIGTERM)
    return "", 204
