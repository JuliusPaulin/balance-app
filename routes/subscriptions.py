"""Recurring charges: what was detected, what was dismissed, what was added by hand."""

from flask import Blueprint, request, jsonify
from database import db_conn
from recurring import detect_recurring
import core
from core import current_user_id, bump_data_version

bp = Blueprint("subscriptions", __name__)


@bp.route("/api/recurring")
def recurring():
    """Detected recurring charges & subscriptions (see recurring.py)."""
    uid = current_user_id()

    def _int_arg(name, default):
        try:
            return int(request.args.get(name, default))
        except (TypeError, ValueError):
            return default

    lookback = _int_arg("lookback_months", 18)
    min_occ = _int_arg("min_occurrences", 3)

    key = (uid, lookback, min_occ)
    cached = core.recurring_cache.get(key)
    if cached and cached[0] == core.data_version:
        return jsonify(cached[1])

    with db_conn() as conn:
        result = detect_recurring(conn, uid, lookback_months=lookback,
                                  min_occurrences=min_occ)
    core.recurring_cache[key] = (core.data_version, result)
    return jsonify(result)


@bp.route("/api/recurring/dismiss", methods=["POST"])
def dismiss_recurring():
    """Hide a recurring series. Body: {"signature": "<normalized>|<cadence>"}."""
    uid = current_user_id()
    data = request.json or {}
    sig = (data.get("signature") or "").strip()
    if not sig:
        return jsonify({"error": "signature is required"}), 400
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO recurring_dismissed (user_id, signature) VALUES (%s, %s) "
            "ON CONFLICT (user_id, signature) DO NOTHING",
            (uid, sig),
        )
    bump_data_version()
    return jsonify({"dismissed": sig}), 201


@bp.route("/api/recurring/dismiss/<path:sig>", methods=["DELETE"])
def undismiss_recurring(sig):
    """Un-hide a previously dismissed recurring series."""
    uid = current_user_id()
    with db_conn() as conn:
        conn.execute(
            "DELETE FROM recurring_dismissed WHERE user_id = %s AND signature = %s",
            (uid, sig),
        )
    bump_data_version()
    return "", 204


_SUB_CADENCES = ("monthly", "quarterly", "yearly")


@bp.route("/api/subscriptions", methods=["POST"])
def add_subscription():
    """Add a manual subscription (one detection missed).

    Body: {store, amount, cadence, category?, type?}. Stored in
    manual_subscriptions and folded into /api/recurring on the next load.
    """
    uid = current_user_id()
    data = request.json or {}
    store = (data.get("store") or "").strip()
    cadence = (data.get("cadence") or "monthly").strip().lower()
    sub_type = (data.get("type") or "expense").strip().lower()
    category = (data.get("category") or "").strip() or None
    try:
        amount = abs(float(data.get("amount")))
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a number"}), 400
    if not store:
        return jsonify({"error": "store is required"}), 400
    if amount <= 0:
        return jsonify({"error": "amount must be greater than 0"}), 400
    if cadence not in _SUB_CADENCES:
        return jsonify({"error": "cadence must be monthly, quarterly or yearly"}), 400
    if sub_type not in ("expense", "income"):
        return jsonify({"error": "type must be expense or income"}), 400

    with db_conn() as conn:
        row = conn.execute(
            "INSERT INTO manual_subscriptions (user_id, store, amount, cadence, "
            "category, type) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (uid, store, amount, cadence, category, sub_type),
        ).fetchone()
    bump_data_version()
    return jsonify({"id": row["id"]}), 201


@bp.route("/api/subscriptions/<int:sub_id>", methods=["DELETE"])
def delete_subscription(sub_id):
    """Delete a manual subscription owned by the current user."""
    uid = current_user_id()
    with db_conn() as conn:
        conn.execute(
            "DELETE FROM manual_subscriptions WHERE id = %s AND user_id = %s",
            (sub_id, uid),
        )
    bump_data_version()
    return "", 204
