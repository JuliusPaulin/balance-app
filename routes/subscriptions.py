"""Recurring charges: what was detected, what was dismissed, what was added by hand."""

from datetime import date

from flask import Blueprint, request, jsonify
from database import db_conn
from recurring import (detect_recurring, GROUPS, GROUP_SUBSCRIPTION,
                       _load_dismissed)
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
        # What the user has hidden rides along with what they have not. The ✕
        # on a row wrote to `recurring_dismissed` and detection then dropped the
        # series without a word, so a series hidden by a misclick left no trace
        # anywhere in the app and took its cost out of the headline with it.
        # A signature is "<normalized store>|<cadence>", which is enough to name
        # the thing being offered back.
        result["dismissed"] = [
            {"signature": sig,
             "store": sig.rsplit("|", 1)[0],
             "cadence": sig.rsplit("|", 1)[-1] if "|" in sig else None}
            for sig in sorted(_load_dismissed(conn, uid))
        ]
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


@bp.route("/api/recurring/group", methods=["PUT"])
def set_recurring_group():
    """Re-file one series into another group. Body: {signature, group}.

    Grouping is inferred from the category, which is a good guess and will be
    wrong for somebody: the gym filed under "Exercise" is a subscription, and a
    repeat purchase of running shoes under the same category is not. Sending
    ``group: null`` clears the override and hands the row back to the guess.
    """
    uid = current_user_id()
    data = request.json or {}
    sig = (data.get("signature") or "").strip()
    group = data.get("group")
    if not sig:
        return jsonify({"error": "signature is required"}), 400
    if group is not None and group not in GROUPS:
        return jsonify({"error": f"group must be one of {', '.join(GROUPS)}"}), 400

    with db_conn() as conn:
        if group is None:
            conn.execute(
                "DELETE FROM recurring_overrides "
                "WHERE user_id = %s AND signature = %s",
                (uid, sig),
            )
        else:
            conn.execute(
                "INSERT INTO recurring_overrides (user_id, signature, group_name) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id, signature) DO UPDATE SET group_name = %s",
                (uid, sig, group, group),
            )
    bump_data_version()
    return jsonify({"signature": sig, "group": group})


@bp.route("/api/recurring/history")
def recurring_history():
    """What the subscriptions actually cost, month by month. ``?months=12``.

    Not a projection of the current list backwards: each month is the sum of the
    real transactions behind the series in the subscription group, so a service
    that ended shows in the months it charged and then stops, and one that put
    its price up steps up on the month it did. That is the only way the number
    can answer "is this creeping?" rather than restating today's total twelve
    times.

    Stopped series are included for the same reason — they are what the money
    went on at the time. Only the group is filtered.
    """
    uid = current_user_id()
    try:
        months = max(1, min(36, int(request.args.get("months", 12))))
    except (TypeError, ValueError):
        months = 12

    today = date.today()
    first = date(today.year, today.month, 1)
    # Walk back `months - 1` whole months from the current one.
    y, m = first.year, first.month
    for _ in range(months - 1):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    start = date(y, m, 1)

    with db_conn() as conn:
        result = detect_recurring(conn, uid)
        # Every original store string of every subscription series, including
        # the merged variants — matching on the display name alone would drop
        # the "GOOGLE *YouTubePremium" half of a series merging exists to join.
        stores = {s.strip().lower()
                  for i in result["items"] if i["group"] == GROUP_SUBSCRIPTION
                  for s in (i.get("stores") or [i["store"]])}
        buckets = {}
        if stores:
            rows = conn.execute(
                "SELECT substr(date, 1, 7) AS month, store, amount "
                "FROM transactions "
                "WHERE user_id = %s AND type = 'expense' AND date >= %s",
                (uid, start.isoformat()),
            ).fetchall()
            for r in rows:
                if (r["store"] or "").strip().lower() in stores:
                    buckets[r["month"]] = buckets.get(r["month"], 0.0) + abs(float(r["amount"]))

    out, y, m = [], start.year, start.month
    for _ in range(months):
        key = f"{y:04d}-{m:02d}"
        out.append({"month": key, "total": round(buckets.get(key, 0.0), 2)})
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return jsonify({"months": out})


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
