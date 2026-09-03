"""Launch Expense Tracker as a desktop app using pywebview."""
import sys
import threading
import webview
import config
from app import app, SERVER_PORT
from data.schema import init_db, seed_local_user, backup_db


def start_server():
    app.run(port=SERVER_PORT, use_reloader=False)


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


if __name__ == "__main__":
    # Tell the page it is running inside the desktop window. It uses this to
    # relay its theme, so the title bar can be drawn to match.
    config.DESKTOP_SHELL = True

    # Local SQLite bootstrap: create the schema, take a safety backup of any
    # existing DB, and ensure the single local user + default categories exist.
    init_db()
    backup_db("launch")
    seed_local_user()

    server = threading.Thread(target=start_server, daemon=True)
    server.start()

    # Warm the model up while the dashboard draws, so the first question does
    # not pay for loading it. Backgrounded and allowed to fail: no model yet is
    # an ordinary first run, and the panel offers the download itself.
    if config.AI_BACKEND == "bundled":
        threading.Thread(target=_warm_model, daemon=True).start()

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
        # No title bar at all. A native one is drawn by macOS in macOS's own
        # grey and cannot be given the app's colours — the page does not reach
        # that band and neither does the window's background, both tested by
        # painting them red. Frameless is the only way the sidebar reaches the
        # top edge. The close, minimise and zoom buttons are drawn by the page
        # and the strip beside them drags the window; see _add_drag_strip.
        frameless=True,
        # pywebview's own easy_drag drags from anywhere in the page, so
        # selecting a figure would move the window.
        easy_drag=False,
    )

    def on_closed():
        # The model server is a child process, not a thread. Without this,
        # closing the window leaves three gigabytes resident with nothing left
        # to talk to it.
        try:
            from ai.runtime import stop
            stop()
        except Exception:
            pass
        sys.exit(0)

    window.events.closed += on_closed

    # How the page tells the window it has changed theme. The Flask server runs
    # in this very process, so the route can reach AppKit directly.
    config.WINDOW_THEME_HOOK = lambda theme: _match_window_to_theme(window, theme)
    config.WINDOW_ACTION_HOOK = lambda action: _window_action(window, action)

    def dress_the_window():
        _match_window_to_theme(window, "light")
        _add_drag_strip(window)

    window.events.shown += dress_the_window

    webview.start()
    sys.exit(0)
