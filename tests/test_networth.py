"""Unit tests for networth.py — against the Postgres ``expense_test`` DB.

Uses the ``user_conn`` fixture (see conftest.py): each test gets a fresh user
and a live psycopg connection, and all of that user's data is cascade-deleted on
teardown. Every networth call is scoped by ``user_id``.

Run: python3 -m pytest test_networth.py
"""
from datetime import date

import networth

TODAY = date(2026, 5, 15)


def _acct(conn, user_id, name, type_, archived=0):
    return conn.execute(
        "INSERT INTO accounts (user_id, name, type, is_archived) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (user_id, name, type_, archived),
    ).fetchone()["id"]


def _bal(conn, user_id, acct_id, as_of, balance):
    conn.execute(
        "INSERT INTO account_balances (user_id, account_id, as_of, balance) "
        "VALUES (%s, %s, %s, %s)",
        (user_id, acct_id, as_of, balance),
    )


def test_carry_forward_and_summary(user_conn):
    conn, uid = user_conn
    savings = _acct(conn, uid, "Savings", "asset")
    invest = _acct(conn, uid, "Investments", "asset")
    loan = _acct(conn, uid, "Car loan", "liability")
    _bal(conn, uid, savings, "2026-03-15", 10000)
    _bal(conn, uid, savings, "2026-05-10", 12000)
    _bal(conn, uid, invest, "2026-04-01", 25000)
    _bal(conn, uid, loan, "2026-03-01", 15000)
    _bal(conn, uid, loan, "2026-05-01", 14000)

    s = networth.summary(conn, uid, today=TODAY)
    assert s["assets"] == 37000      # 12000 savings + 25000 invest
    assert s["liabilities"] == 14000
    assert s["net_worth"] == 23000

    hist = networth.compute_history(conn, uid, months=4, today=TODAY)
    by_month = {p["month"]: p for p in hist}
    # Months before the first recorded balance are dropped, not shown as 0
    # (see DESIGN_CHANGES.md #17) — the series starts at 2026-03.
    assert "2026-02" not in by_month
    assert by_month["2026-03"]["net_worth"] == -5000   # 10000 - 15000
    assert by_month["2026-04"]["net_worth"] == 20000   # (10000+25000) - 15000
    assert by_month["2026-05"]["net_worth"] == 23000   # (12000+25000) - 14000


def test_change_vs_prev(user_conn):
    conn, uid = user_conn
    a = _acct(conn, uid, "Cash", "asset")
    _bal(conn, uid, a, "2026-04-30", 1000)
    _bal(conn, uid, a, "2026-05-10", 1500)
    s = networth.summary(conn, uid, today=TODAY)
    # April month-end = 1000, current = 1500 -> +500
    assert s["change_vs_prev"] == 500


def test_closed_account_leaves_total_but_keeps_history(user_conn):
    """Closing writes a zero at the closing date. Totals come from balances, so
    the account drops out from that date on and every earlier month still counts
    it — closing must not rewrite the past."""
    conn, uid = user_conn
    a = _acct(conn, uid, "Active", "asset")
    z = _acct(conn, uid, "Sold fund", "asset")
    _bal(conn, uid, a, "2026-03-01", 500)
    _bal(conn, uid, z, "2026-03-01", 9999)

    # Sold in May: zero balance + archived, the way /api/accounts/<id>/close does it.
    _bal(conn, uid, z, "2026-05-01", 0)
    conn.execute("UPDATE accounts SET is_archived = 1 WHERE id = %s", (z,))

    s = networth.summary(conn, uid, today=TODAY)
    assert s["assets"] == 500

    hist = {p["month"]: p for p in networth.compute_history(conn, uid, months=4, today=TODAY)}
    assert hist["2026-03"]["assets"] == 10499   # still held it in March
    assert hist["2026-04"]["assets"] == 10499
    assert hist["2026-05"]["assets"] == 500     # gone from the month it was sold

    # Closed accounts stay listed so they can be reopened, flagged as archived.
    assert len(s["accounts"]) == 2
    assert [x["is_archived"] for x in s["accounts"]] == [0, 1]


def test_empty_db(user_conn):
    conn, uid = user_conn
    s = networth.summary(conn, uid, today=TODAY)
    assert s["net_worth"] == 0 and s["assets"] == 0 and s["liabilities"] == 0
    assert s["accounts"] == []
    hist = networth.compute_history(conn, uid, months=3, today=TODAY)
    assert all(p["net_worth"] == 0 for p in hist)


def test_latest_balance_reported(user_conn):
    conn, uid = user_conn
    a = _acct(conn, uid, "Savings", "asset")
    _bal(conn, uid, a, "2026-01-01", 100)
    _bal(conn, uid, a, "2026-05-01", 300)
    acc = networth.summary(conn, uid, today=TODAY)["accounts"][0]
    assert acc["latest_balance"] == 300
    assert acc["latest_as_of"] == "2026-05-01"
