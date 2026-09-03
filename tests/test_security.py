"""Security hardening tests.

Covers CSRF protection and cookie/header hardening. Runs against the scratch
SQLite database built by ``conftest.py``.

The shared ``client`` fixture disables CSRF (so the rest of the suite mutates
without a token). The CSRF tests here use ``csrf_client``, which flips
``CSRF_ENABLED`` back on for the duration of the test and restores it after.

Run: python3 -m pytest test_security.py
"""

import pytest

import app as app_module
import config


@pytest.fixture
def csrf_client(client):
    """The shared client with CSRF enforcement turned ON for this test only."""
    prev = app_module.app.config.get("CSRF_ENABLED")
    app_module.app.config["CSRF_ENABLED"] = True
    try:
        yield client
    finally:
        app_module.app.config["CSRF_ENABLED"] = prev


def _csrf_token(client):
    """Read the session CSRF token via /api/me (requires a logged-in client)."""
    return client.get("/api/me").get_json()["csrf_token"]


# ── CSRF (Step 4.1) ────────────────────────────────────────────────────


def test_mutation_without_token_is_403(csrf_client, make_user, login):
    """A logged-in POST without X-CSRF-Token is rejected with 403."""
    uid = make_user()
    login(csrf_client, uid)
    res = csrf_client.post(
        "/api/transactions",
        json={"date": "2025-01-01", "store": "X", "category_id": 1,
              "amount": 5, "type": "expense"},
    )
    assert res.status_code == 403
    assert res.get_json()["error"] == "CSRF validation failed"


def test_mutation_with_valid_token_succeeds(csrf_client, make_user, login, fresh_conn):
    """A POST carrying a valid X-CSRF-Token is accepted (passes CSRF)."""
    uid = make_user()
    login(csrf_client, uid)
    token = _csrf_token(csrf_client)

    # Use a real category id for this user so the request reaches success.
    cat_id = fresh_conn(lambda c: c.execute(
        "SELECT id FROM categories WHERE user_id = %s AND type = 'expense' LIMIT 1",
        (uid,),
    ).fetchone()["id"])

    res = csrf_client.post(
        "/api/transactions",
        json={"date": "2025-01-01", "store": "X", "category_id": cat_id,
              "amount": 5, "type": "expense"},
        headers={"X-CSRF-Token": token},
    )
    assert res.status_code == 201


def test_get_needs_no_token(csrf_client, make_user, login):
    """GET requests are never CSRF-checked."""
    uid = make_user()
    login(csrf_client, uid)
    assert csrf_client.get("/api/transactions").status_code == 200


def test_mutation_without_token_is_403_even_with_no_body(csrf_client):
    """CSRF is the first gate: there is no login, so nothing is checked before it.

    (This replaced a test asserting 401-before-403. That ordering only meant
    something when the app had accounts to be unauthenticated against.)
    """
    res = csrf_client.post("/api/transactions", json={})
    assert res.status_code == 403
    assert res.get_json()["error"] == "CSRF validation failed"


def test_me_exposes_csrf_token(csrf_client, make_user, login):
    """/api/me surfaces the per-session token so the SPA can read it."""
    uid = make_user()
    login(csrf_client, uid)
    token = csrf_client.get("/api/me").get_json().get("csrf_token")
    assert token and isinstance(token, str)


# ── Header hardening (Step 4.2) ────────────────────────────────────────


def test_security_headers_present(client, make_user, login):
    """Security headers are set on a normal response."""
    uid = make_user()
    login(client, uid)
    res = client.get("/api/me")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "Content-Security-Policy" in res.headers


def test_cookie_hardening_config():
    """Session cookie flags are configured for hardening."""
    assert app_module.app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app_module.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    # Not Secure: the app is served over plain http on 127.0.0.1, where TLS
    # does not apply. A Secure cookie would simply never be sent.
    assert app_module.app.config["SESSION_COOKIE_SECURE"] is False


# ── The desktop window's title bar ─────────────────────────────────────
# The page tells the window which theme it wears, so macOS draws the title bar
# to match instead of in the system's own appearance — which on a Mac in Dark
# Mode was a black strip above a white sidebar.


def test_window_appearance_does_nothing_outside_the_desktop_app(client, make_user, login):
    """No window, no hook. In a browser tab this must be a harmless no-op."""
    uid = make_user()
    login(client, uid)
    res = client.post("/api/window/appearance", json={"theme": "dark"})
    assert res.status_code == 200
    assert res.get_json() == {"applied": False, "theme": "dark"}


def test_window_appearance_reaches_the_window_when_there_is_one(
        client, make_user, login, monkeypatch):
    """main.py registers the hook; the route is what calls it."""
    import config as config_module

    seen = []
    monkeypatch.setattr(config_module, "WINDOW_THEME_HOOK", seen.append,
                        raising=False)
    uid = make_user()
    login(client, uid)

    res = client.post("/api/window/appearance", json={"theme": "dark"})
    assert res.status_code == 200
    assert res.get_json()["applied"] is True
    assert seen == ["dark"]


def test_window_appearance_refuses_a_theme_it_does_not_know(client, make_user, login):
    """Only two appearances exist, and AppKit is not the place to find that out."""
    uid = make_user()
    login(client, uid)
    for bad in ("solarized", "", None):
        res = client.post("/api/window/appearance", json={"theme": bad})
        assert res.status_code == 400


def test_window_actions_reach_the_window(client, make_user, login, monkeypatch):
    """The window is frameless, so these three are the only way to close it."""
    import config as config_module

    seen = []
    monkeypatch.setattr(config_module, "WINDOW_ACTION_HOOK", seen.append,
                        raising=False)
    uid = make_user()
    login(client, uid)

    for action in ("close", "minimise", "zoom"):
        res = client.post(f"/api/window/{action}")
        assert res.status_code == 200
        assert res.get_json() == {"applied": True, "action": action}
    assert seen == ["close", "minimise", "zoom"]


def test_an_unknown_window_action_is_refused(client, make_user, login):
    """A typo must not reach AppKit, and must not look like it worked."""
    uid = make_user()
    login(client, uid)
    res = client.post("/api/window/explode")
    assert res.status_code == 400


def test_window_actions_do_nothing_in_a_browser(client, make_user, login):
    """No hook outside the desktop app; the route must still answer cleanly."""
    uid = make_user()
    login(client, uid)
    res = client.post("/api/window/minimise")
    assert res.status_code == 200
    assert res.get_json()["applied"] is False
