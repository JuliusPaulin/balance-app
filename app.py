"""Balance — the Flask server.

The app object and the shared request plumbing live in :mod:`core`; the routes
live in :mod:`routes`, one module per area. This file is the wiring: it puts the
two together and runs the server.

``main.py`` (the pywebview desktop shell) imports ``app`` and ``SERVER_PORT``
from here, so both names stay put.
"""

import config
import routes
from core import app

# Single source of truth for the local server port. Sourced from config (which
# reads the PORT env var). main.py imports SERVER_PORT so the window and the
# Flask server can never drift apart.
SERVER_PORT = config.PORT

# Debug mode is off by default for the packaged desktop app (the Werkzeug
# debugger/reloader must never ship to end users). Opt in for local dev with
# FLASK_DEBUG=1.
DEBUG = config.DEBUG

routes.register(app)


if __name__ == "__main__":
    app.run(debug=DEBUG, port=SERVER_PORT)
