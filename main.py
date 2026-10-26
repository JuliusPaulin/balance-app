"""Launch Expense Tracker as a desktop app using pywebview."""
import os
import sys
import threading
import time
import traceback
from urllib.error import URLError
from urllib.request import urlopen

import webview
import config

SERVER_HOST = "127.0.0.1"
SERVER_READY_TIMEOUT = 10.0
_stdio_log = None


def _wait_for_server(port, timeout=SERVER_READY_TIMEOUT):
    """Wait for the local Flask app to answer its DB-free health endpoint."""
    deadline = time.monotonic() + timeout
    url = f"http://{SERVER_HOST}:{port}/healthz"
    last_error = None
    while time.monotonic() < deadline:
        remaining = max(0.01, deadline - time.monotonic())
        try:
            with urlopen(url, timeout=min(0.5, remaining)) as response:
                if response.status == 200:
                    return
                last_error = RuntimeError(
                    f"local server returned HTTP {response.status}")
        except (OSError, URLError) as exc:
            last_error = exc
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    raise RuntimeError(
        f"Balance's local server did not become ready within {timeout:g} seconds"
    ) from last_error


def _start_local_server(port=None, ready_timeout=SERVER_READY_TIMEOUT):
    """Bind loopback before launching the webview, then wait for Flask health."""
    from werkzeug.serving import WSGIRequestHandler, make_server

    class QuietRequestHandler(WSGIRequestHandler):
        def log_message(self, format, *args):
            # The local UI does not need an access log for every asset request.
            return

    server_port = SERVER_PORT if port is None else port
    try:
        server = make_server(
            SERVER_HOST, server_port, app,
            threaded=True, request_handler=QuietRequestHandler)
    except SystemExit as exc:
        # Werkzeug exits on bind errors instead of raising its socket error.
        raise RuntimeError(
            f"Could not bind Balance's local server to {SERVER_HOST}:{server_port}") from exc
    thread = threading.Thread(
        target=server.serve_forever, name="balance-http", daemon=True)
    try:
        thread.start()
        _wait_for_server(server.server_address[1], ready_timeout)
    except Exception:
        if thread.is_alive():
            server.shutdown()
            thread.join(timeout=2)
        server.server_close()
        raise
    return server, thread


def _stop_local_server(server, thread):
    """Stop the loopback server after the native window closes."""
    server.shutdown()
    server.server_close()
    if thread.is_alive():
        thread.join(timeout=2)


def _windows_log_path():
    return os.path.join(
        os.path.dirname(os.path.abspath(config.SQLITE_PATH)), "Balance.log")


def _ensure_windows_stdio():
    """Give no-console Windows builds durable stdout/stderr streams."""
    global _stdio_log
    if sys.platform != "win32" or (sys.stdout is not None and sys.stderr is not None):
        return None

    log_path = _windows_log_path()
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        _stdio_log = open(log_path, "a", encoding="utf-8", buffering=1)
        path = log_path
    except OSError:
        _stdio_log = open(os.devnull, "a", encoding="utf-8")
        path = None
    if sys.stdout is None:
        sys.stdout = _stdio_log
    if sys.stderr is None:
        sys.stderr = _stdio_log
    return path


# The no-console executable has ``sys.stdout`` and ``sys.stderr`` set to None.
# Set them before importing Flask's app: its logging handler captures stderr on
# first use during core.py's schema bootstrap.
if sys.platform == "win32":
    _ensure_windows_stdio()

def _load_flask_dependencies():
    from app import app, SERVER_PORT
    from data.schema import init_db, seed_local_user, backup_db

    return app, SERVER_PORT, init_db, seed_local_user, backup_db


app, SERVER_PORT, init_db, seed_local_user, backup_db = _load_flask_dependencies()


def _report_windows_startup_failure(exc, log_path=None):
    """Write a startup traceback and show a short native error message."""
    if log_path is None:
        log_path = _windows_log_path()
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log:
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=log)
    except OSError:
        pass

    try:
        import ctypes
        location = f"See {log_path} for details." if log_path else "See the app log for details."
        ctypes.windll.user32.MessageBoxW(
            None, f"Balance could not start. {location}\n\n{exc}",
            "Balance.", 0x10)
    except Exception:
        pass


TITLEBAR_H = 28.0      # the strip the window controls and the drag region sit in
CONTROLS_W = 92.0      # how much of it the buttons occupy, and must not drag


def _browser_view(window):
    """pywebview's own object for this window, or None.

    Keyed by window rather than by uid: `BrowserView.instances[uid]` handed
    back an object whose webview had no superview, which is how an hour went
    into resizing a view that was not on screen.
    """
    from webview.platforms.cocoa import BrowserView

    return next((i for i in BrowserView.instances.values()
                 if i.window is window.native), None)


def _add_drag_strip(window):
    """Make the top strip drag the window, and only the top strip.

    A frameless window has no title bar to drag, and pywebview's own
    ``easy_drag`` drags from anywhere in the page — so selecting a figure or
    dragging across a chart would move the window instead. This is a single
    transparent view over the top strip, starting after the buttons so it
    cannot swallow their clicks.

    ``performWindowDragWithEvent:`` is AppKit's own drag, so the window snaps
    to edges and to other Spaces exactly as a title bar would.
    """
    try:
        import AppKit
        from PyObjCTools import AppHelper

        class BalanceDragStrip(AppKit.NSView):
            def mouseDown_(self, event):
                self.window().performWindowDragWithEvent_(event)

        def apply():
            try:
                view = _browser_view(window)
                if view is None:
                    print("[balance] no window to attach the drag strip to")
                    return
                host = view.webview
                bounds = host.bounds()
                # WebKitHost is a flipped view, so y=0 is its TOP edge.
                strip = BalanceDragStrip.alloc().initWithFrame_(
                    AppKit.NSMakeRect(CONTROLS_W, 0.0,
                                      bounds.size.width - CONTROLS_W, TITLEBAR_H))
                strip.setAutoresizingMask_(
                    AppKit.NSViewWidthSizable | AppKit.NSViewMaxYMargin)
                host.addSubview_(strip)
            except Exception as exc:
                print(f"[balance] could not add the drag strip: {exc}")

        AppHelper.callAfter(apply)
    except Exception as exc:  # pragma: no cover - cosmetic, and Mac-only
        print(f"[balance] could not add the drag strip: {exc}")


_restore_frame = {}   # the size to come back to when un-zooming


def _window_action(window, action):
    """Close, minimise or zoom — what the title bar's buttons used to do.

    The window is frameless, so those buttons are drawn by the page and this
    is what they call. Main thread, like everything else that touches AppKit.

    Zoom is done by hand rather than with ``zoom:``. The app launches
    maximized by setting the frame outright, so AppKit's idea of the
    "standard" frame is already the whole screen and its toggle has nowhere to
    go — the button posted, the route answered 200, and the window did not
    move. Remembering the frame we zoomed away from is the only way it can
    come back.
    """
    try:
        from PyObjCTools import AppHelper

        def apply():
            try:
                native = window.native
                if action == "minimise":
                    native.miniaturize_(None)
                elif action == "close":
                    window.destroy()
                elif action == "zoom":
                    screen = native.screen()
                    if screen is None:
                        return
                    full = screen.visibleFrame()
                    now = native.frame()
                    filled = (abs(now.size.width - full.size.width) < 2
                              and abs(now.size.height - full.size.height) < 2)
                    if filled:
                        # First zoom of a session that launched maximized has
                        # nothing remembered, so fall back to the window's own
                        # default size, centred. A button that does nothing on
                        # its first press is the same bug as before.
                        import AppKit
                        back = _restore_frame.get("f")
                        if back is None:
                            w, h = 1200.0, 800.0
                            back = AppKit.NSMakeRect(
                                full.origin.x + (full.size.width - w) / 2,
                                full.origin.y + (full.size.height - h) / 2, w, h)
                        native.setFrame_display_animate_(back, True, True)
                    else:
                        _restore_frame["f"] = now
                        native.setFrame_display_animate_(full, True, True)
            except Exception as exc:
                print(f"[balance] window action {action} failed: {exc}")

        AppHelper.callAfter(apply)
    except Exception as exc:  # pragma: no cover
        print(f"[balance] window action {action} failed: {exc}")


def _match_window_to_theme(window, theme):
    """Keep the window's own appearance in step with the page's theme.

    With no title bar there is no grey strip left to match, but the window's
    appearance still decides how AppKit draws everything it owns rather than
    the page: scrollbars, the resize cursor, any sheet the system puts up. A
    light window under a dark page shows itself the moment you scroll.

    Main thread only: AppKit changes a window's appearance from nowhere else,
    and from a worker it fails silently rather than loudly. Best-effort — a
    failure here is cosmetic.
    """
    try:
        import AppKit
        from PyObjCTools import AppHelper

        name = ("NSAppearanceNameDarkAqua" if theme == "dark"
                else "NSAppearanceNameAqua")

        def apply():
            try:
                window.native.setAppearance_(
                    AppKit.NSAppearance.appearanceNamed_(name))
            except Exception as exc:
                print(f"[balance] could not set the window appearance: {exc}")

        AppHelper.callAfter(apply)
    except Exception as exc:  # pragma: no cover - cosmetic, and Mac-only
        print(f"[balance] could not set the window appearance: {exc}")


def _warm_model():
    """Start the bundled model server if its weights are already here.

    Loading three gigabytes takes a few seconds, and they can pass while the
    dashboard draws rather than after the first question is asked. Allowed to
    fail quietly: no model yet is an ordinary first run, and the panel says so.
    """
    try:
        from ai.runtime import ensure_running
        ensure_running()
    except Exception:
        pass


def _launch_screen():
    """Return the screen geometry for a full-size launch, when requested.

    pywebview applies ``maximized=True`` in ``first_show()`` — after it has
    already shown the 1200x800 window and started laying out the page. That
    second resize is the visible launch jerk. Supplying the screen-sized frame
    up front keeps the web view at one size from its first paint onward.
    """
    if not (config.START_MAXIMIZED or config.START_FULLSCREEN):
        return None
    try:
        screens = webview.screens
        return screens[0] if screens else None
    except Exception as exc:  # pragma: no cover - macOS display setup only
        print(f"[balance] could not determine launch screen: {exc}")
        return None


def _window_options():
    """Build native window options without using macOS geometry on Windows."""
    options = {
        "width": 1200,
        "height": 800,
        "min_size": (900, 600),
        "fullscreen": config.START_FULLSCREEN,
        "easy_drag": False,
    }
    if sys.platform == "darwin":
        # The frameless Mac window needs a screen-sized frame before first
        # paint; setting maximized later visibly resizes the page.
        launch_screen = _launch_screen()
        options.update(
            width=launch_screen.width if launch_screen else 1200,
            height=launch_screen.height if launch_screen else 800,
            screen=launch_screen,
            maximized=False,
            frameless=True,
        )
    else:
        # Let Windows and Linux apply their native maximize behavior. The
        # decorated frame provides real OS minimize, maximize and close buttons.
        options.update(maximized=config.START_MAXIMIZED, frameless=False)
    return options


def _webview_gui():
    """Choose the Windows WebView2 backend; retain pywebview's Mac default."""
    return "edgechromium" if sys.platform == "win32" else None


def _native_window_action(window, action):
    """Handle a requested action through pywebview's native window API."""
    if action == "close":
        window.destroy()
    elif action == "minimise":
        window.minimize()
    elif action == "zoom":
        window.maximize()


def _stop_model():
    """Stop the bundled model process when the desktop app exits."""
    try:
        from ai.runtime import stop
        stop()
    except Exception:
        pass


def main():
    """Initialize local data, serve loopback, and run the native window."""
    server = None
    server_thread = None
    log_path = None
    try:
        log_path = _ensure_windows_stdio()
        # This marks a desktop process. routes.system only enables the custom
        # HTML title bar on macOS, where the native window remains frameless.
        config.DESKTOP_SHELL = True

        init_db()
        backup_db("launch")
        seed_local_user()

        server, server_thread = _start_local_server()

        # Warm the bundled model while the page draws. A missing model is an
        # ordinary first run; the assistant panel offers its download.
        if config.AI_BACKEND == "bundled":
            threading.Thread(target=_warm_model, daemon=True).start()

        window = webview.create_window(
            "Balance.",
            f"http://{SERVER_HOST}:{server.server_address[1]}",
            **_window_options(),
        )

        if sys.platform == "darwin":
            # These hooks keep the frameless Mac window's page-drawn buttons
            # and title-bar appearance connected to AppKit.
            config.WINDOW_THEME_HOOK = lambda theme: _match_window_to_theme(window, theme)
            config.WINDOW_ACTION_HOOK = lambda action: _window_action(window, action)

            def dress_the_window():
                _match_window_to_theme(window, "light")
                _add_drag_strip(window)

            window.events.shown += dress_the_window
        else:
            config.WINDOW_THEME_HOOK = None
            config.WINDOW_ACTION_HOOK = lambda action: _native_window_action(window, action)

        webview.start(gui=_webview_gui())
        return 0
    except Exception as exc:
        if sys.platform != "win32":
            raise
        _report_windows_startup_failure(exc, log_path)
        return 1
    except SystemExit as exc:
        if sys.platform != "win32":
            raise
        if exc.code in (None, 0):
            return 0
        failure = RuntimeError(f"Desktop startup exited with status {exc.code}")
        _report_windows_startup_failure(failure, log_path)
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        config.WINDOW_THEME_HOOK = None
        config.WINDOW_ACTION_HOOK = None
        _stop_model()
        if server is not None:
            _stop_local_server(server, server_thread)


if __name__ == "__main__":
    raise SystemExit(main())
