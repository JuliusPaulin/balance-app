"""What kind of recurring charge is this, and what may be totalled with what.

Detection was never the problem. The page was: every series that repeated went
into one list under one total, so on the real database the headline read
774 EUR/mo when the subscriptions came to 73 EUR. The rest was rent, a phone
bill and a takeaway habit standing under a heading that said Subscriptions.

These tests hold the line that split them, at both ends: `classify_group` and
the per-group totals in `recurring.py`, and the endpoints the page reads.
"""
from datetime import date, timedelta

import pytest

from recurring import (classify_group, detect_recurring, signature,
                       GROUP_BILL, GROUP_INCOME, GROUP_SPENDING,
                       GROUP_SUBSCRIPTION, GROUP_TRANSFER)

TODAY = date(2026, 5, 1)


def _series(conn, user_id, store, cat_id, amount, start, count, step_days=30,
            type_="expense"):
    d = start
    for k in range(count):
        amt = amount(k) if callable(amount) else amount
        conn.execute(
            "INSERT INTO transactions "
            "(user_id, date, store, category_id, amount, type) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, d.isoformat(), store, cat_id,
             -abs(amt) if type_ == "expense" else amt, type_),
        )
        d += timedelta(days=step_days)


def _by_store(result, store):
    return next((i for i in result["items"] if i["store"] == store), None)


# ── The classifier itself ───────────────────────────────────────────────
@pytest.mark.parametrize("category,type_,expected", [
    ("Entertainment", "expense", GROUP_SUBSCRIPTION),
    ("Exercise",      "expense", GROUP_SUBSCRIPTION),
    ("Medical",       "expense", GROUP_SUBSCRIPTION),
    ("Rent",          "expense", GROUP_BILL),
    ("Condo fees",    "expense", GROUP_BILL),
    ("Phone bill",    "expense", GROUP_BILL),
    ("Insurance",     "expense", GROUP_BILL),
    ("Groceries",     "expense", GROUP_SPENDING),
    ("Restaurant",    "expense", GROUP_SPENDING),
    ("Investments",   "expense", GROUP_TRANSFER),
    ("Debt",          "expense", GROUP_TRANSFER),
    ("Job",           "income",  GROUP_INCOME),
    # Income wins over everything: an "Investments" dividend is still not a
    # subscription, and neither is anything else arriving rather than leaving.
    ("Investments",   "income",  GROUP_INCOME),
])
def test_classify_group(category, type_, expected):
    assert classify_group(category, type_) == expected


def test_unknown_category_lands_in_subscriptions():
    """The permissive default, on purpose.

    Categories are the user's to rename and invent. One nobody has taught the
    classifier shows up in the headline group, where it is visible and can be
    moved, rather than being filed somewhere nobody looks. Hiding is the more
    expensive mistake.
    """
    assert classify_group("Sauna club", "expense") == GROUP_SUBSCRIPTION
    assert classify_group(None, "expense") == GROUP_SUBSCRIPTION


# ── The totals ──────────────────────────────────────────────────────────
def test_rent_is_not_a_subscription(user_conn, make_category):
    """The bug this whole split exists for.

    Rent and a streaming service both repeat monthly and both cleared every gate
    the detector has. Adding them together produced a number that was true of
    nothing anybody wanted to know.
    """
    conn, uid = user_conn
    rent = make_category(conn, uid, "Rent")
    ent = make_category(conn, uid, "Entertainment")
    _series(conn, uid, "Vuokra Oy", rent, 1250.0, date(2025, 6, 1), 11)
    _series(conn, uid, "Netflix", ent, 15.99, date(2025, 6, 5), 11)

    res = detect_recurring(conn, uid, today=TODAY)
    assert _by_store(res, "Vuokra Oy")["group"] == GROUP_BILL
    assert _by_store(res, "Netflix")["group"] == GROUP_SUBSCRIPTION

    s = res["summary"]
    # The headline counts the subscription and nothing else.
    assert s["monthly_total"] == pytest.approx(16.2, abs=1.0)
    assert s["active_count"] == 1
    # The rent is not lost — it has its own subtotal, on its own heading.
    assert s["groups"][GROUP_BILL]["monthly_total"] == pytest.approx(1266, abs=10)
    assert s["groups"][GROUP_BILL]["active_count"] == 1


def test_every_group_totals_separately(user_conn, make_category):
    conn, uid = user_conn
    cats = {n: make_category(conn, uid, n, t) for n, t in [
        ("Entertainment", "expense"), ("Rent", "expense"),
        ("Groceries", "expense"), ("Investments", "expense"), ("Job", "income")]}
    _series(conn, uid, "Netflix", cats["Entertainment"], 16.0, date(2025, 6, 1), 11)
    _series(conn, uid, "Vuokra Oy", cats["Rent"], 1250.0, date(2025, 6, 2), 11)
    _series(conn, uid, "K-Market", cats["Groceries"], 60.0, date(2025, 6, 3), 11)
    _series(conn, uid, "Nordnet", cats["Investments"], 500.0, date(2025, 6, 4), 11)
    _series(conn, uid, "Employer Oy", cats["Job"], 3200.0, date(2025, 6, 5), 11,
            type_="income")

    g = detect_recurring(conn, uid, today=TODAY)["summary"]["groups"]
    assert g[GROUP_SUBSCRIPTION]["monthly_total"] == pytest.approx(16, abs=2)
    assert g[GROUP_BILL]["monthly_total"] == pytest.approx(1266, abs=10)
    assert g[GROUP_SPENDING]["monthly_total"] == pytest.approx(61, abs=3)
    assert g[GROUP_TRANSFER]["monthly_total"] == pytest.approx(507, abs=10)
    # Income is never costed. Totalling a salary would be the same mistake one
    # level up from the one this file is about.
    assert g[GROUP_INCOME]["monthly_total"] == 0
    assert g[GROUP_INCOME]["count"] == 1


def test_stopped_series_stay_out_of_their_group_total(user_conn, make_category):
    conn, uid = user_conn
    ent = make_category(conn, uid, "Entertainment")
    _series(conn, uid, "Netflix", ent, 16.0, date(2025, 6, 1), 11)
    _series(conn, uid, "DeadGym", ent, 200.0, date(2024, 9, 1), 6)

    res = detect_recurring(conn, uid, today=TODAY)
    assert _by_store(res, "DeadGym")["status"] == "stopped"
    s = res["summary"]
    assert s["monthly_total"] < 50          # the gym is not part of what is paid
    assert s["ended_count"] == 1
    assert s["groups"][GROUP_SUBSCRIPTION]["count"] == 2       # still listed
    assert s["groups"][GROUP_SUBSCRIPTION]["active_count"] == 1  # not costed


# ── The price change, with its prices ───────────────────────────────────
def test_price_change_carries_both_amounts(user_conn, make_category):
    """A badge saying a price moved, without saying to what, is half a fact."""
    conn, uid = user_conn
    ent = make_category(conn, uid, "Entertainment")
    _series(conn, uid, "CloudStore", ent, lambda k: 10 if k < 7 else 13,
            date(2025, 9, 1), 8)
    item = _by_store(detect_recurring(conn, uid, today=TODAY), "CloudStore")
    assert item["status"] == "price_changed"
    assert item["prev_amount"] == pytest.approx(10.0)
    assert item["last_amount"] == pytest.approx(13.0)
    assert item["price_change_pct"] == pytest.approx(30.0, abs=0.5)
    assert item["price_changed_on"] is not None


def test_steady_series_carries_no_price_move(user_conn, make_category):
    conn, uid = user_conn
    ent = make_category(conn, uid, "Entertainment")
    _series(conn, uid, "Netflix", ent, 16.0, date(2025, 6, 1), 11)
    item = _by_store(detect_recurring(conn, uid, today=TODAY), "Netflix")
    assert item["prev_amount"] is None
    assert item["price_change_pct"] is None


# ── The override ────────────────────────────────────────────────────────
def test_override_moves_a_series_and_its_cost(user_conn, make_category):
    """The escape hatch. Category grouping is a guess, and this is the last word."""
    conn, uid = user_conn
    ex = make_category(conn, uid, "Exercise")
    _series(conn, uid, "Kuntokeskus", ex, 34.0, date(2025, 6, 1), 11)

    res = detect_recurring(conn, uid, today=TODAY)
    item = _by_store(res, "Kuntokeskus")
    assert item["group"] == GROUP_SUBSCRIPTION
    assert item["moved"] is False
    sig = item["signature"]

    moved = detect_recurring(conn, uid, today=TODAY,
                             overrides={sig: GROUP_BILL})
    item = _by_store(moved, "Kuntokeskus")
    assert item["group"] == GROUP_BILL
    assert item["moved"] is True
    # The cost moves with it, in both directions.
    assert moved["summary"]["monthly_total"] == 0
    assert moved["summary"]["groups"][GROUP_BILL]["monthly_total"] > 30


def test_override_is_read_from_the_table(user_conn, make_category):
    conn, uid = user_conn
    ex = make_category(conn, uid, "Exercise")
    _series(conn, uid, "Kuntokeskus", ex, 34.0, date(2025, 6, 1), 11)
    sig = signature("Kuntokeskus", "monthly")
    conn.execute(
        "INSERT INTO recurring_overrides (user_id, signature, group_name) "
        "VALUES (%s, %s, %s)", (uid, sig, GROUP_SPENDING))
    item = _by_store(detect_recurring(conn, uid, today=TODAY), "Kuntokeskus")
    assert item["group"] == GROUP_SPENDING


def test_a_nonsense_override_is_ignored(user_conn, make_category):
    """A group name the code does not know must not blank the row's group."""
    conn, uid = user_conn
    ex = make_category(conn, uid, "Exercise")
    _series(conn, uid, "Kuntokeskus", ex, 34.0, date(2025, 6, 1), 11)
    conn.execute(
        "INSERT INTO recurring_overrides (user_id, signature, group_name) "
        "VALUES (%s, %s, %s)",
        (uid, signature("Kuntokeskus", "monthly"), "nonsense"))
    item = _by_store(detect_recurring(conn, uid, today=TODAY), "Kuntokeskus")
    assert item["group"] == GROUP_SUBSCRIPTION


# ── The merged store names the history endpoint sums by ─────────────────
def test_merged_series_reports_every_store_string(user_conn, make_category):
    """Matching on the display name alone would drop half a merged series.

    Merging exists because one service arrives under several store strings. The
    history sums the real transactions behind a series, so it needs all of them.
    """
    conn, uid = user_conn
    ent = make_category(conn, uid, "Entertainment")
    start = date(2025, 6, 5)
    for k in range(12):
        variant = ["GOOGLE *YouTubePremium", "Google YouTubePremium"][k % 2]
        conn.execute(
            "INSERT INTO transactions "
            "(user_id, date, store, category_id, amount, type) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (uid, (start + timedelta(days=30 * k)).isoformat(), variant,
             ent, -11.99, "expense"))
    yt = [i for i in detect_recurring(conn, uid, today=TODAY)["items"]
          if "youtube" in i["store"].lower()]
    assert len(yt) == 1
    assert set(yt[0]["stores"]) == {"GOOGLE *YouTubePremium",
                                    "Google YouTubePremium"}
