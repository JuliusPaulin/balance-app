"""Merchant rules — the store-name patterns that auto-assign a category."""

import difflib
from flask import Blueprint, request, jsonify
from database import db_conn
from core import current_user_id, bump_data_version

bp = Blueprint("merchant_rules", __name__)


@bp.route("/api/merchant-rules")
def get_merchant_rules():
    uid = current_user_id()
    with db_conn() as conn:
        rows = conn.execute("""
            SELECT mr.*, c.name as category_name
            FROM merchant_rules mr
            JOIN categories c ON mr.category_id = c.id
            WHERE mr.user_id = %s
            ORDER BY mr.pattern
        """, (uid,)).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route("/api/merchant-rules", methods=["POST"])
def create_merchant_rule():
    uid = current_user_id()
    data = request.json
    with db_conn() as conn:
        # The category the rule points at must belong to this user.
        cat = conn.execute(
            "SELECT 1 FROM categories WHERE id = %s AND user_id = %s",
            (data["category_id"], uid),
        ).fetchone()
        if not cat:
            return jsonify({"error": "Category not found"}), 400
        cursor = conn.execute(
            "INSERT INTO merchant_rules (user_id, pattern, category_id, match_type) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (uid, data["pattern"], data["category_id"], data.get("match_type", "exact")),
        )
        new_id = cursor.fetchone()["id"]
        row = conn.execute("""
            SELECT mr.*, c.name as category_name FROM merchant_rules mr
            JOIN categories c ON mr.category_id = c.id
            WHERE mr.id = %s AND mr.user_id = %s
        """, (new_id, uid)).fetchone()
    return jsonify(dict(row)), 201


@bp.route("/api/merchant-rules/<int:rule_id>", methods=["PUT"])
def update_merchant_rule(rule_id):
    uid = current_user_id()
    data = request.json
    with db_conn() as conn:
        cat = conn.execute(
            "SELECT 1 FROM categories WHERE id = %s AND user_id = %s",
            (data["category_id"], uid),
        ).fetchone()
        if not cat:
            return jsonify({"error": "Category not found"}), 400
        conn.execute(
            "UPDATE merchant_rules SET pattern = %s, category_id = %s, match_type = %s "
            "WHERE id = %s AND user_id = %s",
            (data["pattern"], data["category_id"], data.get("match_type", "exact"),
             rule_id, uid),
        )
        row = conn.execute("""
            SELECT mr.*, c.name as category_name FROM merchant_rules mr
            JOIN categories c ON mr.category_id = c.id
            WHERE mr.id = %s AND mr.user_id = %s
        """, (rule_id, uid)).fetchone()
    if not row:
        return jsonify({"error": "Rule not found"}), 404
    return jsonify(dict(row))


@bp.route("/api/merchant-rules/<int:rule_id>", methods=["DELETE"])
def delete_merchant_rule(rule_id):
    uid = current_user_id()
    with db_conn() as conn:
        conn.execute(
            "DELETE FROM merchant_rules WHERE id = %s AND user_id = %s",
            (rule_id, uid),
        )
    return "", 204


def _matches_rule(store: str, pattern: str, match_type: str) -> bool:
    if not store or not pattern:
        return False
    s = store.strip().lower()
    p = pattern.strip().lower()
    if match_type == "exact":
        return s == p
    if match_type == "contains":
        return p in s
    if match_type == "smart":
        return difflib.SequenceMatcher(None, s, p).ratio() >= 0.72
    return False


@bp.route("/api/merchant-rules/preview")
def preview_merchant_rule():
    """Return transactions that would match a candidate rule. Used live in the modal."""
    uid = current_user_id()
    pattern = (request.args.get("pattern") or "").strip()
    match_type = request.args.get("match_type", "exact")
    limit = request.args.get("limit", default=20, type=int)
    if not pattern:
        return jsonify({"matches": [], "match_count": 0, "distinct_stores": 0})

    with db_conn() as conn:
        rows = conn.execute("""
            SELECT t.id, t.date, t.store, t.amount, t.type, c.name as category_name
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.store != ''
            ORDER BY t.date DESC
        """, (uid,)).fetchall()

    matches = [dict(r) for r in rows if _matches_rule(r["store"], pattern, match_type)]
    distinct = len({m["store"].strip().lower() for m in matches})
    return jsonify({
        "match_count": len(matches),
        "distinct_stores": distinct,
        "matches": matches[:limit],
    })


@bp.route("/api/merchant-rules/<int:rule_id>/apply", methods=["POST"])
def apply_rule_to_history(rule_id):
    """Re-categorize all historical transactions whose store matches this rule."""
    uid = current_user_id()
    with db_conn() as conn:
        rule = conn.execute("""
            SELECT mr.*, c.name as category_name, c.type as category_type
            FROM merchant_rules mr
            JOIN categories c ON mr.category_id = c.id
            WHERE mr.id = %s AND mr.user_id = %s
        """, (rule_id, uid)).fetchone()
        if not rule:
            return jsonify({"error": "Rule not found"}), 404

        rows = conn.execute("""
            SELECT id, store, category_id, type FROM transactions
            WHERE user_id = %s AND store != ''
        """, (uid,)).fetchall()

        updated = 0
        for r in rows:
            if r["category_id"] == rule["category_id"]:
                continue
            if rule["category_type"] != r["type"]:
                # Skip cross-type updates — don't reassign income tx to expense category, etc.
                continue
            if _matches_rule(r["store"], rule["pattern"], rule["match_type"]):
                conn.execute(
                    "UPDATE transactions SET category_id = %s, updated_at = now() "
                    "WHERE id = %s AND user_id = %s",
                    (rule["category_id"], r["id"], uid),
                )
                updated += 1

    bump_data_version()
    return jsonify({"updated": updated, "rule_id": rule_id})


@bp.route("/api/merchant-rules/stats")
def merchant_rule_stats():
    """Per-rule hit counts and last-match date — basis for dead-rule report."""
    uid = current_user_id()
    with db_conn() as conn:
        rules = conn.execute("""
            SELECT mr.*, c.name as category_name, c.type as category_type
            FROM merchant_rules mr
            JOIN categories c ON mr.category_id = c.id
            WHERE mr.user_id = %s
        """, (uid,)).fetchall()

        rows = conn.execute("""
            SELECT id, store, date, type FROM transactions
            WHERE user_id = %s AND store != ''
        """, (uid,)).fetchall()

    stats = []
    for rule in rules:
        hit_count = 0
        last_match = None
        for r in rows:
            if rule["category_type"] != r["type"]:
                continue
            if _matches_rule(r["store"], rule["pattern"], rule["match_type"]):
                hit_count += 1
                if last_match is None or r["date"] > last_match:
                    last_match = r["date"]
        stats.append({
            "rule_id": rule["id"],
            "hit_count": hit_count,
            "last_match": last_match,
        })

    return jsonify(stats)


def _rebuild_merchant_rules(conn, uid):
    """Rebuild all merchant rules from transaction history.

    Shared by the manual Start Training endpoint and the auto-retrain that runs
    after an import is confirmed. Returns (inserted, total_stores).
    """
    NOISE_STORES = {"Other", "Rent", "Missing info", "Monthly fee", "Korko", ""}
    CONFIDENCE_THRESHOLD = 70.0

    cur = conn.cursor()

    # Compute dominant category per store
    cur.execute("""
        SELECT
            t.store,
            t.category_id,
            c.name as category_name,
            c.type as category_type,
            COUNT(*) as cnt,
            total_counts.total,
            ROUND(100.0 * COUNT(*) / total_counts.total, 1) as confidence
        FROM transactions t
        JOIN categories c ON t.category_id = c.id
        JOIN (
            SELECT store, COUNT(*) as total
            FROM transactions
            WHERE user_id = %s AND store != '' AND store IS NOT NULL
            GROUP BY store
        ) total_counts ON t.store = total_counts.store
        WHERE t.user_id = %s AND t.store != '' AND t.store IS NOT NULL
        GROUP BY t.store, t.category_id, c.name, c.type, total_counts.total
        ORDER BY t.store, cnt DESC
    """, (uid, uid))
    store_data = {}
    for r in cur.fetchall():
        store = r["store"]
        if store not in store_data:
            store_data[store] = {
                "category_id": r["category_id"],
                "category_name": r["category_name"],
                "confidence": r["confidence"],
                "total": r["total"],
            }

    eligible = {
        s: d for s, d in store_data.items()
        if s not in NOISE_STORES and d["confidence"] >= CONFIDENCE_THRESHOLD
    }

    # Find contains-pattern groups
    stores_by_len = sorted(eligible.keys(), key=len)
    used = set()
    all_rules = []

    for base in stores_by_len:
        if base in used or len(base) < 4:
            continue
        base_lower = base.lower()
        base_cat = eligible[base]["category_id"]
        group = [base]
        for other in stores_by_len:
            if other == base or other in used:
                continue
            if base_lower in other.lower() and eligible[other]["category_id"] == base_cat:
                group.append(other)
        if len(group) > 1:
            all_rules.append({
                "pattern": base,
                "category_id": base_cat,
                "match_type": "contains",
            })
            used.update(group)

    # Exact rules for remaining stores
    for store, d in store_data.items():
        if store in used or store in NOISE_STORES or d["confidence"] < CONFIDENCE_THRESHOLD:
            continue
        all_rules.append({
            "pattern": store,
            "category_id": d["category_id"],
            "match_type": "exact",
        })

    cur.execute("DELETE FROM merchant_rules WHERE user_id = %s", (uid,))
    inserted = 0
    for rule in all_rules:
        cur.execute(
            "INSERT INTO merchant_rules (user_id, pattern, category_id, match_type) "
            "VALUES (%s, %s, %s, %s)",
            (uid, rule["pattern"], rule["category_id"], rule["match_type"])
        )
        inserted += 1

    return inserted, len(store_data)


@bp.route("/api/merchant-rules/train", methods=["POST"])
def train_merchant_rules():
    """Rebuild all merchant rules from transaction history (manual trigger)."""
    uid = current_user_id()
    with db_conn() as conn:
        inserted, total_stores = _rebuild_merchant_rules(conn, uid)
    return jsonify({"inserted": inserted, "total_stores": total_stores})
