"""Spending categories: list, create, rename, delete (with reassignment)."""

from flask import Blueprint, request, jsonify
from data import db
from data.schema import db_conn
from core import current_user_id, bump_data_version

bp = Blueprint("categories", __name__)


@bp.route("/api/categories")
def get_categories():
    """Categories with usage info (transaction count + last used date)."""
    uid = current_user_id()
    cat_type = request.args.get("type")
    base = """
        SELECT c.*, COUNT(t.id) AS tx_count, MAX(t.date) AS last_used
        FROM categories c
        LEFT JOIN transactions t ON t.category_id = c.id AND t.user_id = c.user_id
        WHERE c.user_id = %s {extra}
        GROUP BY c.id
        ORDER BY {order}
    """
    with db_conn() as conn:
        if cat_type:
            rows = conn.execute(base.format(extra="AND c.type = %s", order="c.name"),
                                (uid, cat_type)).fetchall()
        else:
            rows = conn.execute(base.format(extra="", order="c.type, c.name"),
                                (uid,)).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route("/api/categories", methods=["POST"])
def create_category():
    uid = current_user_id()
    data = request.json
    with db_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO categories (user_id, name, type, is_default, color) "
            "VALUES (%s, %s, %s, 0, %s) RETURNING id",
            (uid, data["name"], data["type"], data.get("color")),
        )
        new_id = cursor.fetchone()["id"]
        cat = conn.execute(
            "SELECT * FROM categories WHERE id = %s AND user_id = %s",
            (new_id, uid),
        ).fetchone()
    return jsonify(dict(cat)), 201


@bp.route("/api/categories/<int:cat_id>", methods=["PUT"])
def update_category(cat_id):
    uid = current_user_id()
    data = request.json
    # Partial update: only fields present in the body change. "color": null
    # clears a stored color (falls back to the palette-derived one).
    sets, params = [], []
    if "name" in data:
        sets.append("name = %s"); params.append(data["name"])
    if "color" in data:
        sets.append("color = %s"); params.append(data["color"])
    if not sets:
        return jsonify({"error": "Nothing to update"}), 400
    with db_conn() as conn:
        conn.execute(
            f"UPDATE categories SET {', '.join(sets)}, updated_at = now() "
            "WHERE id = %s AND user_id = %s",
            (*params, cat_id, uid),
        )
        cat = conn.execute(
            "SELECT * FROM categories WHERE id = %s AND user_id = %s",
            (cat_id, uid),
        ).fetchone()
    if not cat:
        return jsonify({"error": "Category not found"}), 404
    return jsonify(dict(cat))


@bp.route("/api/categories/<int:cat_id>", methods=["DELETE"])
def delete_category(cat_id):
    uid = current_user_id()
    reassign_to = request.args.get("reassign_to", type=int)
    try:
        with db_conn() as conn:
            if reassign_to:
                # The reassignment target must also belong to this user.
                target = conn.execute(
                    "SELECT 1 FROM categories WHERE id = %s AND user_id = %s",
                    (reassign_to, uid),
                ).fetchone()
                if not target:
                    return jsonify({"error": "Reassignment target not found"}), 400
                conn.execute(
                    "UPDATE transactions SET category_id = %s "
                    "WHERE category_id = %s AND user_id = %s",
                    (reassign_to, cat_id, uid),
                )
                conn.execute(
                    "UPDATE import_staging SET final_category_id = %s "
                    "WHERE final_category_id = %s AND user_id = %s",
                    (reassign_to, cat_id, uid),
                )
            conn.execute(
                "DELETE FROM merchant_rules WHERE category_id = %s AND user_id = %s",
                (cat_id, uid),
            )
            cursor = conn.execute(
                "DELETE FROM categories WHERE id = %s AND user_id = %s",
                (cat_id, uid),
            )
            if cursor.rowcount == 0:
                return jsonify({"error": "Category not found"}), 404
            bump_data_version()
    except db.IntegrityError:
        # Category is still referenced by transactions/staging and no valid
        # reassignment target was supplied (or it left dangling references).
        return jsonify({
            "error": "Category is still in use. Reassign its transactions to "
                     "another category before deleting it."
        }), 409
    return "", 204
