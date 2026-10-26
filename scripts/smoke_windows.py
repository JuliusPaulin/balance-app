"""Exercise the packaged Windows window and SQLite persistence in a scratch home."""

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import time

import requests
import websocket
from PIL import ImageGrab


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(check, process, timeout=90):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Balance exited early: {process.returncode}")
        try:
            value = check()
            if value:
                return value
        except (requests.RequestException, OSError, ValueError) as exc:
            last = exc
        time.sleep(0.3)
    raise RuntimeError(f"Balance did not become ready: {last}")


def window_handle(pid):
    handles = []
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(handle, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(handle):
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(handle, title, len(title))
            if title.value == "Balance.":
                handles.append(handle)
        return True

    user32.EnumWindows(visit, 0)
    return handles[0] if handles else None


class DevTools:
    def __init__(self, target):
        self.socket = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=15, suppress_origin=True)
        self.next_id = 0

    def call(self, method, params=None):
        self.next_id += 1
        self.socket.send(json.dumps({"id": self.next_id, "method": method,
                                    "params": params or {}}))
        while True:
            reply = json.loads(self.socket.recv())
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise RuntimeError(reply["error"])
                return reply["result"]

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {
            "expression": expression, "awaitPromise": True, "returnByValue": True})
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result["result"].get("value")


def smoke(executable, with_ai=False):
    if sys.platform != "win32":
        raise RuntimeError("Run this smoke test on Windows")
    executable = Path(executable).resolve()
    output = Path("build/windows-smoke").resolve()
    output.mkdir(parents=True, exist_ok=True)
    version = Path("VERSION").read_text(encoding="utf-8").strip()
    with tempfile.TemporaryDirectory(prefix="Balance smoke ä space ") as scratch:
        home = Path(scratch)
        db = home / "AppData" / "Balance" / "expenses.db"
        created = None
        for attempt in range(2):
            port, debug_port, model_port = free_port(), free_port(), free_port()
            base = f"http://127.0.0.1:{port}"
            env = {**os.environ, "SQLITE_PATH": str(db), "PORT": str(port),
                   "LOCALAPPDATA": str(home / "AppData"),
                   "MODEL_PATH": str(db.parent / "models" / "missing.gguf"),
                   "LLAMACPP_PORT": str(model_port), "AI_BACKEND": "bundled",
                   "START_MAXIMIZED": "0", "CSRF_ENABLED": "1",
                   "BALANCE_WEBVIEW_DEBUG_PORT": str(debug_port)}
            process = subprocess.Popen([str(executable)], cwd=home, env=env)
            devtools = None
            try:
                session = requests.Session()

                def app_ready():
                    reply = session.get(base + "/api/me", timeout=2)
                    reply.raise_for_status()
                    return reply.json()

                me = wait_until(app_ready, process)
                assert me["version"] == version, me
                session.headers["X-CSRF-Token"] = me["csrf_token"]
                window = wait_until(lambda: window_handle(process.pid), process)
                assert session.get(base + "/healthz/db", timeout=5).json()["db"]
                page = session.get(base, timeout=5)
                assert page.status_code == 200 and 'class="desktop-shell"' not in page.text
                for asset in ("/static/js/main.js", "/static/css/style.css"):
                    assert session.get(base + asset, timeout=5).status_code == 200
                status = session.get(base + "/api/chat/status", timeout=10).json()
                assert status["state"] == "needs_download", status
                assert status["accelerator"] == "cpu", status

                if attempt == 0:
                    categories = session.get(base + "/api/categories", timeout=5).json()
                    category = next(c for c in categories
                                    if c["name"] == "Groceries" and c["type"] == "expense")
                    reply = session.post(base + "/api/transactions", json={
                        "date": "2026-01-15", "store": "Windows smoke ä",
                        "category_id": category["id"], "amount": 12.34,
                        "type": "expense"}, timeout=5)
                    assert reply.status_code == 201, reply.text
                    created = reply.json()["id"]
                listed = session.get(base + "/api/transactions", timeout=5).json()
                assert listed["total"] == 1 and listed["items"][0]["id"] == created, listed

                def webview_target():
                    reply = requests.get(f"http://127.0.0.1:{debug_port}/json/list", timeout=2)
                    reply.raise_for_status()
                    return next((t for t in reply.json()
                                 if t.get("type") == "page" and t.get("url", "").startswith(base)), None)

                devtools = DevTools(wait_until(webview_target, process))
                wait_until(lambda: devtools.evaluate(
                    "document.readyState === 'complete' && typeof Chart === 'function' "
                    "&& typeof loadDashboard === 'function' && typeof charts !== 'undefined' "
                    "&& !!charts.monthly"), process)
                # Exercise the actual renderer, navigation, and theme code.
                assert devtools.evaluate("(async () => { await loadDashboard(); "
                    "document.querySelector('[data-page=transactions]').click(); "
                    "await loadTransactions(); return document.body.innerText.includes('Windows smoke ä'); })()")
                devtools.evaluate("localStorage.setItem('theme', 'dark'); applyThemeAttr('dark'); true")
                assert devtools.evaluate("document.documentElement.dataset.theme === 'dark'")
                wait_until(lambda: devtools.evaluate(
                    "_loadingCount === 0 && !document.documentElement.classList.contains('loading')"),
                    process)
                devtools.evaluate("(async () => { await document.fonts.ready; "
                    "await new Promise(r => setTimeout(r, 400)); return true; })()")
                if attempt == 1:
                    screenshot = devtools.call("Page.captureScreenshot", {"format": "png"})
                    (output / "desktop.png").write_bytes(base64.b64decode(screenshot["data"]))
                    if with_ai:
                        reply = session.post(base + "/api/chat/download", timeout=10)
                        reply.raise_for_status()

                        def model_ready():
                            state = session.get(base + "/api/chat/status", timeout=10).json()
                            if state["state"] in ("download_failed", "start_failed"):
                                raise RuntimeError(state)
                            return state if state["state"] == "ready" else None

                        wait_until(model_ready, process, timeout=1200)
                        reply = requests.post(
                            f"http://127.0.0.1:{model_port}/v1/chat/completions",
                            json={"messages": [{"role": "user", "content": "Say OK."}],
                                  "max_tokens": 16, "temperature": 0,
                                  "chat_template_kwargs": {"enable_thinking": False}},
                            timeout=180)
                        reply.raise_for_status()
                        assert reply.json()["choices"][0]["message"]["content"].strip(), reply.text
                        print("Bundled Windows runtime generated a model reply", flush=True)
                ctypes.windll.user32.SetForegroundWindow(wintypes.HWND(window))
                ImageGrab.grab().save(output / "native-desktop.png")
                devtools.socket.close()
                devtools = None
                # A real WM_CLOSE tests the native titlebar's shutdown path.
                ctypes.windll.user32.PostMessageW(wintypes.HWND(window), 0x0010, 0, 0)
                assert process.wait(timeout=30) == 0
                if with_ai and attempt == 1:
                    try:
                        requests.get(f"http://127.0.0.1:{model_port}/health", timeout=2)
                    except requests.RequestException:
                        pass
                    else:
                        raise AssertionError("The model server outlived the desktop app")
            finally:
                # Keep native-window evidence even if page inspection fails.
                if process.poll() is None:
                    try:
                        ImageGrab.grab().save(output / "native-desktop.png")
                    except OSError:
                        pass
                if devtools is not None:
                    devtools.socket.close()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                for log in (db.parent / "Balance.log", db.parent / "models" / "server.log"):
                    if log.is_file():
                        shutil.copy2(log, output / log.name)
        report = {"version": version, "launches": 2, "native_window": True,
                  "webview2_rendered": True, "transactions_persisted": True,
                  "dark_theme": True, "ai_runtime": status["runtime_build"],
                  "native_close": True, "scratch_database": True,
                  "model_inference": with_ai}
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    smoke(sys.argv[1], with_ai="--with-ai" in sys.argv[2:])
