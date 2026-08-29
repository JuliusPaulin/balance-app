"""Launch Expense Tracker as a desktop app using pywebview."""
import sys
import threading
import webview
from app import app, SERVER_PORT
from database import init_db, seed_local_user, backup_db


def start_server():
    app.run(port=SERVER_PORT, use_reloader=False)


if __name__ == "__main__":
    # Local SQLite bootstrap: create the schema, take a safety backup of any
    # existing DB, and ensure the single local user + default categories exist.
    init_db()
    backup_db("launch")
    seed_local_user()

    server = threading.Thread(target=start_server, daemon=True)
    server.start()

    window = webview.create_window(
        "Balance.",
        f"http://127.0.0.1:{SERVER_PORT}",
        width=1200,
        height=800,
        min_size=(900, 600),
    )

    def on_closed():
        sys.exit(0)

    window.events.closed += on_closed

    webview.start()
    sys.exit(0)
