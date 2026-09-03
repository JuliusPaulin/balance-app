"""Net worth tracking — carry-forward over manually entered account balances.

See docs/plans/NET_WORTH_PLAN.md. Net worth at a date =
  sum(latest asset balance with as_of <= date)
  - sum(latest liability balance with as_of <= date).
Balances carry forward: the most recent snapshot per account is used until a
newer one exists. Fully manual, no aggregation.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date


def _month_end(year: int, month: int, today: date) -> date:
    """Last day of the given month, clamped to ``today`` for the current month."""
    last = date(year, month, monthrange(year, month)[1])
    if (year, month) == (today.year, today.month):
        return min(last, today)
    return last


def _totals_as_of(conn, user_id, as_of: str):
    """(assets, liabilities) summing each account's latest balance with ``as_of``
    on or before the given date, scoped to ``user_id``.

    Closed accounts are deliberately NOT filtered out. Closing an account writes
    a zero balance at the closing date, so carry-forward drops it from every
    later total on its own, while every earlier total still counts what you
    actually held. Filtering on ``is_archived`` here would rewrite history —
    selling a fund in July would erase it from last January's net worth too.
    """
    rows = conn.execute(
        """
        SELECT a.type AS type, ab.balance AS balance
        FROM accounts a
        JOIN account_balances ab ON ab.account_id = a.id
        WHERE a.user_id = %s
          AND ab.as_of = (
              SELECT MAX(as_of) FROM account_balances
              WHERE account_id = a.id AND as_of <= %s
          )
        """,
        (user_id, as_of),
    ).fetchall()
    assets = sum(r["balance"] for r in rows if r["type"] == "asset")
    liabilities = sum(r["balance"] for r in rows if r["type"] == "liability")
    return round(assets, 2), round(liabilities, 2)


def _month_sequence(today: date, months: int):
    """List of (year, month) for the last ``months`` months, oldest first."""
    y, m = today.year, today.month
    seq = []
    for _ in range(max(1, months)):
        seq.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    seq.reverse()
    return seq


def compute_history(conn, user_id, months: int = 12, today: date | None = None):
    """Monthly net-worth time series (carry-forward) for the last ``months``,
    scoped to ``user_id``.

    The series starts at the first month that has any recorded balance —
    months before the first data point would all read 0 and draw a misleading
    flat line, so they are dropped (design change #17).
    """
    today = today or date.today()
    first_row = conn.execute(
        """
        SELECT MIN(ab.as_of) AS first_as_of
        FROM account_balances ab
        JOIN accounts a ON a.id = ab.account_id
        WHERE a.user_id = %s
        """,
        (user_id,),
    ).fetchone()
    first_month = (first_row["first_as_of"] or "")[:7]  # "" when no data yet

    series = []
    for year, month in _month_sequence(today, months):
        ym = f"{year:04d}-{month:02d}"
        if first_month and ym < first_month:
            continue
        as_of = _month_end(year, month, today).isoformat()
        assets, liabilities = _totals_as_of(conn, user_id, as_of)
        series.append({
            "month": ym,
            "as_of": as_of,
            "assets": assets,
            "liabilities": liabilities,
            "net_worth": round(assets - liabilities, 2),
        })
    return series


def summary(conn, user_id, today: date | None = None):
    """Current totals, change vs last month, and per-account latest balances,
    scoped to ``user_id``."""
    today = today or date.today()
    assets, liabilities = _totals_as_of(conn, user_id, today.isoformat())
    net = round(assets - liabilities, 2)

    hist = compute_history(conn, user_id, months=2, today=today)
    prev_net = hist[0]["net_worth"] if len(hist) >= 2 else net

    # Investment grouping + holdings drill-down: accounts can belong to a broker
    # group (``group_name``) and carry imported holdings.
    extra_cols = (
        "a.group_name AS group_name, "
        "(SELECT COUNT(*) FROM holdings h "
        "  WHERE h.account_id = a.id AND h.as_of = ("
        "      SELECT MAX(as_of) FROM holdings WHERE account_id = a.id"
        "  )) AS holdings_count,"
    )

    accounts = conn.execute(
        f"""
        SELECT a.id, a.name, a.type, a.sort_order, a.is_archived, {extra_cols}
               (SELECT balance FROM account_balances
                WHERE account_id = a.id ORDER BY as_of DESC LIMIT 1) AS latest_balance,
               (SELECT as_of FROM account_balances
                WHERE account_id = a.id ORDER BY as_of DESC LIMIT 1) AS latest_as_of
        FROM accounts a
        WHERE a.user_id = %s
        ORDER BY a.is_archived, a.type, a.sort_order, a.name
        """,
        (user_id,),
    ).fetchall()

    return {
        "net_worth": net,
        "assets": assets,
        "liabilities": liabilities,
        "change_vs_prev": round(net - prev_net, 2),
        "accounts": [dict(r) for r in accounts],
    }
