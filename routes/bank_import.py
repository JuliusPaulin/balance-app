"""Open Banking import (Enable Banking): consent, fetch, disconnect."""

import secrets
from datetime import datetime
from flask import Blueprint, request, jsonify, session, redirect
import db
from database import get_db, db_conn
import config
import enable_banking
from enable_banking import BankAuthError, BankConfigMissing, BankError, SessionExpired
from core import app, limiter, current_user_id
from routes.csv_import import suggest_category, _staging_response, _cleanup_import_batch

bp = Blueprint("bank_import", __name__)


# In-app PSD2 consent + per-user session storage. The desktop build bootstraps
# its bank connection from a local config file; on the hosted server there is no
# such file, so the user consents in-browser (connect → bank → callback) and the
# resulting session is stored per-user in bank_sessions. Every route below is
# behind the normal auth/status guard and scoped by current_user_id(), so one
# user can never see or use another user's bank session.

# Session key holding the per-flow CSRF nonce for the bank consent redirect. The
# bank echoes ``state`` back to the callback; we compare it to this stored value
# to reject forged/replayed callbacks (in addition to the standard CSRF on the
# mutating routes — connect/callback are top-level GET navigations).
_BANK_STATE_KEY = "bank_oauth_state"


def _bank_failed(reason, detail):
    """Send a failed consent step back to the Import tab with a reason code.

    /connect and /callback are full-page browser navigations, so returning JSON
    would leave the user staring at a raw error object. Instead we log the detail
    for whoever runs the server and bounce the browser to /#import?bank=<reason>,
    where the UI turns the code into a sentence. ``reason`` is one of
    ``not_configured``, ``auth_error``, ``error`` or ``cancelled``.
    """
    app.logger.warning("Bank consent step failed (%s): %s", reason, detail)
    return redirect(f"/#import?bank={reason}")


def _load_bank_session(uid):
    """Return the user's bank_sessions row as a dict, or None."""
    with db_conn() as conn:
        return conn.execute(
            "SELECT id, session_id, aspsp_name, aspsp_country, valid_until, accounts "
            "FROM bank_sessions WHERE user_id = %s",
            (uid,),
        ).fetchone()


@bp.route("/api/import/bank/status")
def bank_status():
    """Report this user's bank-connection state for the Import UI.

    Returns ``{connected, valid_until, accounts}``. ``connected`` is True only
    when a session row exists AND its consent has not expired; an expired session
    reports ``connected=False`` but still surfaces ``valid_until`` so the UI can
    prompt a reconnect. ``configured`` tells the UI whether the server even has
    Enable Banking credentials (so it can hide the card when not).
    """
    uid = current_user_id()
    row = _load_bank_session(uid)
    if not row:
        return jsonify({
            "connected": False,
            "configured": config.enable_banking_configured(),
            "valid_until": None,
            "accounts": [],
        })
    valid = enable_banking.session_valid(row["valid_until"])
    vu = row["valid_until"]
    return jsonify({
        "connected": bool(valid),
        "expired": not valid,
        "configured": config.enable_banking_configured(),
        "valid_until": vu.isoformat() if hasattr(vu, "isoformat") else vu,
        "aspsp_name": row["aspsp_name"],
        "accounts": db.load_json(row["accounts"]) or [],
    })


@bp.route("/api/import/bank/connect")
def bank_connect():
    """Start the bank consent flow: mint a CSRF state, 302 to the bank.

    A random ``state`` nonce is stored in the Flask session and passed to Enable
    Banking; the callback verifies the echoed value matches. The redirect target
    is our own callback under BANK_REDIRECT_BASE. A full-page browser redirect
    (302) is returned so the user lands on their bank's authorization page.
    """
    current_user_id()  # enforce auth (raises 401 otherwise)
    if not config.enable_banking_configured():
        return _bank_failed("not_configured", "Bank import is not configured.")

    state = secrets.token_urlsafe(32)
    session[_BANK_STATE_KEY] = state
    redirect_url = config.BANK_REDIRECT_BASE + "/api/import/bank/callback"
    try:
        auth_url = enable_banking.start_auth(
            config.ENABLE_BANKING_ASPSP,
            config.ENABLE_BANKING_COUNTRY,
            redirect_url,
            state,
        )
    except BankConfigMissing as e:
        return _bank_failed("not_configured", e)
    except BankAuthError as e:
        return _bank_failed("auth_error", e)
    except BankError as e:
        return _bank_failed("error", e)
    return redirect(auth_url)


@bp.route("/api/import/bank/callback")
def bank_callback():
    """Complete the consent: verify state, create a session, store it per-user.

    The bank redirects here with ``code`` + ``state``. We reject any callback
    whose ``state`` doesn't match the value we stashed in :func:`bank_connect`
    (CSRF defence). On success we exchange the code for a session and upsert the
    user's single ``bank_sessions`` row (delete-then-insert under UNIQUE(user_id)),
    then bounce back to the Import page.
    """
    uid = current_user_id()

    expected = session.pop(_BANK_STATE_KEY, None)
    got = request.args.get("state")
    if not expected or not got or not secrets.compare_digest(str(got), str(expected)):
        return jsonify({"error": "Invalid or missing state (CSRF check failed)."}), 400

    # The bank may redirect back with an error instead of a code (user declined).
    if request.args.get("error") or not request.args.get("code"):
        return redirect("/#import?bank=cancelled")

    code = request.args.get("code")
    try:
        result = enable_banking.create_session(code)
    except BankConfigMissing as e:
        return _bank_failed("not_configured", e)
    except BankAuthError as e:
        return _bank_failed("auth_error", e)
    except BankError as e:
        return _bank_failed("error", e)

    with db_conn() as conn:
        # Delete-then-insert keeps the UNIQUE(user_id) "one active connection"
        # invariant on reconnect without relying on ON CONFLICT semantics.
        conn.execute("DELETE FROM bank_sessions WHERE user_id = %s", (uid,))
        conn.execute(
            "INSERT INTO bank_sessions "
            "(user_id, session_id, aspsp_name, aspsp_country, valid_until, accounts) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                uid,
                result["session_id"],
                config.ENABLE_BANKING_ASPSP,
                config.ENABLE_BANKING_COUNTRY,
                result.get("valid_until"),
                db.Json(result.get("accounts") or []),
            ),
        )
    return redirect("/#import?bank=connected")


@bp.route("/api/import/bank/fetch", methods=["POST"])
@limiter.limit("30/hour")
def bank_fetch():
    """Fetch transactions from the bank into the standard import review pipeline.

    Body: ``{account_uid, date_from, date_to}``. Loads the user's bank session
    (401 ``not_connected`` / ``session_expired`` when missing/expired), validates
    the dates, then creates an ``import_batches`` row and stages each normalized
    transaction into ``import_staging`` using the existing per-user
    ``suggest_category``. Returns ``{batch_id, count, items}`` — the SAME shape as
    /api/import/upload so the frontend reuses the review table.
    """
    uid = current_user_id()
    data = request.json or {}
    account_uid = data.get("account_uid")
    date_from = data.get("date_from")
    date_to = data.get("date_to")

    if not account_uid:
        return jsonify({"error": "account_uid is required"}), 400
    if not _valid_iso_date(date_from) or not _valid_iso_date(date_to):
        return jsonify({"error": "date_from and date_to must be YYYY-MM-DD"}), 400
    if date_from > date_to:
        return jsonify({"error": "date_from must be on or before date_to"}), 400

    row = _load_bank_session(uid)
    if not row:
        return jsonify({"error": "not_connected"}), 401
    if not enable_banking.session_valid(row["valid_until"]):
        return jsonify({"error": "session_expired"}), 401

    # Guard: the requested account must belong to this user's stored session.
    account_uids = {a.get("uid") for a in (db.load_json(row["accounts"]) or []) if isinstance(a, dict)}
    if account_uids and account_uid not in account_uids:
        return jsonify({"error": "unknown account for this connection"}), 400

    try:
        txns = enable_banking.get_transactions(
            row["session_id"], account_uid, date_from, date_to
        )
    except SessionExpired:
        return jsonify({"error": "session_expired"}), 401
    except BankConfigMissing as e:
        return jsonify({"error": str(e)}), 400
    except BankAuthError as e:
        app.logger.warning("Enable Banking refused the app credentials: %s", e)
        return jsonify({"error": "bank_auth"}), 502
    except BankError as e:
        return jsonify({"error": f"Bank error: {e}"}), 502

    aspsp = row["aspsp_name"] or "Bank"
    filename = f"Bank · {aspsp} · {date_from}..{date_to}"

    conn = get_db()
    batch_id = None
    try:
        cursor = conn.execute(
            "INSERT INTO import_batches (user_id, filename) VALUES (%s, %s) RETURNING id",
            (uid, filename),
        )
        batch_id = cursor.fetchone()["id"]

        for txn in txns:
            # Same rule as the CSV path: the suggestion is scoped to the type
            # the bank said the row is (DBIT/CRDT), so a type-blind merchant
            # rule cannot flip a credit into spending. See suggest_category.
            suggested = suggest_category(txn["store"], conn, uid, txn["type"])
            conn.execute(
                "INSERT INTO import_staging "
                "(user_id, date, store, suggested_category, amount, type, import_batch_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (uid, txn["date"], txn["store"], suggested, txn["amount"],
                 txn["type"], batch_id),
            )
        conn.commit()
        return _staging_response(conn, uid, batch_id)
    except Exception as e:
        conn.rollback()
        if batch_id is not None:
            try:
                _cleanup_import_batch(conn, batch_id, uid)
                conn.commit()
            except db.DatabaseError:
                conn.rollback()
        return jsonify({"error": f"Import failed: {e}"}), 500
    finally:
        conn.close()


@bp.route("/api/import/bank/disconnect", methods=["POST"])
def bank_disconnect():
    """Forget this user's bank connection (delete their bank_sessions row)."""
    uid = current_user_id()
    with db_conn() as conn:
        conn.execute("DELETE FROM bank_sessions WHERE user_id = %s", (uid,))
    return jsonify({"status": "ok"})


def _valid_iso_date(value):
    """True when ``value`` is a 'YYYY-MM-DD' string parseable as a real date."""
    if not isinstance(value, str):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False
