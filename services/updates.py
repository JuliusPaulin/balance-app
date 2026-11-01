"""Native Sparkle updater for the packaged macOS app.

The Flask routes call this module from worker threads. Sparkle and AppKit stay
on pywebview's main thread; source runs and ordinary browser launches do not
load any Objective-C frameworks.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import threading
from collections.abc import Callable
from urllib.parse import urlparse

import config


MAIN_THREAD_TIMEOUT = 5.0
_UNAVAILABLE_MESSAGE = (
    "Automatic updates are available only in the packaged macOS desktop app."
)


class UpdateUnavailable(RuntimeError):
    """The native updater is not present in this process."""


class UpdateBusy(RuntimeError):
    """Sparkle cannot start another user-initiated check right now."""


class UpdatePreferenceNotSupported(RuntimeError):
    """Sparkle or the app bundle does not support the requested preference."""


class MainThreadTimeout(RuntimeError):
    """The AppKit main thread did not answer within the bounded wait."""

    def __init__(self, message: str, *, operation_started: bool = False):
        super().__init__(message)
        self.operation_started = operation_started


def _update_config_path(bundle=None) -> Path:
    """Find the same update settings in a checkout and a PyInstaller bundle."""
    candidates = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / "services" / "update_config.json")
    if bundle is not None:
        resource_path = bundle.resourcePath()
        if resource_path:
            candidates.append(Path(str(resource_path)) / "services" / "update_config.json")
    candidates.append(Path(__file__).with_name("update_config.json"))
    return next((path for path in candidates if path.is_file()), candidates[-1])


def _read_update_config(bundle=None) -> dict:
    try:
        with _update_config_path(bundle).open(encoding="utf-8") as handle:
            settings = json.load(handle)
    except (OSError, ValueError) as exc:
        raise UpdateUnavailable("Sparkle update settings are missing or invalid.") from exc
    if not isinstance(settings, dict):
        raise UpdateUnavailable("Sparkle update settings are missing or invalid.")
    feed_url = settings.get("feed_url")
    parsed = urlparse(feed_url if isinstance(feed_url, str) else "")
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise UpdateUnavailable("Sparkle requires a valid HTTPS update feed.")
    if not isinstance(settings.get("public_key"), str) or not settings["public_key"].strip():
        raise UpdateUnavailable("Sparkle's public key is missing from app settings.")
    return settings


def _timestamp(value) -> str | None:
    """Convert Sparkle's NSDate (or a datetime in tests) to an ISO timestamp."""
    if value is None:
        return None
    try:
        if isinstance(value, datetime):
            moment = value
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
        else:
            moment = datetime.fromtimestamp(
                float(value.timeIntervalSince1970()), tz=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _is_sparkle_no_update_error(error) -> bool:
    """SUNoUpdateError (1001) is a normal result, including OS incompatibility."""
    if error is None:
        return False
    try:
        return (str(error.domain()) == "SUSparkleErrorDomain"
                and int(error.code()) == 1001)
    except Exception:
        return False


def _error_message(error) -> str | None:
    if error is None or _is_sparkle_no_update_error(error):
        return None
    try:
        return str(error.localizedDescription())
    except Exception:
        return str(error)


def _unavailable_status(message: str = _UNAVAILABLE_MESSAGE) -> dict:
    return {
        "available": False,
        "version": config.APP_VERSION,
        "update_available": None,
        "available_version": None,
        # Match the packaged Info.plist defaults. No live updater exists here
        # to read a user's persisted Sparkle choice.
        "automatic_checks": True,
        "automatic_installation": False,
        "can_check": False,
        "checking": False,
        "last_check": None,
        "message": message,
    }


class _UpdateState:
    """State delivered asynchronously by Sparkle's updater delegate."""

    def __init__(self, feed_url: str, cleanup: Callable[[], None] | None):
        self.feed_url = feed_url
        self.cleanup = cleanup
        self._lock = threading.RLock()
        self._available: bool | None = None
        self._available_version: str | None = None
        self._checking = False
        self._last_check: str | None = None
        self._error: str | None = None
        self._cleaned_up = False

    def begin_check(self):
        with self._lock:
            self._available = None
            self._available_version = None
            self._checking = True
            self._error = None

    def found_update(self, version):
        with self._lock:
            self._available = True
            self._available_version = str(version) if version else None

    def found_no_update(self, error=None):
        with self._lock:
            self._available = False
            self._available_version = None
            self._error = _error_message(error)

    def finished_check(self, error=None):
        with self._lock:
            self._checking = False
            self._last_check = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if _is_sparkle_no_update_error(error):
                if self._available is None:
                    self._available = False
                    self._available_version = None
                self._error = None
            elif error is not None:
                self._error = _error_message(error)

    def prepare_for_relaunch(self):
        """Run the app-provided cleanup at most once during an update cycle."""
        with self._lock:
            if self._cleaned_up:
                return
            self._cleaned_up = True
        if self.cleanup is not None:
            self.cleanup()

    def snapshot(self):
        with self._lock:
            return {
                "available": self._available,
                "available_version": self._available_version,
                "checking": self._checking,
                "last_check": self._last_check,
                "error": self._error,
            }


class NativeUpdateManager:
    """Small main-thread facade around Sparkle's standard updater controller."""

    def __init__(self, controller, updater, state: _UpdateState,
                 main_dispatch=None):
        self.controller = controller  # Keep Sparkle alive for the app lifetime.
        self.updater = updater
        self.state = state
        self._main_dispatch = main_dispatch or _dispatch_to_main

    def _read_status_on_main(self):
        state = self.state.snapshot()
        try:
            last_check = _timestamp(self.updater.lastUpdateCheckDate())
        except AttributeError:
            last_check = None
        last_check = last_check or state["last_check"]
        checking = bool(self.updater.sessionInProgress()) or state["checking"]
        if checking:
            message = "An update check or installation is in progress."
        elif state["error"]:
            message = f"Update check failed: {state['error']}"
        elif state["available"] is True:
            message = f"Version {state['available_version']} is available."
        elif state["available"] is False:
            message = "No compatible updates were available at the last check."
        else:
            message = "Check for updates to see whether a newer version is available."
        return {
            # `available` means that this build has a usable native updater.
            # Whether Sparkle found a new version is separate state.
            "available": True,
            "version": config.APP_VERSION,
            "update_available": state["available"],
            "available_version": state["available_version"],
            "automatic_checks": bool(self.updater.automaticallyChecksForUpdates()),
            "automatic_installation": bool(self.updater.automaticallyDownloadsUpdates()),
            "can_check": bool(self.updater.canCheckForUpdates()),
            "checking": checking,
            "last_check": last_check,
            "message": message,
        }

    def status(self):
        return self._main_dispatch(self._read_status_on_main)

    def check(self):
        def start():
            if not self.updater.canCheckForUpdates():
                raise UpdateBusy("Sparkle is already checking or installing an update.")
            self.state.begin_check()
            # This is Sparkle's native, user-visible check UI. The HTTP route
            # never downloads or inspects the feed itself.
            self.controller.checkForUpdates_(None)

        self._main_dispatch(start)

    def set_preferences(self, preferences: dict):
        def apply():
            old_checks = bool(self.updater.automaticallyChecksForUpdates())
            old_downloads = bool(self.updater.automaticallyDownloadsUpdates())
            target_checks = preferences.get("automatic_checks", old_checks)
            target_downloads = preferences.get("automatic_installation", old_downloads)

            try:
                if "automatic_checks" in preferences and target_checks != old_checks:
                    self.updater.setAutomaticallyChecksForUpdates_(target_checks)
                if ("automatic_installation" in preferences and target_downloads
                        and not self.updater.allowsAutomaticUpdates()):
                    raise UpdatePreferenceNotSupported(
                        "This app build does not allow automatic update installation.")
                if "automatic_installation" in preferences:
                    self.updater.setAutomaticallyDownloadsUpdates_(target_downloads)
                if ("automatic_installation" in preferences
                        and bool(self.updater.automaticallyDownloadsUpdates())
                        != target_downloads):
                    raise UpdatePreferenceNotSupported(
                        "Sparkle did not accept the automatic installation setting.")
            except Exception as exc:
                # Avoid leaving the first preference changed if the requested
                # pair cannot be represented by this app's Sparkle settings.
                if old_checks != target_checks:
                    try:
                        self.updater.setAutomaticallyChecksForUpdates_(old_checks)
                    except Exception:
                        pass
                if old_downloads != target_downloads:
                    try:
                        self.updater.setAutomaticallyDownloadsUpdates_(old_downloads)
                    except Exception:
                        pass
                if isinstance(exc, UpdatePreferenceNotSupported):
                    raise
                raise UpdatePreferenceNotSupported(
                    "Sparkle could not apply the requested update preferences.") from exc

        self._main_dispatch(apply)


def _queue_on_main(function):
    """Enqueue a callable on pywebview's AppKit main run loop."""
    from PyObjCTools import AppHelper
    AppHelper.callAfter(function)


def _dispatch_to_main(function):
    """Run an AppKit/Sparkle operation on the Python main thread with a bound."""
    if threading.current_thread() is threading.main_thread():
        return function()

    done = threading.Event()
    state_lock = threading.Lock()
    phase = {"value": "pending"}
    result = {}

    def invoke():
        with state_lock:
            if phase["value"] != "pending":
                return
            phase["value"] = "running"
        try:
            result["value"] = function()
        except BaseException as exc:
            result["error"] = exc
        finally:
            with state_lock:
                phase["value"] = "finished"
                done.set()

    try:
        _queue_on_main(invoke)
    except Exception as exc:  # pragma: no cover - only on a broken Mac install
        raise UpdateUnavailable("The macOS main-thread dispatcher is unavailable.") from exc
    if not done.wait(MAIN_THREAD_TIMEOUT):
        with state_lock:
            if phase["value"] == "pending":
                phase["value"] = "cancelled"
                raise MainThreadTimeout("The app is busy; try again in a moment.")
            operation_started = phase["value"] == "running"

        # A callback that just started gets one more bounded interval to finish.
        # This prevents a timeout response while a preference setter is still
        # applying. Work that has not started is cancelled above.
        if not done.wait(MAIN_THREAD_TIMEOUT):
            raise MainThreadTimeout(
                "The native update operation is still running; check its status.",
                operation_started=operation_started)
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _native_bundle_and_framework():
    """Return the actual app bundle and its private Sparkle framework path."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        raise UpdateUnavailable()
    from Foundation import NSBundle

    bundle = NSBundle.mainBundle()
    bundle_path = str(bundle.bundlePath())
    frameworks_path = bundle.privateFrameworksPath()
    if (not bundle_path.endswith(".app") or not frameworks_path):
        raise UpdateUnavailable()
    framework = os.path.join(str(frameworks_path), "Sparkle.framework")
    if not os.path.isdir(framework):
        raise UpdateUnavailable()
    return bundle, framework


def _make_native_manager(cleanup=None) -> NativeUpdateManager:
    """Load bundled Sparkle, configure its delegate and start its scheduler."""
    bundle, framework_path = _native_bundle_and_framework()
    settings = _read_update_config(bundle)
    plist_key = bundle.objectForInfoDictionaryKey_("SUPublicEDKey")
    if str(plist_key or "") != settings["public_key"]:
        raise UpdateUnavailable("The Sparkle public key is missing from this app bundle.")

    import objc
    from Foundation import NSObject

    objc.loadBundle("Sparkle", globals(), bundle_path=framework_path,
                    scan_classes=False)
    controller_class = objc.lookUpClass("SPUStandardUpdaterController")
    updater_protocol = objc.protocolNamed("SPUUpdaterDelegate")
    state = _UpdateState(settings["feed_url"], cleanup)

    class BalanceSparkleDelegate(NSObject, protocols=[updater_protocol]):
        def feedURLStringForUpdater_(self, updater):
            return state.feed_url

        def updater_didFindValidUpdate_(self, updater, item):
            state.found_update(item.displayVersionString())

        def updaterDidNotFindUpdate_(self, updater):
            state.found_no_update()

        def updaterDidNotFindUpdate_error_(self, updater, error):
            state.found_no_update(error)

        def updater_didFinishUpdateCycleForUpdateCheck_error_(
                self, updater, update_check, error):
            state.finished_check(error)

        def updater_willInstallUpdate_(self, updater, item):
            state.prepare_for_relaunch()

        def updaterWillRelaunchApplication_(self, updater):
            state.prepare_for_relaunch()

    delegate = BalanceSparkleDelegate.alloc().init()
    controller = controller_class.alloc().initWithStartingUpdater_updaterDelegate_userDriverDelegate_(
        False, delegate, None)
    if controller is None:
        raise UpdateUnavailable("Sparkle could not create its updater controller.")
    updater = controller.updater()
    started = updater.startUpdater_(None)
    if isinstance(started, tuple):
        started, error = started
    else:
        error = None
    if not started:
        detail = error.localizedDescription() if error else "Sparkle could not start."
        raise UpdateUnavailable(str(detail))

    manager = NativeUpdateManager(controller, updater, state)
    manager.delegate = delegate  # Sparkle holds delegates weakly.
    return manager


_manager = None
_initialization_attempted = False
_initialization_lock = threading.RLock()
_initialization_message: str | None = None


def initialize_native_updater(cleanup=None):
    """Start Sparkle once, after the desktop window has been shown."""
    global _manager, _initialization_attempted, _initialization_message
    with _initialization_lock:
        if _manager is not None:
            return _manager
        if _initialization_attempted:
            return None
        _initialization_attempted = True

    try:
        def create_and_retain_manager():
            global _manager, _initialization_message
            manager = _make_native_manager(cleanup)
            # Retain the controller on the main thread before returning to the
            # worker. If the bounded wait expires after creation starts, this
            # still keeps Sparkle and its weakly-held delegate alive.
            with _initialization_lock:
                _manager = manager
                _initialization_message = None
            return manager

        manager = _dispatch_to_main(create_and_retain_manager)
    except UpdateUnavailable as exc:
        if sys.platform != "darwin" or not getattr(sys, "frozen", False):
            _initialization_message = _UNAVAILABLE_MESSAGE
        else:
            if str(exc):
                print(f"[balance] Sparkle updater unavailable: {exc}")
            _initialization_message = "Updates are unavailable in this build."
        return None
    except Exception as exc:  # Keep a broken/native-less build usable.
        _initialization_message = "Updates are unavailable in this build."
        print(f"[balance] Sparkle updater could not start: {exc}")
        return None

    return manager


def status() -> dict:
    manager = _manager
    if manager is None:
        return _unavailable_status(_initialization_message or _UNAVAILABLE_MESSAGE)
    return manager.status()


def check_for_updates():
    manager = _manager
    if manager is None:
        raise UpdateUnavailable(_initialization_message or _UNAVAILABLE_MESSAGE)
    manager.check()


def set_preferences(preferences: dict):
    manager = _manager
    if manager is None:
        raise UpdateUnavailable(_initialization_message or _UNAVAILABLE_MESSAGE)
    manager.set_preferences(preferences)
