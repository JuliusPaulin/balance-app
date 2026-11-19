"""The Flask app over a real local socket, not the test client.

Synthetic transaction only. The database is the suite's throwaway file.
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

import config

ROOT = Path(__file__).resolve().parents[2]


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait(session, url):
    last = None
    for _ in range(50):
        try:
            response = session.get(url, timeout=0.5)
            if response.status_code == 200:
                return response
            last = response.status_code
        except requests.RequestException as exc:
            last = exc
        time.sleep(0.1)
    raise AssertionError(f"server did not answer {url}: {last}")


def test_a_local_server_accepts_a_transaction():
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable, "-c",
            "import app\n"
            "app.app.run(host='127.0.0.1', port=app.SERVER_PORT, use_reloader=False)",
        ],
        cwd=ROOT,
        env={**os.environ, "PORT": str(port), "SQLITE_PATH": config.SQLITE_PATH},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    session = requests.Session()
    base = f"http://127.0.0.1:{port}"
    try:
        health = _wait(session, base + "/healthz")
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
