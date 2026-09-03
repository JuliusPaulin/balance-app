"""Everything the Dashboard, Reports and Trends pages read."""

from datetime import datetime
from flask import Blueprint, request, jsonify
from data.schema import db_conn
from core import current_user_id

bp = Blueprint("dashboard", __name__)

# How many months behind the one on screen count as "normal", and how flat a
# category has to be before we stop reporting its movement at all.
_BASELINE_MONTHS = 6
_FIXED_SPREAD = 0.03
# Below this many months of history there is no honest baseline to draw.
_MIN_BASELINE_MONTHS = 3


def _month_add(month, delta):
    """"2026-05" plus a number of months, as "YYYY-MM"."""
    index = int(month[:4]) * 12 + int(month[5:7]) - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _attach_baseline(conn, uid, txn_type, month, items):
    """Put "normal" beside each category's total, so a number stops being a
    length and starts being a judgement.

    The baseline is the median of that category's monthly totals over the six
    months BEFORE the one on screen. The month being judged stays out of its
    own baseline, so "usual" means what it says. A median rather than a mean,
    because one holiday should not get to redefine normal.

    A month where a category saw nothing counts as **0**, not as missing:
    something you buy twice a year is unusual every time, and dropping the
    empty months from the median would report it as routine.

    Only single-month views get a baseline. A twelve-month sum has no monthly
    normal to sit beside, and inventing one would be a claim the data does
    not make — so `median` comes back None and the bars draw as they always
    have.
    """
    baseline_months = [_month_add(month, -d) for d in range(1, _BASELINE_MONTHS + 1)]
    rows = conn.execute("""
        SELECT c.name as name, substr(t.date, 1, 7) as m, SUM(t.amount) as total
        FROM transactions t
        JOIN categories c ON t.category_id = c.id
        WHERE t.user_id = %s AND t.type = %s
          AND substr(t.date, 1, 7) >= %s AND substr(t.date, 1, 7) <= %s
        GROUP BY c.id, c.name, m
    """, (uid, txn_type, baseline_months[-1], baseline_months[0])).fetchall()

    # Too little history to say what normal is. Saying nothing beats telling
    # someone their second recorded month is a 300% overspend.
    if len({r["m"] for r in rows}) < _MIN_BASELINE_MONTHS:
        for item in items:
            item["median"] = None
        return items

    by_category = {}
    for r in rows:
        by_category.setdefault(r["name"], {})[r["m"]] = r["total"]

    for item in items:
        history = by_category.get(item["name"], {})
        monthly = [history.get(m, 0.0) for m in baseline_months]
        median = _median(monthly)
        item["median"] = median
        # Rent does not move, and a category that never moves has no news in
        # it. Reporting "0% off normal" beside it every month teaches the eye
        # to skip the column that carries the actual news.
        #
        # "Fixed" has to mean this month too, not just the six behind it. A
        # category that has been flat for half a year and then jumps is the
        # loudest thing on the card, and an earlier version of this line
        # marked it fixed and said nothing.
        deviation = _median([abs(v - median) for v in monthly])
        flat = median > 0 and (deviation / median) <= _FIXED_SPREAD
        item["fixed"] = flat and abs(item["total"] - median) / median <= _FIXED_SPREAD
    return items



@bp.route("/api/dashboard/monthly-summary")
def monthly_summary():
    uid = current_user_id()
    with db_conn() as conn:
        rows = conn.execute("""
            SELECT substr(date, 1, 7) as month,
                   type,
                   SUM(amount) as total
            FROM transactions
            WHERE user_id = %s
            GROUP BY substr(date, 1, 7), type
            UNION ALL
            SELECT substr(t.date, 1, 7) as month,
                   'investment' as type,
                   SUM(t.amount) as total
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = 'expense' AND c.name = 'Investments'
            GROUP BY substr(t.date, 1, 7)
            ORDER BY month
        """, (uid, uid)).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route("/api/dashboard/top-expenses")
def top_expenses():
    uid = current_user_id()
    with db_conn() as conn:
        latest = conn.execute("""
            SELECT substr(date, 1, 7) as month
            FROM transactions WHERE user_id = %s AND type = 'expense'
            ORDER BY date DESC LIMIT 1
        """, (uid,)).fetchone()

        if not latest:
            return jsonify({"latest_month": None, "categories": [], "trends": []})

        latest_month = latest["month"]

        top_cats = conn.execute("""
            SELECT c.id, c.name, SUM(t.amount) as total
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = 'expense'
              AND substr(t.date, 1, 7) = %s
            GROUP BY c.id, c.name
            ORDER BY total DESC
            LIMIT 5
        """, (uid, latest_month)).fetchall()

        cat_ids = [r["id"] for r in top_cats]
        if not cat_ids:
            return jsonify({"latest_month": latest_month, "categories": [], "trends": []})

        placeholders = ",".join(["%s"] * len(cat_ids))
        trends = conn.execute(f"""
            SELECT substr(t.date, 1, 7) as month,
                   c.id as category_id, c.name as category_name,
                   SUM(t.amount) as total
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = 'expense' AND c.id IN ({placeholders})
            GROUP BY substr(t.date, 1, 7), c.id, c.name
            ORDER BY month
        """, [uid] + cat_ids).fetchall()

    return jsonify({
        "latest_month": latest_month,
        "categories": [dict(r) for r in top_cats],
        "trends": [dict(r) for r in trends],
    })


@bp.route("/api/dashboard/category-trends")
def category_trends():
    uid = current_user_id()
    cat_ids_str = request.args.get("ids", "")
    if not cat_ids_str:
        return jsonify({"categories": [], "trends": []})

    try:
        cat_ids = [int(x) for x in cat_ids_str.split(",") if x.strip()]
    except ValueError:
        return jsonify({"error": "category_ids must be integers"}), 400
    if not cat_ids:
        return jsonify({"categories": [], "trends": []})

    placeholders = ",".join(["%s"] * len(cat_ids))

    with db_conn() as conn:
        cats = conn.execute(
            f"SELECT id, name FROM categories "
            f"WHERE user_id = %s AND id IN ({placeholders})",
            [uid] + cat_ids,
        ).fetchall()

        trends = conn.execute(f"""
            SELECT substr(t.date, 1, 7) as month,
                   c.id as category_id, c.name as category_name,
                   SUM(t.amount) as total
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND c.id IN ({placeholders})
            GROUP BY substr(t.date, 1, 7), c.id, c.name
            ORDER BY month
        """, [uid] + cat_ids).fetchall()

    return jsonify({
        "categories": [dict(r) for r in cats],
        "trends": [dict(r) for r in trends],
    })


@bp.route("/api/dashboard/category-breakdown")
def category_breakdown():
    uid = current_user_id()
    month = request.args.get("month")
    year  = request.args.get("year")
    # "expense" (default) or "income" — the dashboard draws one card of each.
    txn_type = request.args.get("type", "expense")
    if txn_type not in ("expense", "income"):
        txn_type = "expense"

    with db_conn() as conn:
        months_param = request.args.get("months")  # comma-separated YYYY-MM
        if months_param:
            months_list = [m.strip() for m in months_param.split(",") if m.strip()]
            if months_list:
                placeholders = ",".join(["%s"] * len(months_list))
                rows = conn.execute(f"""
                    SELECT c.name, SUM(t.amount) as total
                    FROM transactions t
                    JOIN categories c ON t.category_id = c.id
                    WHERE t.user_id = %s AND t.type = %s
                      AND substr(t.date, 1, 7) IN ({placeholders})
                    GROUP BY c.id, c.name
                    ORDER BY total DESC
                """, [uid, txn_type] + months_list).fetchall()
                items = [dict(r) for r in rows]
                # "Latest month" scope arrives here as a one-element list, so
                # a single month gets its baseline whichever way it was asked
                # for. Several months are a sum, and a sum has no monthly
                # normal to stand beside.
                if len(months_list) == 1:
                    _attach_baseline(conn, uid, txn_type, months_list[0], items)
                return jsonify({"type": txn_type, "months": months_list, "items": items})

        if year:
            rows = conn.execute("""
                SELECT c.name, SUM(t.amount) as total
                FROM transactions t
                JOIN categories c ON t.category_id = c.id
                WHERE t.user_id = %s AND t.type = %s
                  AND substr(t.date, 1, 4) = %s
                GROUP BY c.id, c.name
                ORDER BY total DESC
            """, (uid, txn_type, year)).fetchall()
            return jsonify({"type": txn_type, "year": year, "items": [dict(r) for r in rows]})

        if not month:
            latest = conn.execute("""
                SELECT substr(date, 1, 7) as month
                FROM transactions WHERE user_id = %s AND type = %s
                ORDER BY date DESC LIMIT 1
            """, (uid, txn_type)).fetchone()
            month = latest["month"] if latest else None

        if not month:
            return jsonify({"type": txn_type, "month": None, "items": []})

        rows = conn.execute("""
            SELECT c.name, SUM(t.amount) as total
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = %s
              AND substr(t.date, 1, 7) = %s
            GROUP BY c.id, c.name
            ORDER BY total DESC
        """, (uid, txn_type, month)).fetchall()

        items = [dict(r) for r in rows]
        _attach_baseline(conn, uid, txn_type, month, items)

    return jsonify({"type": txn_type, "month": month, "items": items})


@bp.route("/api/dashboard/heatmap")
def dashboard_heatmap():
    """Daily expense totals for a year — drives the GitHub-style heatmap."""
    uid = current_user_id()
    year = request.args.get("year", type=int)

    with db_conn() as conn:
        if not year:
            latest = conn.execute(
                "SELECT substr(date, 1, 4) as year FROM transactions "
                "WHERE user_id = %s ORDER BY date DESC LIMIT 1",
                (uid,),
            ).fetchone()
            year = int(latest["year"]) if latest else datetime.now().year

        rows = conn.execute("""
            SELECT date, SUM(amount) as total
            FROM transactions
            WHERE user_id = %s AND type = 'expense' AND substr(date, 1, 4) = %s
            GROUP BY date
        """, (uid, str(year))).fetchall()

        available_years = conn.execute("""
            SELECT DISTINCT substr(date, 1, 4) as year
            FROM transactions WHERE user_id = %s ORDER BY year DESC
        """, (uid,)).fetchall()

    return jsonify({
        "year": year,
        "items": [dict(r) for r in rows],
        "available_years": [int(r["year"]) for r in available_years],
    })


# ── Annual Report API ──────────────────────────────────────────────────

@bp.route("/api/reports/annual")
def annual_report():
    uid = current_user_id()
    year = request.args.get("year", type=int) or datetime.now().year
    year_str = str(year)
    prev_year_str = str(year - 1)

    with db_conn() as conn:
        # Which calendar months this year actually has. Everything compared
        # against last year is held to the same months: seven months of a year
        # in progress measured against a full twelve reads as a collapse in
        # both income and spending, which is an artefact of the calendar and
        # not of anything the user did.
        compare_months = [r["mm"] for r in conn.execute("""
            SELECT DISTINCT substr(date, 6, 2) as mm
            FROM transactions WHERE user_id = %s AND substr(date, 1, 4) = %s
            ORDER BY mm
        """, (uid, year_str)).fetchall()]
        mm_ph = ",".join(["%s"] * len(compare_months))

        totals = conn.execute("""
            SELECT type, SUM(amount) as total
            FROM transactions WHERE user_id = %s AND substr(date, 1, 4) = %s
            GROUP BY type
        """, (uid, year_str)).fetchall()

        prev_totals = conn.execute(f"""
            SELECT type, SUM(amount) as total
            FROM transactions WHERE user_id = %s AND substr(date, 1, 4) = %s
              AND substr(date, 6, 2) IN ({mm_ph})
            GROUP BY type
        """, [uid, prev_year_str] + compare_months).fetchall() if compare_months else []

        categories_data = conn.execute("""
            SELECT c.name, SUM(t.amount) as total, COUNT(*) as count
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = 'expense'
              AND substr(t.date, 1, 4) = %s
            GROUP BY c.id, c.name
            ORDER BY total DESC
        """, (uid, year_str)).fetchall()

        income_categories = conn.execute("""
            SELECT c.name, SUM(t.amount) as total, COUNT(*) as count
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = 'income'
              AND substr(t.date, 1, 4) = %s
            GROUP BY c.id, c.name
            ORDER BY total DESC
        """, (uid, year_str)).fetchall()

        # Previous-year expense totals per category, for YoY deltas in the
        # category breakdown (design change #16). Same months as above.
        prev_categories = conn.execute(f"""
            SELECT c.name, SUM(t.amount) as total
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = 'expense'
              AND substr(t.date, 1, 4) = %s
              AND substr(t.date, 6, 2) IN ({mm_ph})
            GROUP BY c.id, c.name
        """, [uid, prev_year_str] + compare_months).fetchall() if compare_months else []

        top_transactions = conn.execute("""
            SELECT t.*, c.name as category_name
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = 'expense'
              AND substr(t.date, 1, 4) = %s
            ORDER BY t.amount DESC
            LIMIT 10
        """, (uid, year_str)).fetchall()

        top_income = conn.execute("""
            SELECT t.*, c.name as category_name
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s AND t.type = 'income'
              AND substr(t.date, 1, 4) = %s
            ORDER BY t.amount DESC
            LIMIT 10
        """, (uid, year_str)).fetchall()

        monthly = conn.execute("""
            SELECT substr(date, 1, 7) as month, type, SUM(amount) as total
            FROM transactions
            WHERE user_id = %s AND substr(date, 1, 4) = %s
            GROUP BY substr(date, 1, 7), type
            ORDER BY month
        """, (uid, year_str)).fetchall()

        prev_monthly = conn.execute("""
            SELECT substr(date, 1, 7) as month, type, SUM(amount) as total
            FROM transactions
            WHERE user_id = %s AND substr(date, 1, 4) = %s
            GROUP BY substr(date, 1, 7), type
            ORDER BY month
        """, (uid, prev_year_str)).fetchall()

        transaction_count = conn.execute("""
            SELECT type, COUNT(*) as count
            FROM transactions WHERE user_id = %s AND substr(date, 1, 4) = %s
            GROUP BY type
        """, (uid, year_str)).fetchall()

        available_years = conn.execute("""
            SELECT DISTINCT substr(date, 1, 4) as year
            FROM transactions WHERE user_id = %s ORDER BY year DESC
        """, (uid,)).fetchall()

    return jsonify({
        "year": year,
        "totals": {r["type"]: r["total"] for r in totals},
        "prev_totals": {r["type"]: r["total"] for r in prev_totals},
        # The months both sides of every year-on-year figure are limited to.
        "compare_months": compare_months,
        "categories": [dict(r) for r in categories_data],
        "prev_categories": {r["name"]: r["total"] for r in prev_categories},
        "income_categories": [dict(r) for r in income_categories],
        "top_transactions": [dict(r) for r in top_transactions],
        "top_income": [dict(r) for r in top_income],
        "monthly": [dict(r) for r in monthly],
        "prev_monthly": [dict(r) for r in prev_monthly],
        "transaction_count": {r["type"]: r["count"] for r in transaction_count},
        "available_years": [r["year"] for r in available_years],
    })


# ── Trends API ────────────────────────────────────────────────────────

@bp.route("/api/trends/category")
def trends_category():
    uid         = current_user_id()
    ids_raw     = request.args.get("category_ids") or request.args.get("category_id", "")
    try:
        cat_ids = [int(x) for x in ids_raw.split(",") if x.strip()]
    except ValueError:
        return jsonify({"error": "category_ids must be integers"}), 400
    months_back = request.args.get("months", type=int, default=12)

    if not cat_ids:
        return jsonify({"error": "category_ids required"}), 400

    ph   = ",".join(["%s"] * len(cat_ids))

    if months_back > 0:
        now   = datetime.now()
        month = now.month - months_back
        year  = now.year
        while month <= 0:
            month += 12
            year  -= 1
        from_ym     = f"{year:04d}-{month:02d}"
        date_cond   = "AND substr(date, 1, 7) >= %s"
        date_params = [from_ym]
    else:
        date_cond   = ""
        date_params = []

    with db_conn() as conn:
        cats = conn.execute(
            f"SELECT id, name, type FROM categories "
            f"WHERE user_id = %s AND id IN ({ph})",
            [uid] + cat_ids,
        ).fetchall()
        if not cats:
            return jsonify({"error": "category not found"}), 404

        if len(cats) == 1:
            category = dict(cats[0])
        else:
            names    = " + ".join(c["name"] for c in cats[:3])
            if len(cats) > 3: names += f" +{len(cats)-3}"
            cat_type = "income" if all(c["type"] == "income" for c in cats) else "expense"
            category = {"id": None, "name": names, "type": cat_type}

        # user_id first, then category ids, then optional date param.
        monthly_params = [uid] + cat_ids + date_params
        monthly = conn.execute(f"""
            SELECT substr(date, 1, 7) as month,
                   SUM(amount) as total,
                   COUNT(*) as count
            FROM transactions
            WHERE user_id = %s AND category_id IN ({ph}) {date_cond}
            GROUP BY substr(date, 1, 7)
            ORDER BY month
        """, monthly_params).fetchall()

        merchant_params = [uid] + cat_ids + date_params
        top_merchants = conn.execute(f"""
            SELECT COALESCE(NULLIF(store,''), '(no name)') as store,
                   SUM(amount) as total,
                   COUNT(*) as count
            FROM transactions
            WHERE user_id = %s AND category_id IN ({ph})
              AND store != '' {date_cond}
            GROUP BY store
            ORDER BY total DESC
            LIMIT 12
        """, merchant_params).fetchall()

        monthly_list = [dict(r) for r in monthly]

    # Fill gaps so every month in the range has an entry (total=0, count=0)
    now_dt     = datetime.now()
    current_ym = now_dt.strftime("%Y-%m")
    if monthly_list:
        start_ym = from_ym if months_back > 0 else monthly_list[0]["month"]
        monthly_dict = {r["month"]: r for r in monthly_list}
        filled = []
        fy, fm = int(start_ym[:4]), int(start_ym[5:7])
        while True:
            ym = f"{fy:04d}-{fm:02d}"
            if ym > current_ym:
                break
            filled.append(monthly_dict.get(ym, {"month": ym, "total": 0.0, "count": 0}))
            fm += 1
            if fm > 12:
                fm = 1
                fy += 1
        monthly_list = filled

    total    = sum(r["total"] for r in monthly_list)
    tx_count = sum(r["count"] for r in monthly_list)
    months_n = len(monthly_list)

    return jsonify({
        "category":      category,
        "monthly":       monthly_list,
        "top_merchants": [dict(r) for r in top_merchants],
        "stats": {
            "total":            total,
            "avg_monthly":      total / months_n  if months_n  else 0,
            "tx_count":         tx_count,
            "avg_per_tx":       total / tx_count  if tx_count  else 0,
            "months_with_data": sum(1 for r in monthly_list if r["count"] > 0),
        },
    })
