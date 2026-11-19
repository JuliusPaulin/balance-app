"""The Flask app over a real local socket, not the test client.

Synthetic transaction only. The database is the suite's throwaway file.

The server is started the way ``main.py`` starts it, ``app.run()``, with one
step taken out. Werkzeug binds the port, then asks for the name of the bind
address (``socket.getfqdn("127.0.0.1")``), and only then listens. The name only
fills in a default ``SERVER_NAME``. On a normal Mac the lookup is instant, but
on some GitHub macOS runners the first reverse lookup waits on the runner's
upstream DNS and takes 35 seconds, even with 127.0.0.1 in /etc/hosts. While a
socket is bound but not listening, macOS drops a connection instead of
refusing it, so the test saw connect timeouts and gave up: that is what failed
the v1.16.0 release. The child below answers the lookup itself, so the test is
about the app, not about the machine's DNS. The deadline is still long, and a
failure says which state the server was in.
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

import config

ROOT = Path(__file__).resolve().parents[2]


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Wall-clock, not a count of tries: a try that times out costs half a second
# and one that is refused costs nothing, so a count meant anything from 5 to 30
# seconds depending on which the machine did.
STARTUP_DEADLINE = 90


def _wait(session, url, proc, log):
    deadline = time.monotonic() + STARTUP_DEADLINE
    last = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"server exited with {proc.returncode} before answering:\n{_output(log)}")
        try:
            response = session.get(url, timeout=0.5)
            if response.status_code == 200:
                return response
            last = response.status_code
        except requests.RequestException as exc:
            last = exc
        time.sleep(0.1)
    output = _output(log)
    hint = ""
    if "Running on" not in output:
        hint = ("\nThe server never reached listen(): it was still importing "
                "the app or binding the port.")
    raise AssertionError(
        f"server did not answer {url} in {STARTUP_DEADLINE}s: {last}{hint}\n{output}")


def _output(log):
    log.flush()
    log.seek(0)
    return log.read().decode("utf-8", "replace")[-4000:]


def test_a_local_server_accepts_a_transaction():
    port = _free_port()
    # A file, not a pipe: nothing reads a pipe until the end, and a server that
    # logs enough to fill one blocks on its next write.
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        [
            sys.executable, "-c",
            # Werkzeug resolves the bind address's name before it listens; see
            # the module docstring. Answer it here instead of from DNS.
            "import socket\n"
            "socket.getfqdn = lambda name='': name or 'localhost'\n"
            "import app\n"
            "app.app.run(host='127.0.0.1', port=app.SERVER_PORT, use_reloader=False)",
        ],
        cwd=ROOT,
        env={**os.environ, "PORT": str(port), "SQLITE_PATH": config.SQLITE_PATH},
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    session = requests.Session()
    base = f"http://127.0.0.1:{port}"
    try:
        health = _wait(session, base + "/healthz", proc, log)
        assert health.json()["status"] == "ok"
        db_health = session.get(base + "/healthz/db", timeout=2)
        assert db_health.status_code == 200
        assert db_health.json()["db"] is True
        page = session.get(base + "/", timeout=2)
        assert page.status_code == 200
        assert "Balance" in page.text
        token = session.get(base + "/api/me", timeout=2).json()["csrf_token"]
        categories = session.get(base + "/api/categories", timeout=2).json()
        groceries = next(
            row["id"] for row in categories
            if row["name"] == "Groceries" and row["type"] == "expense"
        )
        created = session.post(
            base + "/api/transactions",
            json={
                "date": "2024-06-01",
                "store": "Corner shop",
                "category_id": groceries,
                "amount": 6.5,
                "type": "expense",
            },
            headers={"X-CSRF-Token": token},
            timeout=2,
        )
        assert created.status_code == 201, created.text
        listed = session.get(
            base + "/api/transactions", params={"q": "Corner shop"}, timeout=2,
        )
        assert listed.status_code == 200
        assert any(row["store"] == "Corner shop" for row in listed.json()["items"])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log.close()
