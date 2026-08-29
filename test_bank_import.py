"""Tests for the hosted Open Banking import (Enable Banking).

Covers four layers, all with the EB network mocked (no real key, no HTTP):

1. ``enable_banking`` core: JWT built from the base64 env key (verified by
   decoding it back with the matching public key + checking the ``kid`` header),
   ``get_transactions`` normalisation + pagination, and request shaping for
   ``start_auth`` / ``create_session`` (we mock the thin ``_get`` / ``_post``).
2. Routes: ``/connect`` issues a 302 and stores the CSRF state; ``/callback``
   rejects a bad state and upserts on a good code; ``/fetch`` stages user-scoped
   rows in the SAME shape as /api/import/upload; an expired session → 401.
3. Tenant isolation: user B cannot read user A's bank session, cannot fetch with
   it, and disconnect/fetch are scoped to the caller.
4. Migration: ``init_db()`` creates the ``bank_sessions`` table.

The route/isolation/migration tests use the shared ``conftest.py`` Postgres
fixtures (``client``, ``login``, ``make_user``, ``fresh_conn``); the EB module
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

    def fake_post(path, body):
        captured["path"] = path
        captured["body"] = body
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


def test_start_auth_missing_url_raises(monkeypatch):
    monkeypatch.setattr(eb, "_post", lambda p, b: {})
    with pytest.raises(eb.BankError):
        eb.start_auth("Nordea", "FI", "https://app/cb", "s")


def test_create_session_shapes_result(monkeypatch):
    """create_session POSTs /sessions and normalises accounts + valid_until."""
    captured = {}

    def fake_post(path, body):
        captured["path"] = path
        captured["body"] = body
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


def test_create_session_missing_id_raises(monkeypatch):
    monkeypatch.setattr(eb, "_post", lambda p, b: {"accounts": []})
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

    def fake_get(path):
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
    """Insert a bank_sessions row for a user (committed); return its id."""
    from psycopg.types.json import Json

    if accounts is None:
        accounts = [{"uid": "acc-1", "iban": "FI00", "name": "Acc", "currency": "EUR"}]
    with db.db_conn() as conn:
        row = conn.execute(
            "INSERT INTO bank_sessions "
            "(user_id, session_id, aspsp_name, aspsp_country, valid_until, accounts) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (user_id, session_id, aspsp, "FI", valid_until, Json(accounts)),
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
    assert r.status_code == 400


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
# 3. Tenant isolation (user B vs user A)
# ───────────────────────────────────────────────────────────────────────


def test_user_b_cannot_read_user_a_session(client, login, make_user):
    a = make_user()
    b = make_user()
    _insert_bank_session(a, session_id="sess-A")
    # B has no session of their own.
    login(client, b)
    body = client.get("/api/import/bank/status").get_json()
    assert body["connected"] is False
    assert body["accounts"] == []


def test_user_b_cannot_fetch_with_user_a_session(client, login, make_user, monkeypatch):
    import app as app_module

    a = make_user()
    b = make_user()
    _insert_bank_session(a, session_id="sess-A")

    # If the route were unscoped it might use A's session; assert it does NOT —
    # B has no connection, so fetch must 401 not_connected and never call EB.
    called = {"n": 0}

    def spy(*args, **kwargs):
        called["n"] += 1
        return []

    monkeypatch.setattr(app_module.enable_banking, "get_transactions", spy)
    login(client, b)
    r = client.post("/api/import/bank/fetch", json={
        "account_uid": "acc-1", "date_from": "2025-01-01", "date_to": "2025-01-31",
    })
    assert r.status_code == 401
    assert r.get_json()["error"] == "not_connected"
    assert called["n"] == 0
    assert _staging_count(b) == 0


def test_user_b_disconnect_does_not_touch_user_a(client, login, make_user):
    a = make_user()
    b = make_user()
    _insert_bank_session(a, session_id="sess-A")
    login(client, b)
    client.post("/api/import/bank/disconnect")
    # A's session is untouched.
    assert _bank_session_count(a) == 1


# ───────────────────────────────────────────────────────────────────────
# 4. Migration
# ───────────────────────────────────────────────────────────────────────


def test_init_db_creates_bank_sessions():
    """init_db() is idempotent and leaves bank_sessions present with its index."""
    import database

    database.init_db()
    with db.db_conn() as conn:
        exists = conn.execute(
            "SELECT to_regclass('public.bank_sessions') AS t"
        ).fetchone()["t"]
        assert exists == "bank_sessions"
        idx = conn.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'bank_sessions' AND indexname = 'idx_bank_sessions_user'"
        ).fetchone()
        assert idx is not None
