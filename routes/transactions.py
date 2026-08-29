"""Transactions: the filtered list, and create / update / delete."""

from flask import Blueprint, request, jsonify
from database import db_conn
from core import current_user_id, bump_data_version

bp = Blueprint("transactions", __name__)


@bp.route("/api/transactions")
def get_transactions():
    uid       = current_user_id()
    page      = request.args.get("page", 1, type=int)
    per_page  = request.args.get("per_page", 50, type=int)
    t_type    = request.args.get("type")
    month     = request.args.get("month")        # YYYY-MM
    q         = request.args.get("q", "").strip()
    date_from = request.args.get("date_from")    # YYYY-MM-DD
    date_to   = request.args.get("date_to")      # YYYY-MM-DD
    cat_ids   = request.args.get("category_ids") # comma-separated
    amt_min   = request.args.get("amount_min", type=float)
    amt_max   = request.args.get("amount_max", type=float)
    sort_col  = request.args.get("sort", "date")
    sort_dir  = request.args.get("dir", "desc")

    # Dates are stored as ISO YYYY-MM-DD strings, which sort chronologically as
    # plain strings on both engines. (CAST(t.date AS date) is NOT safe here:
    # SQLite has no date type, so the cast collapses every value to the integer
    # year and the id tiebreak ends up deciding the order.)
    SORT_COLS = {
        "date": "t.date",
        "amount": "t.amount",
        "store": "t.store",
        "category": "c.name",
    }
    dir_sql = "ASC" if sort_dir == "asc" else "DESC"
    order = f"{SORT_COLS.get(sort_col, 'CAST(t.date AS date)')} {dir_sql}, t.id {dir_sql}"

    # The driving table (transactions) is always scoped to the requesting user.
    conditions, params = ["t.user_id = %s"], [uid]

    if t_type:
        conditions.append("t.type = %s"); params.append(t_type)
    if month:
        conditions.append("substr(t.date, 1, 7) = %s"); params.append(month)
    if q:
        # Postgres LIKE is case-sensitive (SQLite's was not) — use ILIKE to
        # preserve the original case-insensitive search behaviour.
        conditions.append("(t.store ILIKE %s OR c.name ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    if date_from:
        conditions.append("t.date >= %s"); params.append(date_from)
    if date_to:
        conditions.append("t.date <= %s"); params.append(date_to)
    if cat_ids:
        ids = [x.strip() for x in cat_ids.split(",") if x.strip()]
        conditions.append(f"t.category_id IN ({','.join(['%s']*len(ids))})"); params += ids
    months_filter = request.args.get("months")
    if months_filter:
        ml = [m.strip() for m in months_filter.split(",") if m.strip()]
        if ml:
            conditions.append(f"substr(t.date, 1, 7) IN ({','.join(['%s']*len(ml))})"); params += ml
    if amt_min is not None:
        conditions.append("t.amount >= %s"); params.append(amt_min)
    if amt_max is not None:
        conditions.append("t.amount <= %s"); params.append(amt_max)

    where = " WHERE " + " AND ".join(conditions)
    base  = "FROM transactions t JOIN categories c ON t.category_id = c.id" + where

    with db_conn() as conn:
        # One aggregate pass gives the count AND the filtered money totals, so
        # the list header can answer "how much?" for the current filter.
        agg = conn.execute(
            f"""SELECT COUNT(*) AS n,
                       COALESCE(SUM(CASE WHEN t.type = 'expense' THEN t.amount END), 0) AS sum_expense,
                       COALESCE(SUM(CASE WHEN t.type = 'income'  THEN t.amount END), 0) AS sum_income
                {base}""", params).fetchone()
        rows = conn.execute(
            f"SELECT t.*, c.name as category_name {base} ORDER BY {order} LIMIT %s OFFSET %s",
            params + [per_page, (page - 1) * per_page]
        ).fetchall()

    return jsonify({
        "items": [dict(r) for r in rows],
        "total": agg["n"],
        "sum_expense": float(agg["sum_expense"]),
        "sum_income": float(agg["sum_income"]),
        "page": page,
    })


@bp.route("/api/transactions", methods=["POST"])
def create_transaction():
    uid = current_user_id()
    data = request.json
    with db_conn() as conn:
        # The category must belong to this user.
        cat = conn.execute(
            "SELECT 1 FROM categories WHERE id = %s AND user_id = %s",
            (data["category_id"], uid),
        ).fetchone()
        if not cat:
            return jsonify({"error": "Category not found"}), 400
        cursor = conn.execute(
            """INSERT INTO transactions (user_id, date, store, category_id, amount, type)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (uid, data["date"], data.get("store", ""), data["category_id"],
             data["amount"], data["type"]),
        )
        new_id = cursor.fetchone()["id"]
        row = conn.execute(
            """SELECT t.*, c.name as category_name FROM transactions t
               JOIN categories c ON t.category_id = c.id
               WHERE t.id = %s AND t.user_id = %s""",
            (new_id, uid),
        ).fetchone()
    bump_data_version()
    return jsonify(dict(row)), 201


@bp.route("/api/transactions/<int:t_id>", methods=["PUT"])
def update_transaction(t_id):
    uid = current_user_id()
    data = request.json
    with db_conn() as conn:
        cat = conn.execute(
            "SELECT 1 FROM categories WHERE id = %s AND user_id = %s",
            (data["category_id"], uid),
        ).fetchone()
        if not cat:
            return jsonify({"error": "Category not found"}), 400
        cursor = conn.execute(
            """UPDATE transactions
               SET date = %s, store = %s, category_id = %s, amount = %s, type = %s,
                   updated_at = now()
               WHERE id = %s AND user_id = %s""",
            (data["date"], data.get("store", ""), data["category_id"],
             data["amount"], data["type"], t_id, uid),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "Transaction not found"}), 404
        row = conn.execute(
            """SELECT t.*, c.name as category_name FROM transactions t
               JOIN categories c ON t.category_id = c.id
               WHERE t.id = %s AND t.user_id = %s""",
            (t_id, uid),
        ).fetchone()
    bump_data_version()
    return jsonify(dict(row))


@bp.route("/api/transactions/<int:t_id>", methods=["DELETE"])
def delete_transaction(t_id):
    uid = current_user_id()
    with db_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM transactions WHERE id = %s AND user_id = %s",
            (t_id, uid),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "Transaction not found"}), 404
    bump_data_version()
    return "", 204
