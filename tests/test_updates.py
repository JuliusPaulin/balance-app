"""HTTP contract tests for the optional native Sparkle updater."""

import threading

import pytest

import app as app_module
from services import updates


class FakeUpdateManager:
    def __init__(self):
        self.preferences = {
            "automatic_checks": True,
            "automatic_installation": False,
        }
        self.check_calls = 0
        self.unavailable_preference = False

    def status(self):
        return {
            "available": True,
            "version": "1.14.1",
            "update_available": False,
            "available_version": None,
            "automatic_checks": self.preferences["automatic_checks"],
            "automatic_installation": self.preferences["automatic_installation"],
            "can_check": True,
            "checking": self.check_calls > 0,
            "last_check": None,
            "message": "Checking for updates." if self.check_calls else "Ready.",
        }

    def check(self):
        self.check_calls += 1

    def set_preferences(self, preferences):
        if self.unavailable_preference and preferences.get("automatic_installation"):
            raise updates.UpdatePreferenceNotSupported(
                "Automatic installation is unavailable.")
        self.preferences.update(preferences)


class FakeSparkleUpdater:
    def __init__(self):
        self.checks = True
        self.downloads = False
        self.allowed = True
        self.can_check = True

    def lastUpdateCheckDate(self):
        return None

    def sessionInProgress(self):
        return False

    def automaticallyChecksForUpdates(self):
        return self.checks

    def automaticallyDownloadsUpdates(self):
        return self.downloads

    def canCheckForUpdates(self):
        return self.can_check

    def allowsAutomaticUpdates(self):
        return self.allowed

    def setAutomaticallyChecksForUpdates_(self, value):
        self.checks = value

    def setAutomaticallyDownloadsUpdates_(self, value):
        self.downloads = value


class FakeSparkleController:
    def __init__(self):
        self.check_calls = 0

    def checkForUpdates_(self, sender):
        self.check_calls += 1


@pytest.fixture
def fake_update_manager(monkeypatch):
    manager = FakeUpdateManager()
    monkeypatch.setattr(updates, "_manager", manager)
    return manager


@pytest.fixture
def csrf_client(client):
    previous = app_module.app.config.get("CSRF_ENABLED")
    app_module.app.config["CSRF_ENABLED"] = True
    try:
        yield client
    finally:
        app_module.app.config["CSRF_ENABLED"] = previous


def _csrf_token(client):
    return client.get("/api/me").get_json()["csrf_token"]


def test_update_status_is_honestly_unavailable_outside_native_app(client, monkeypatch):
    monkeypatch.setattr(updates, "_manager", None)
    monkeypatch.setattr(updates, "_initialization_message", None)

    response = client.get("/api/updates")

    assert response.status_code == 200
    data = response.get_json()
    assert data["available"] is False
    assert data["update_available"] is None
    assert data["can_check"] is False
    assert data["checking"] is False
    assert data["automatic_checks"] is True
    assert data["automatic_installation"] is False
    assert data["last_check"] is None
    assert "packaged macOS desktop app" in data["message"]


def test_update_status_and_check_use_the_injected_native_manager(
        client, fake_update_manager):
    response = client.get("/api/updates")
    assert response.status_code == 200
    assert response.get_json()["available"] is True
    assert response.get_json()["version"] == "1.14.1"

    response = client.post("/api/updates/check")
    assert response.status_code == 202
    assert fake_update_manager.check_calls == 1
    assert response.get_json()["checking"] is True


def test_update_preferences_accept_only_known_boolean_fields(
        client, fake_update_manager):
    response = client.put("/api/updates/preferences",
                          json={"automatic_checks": False,
                                "automatic_installation": True})
    assert response.status_code == 200
    assert fake_update_manager.preferences == {
        "automatic_checks": False,
        "automatic_installation": True,
    }

    for body in (
        {},
        {"automatic_checks": 1},
        {"unknown": True},
        ["automatic_checks"],
    ):
        response = client.put("/api/updates/preferences", json=body)
        assert response.status_code == 400

    response = client.put("/api/updates/preferences", data="{broken",
                          content_type="application/json")
    assert response.status_code == 400


def test_update_preferences_report_sparkle_unsupported_state(
        client, fake_update_manager):
    fake_update_manager.unavailable_preference = True

    response = client.put("/api/updates/preferences",
                          json={"automatic_installation": True})

    assert response.status_code == 409
    assert "unavailable" in response.get_json()["error"]


def test_update_check_and_preferences_keep_csrf_protection(
        csrf_client, fake_update_manager):
    response = csrf_client.post("/api/updates/check")
    assert response.status_code == 403

    token = _csrf_token(csrf_client)
    response = csrf_client.post("/api/updates/check",
                                headers={"X-CSRF-Token": token})
    assert response.status_code == 202

    response = csrf_client.put("/api/updates/preferences",
                               json={"automatic_checks": False})
    assert response.status_code == 403

    response = csrf_client.put("/api/updates/preferences",
                               json={"automatic_checks": False},
                               headers={"X-CSRF-Token": token})
    assert response.status_code == 200
    assert fake_update_manager.preferences["automatic_checks"] is False


def test_update_mutations_return_unavailable_when_native_sparkle_is_absent(
        client, monkeypatch):
    monkeypatch.setattr(updates, "_manager", None)
    monkeypatch.setattr(updates, "_initialization_message", None)

    check = client.post("/api/updates/check")
    preference = client.put("/api/updates/preferences",
                            json={"automatic_checks": False})

    assert check.status_code == 503
    assert preference.status_code == 503


def test_quit_uses_native_hook_to_allow_installation_on_quit(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.config, "WINDOW_QUIT_HOOK",
                        lambda: calls.append("native-quit"), raising=False)
    response = client.post("/api/quit")
    assert response.status_code == 204
    assert calls == ["native-quit"]


def test_native_manager_reads_and_writes_sparkle_state_on_dispatcher():
    updater = FakeSparkleUpdater()
    controller = FakeSparkleController()
    cleaned = []
    state = updates._UpdateState("https://example.invalid/appcast.xml",
                                 lambda: cleaned.append(True))
    manager = updates.NativeUpdateManager(
        controller, updater, state, main_dispatch=lambda function: function())

    status = manager.status()
    assert status["available"] is True
    assert status["automatic_checks"] is True
    assert status["automatic_installation"] is False

    manager.set_preferences({"automatic_checks": False,
                             "automatic_installation": True})
    assert updater.checks is False
    assert updater.downloads is True

    manager.check()
    assert controller.check_calls == 1
    assert manager.status()["checking"] is True

    state.prepare_for_relaunch()
    state.prepare_for_relaunch()
    assert cleaned == [True]


def test_native_manager_rolls_back_unsupported_sparkle_preference():
    updater = FakeSparkleUpdater()
    updater.allowed = False
    state = updates._UpdateState("https://example.invalid/appcast.xml", None)
    manager = updates.NativeUpdateManager(
        FakeSparkleController(), updater, state,
        main_dispatch=lambda function: function())

    with pytest.raises(updates.UpdatePreferenceNotSupported):
        manager.set_preferences({"automatic_checks": False,
                                 "automatic_installation": True})

    assert updater.checks is True
    assert updater.downloads is False


def test_queued_main_thread_operation_is_cancelled_after_timeout(monkeypatch):
    queued = []
    ran = []
    errors = []
    monkeypatch.setattr(updates, "MAIN_THREAD_TIMEOUT", 0.01)
    monkeypatch.setattr(updates, "_queue_on_main", queued.append)

    def worker():
        try:
            updates._dispatch_to_main(lambda: ran.append(True))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert len(queued) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], updates.MainThreadTimeout)
    assert errors[0].operation_started is False

    # A callback left in AppKit's queue after the HTTP timeout must be inert.
    queued[0]()
    assert ran == []


def test_sparkle_no_update_error_is_a_normal_check_result():
    class NoCompatibleUpdateError:
        def domain(self):
            return "SUSparkleErrorDomain"

        def code(self):
            return 1001

        def localizedDescription(self):
            return "No update is available for this operating system."

    error = NoCompatibleUpdateError()
    state = updates._UpdateState("https://example.invalid/appcast.xml", None)
    updater = FakeSparkleUpdater()
    manager = updates.NativeUpdateManager(
        FakeSparkleController(), updater, state,
        main_dispatch=lambda function: function())

    state.begin_check()
    state.found_no_update(error)
    state.finished_check(error)
    status = manager.status()

    assert status["checking"] is False
    assert status["update_available"] is False
    assert status["last_check"] is not None
    assert status["message"] == "No compatible updates were available at the last check."
    assert "failed" not in status["message"].lower()


def test_started_preference_timeout_returns_pending_instead_of_failure(
        client, fake_update_manager, monkeypatch):
    def timed_out(_preferences):
        raise updates.MainThreadTimeout(
            "The native update operation is still running; check its status.",
            operation_started=True)

    monkeypatch.setattr(updates, "set_preferences", timed_out)

    response = client.put("/api/updates/preferences",
                          json={"automatic_checks": False})

    assert response.status_code == 202
    assert response.get_json()["pending"] is True
    assert "error" not in response.get_json()
