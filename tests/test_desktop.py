import builtins
from types import SimpleNamespace

import pytest

import config
import main


def test_default_database_paths_follow_the_platform(tmp_path):
    root = str(tmp_path)
    assert config._default_sqlite_path(
        "win32", {"LOCALAPPDATA": root}, root
    ) == str(tmp_path / "Balance" / "expenses.db")
    assert config._default_sqlite_path(
        "win32", {}, root
    ) == str(tmp_path / "AppData" / "Local" / "Balance" / "expenses.db")
    assert config._default_sqlite_path(
        "darwin", {}, root
    ) == str(tmp_path / "Library" / "Application Support" / "Balance" / "expenses.db")
    assert config._default_sqlite_path(
        "linux", {"XDG_DATA_HOME": root}, root
    ) == str(tmp_path / "Balance" / "expenses.db")
    assert config._default_sqlite_path(
        "linux", {}, root
    ) == str(tmp_path / ".local" / "share" / "Balance" / "expenses.db")


def test_windows_window_uses_native_chrome_and_edgechromium(monkeypatch):
    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setattr(config, "START_MAXIMIZED", True)
    monkeypatch.setattr(config, "START_FULLSCREEN", False)

    options = main._window_options()

    assert options["frameless"] is False
    assert options["maximized"] is True
    assert options["fullscreen"] is False
    assert options["width"] == 1200
    assert options["height"] == 800
    assert "screen" not in options
    assert main._webview_gui() == "edgechromium"


def test_macos_window_keeps_frameless_screen_sized_launch(monkeypatch):
    screen = SimpleNamespace(width=1920, height=1080)
    monkeypatch.setattr(main.sys, "platform", "darwin")
    monkeypatch.setattr(main, "_launch_screen", lambda: screen)

    options = main._window_options()

    assert options["frameless"] is True
    assert options["maximized"] is False
    assert options["screen"] is screen
    assert options["width"] == 1920
    assert options["height"] == 1080
    assert main._webview_gui() is None


def test_no_console_windows_stdio_uses_a_local_log(monkeypatch, tmp_path):
    log_path = tmp_path / "Balance.log"
    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setattr(main.sys, "stdout", None)
    monkeypatch.setattr(main.sys, "stderr", None)
    monkeypatch.setattr(config, "SQLITE_PATH", str(tmp_path / "expenses.db"))
    monkeypatch.setattr(main, "_stdio_log", None)

    try:
        assert main._ensure_windows_stdio() == str(log_path)
        assert main.sys.stdout is main.sys.stderr
        main.sys.stderr.write("desktop startup diagnostic\n")
        main.sys.stderr.flush()
        assert "desktop startup diagnostic" in log_path.read_text(encoding="utf-8")
    finally:
        if main._stdio_log is not None:
            main._stdio_log.close()


def test_local_server_binds_loopback_and_waits_for_health(monkeypatch):
    import werkzeug.serving

    calls = []
    fake_server = SimpleNamespace(
        server_address=("127.0.0.1", 43210), serve_forever=lambda: None)

    class FakeThread:
        def __init__(self, target, **kwargs):
            self.target = target
            calls.append(("thread", kwargs))

        def start(self):
            calls.append("started")

    def make_server(host, port, app, **kwargs):
        calls.append(("bind", host, port, kwargs))
        return fake_server

    def wait_for_server(port, timeout):
        calls.append(("ready", port, timeout))

    monkeypatch.setattr(werkzeug.serving, "make_server", make_server)
    monkeypatch.setattr(main.threading, "Thread", FakeThread)
    monkeypatch.setattr(main, "_wait_for_server", wait_for_server)

    server, thread = main._start_local_server(port=0, ready_timeout=2)

    assert server is fake_server
    assert isinstance(thread, FakeThread)
    assert calls[0][0:3] == ("bind", "127.0.0.1", 0)
    assert calls[1] == ("thread", {"name": "balance-http", "daemon": True})
    assert calls[2] == "started"
    assert calls[3] == ("ready", 43210, 2)


def test_readiness_probe_uses_loopback_health_endpoint(monkeypatch):
    calls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def open_url(url, timeout):
        calls.append((url, timeout))
        return Response()

    monkeypatch.setattr(main, "urlopen", open_url)

    main._wait_for_server(43210, timeout=1)

    assert calls[0][0] == "http://127.0.0.1:43210/healthz"
    assert 0 < calls[0][1] <= 0.5


def test_windows_bootstrap_precedes_window_and_never_imports_appkit(
        monkeypatch):
    calls = []

    class Event:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, callback):
            self.handlers.append(callback)
            return self

    window = SimpleNamespace(events=SimpleNamespace(shown=Event()))
    fake_server = SimpleNamespace(server_address=("127.0.0.1", 45678))
    fake_thread = SimpleNamespace(is_alive=lambda: True)

    class FakeWebview:
        def create_window(self, title, url, **options):
            calls.append(("window", title, url, options))
            return window

        def start(self, gui=None):
            calls.append(("webview", gui))
            assert config.DESKTOP_SHELL is True
            assert config.WINDOW_ACTION_HOOK is not None

    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setattr(main, "webview", FakeWebview())
    monkeypatch.setattr(main, "_ensure_windows_stdio", lambda: None)
    monkeypatch.setattr(main, "init_db", lambda: calls.append("init"))
    monkeypatch.setattr(main, "backup_db", lambda reason: calls.append(f"backup:{reason}"))
    monkeypatch.setattr(main, "seed_local_user", lambda: calls.append("seed"))
    monkeypatch.setattr(
        main, "_start_local_server",
        lambda: (calls.append("ready") or (fake_server, fake_thread)))
    monkeypatch.setattr(main, "_stop_local_server", lambda *_: calls.append("stop-server"))
    monkeypatch.setattr(main, "_stop_model", lambda: calls.append("stop-model"))
    monkeypatch.setattr(config, "AI_BACKEND", "local")
    monkeypatch.setattr(config, "DESKTOP_SHELL", False, raising=False)

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"AppKit", "webview.platforms.cocoa"} or name.startswith("PyObjCTools"):
            raise AssertionError(f"Windows launcher imported macOS module {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    assert main.main() == 0

    assert [call if isinstance(call, str) else call[0] for call in calls] == [
        "init", "backup:launch", "seed", "ready", "window", "webview",
        "stop-model", "stop-server",
    ]
    _, title, url, options = calls[4]
    assert title == "Balance."
    assert url == "http://127.0.0.1:45678"
    assert options["frameless"] is False
    assert calls[5] == ("webview", "edgechromium")


@pytest.mark.parametrize(
    ("platform_name", "has_mac_controls"),
    [("win32", False), ("darwin", True)],
)
def test_index_only_renders_custom_controls_for_the_macos_shell(
        client, monkeypatch, platform_name, has_mac_controls):
    import routes.system as system_routes

    monkeypatch.setattr(system_routes.sys, "platform", platform_name)
    monkeypatch.setattr(config, "DESKTOP_SHELL", True, raising=False)

    response = client.get("/")

    assert response.status_code == 200
    assert (b'class="desktop-shell"' in response.data) is has_mac_controls


def test_quit_route_backs_up_then_closes_the_desktop_window(client, monkeypatch):
    import routes.system as system_routes

    calls = []
    monkeypatch.setattr(system_routes, "backup_db", lambda reason: calls.append(reason))
    monkeypatch.setattr(config, "WINDOW_ACTION_HOOK", lambda action: calls.append(action),
                        raising=False)

    response = client.post("/api/quit")

    assert response.status_code == 204
    assert calls == ["on-quit", "close"]
