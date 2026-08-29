"""Enable Banking (PSD2) client — hosted/server variant.

Server-side port of the desktop ``enable_banking.py`` (on the ``dev`` branch).
The desktop build bootstraps its credentials + session from a local
``~/.bank-mcp/config.json`` written by ``@bank-mcp/server``; that file does not
exist on the hosted server, so this variant:

- builds the app-authenticating RS256 JWT (``kid = ENABLE_BANKING_APP_ID``) from
  the **base64-encoded PEM in ``config.ENABLE_BANKING_PRIVATE_KEY``** (an env var
  / Render secret) — never a file on disk;
- drives the full **in-app consent flow** (``start_auth`` → bank → ``create_session``)
  because there is no local interactive wizard;
- holds no per-user state: the caller (the Flask routes) owns the
  ``bank_sessions`` table and passes ``session_id`` / ``account_uid`` in.

Design for testability: every HTTP call funnels through the thin ``_get`` /
``_post`` helpers, which are the only functions that touch the network. The unit
tests monkeypatch those two so the JWT-building, request-shaping, pagination, and
transaction-normalisation logic can be exercised with no network and no real key.

Security: the private key is read only from the environment (via :mod:`config`),
is never logged, and is never returned to a caller.
"""

import base64
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import jwt  # PyJWT
import requests

import config

API_BASE = "https://api.enablebanking.com"

# Only import settled transactions. Pending entries (status "PDNG") can change or
# disappear before they settle, so we skip them to avoid importing rows the bank
# may later revise. (Matches the desktop client.)
INCLUDED_TRANSACTION_STATUS = "BOOK"

# PSU type for personal (vs business) bank access.
PSU_TYPE = "personal"

# Network timeout (seconds) for every EB call.
_HTTP_TIMEOUT = 30


# ── Errors ───────────────────────────────────────────────────────────


class BankError(Exception):
    """Generic Enable Banking failure (network / unexpected HTTP status)."""


class SessionExpired(BankError):
    """The PSD2 session is past valid_until or the API rejected it (401/403).

    Recovery requires the user to re-consent at their bank (the "Reconnect"
    flow), since PSD2 SCA forces re-auth roughly every 90 days.
    """


class BankConfigMissing(BankError):
    """Enable Banking is not configured (app id / private key env vars unset)."""


# ── Auth (JWT from env, never a file) ────────────────────────────────


def _load_private_key() -> str:
    """Decode the base64-encoded PEM private key from the environment.

    Raises :class:`BankConfigMissing` when the app id or key env var is unset, or
    when the configured value isn't valid base64. The decoded PEM is returned as
    a string and is NEVER logged.
    """
    if not config.enable_banking_configured():
        raise BankConfigMissing(
            "Enable Banking is not configured. Set ENABLE_BANKING_APP_ID and "
            "ENABLE_BANKING_PRIVATE_KEY (base64-encoded PEM) in the environment."
        )
    try:
        return base64.b64decode(config.ENABLE_BANKING_PRIVATE_KEY).decode("utf-8")
    except Exception as e:  # noqa: BLE001 - normalise any decode failure
        raise BankConfigMissing(
            "ENABLE_BANKING_PRIVATE_KEY is not valid base64-encoded PEM."
        ) from e


def _jwt() -> str:
    """Build the short-lived RS256 JWT that authenticates the app on every call.

    ``kid`` is the Enable Banking application id; the key is the PEM decoded from
    the env var. Valid for one hour.
    """
    key = _load_private_key()
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "enablebanking.com",
            "aud": "api.enablebanking.com",
            "iat": now,
            "exp": now + 3600,
        },
        key,
        algorithm="RS256",
        headers={"kid": config.ENABLE_BANKING_APP_ID, "typ": "JWT"},
    )


def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {_jwt()}",
        "Accept": "application/json",
    }


# ── Thin HTTP helpers (the only network surface — mocked in tests) ───


def _get(path: str) -> dict:
    """GET ``{API_BASE}{path}`` with the app JWT; return parsed JSON.

    Maps 401/403 to :class:`SessionExpired` and any other non-2xx (or network
    error) to :class:`BankError`. Kept deliberately thin so tests mock it.
    """
    try:
        resp = requests.get(
            f"{API_BASE}{path}", headers=_auth_headers(), timeout=_HTTP_TIMEOUT
        )
    except requests.RequestException as e:
        raise BankError(f"Network error calling Enable Banking: {e}") from e
    return _handle_response(path, resp)


def _post(path: str, body: dict) -> dict:
    """POST JSON ``body`` to ``{API_BASE}{path}`` with the app JWT; return JSON.

    Same error mapping as :func:`_get`. Kept thin so tests mock it.
    """
    try:
        resp = requests.post(
            f"{API_BASE}{path}",
            headers=_auth_headers(),
            json=body,
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        raise BankError(f"Network error calling Enable Banking: {e}") from e
    return _handle_response(path, resp)


def _handle_response(path: str, resp) -> dict:
    """Translate an HTTP response into parsed JSON or a typed error."""
    if resp.status_code in (401, 403):
        raise SessionExpired(
            f"Enable Banking rejected the request (HTTP {resp.status_code}). "
            "The bank consent has expired — reconnect your bank."
        )
    if not (200 <= resp.status_code < 300):
        body = ""
        try:
            body = resp.text[:300]
        except Exception:  # pragma: no cover - defensive
            pass
        raise BankError(f"Enable Banking {path} -> HTTP {resp.status_code}. {body}")
    try:
        return resp.json()
    except ValueError as e:
        raise BankError(f"Enable Banking {path} returned non-JSON body.") from e


# ── Consent flow ─────────────────────────────────────────────────────


def start_auth(aspsp_name: str, aspsp_country: str, redirect_url: str, state: str) -> str:
    """Begin a PSD2 consent: POST /auth, return the bank authorization URL.

    The caller (route) redirects the user's browser to the returned URL where
    they log into their bank and consent. ``state`` is our CSRF nonce; the bank
    echoes it back to ``redirect_url`` so the callback can verify it.
    """
    payload = _post(
        "/auth",
        {
            "access": {"valid_until": _default_valid_until()},
            "aspsp": {"name": aspsp_name, "country": aspsp_country},
            "redirect_url": redirect_url,
            "state": state,
            "psu_type": PSU_TYPE,
        },
    )
    url = payload.get("url")
    if not url:
        raise BankError("Enable Banking /auth did not return an authorization url.")
    return url


def create_session(code: str) -> dict:
    """Exchange the consent ``code`` for a session: POST /sessions.

    Returns ``{session_id, accounts, valid_until}`` where ``accounts`` is a list
    of ``{uid, iban, name, currency}`` dicts (normalised from the EB account
    objects, which may be bare uid strings or rich objects depending on the API).
    """
    payload = _post("/sessions", {"code": code})
    session_id = payload.get("session_id")
    if not session_id:
        raise BankError("Enable Banking /sessions did not return a session_id.")
    accounts = _normalize_accounts(payload.get("accounts", []))
    valid_until = (payload.get("access") or {}).get("valid_until") or payload.get(
        "valid_until"
    )
    return {
        "session_id": session_id,
        "accounts": accounts,
        "valid_until": valid_until,
    }


def list_accounts(session: dict) -> list[dict]:
    """Return the cached ``[{uid, iban, name, currency}]`` for a stored session.

    ``session`` is the dict persisted in ``bank_sessions`` (its ``accounts``
    field). No network call — we always cache the account metadata at
    ``create_session`` time, so this is a pure accessor.
    """
    return _normalize_accounts((session or {}).get("accounts", []))


def session_valid(valid_until) -> bool:
    """Cheap pre-flight: is ``valid_until`` in the future?

    Accepts an ISO-8601 string or a ``datetime``. Unknown/unparseable values
    return ``True`` and let the API be the final judge (a real 401 then maps to
    :class:`SessionExpired`).
    """
    if not valid_until:
        return True
    if isinstance(valid_until, datetime):
        vu = valid_until
    else:
        try:
            vu = datetime.fromisoformat(str(valid_until).replace("Z", "+00:00"))
        except ValueError:
            return True
    if vu.tzinfo is None:
        vu = vu.replace(tzinfo=timezone.utc)
    return vu > datetime.now(timezone.utc)


# ── Transactions (ported from the desktop client) ────────────────────


def get_transactions(
    session_id: str, account_uid: str, date_from: str, date_to: str
) -> list[dict]:
    """Collect normalized BOOKed transactions for one account over a date range.

    Calls ``GET /accounts/{account_uid}/transactions``, paginating via
    ``continuation_key`` until exhausted. Filters to settled (status == "BOOK")
    transactions and returns dicts shaped like
    ``{date, store, amount, type, currency, reference}``.

    ``session_id`` is accepted for parity with the desktop signature and to scope
    the access; EB ties the account uid to the session server-side.
    """
    results: list[dict] = []
    continuation_key = None

    while True:
        params = {"date_from": date_from, "date_to": date_to}
        if continuation_key:
            params["continuation_key"] = continuation_key
        path = f"/accounts/{account_uid}/transactions?{urlencode(params)}"
        payload = _get(path)

        for txn in payload.get("transactions", []) or []:
            status = (txn.get("status") or "").upper()
            if status and status != INCLUDED_TRANSACTION_STATUS:
                continue
            normalized = _normalize_transaction(txn)
            if normalized:
                results.append(normalized)

        continuation_key = payload.get("continuation_key")
        if not continuation_key:
            break

    return results


# ── Normalisation helpers ────────────────────────────────────────────


def _normalize_accounts(raw_accounts) -> list[dict]:
    """Coerce EB account entries into ``[{uid, iban, name, currency}]``.

    EB's ``accounts`` may be a list of bare uid strings or of richer objects.
    Defensive: missing fields fall back to sensible defaults.
    """
    out = []
    for a in raw_accounts or []:
        if isinstance(a, str):
            out.append({"uid": a, "iban": None, "name": "Account", "currency": None})
            continue
        if not isinstance(a, dict):
            continue
        uid = a.get("uid") or a.get("account_uid") or a.get("id")
        if not uid:
            continue
        iban = a.get("iban") or (a.get("account_id") or {}).get("iban")
        name = a.get("name") or a.get("product") or "Account"
        currency = a.get("currency")
        out.append({"uid": uid, "iban": iban, "name": name, "currency": currency})
    return out


def _txn_date(txn: dict):
    """Pick a usable YYYY-MM-DD date from an EB transaction.

    Prefers booking_date, then value_date, then transaction_date. Takes the
    leading 10 chars in case a full datetime ever appears.
    """
    for key in ("booking_date", "value_date", "transaction_date"):
        val = txn.get(key)
        if val:
            return str(val)[:10]
    return None


def _txn_store(txn: dict, txn_type: str) -> str:
    """Derive a human-readable counterparty name for an EB transaction.

    Expenses → creditor (who got paid); income → debtor (who paid). Falls back to
    the other party, then remittance information, then "Unknown".
    """
    creditor = (txn.get("creditor") or {}).get("name")
    debtor = (txn.get("debtor") or {}).get("name")

    primary, secondary = (creditor, debtor) if txn_type == "expense" else (debtor, creditor)
    for candidate in (primary, secondary):
        if candidate and str(candidate).strip():
            return str(candidate).strip()

    remittance = txn.get("remittance_information")
    if isinstance(remittance, list):
        joined = " ".join(str(p).strip() for p in remittance if p and str(p).strip())
        if joined:
            return joined[:200]
    elif remittance and str(remittance).strip():
        return str(remittance).strip()[:200]

    return "Unknown"


def _normalize_transaction(txn: dict):
    """Map one raw EB transaction to a staging-shaped dict, or None to skip.

    Returns ``{date, store, amount, type, currency, reference}``. Defensive: EB
    fields are frequently missing or partial. CRDT → income/debtor; everything
    else → expense/creditor.
    """
    indicator = (txn.get("credit_debit_indicator") or "").upper()
    txn_type = "income" if indicator == "CRDT" else "expense"

    amount_obj = txn.get("transaction_amount") or {}
    raw_amount = amount_obj.get("amount")
    if raw_amount is None:
        return None
    try:
        amount = abs(float(raw_amount))
    except (TypeError, ValueError):
        return None
    if amount == 0:
        return None

    txn_date = _txn_date(txn)
    if not txn_date:
        return None

    return {
        "date": txn_date,
        "store": _txn_store(txn, txn_type),
        "amount": amount,
        "type": txn_type,
        "currency": amount_obj.get("currency"),
        "reference": txn.get("entry_reference") or txn.get("transaction_id"),
    }


def _default_valid_until() -> str:
    """ISO-8601 timestamp ~90 days out, the max PSD2 consent window.

    Used in the /auth ``access`` block to request the longest allowed consent.
    """
    from datetime import timedelta

    return (datetime.now(timezone.utc) + timedelta(days=90)).strftime(
        "%Y-%m-%dT%H:%M:%S.000000+00:00"
    )
