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
    # `desktop_shell` is set by main.py and absent when the server is run on
    # its own for development. It reserves the strip the window's own
    # close/minimise/zoom buttons are drawn into, which exists in the app and
    # not in a browser tab.
    return render_template(
        "index.html", desktop_shell=getattr(config, "DESKTOP_SHELL", False))


@bp.route("/api/window/appearance", methods=["POST"])
def window_appearance():
    """The page telling the window which theme it is wearing.

    The title bar is drawn by macOS in an appearance of its own, and on a Mac
    set to Dark Mode that is a black strip above a white sidebar — the "black
    bar" the window used to wear. The theme lives in the browser's
    localStorage, so only the page can say which one is on.

    This works because the server runs inside the desktop process: the hook
    main.py registers reaches the real window. Outside it there is no hook and
    this does nothing, which is right for a browser tab.
    """
    theme = (request.get_json(silent=True) or {}).get("theme")
    if theme not in ("light", "dark"):
        return jsonify({"error": "theme must be 'light' or 'dark'"}), 400
    hook = getattr(config, "WINDOW_THEME_HOOK", None)
    if hook is not None:
        hook(theme)
    return jsonify({"applied": hook is not None, "theme": theme})


@bp.route("/api/window/<action>", methods=["POST"])
def window_action(action):
    """Close, minimise or zoom the window.

    The window is frameless — macOS draws it no title bar and therefore no
    buttons — so the page draws its own, and they call this. Same trick as the
    appearance route: the server runs inside the desktop process, so the hook
    main.py registers reaches the real window.
    """
    if action not in ("close", "minimise", "zoom"):
        return jsonify({"error": f"unknown window action {action!r}"}), 400
    hook = getattr(config, "WINDOW_ACTION_HOOK", None)
    if hook is not None:
        hook(action)
    return jsonify({"applied": hook is not None, "action": action})


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
