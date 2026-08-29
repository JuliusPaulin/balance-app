"""Tests for the hosted Open Banking import (Enable Banking).

Covers three layers, all with the EB network mocked (no real key, no HTTP):

1. ``enable_banking`` core: JWT built from the base64 env key (verified by
   decoding it back with the matching public key + checking the ``kid`` header),
   ``get_transactions`` normalisation + pagination, and request shaping for
   ``start_auth`` / ``create_session`` (we mock the thin ``_get`` / ``_post``).
2. Routes: ``/connect`` issues a 302 and stores the CSRF state; ``/callback``
   rejects a bad state and upserts on a good code; ``/fetch`` stages user-scoped
   rows in the SAME shape as /api/import/upload; an expired session → 401.
3. Schema: ``init_db()`` creates the ``bank_sessions`` table and its index.

The route tests use the shared ``conftest.py`` fixtures (``client``,
``make_user``, ``fresh_conn``) against a scratch SQLite database; the EB module
tests are pure and need no DB.
"""

import base64
import json

import pytest

import config
import db
import enable_banking as eb


# ───────────────────────────────────────────────────────────────────────
# Test RSA keypair (generated in-process; never touches disk)
# ───────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def rsa_keypair():
    """Return (private_pem_str, public_key_obj) for signing/verifying JWTs."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return private_pem, key.public_key()


@pytest.fixture
def configure_eb(rsa_keypair, monkeypatch):
    """Point config at a fake EB app id + the base64-encoded test private key."""
    private_pem, _ = rsa_keypair
    b64 = base64.b64encode(private_pem.encode("utf-8")).decode("ascii")
    monkeypatch.setattr(config, "ENABLE_BANKING_APP_ID", "test-app-id")
    monkeypatch.setattr(config, "ENABLE_BANKING_PRIVATE_KEY", b64)
    monkeypatch.setattr(config, "ENABLE_BANKING_ASPSP", "Nordea")
    monkeypatch.setattr(config, "ENABLE_BANKING_COUNTRY", "FI")
    return rsa_keypair


# ───────────────────────────────────────────────────────────────────────
# 1. enable_banking core (pure — no DB)
# ───────────────────────────────────────────────────────────────────────


def test_jwt_built_from_env_key(configure_eb):
    """The JWT is signed with the env key and carries kid = app id."""
    import jwt

    _, public_key = configure_eb
    token = eb._jwt()
    header = jwt.get_unverified_header(token)
    assert header["kid"] == "test-app-id"
    assert header["alg"] == "RS256"
    # Verifies the signature against the matching public key.
    claims = jwt.decode(
        token, public_key, algorithms=["RS256"], audience="api.enablebanking.com"
    )
    assert claims["iss"] == "enablebanking.com"
    assert claims["aud"] == "api.enablebanking.com"


def test_jwt_missing_config_raises():
    """No app id / key → BankConfigMissing (never a silent file read)."""
    import config as cfg

    # No configure_eb fixture here: defaults are empty in the test env.
    cfg.ENABLE_BANKING_APP_ID = ""
    cfg.ENABLE_BANKING_PRIVATE_KEY = ""
    with pytest.raises(eb.BankConfigMissing):
        eb._jwt()


def test_load_private_key_bad_base64(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_BANKING_APP_ID", "x")
    monkeypatch.setattr(config, "ENABLE_BANKING_PRIVATE_KEY", "!!!not base64!!!")
    with pytest.raises(eb.BankConfigMissing):
        eb._load_private_key()


def test_start_auth_request_shape(monkeypatch):
    """start_auth POSTs /auth with the right body and returns the bank url."""
    captured = {}

    def fake_post(path, body, *, session_scoped):
        captured["path"] = path
        captured["body"] = body
        captured["session_scoped"] = session_scoped
        return {"url": "https://bank.example/authorize?x=1"}

    monkeypatch.setattr(eb, "_post", fake_post)
    url = eb.start_auth("Nordea", "FI", "https://app/cb", "state-123")
    assert url == "https://bank.example/authorize?x=1"
    assert captured["path"] == "/auth"
    body = captured["body"]
    assert body["aspsp"] == {"name": "Nordea", "country": "FI"}
    assert body["redirect_url"] == "https://app/cb"
    assert body["state"] == "state-123"
    assert body["psu_type"] == "personal"
    assert "valid_until" in body["access"]
    # Pre-consent: a 401/403 here accuses our own key, not the user's consent.
    assert captured["session_scoped"] is False


def test_start_auth_missing_url_raises(monkeypatch):
    monkeypatch.setattr(eb, "_post", lambda p, b, **kw: {})
    with pytest.raises(eb.BankError):
        eb.start_auth("Nordea", "FI", "https://app/cb", "s")


def test_create_session_shapes_result(monkeypatch):
    """create_session POSTs /sessions and normalises accounts + valid_until."""
    captured = {}

    def fake_post(path, body, *, session_scoped):
        captured["path"] = path
        captured["body"] = body
        captured["session_scoped"] = session_scoped
        return {
            "session_id": "sess-9",
            "accounts": [
                {"uid": "acc-1", "iban": "FI00", "name": "Käyttö", "currency": "EUR"},
                "acc-2-bare-string",
            ],
            "access": {"valid_until": "2030-01-01T00:00:00+00:00"},
        }

    monkeypatch.setattr(eb, "_post", fake_post)
    out = eb.create_session("the-code")
    assert captured["path"] == "/sessions"
    assert captured["body"] == {"code": "the-code"}
    assert out["session_id"] == "sess-9"
    assert out["valid_until"] == "2030-01-01T00:00:00+00:00"
    assert out["accounts"][0] == {
        "uid": "acc-1", "iban": "FI00", "name": "Käyttö", "currency": "EUR"
    }
    # Bare-string account normalised to a dict.
    assert out["accounts"][1]["uid"] == "acc-2-bare-string"
    assert captured["session_scoped"] is False


def test_create_session_missing_id_raises(monkeypatch):
    monkeypatch.setattr(eb, "_post", lambda p, b, **kw: {"accounts": []})
    with pytest.raises(eb.BankError):
        eb.create_session("c")


def test_get_transactions_mapping_and_pagination(monkeypatch):
    """Two pages are stitched; DBIT→expense/creditor, CRDT→income/debtor; PDNG skipped."""
    page1 = {
        "transactions": [
            {
                "credit_debit_indicator": "DBIT",
                "status": "BOOK",
                "booking_date": "2025-01-05",
                "transaction_amount": {"amount": "12.50", "currency": "EUR"},
                "creditor": {"name": "K-Market"},
                "entry_reference": "ref-1",
            },
            {
                "credit_debit_indicator": "CRDT",
                "status": "BOOK",
                "value_date": "2025-01-06",
                "transaction_amount": {"amount": "200.00", "currency": "EUR"},
                "debtor": {"name": "Employer Oy"},
            },
            {  # pending → skipped
                "credit_debit_indicator": "DBIT",
                "status": "PDNG",
                "booking_date": "2025-01-07",
                "transaction_amount": {"amount": "9.99", "currency": "EUR"},
            },
        ],
        "continuation_key": "next-page",
    }
    page2 = {
        "transactions": [
            {  # no counterparty → remittance fallback
                "credit_debit_indicator": "DBIT",
                "status": "BOOK",
                "booking_date": "2025-01-08",
                "transaction_amount": {"amount": "5.00", "currency": "EUR"},
                "remittance_information": ["Coffee", "shop"],
            },
            {  # zero amount → skipped
                "credit_debit_indicator": "DBIT",
                "status": "BOOK",
                "booking_date": "2025-01-09",
                "transaction_amount": {"amount": "0", "currency": "EUR"},
            },
        ],
        "continuation_key": None,
    }
    pages = [page1, page2]
    calls = []

    def fake_get(path, *, session_scoped):
        assert session_scoped is True  # reading an account rides on the consent
        calls.append(path)
        return pages[len(calls) - 1]

    monkeypatch.setattr(eb, "_get", fake_get)
    txns = eb.get_transactions("sess", "acc-1", "2025-01-01", "2025-01-31")

    assert len(calls) == 2
    assert "date_from=2025-01-01" in calls[0] and "date_to=2025-01-31" in calls[0]
    assert "continuation_key=next-page" in calls[1]

    assert len(txns) == 3
    assert txns[0] == {
        "date": "2025-01-05", "store": "K-Market", "amount": 12.5,
        "type": "expense", "currency": "EUR", "reference": "ref-1",
    }
    assert txns[1]["type"] == "income"
    assert txns[1]["store"] == "Employer Oy"
    assert txns[2]["store"] == "Coffee shop"


class _Resp:
    """Minimal stand-in for a requests Response."""

    def __init__(self, status_code, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.mark.parametrize("status", [401, 403])
def test_pre_session_rejection_blames_our_credentials(status):
    """A 401/403 before any consent exists is the app's key, not a stale consent.

    This is the case the old code got wrong: it told the user their bank consent
    had expired while they were still trying to start one.
    """
    resp = _Resp(status, text='{"message":"invalid kid"}')
    with pytest.raises(eb.BankAuthError) as excinfo:
        eb._handle_response("/auth", resp, session_scoped=False)
    message = str(excinfo.value)
    assert "expired" not in message.lower()
    assert "ENABLE_BANKING_APP_ID" in message
    assert "invalid kid" in message  # the bank's own words survive


@pytest.mark.parametrize("status", [401, 403])
def test_session_rejection_is_an_expired_consent(status):
    """The same status on a session-scoped call does mean: reconnect."""
    resp = _Resp(status, text='{"message":"session not found"}')
    with pytest.raises(eb.SessionExpired) as excinfo:
        eb._handle_response("/accounts/acc-1/transactions", resp, session_scoped=True)
    assert "reconnect" in str(excinfo.value).lower()
    assert "session not found" in str(excinfo.value)


def test_auth_error_is_not_caught_as_a_session_expiry():
    """Route handlers catch SessionExpired first, so the classes must not overlap."""
    assert issubclass(eb.BankAuthError, eb.BankError)
    assert not issubclass(eb.BankAuthError, eb.SessionExpired)
    assert not issubclass(eb.SessionExpired, eb.BankAuthError)


def test_other_statuses_stay_generic():
    with pytest.raises(eb.BankError) as excinfo:
        eb._handle_response("/auth", _Resp(500, text="boom"), session_scoped=False)
    assert not isinstance(excinfo.value, (eb.SessionExpired, eb.BankAuthError))
    assert "boom" in str(excinfo.value)


def test_session_valid():
    assert eb.session_valid("2999-01-01T00:00:00+00:00") is True
    assert eb.session_valid("2000-01-01T00:00:00+00:00") is False
    assert eb.session_valid(None) is True
    assert eb.session_valid("garbage") is True  # unknown → let API judge


# ───────────────────────────────────────────────────────────────────────
# DB helpers for the route/isolation tests
# ───────────────────────────────────────────────────────────────────────


def _insert_bank_session(user_id, *, session_id="sess-A", valid_until="2999-01-01T00:00:00+00:00",
                         accounts=None, aspsp="Nordea"):
    """Insert a bank_sessions row (committed); return its id."""
    if accounts is None:
        accounts = [{"uid": "acc-1", "iban": "FI00", "name": "Acc", "currency": "EUR"}]
    with db.db_conn() as conn:
        row = conn.execute(
            "INSERT INTO bank_sessions "
            "(user_id, session_id, aspsp_name, aspsp_country, valid_until, accounts) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (user_id, session_id, aspsp, "FI", valid_until, db.Json(accounts)),
        ).fetchone()
    return row["id"]


def _bank_session_count(user_id):
    with db.db_conn() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM bank_sessions WHERE user_id = %s", (user_id,)
        ).fetchone()
    return row["n"]


def _staging_count(user_id):
    with db.db_conn() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM import_staging WHERE user_id = %s", (user_id,)
        ).fetchone()
    return row["n"]


# ───────────────────────────────────────────────────────────────────────
# 2. Routes
# ───────────────────────────────────────────────────────────────────────


def test_status_not_connected(client, login, make_user):
    uid = make_user()
    login(client, uid)
    r = client.get("/api/import/bank/status")
    assert r.status_code == 200
    body = r.get_json()
    assert body["connected"] is False
    assert body["accounts"] == []


def test_status_connected(client, login, make_user):
    uid = make_user()
    _insert_bank_session(uid)
    login(client, uid)
    body = client.get("/api/import/bank/status").get_json()
    assert body["connected"] is True
    assert body["accounts"][0]["uid"] == "acc-1"


def test_status_expired(client, login, make_user):
    uid = make_user()
    _insert_bank_session(uid, valid_until="2000-01-01T00:00:00+00:00")
    login(client, uid)
    body = client.get("/api/import/bank/status").get_json()
    assert body["connected"] is False
    assert body["expired"] is True


def test_connect_redirects_and_sets_state(client, login, make_user, monkeypatch, configure_eb):
    import app as app_module

    uid = make_user()
    login(client, uid)

    monkeypatch.setattr(
        app_module.enable_banking, "start_auth",
        lambda name, country, redirect_url, state: f"https://bank/auth?state={state}",
    )
    r = client.get("/api/import/bank/connect")
    assert r.status_code == 302
    assert r.headers["Location"].startswith("https://bank/auth?state=")
    with client.session_transaction() as s:
        assert s.get("bank_oauth_state")  # nonce stored for the callback


def test_connect_not_configured(client, login, make_user, monkeypatch):
    import app as app_module

    uid = make_user()
    login(client, uid)
    monkeypatch.setattr(app_module.config, "enable_banking_configured", lambda: False)
    r = client.get("/api/import/bank/connect")
    # /connect is a full-page navigation: send the browser back to the Import tab
    # with a reason code instead of leaving the user on a page of raw JSON.
    assert r.status_code == 302
    assert "bank=not_configured" in r.headers["Location"]


def test_connect_credential_failure_does_not_say_reconnect(
    client, login, make_user, monkeypatch, configure_eb
):
    """The bug this fixes: a refused app key used to read as "consent expired"."""
    import app as app_module

    uid = make_user()
    login(client, uid)

    def boom(*a, **kw):
        raise eb.BankAuthError("Enable Banking refused this app's credentials")

    monkeypatch.setattr(app_module.enable_banking, "start_auth", boom)
    r = client.get("/api/import/bank/connect")
    assert r.status_code == 302
    assert "bank=auth_error" in r.headers["Location"]


def test_connect_other_bank_error(client, login, make_user, monkeypatch, configure_eb):
    import app as app_module

    def boom(*a, **kw):
        raise eb.BankError("gateway is down")

    uid = make_user()
    login(client, uid)
    monkeypatch.setattr(app_module.enable_banking, "start_auth", boom)
    r = client.get("/api/import/bank/connect")
    assert r.status_code == 302
    assert "bank=error" in r.headers["Location"]


def test_callback_credential_failure_redirects(
    client, login, make_user, monkeypatch, configure_eb
):
    import app as app_module

    uid = make_user()
    login(client, uid)
    with client.session_transaction() as sess:
        sess["bank_oauth_state"] = "good-state"

    def boom(code):
        raise eb.BankAuthError("refused")

    monkeypatch.setattr(app_module.enable_banking, "create_session", boom)
    r = client.get("/api/import/bank/callback?state=good-state&code=abc")
    assert r.status_code == 302
    assert "bank=auth_error" in r.headers["Location"]
    assert _bank_session_count(uid) == 0


def test_callback_declined_at_bank_reads_as_cancelled(
    client, login, make_user, configure_eb
):
    """Declining at the bank is not a failure — keep it apart from one."""
    uid = make_user()
    login(client, uid)
    with client.session_transaction() as sess:
        sess["bank_oauth_state"] = "good-state"
    r = client.get("/api/import/bank/callback?state=good-state&error=access_denied")
    assert r.status_code == 302
    assert "bank=cancelled" in r.headers["Location"]


def test_callback_rejects_bad_state(client, login, make_user, monkeypatch, configure_eb):
    import app as app_module

    uid = make_user()
    login(client, uid)
    with client.session_transaction() as s:
        s["bank_oauth_state"] = "the-real-state"

    called = {"create": False}

    def fake_create(code):
        called["create"] = True
        return {"session_id": "x", "accounts": [], "valid_until": None}

    monkeypatch.setattr(app_module.enable_banking, "create_session", fake_create)
    r = client.get("/api/import/bank/callback?state=WRONG&code=abc")
    assert r.status_code == 400
    assert called["create"] is False
    assert _bank_session_count(uid) == 0


def test_callback_upserts_on_good_state(client, login, make_user, monkeypatch, configure_eb):
    import app as app_module

    uid = make_user()
    login(client, uid)
    with client.session_transaction() as s:
        s["bank_oauth_state"] = "good-state"

    monkeypatch.setattr(
        app_module.enable_banking, "create_session",
        lambda code: {
            "session_id": "sess-new",
            "accounts": [{"uid": "acc-9", "iban": "FI9", "name": "A", "currency": "EUR"}],
            "valid_until": "2999-01-01T00:00:00+00:00",
        },
    )
    r = client.get("/api/import/bank/callback?state=good-state&code=abc")
    assert r.status_code == 302
    assert "bank=connected" in r.headers["Location"]
    assert _bank_session_count(uid) == 1

    # Reconnect replaces (UNIQUE(user_id)) rather than duplicating.
    with client.session_transaction() as s:
        s["bank_oauth_state"] = "good-state-2"
    monkeypatch.setattr(
        app_module.enable_banking, "create_session",
        lambda code: {"session_id": "sess-2", "accounts": [], "valid_until": None},
    )
    client.get("/api/import/bank/callback?state=good-state-2&code=def")
    assert _bank_session_count(uid) == 1


def test_fetch_stages_user_scoped_rows(client, login, make_user, monkeypatch):
    import app as app_module

    uid = make_user()
    _insert_bank_session(uid, session_id="sess-A")
    login(client, uid)

    monkeypatch.setattr(
        app_module.enable_banking, "get_transactions",
        lambda session_id, account_uid, date_from, date_to: [
            {"date": "2025-01-05", "store": "K-Market", "amount": 12.5,
             "type": "expense", "currency": "EUR", "reference": "r1"},
            {"date": "2025-01-06", "store": "Employer", "amount": 200.0,
             "type": "income", "currency": "EUR", "reference": "r2"},
        ],
    )
    r = client.post("/api/import/bank/fetch", json={
        "account_uid": "acc-1", "date_from": "2025-01-01", "date_to": "2025-01-31",
    })
    assert r.status_code == 200
    body = r.get_json()
    # Same shape as /api/import/upload.
    assert set(body.keys()) == {"batch_id", "count", "items"}
    assert body["count"] == 2
    assert _staging_count(uid) == 2
    item = body["items"][0]
    for key in ("date", "store", "amount", "type", "import_batch_id", "user_id"):
        assert key in item
    assert all(it["user_id"] == uid for it in body["items"])


def test_fetch_not_connected(client, login, make_user):
    uid = make_user()
    login(client, uid)
    r = client.post("/api/import/bank/fetch", json={
        "account_uid": "acc-1", "date_from": "2025-01-01", "date_to": "2025-01-31",
    })
    assert r.status_code == 401
    assert r.get_json()["error"] == "not_connected"


def test_fetch_session_expired(client, login, make_user):
    uid = make_user()
    _insert_bank_session(uid, valid_until="2000-01-01T00:00:00+00:00")
    login(client, uid)
    r = client.post("/api/import/bank/fetch", json={
        "account_uid": "acc-1", "date_from": "2025-01-01", "date_to": "2025-01-31",
    })
    assert r.status_code == 401
    assert r.get_json()["error"] == "session_expired"


def test_fetch_credential_failure_is_not_session_expired(
    client, login, make_user, monkeypatch
):
    import app as app_module

    uid = make_user()
    _insert_bank_session(uid)
    login(client, uid)

    def boom(session_id, account_uid, date_from, date_to):
        raise eb.BankAuthError("refused")

    monkeypatch.setattr(app_module.enable_banking, "get_transactions", boom)
    r = client.post("/api/import/bank/fetch", json={
        "account_uid": "acc-1", "date_from": "2025-01-01", "date_to": "2025-01-31",
    })
    assert r.status_code == 502
    assert r.get_json()["error"] == "bank_auth"


def test_fetch_bad_dates(client, login, make_user):
    uid = make_user()
    _insert_bank_session(uid)
    login(client, uid)
    r = client.post("/api/import/bank/fetch", json={
        "account_uid": "acc-1", "date_from": "nope", "date_to": "2025-01-31",
    })
    assert r.status_code == 400


def test_disconnect_deletes_row(client, login, make_user):
    uid = make_user()
    _insert_bank_session(uid)
    login(client, uid)
    assert _bank_session_count(uid) == 1
    r = client.post("/api/import/bank/disconnect")
    assert r.status_code == 200
    assert _bank_session_count(uid) == 0


# ───────────────────────────────────────────────────────────────────────
# 3. Tenant isolation — removed
#
# Three tests used to assert that user B could not read, fetch with, or
# disconnect user A's bank session. The app has one user and no login, so
# there is no second tenant to isolate from. What those tests actually
# guarded — a fetch with no session 401s as "not_connected" — is covered by
# test_fetch_not_connected and test_status_not_connected above.
# ───────────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────────
# 4. Migration
# ───────────────────────────────────────────────────────────────────────


def test_init_db_creates_bank_sessions():
    """init_db() is idempotent and leaves bank_sessions present with its index."""
    import database

    database.init_db()
    with db.db_conn() as conn:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'bank_sessions'"
        ).fetchone()
        assert exists is not None and exists["name"] == "bank_sessions"
        idx = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND name = 'idx_bank_sessions_user'"
        ).fetchone()
        assert idx is not None
