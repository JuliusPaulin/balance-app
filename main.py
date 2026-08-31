"""Launch Expense Tracker as a desktop app using pywebview."""
import sys
import threading
import webview
import config
from app import app, SERVER_PORT
from database import init_db, seed_local_user, backup_db


def start_server():
    app.run(port=SERVER_PORT, use_reloader=False)


def _warm_model():
    """Start the bundled model server if its weights are already here.

    Loading three gigabytes takes a few seconds, and they can pass while the
    dashboard draws rather than after the first question is asked. Allowed to
    fail quietly: no model yet is an ordinary first run, and the panel says so.
    """
    try:
        from model_runtime import ensure_running
        ensure_running()
    except Exception:
        pass


if __name__ == "__main__":
    # Local SQLite bootstrap: create the schema, take a safety backup of any
    # existing DB, and ensure the single local user + default categories exist.
    init_db()
    backup_db("launch")
    seed_local_user()

    server = threading.Thread(target=start_server, daemon=True)
    server.start()

    # Fill the screen on launch. The 1200x800 box was a starting size nobody
    # kept: the Transactions rail beside its table, and the dashboard's cards,
    # both want the width. width/height stay as the size to fall back to when
    # the window is un-maximized. See config.START_MAXIMIZED / START_FULLSCREEN.
    window = webview.create_window(
        "Balance.",
        f"http://127.0.0.1:{SERVER_PORT}",
        width=1200,
        height=800,
        min_size=(900, 600),
        maximized=config.START_MAXIMIZED,
        fullscreen=config.START_FULLSCREEN,
    )

    def on_closed():
        sys.exit(0)

    window.events.closed += on_closed

    webview.start()
    sys.exit(0)
