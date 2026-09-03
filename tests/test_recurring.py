"""Unit tests for recurring.detect_recurring — against ``expense_test`` (Postgres).

Uses the ``user_conn`` fixture (see conftest.py): each test gets a fresh user
and a live psycopg connection; the user's data cascade-deletes on teardown.

Categories used to be referenced by hardcoded numeric ids (1..7). Identity PKs
mean ids can't be forced, so each test creates the categories it needs for its
user and maps NAME -> id via the ``_cats`` helper. The semantics are preserved:
``Investments`` and ``Debt`` drive the transfer-exclusion logic, ``Job`` is the
income category, and ``Entertainment`` is the common subscription category.

Run: python3 -m pytest test_recurring.py
"""
from datetime import date, timedelta

from services.recurring import detect_recurring, signature

TODAY = date(2026, 5, 1)

# Categories the tests reference, with their semantic type. Created per-user.
_CATEGORY_DEFS = [
    ("Telecom", "expense"),
    ("Utilities", "expense"),
    ("Entertainment", "expense"),
    ("Job", "income"),
    ("Groceries", "expense"),
    ("Investments", "expense"),
    ("Debt", "expense"),
]


def _cats(conn, user_id):
    """Create the test categories for ``user_id`` and return a name -> id map."""
    mapping = {}
    for name, type_ in _CATEGORY_DEFS:
        row = conn.execute(
            "INSERT INTO categories (user_id, name, type) "
            "VALUES (%s, %s, %s) RETURNING id",
            (user_id, name, type_),
        ).fetchone()
        mapping[name] = row["id"]
    return mapping


def _add_series(conn, user_id, store, cat_id, amount, start, count, step_days,
                type_="expense", jitter=None):
    d = start
    for k in range(count):
        amt = amount(k) if callable(amount) else amount
        offset = jitter[k] if jitter else 0
        day = d + timedelta(days=offset)
        conn.execute(
            "INSERT INTO transactions "
            "(user_id, date, store, category_id, amount, type) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, day.isoformat(), store, cat_id,
             -abs(amt) if type_ == "expense" else amt, type_),
        )
        d = d + timedelta(days=step_days)


def _by_store(result, store):
    return next((i for i in result["items"] if i["store"] == store), None)


def test_clean_monthly_subscription(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    _add_series(conn, uid, "Spotify", cats["Entertainment"], 9.99,
                date(2025, 6, 3), 11, 30)
    res = detect_recurring(conn, uid, today=TODAY)
    item = _by_store(res, "Spotify")
    assert item is not None
    assert item["cadence"] == "monthly"
    assert item["occurrences"] == 11
    assert item["confidence"] >= 0.8


def test_jittery_monthly_still_detected(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    _add_series(conn, uid, "Netflix", cats["Entertainment"], 15.99,
                date(2025, 7, 1), 9, 30,
                jitter=[0, 2, -1, 1, 3, -2, 0, 1, -1])
    res = detect_recurring(conn, uid, today=TODAY)
    item = _by_store(res, "Netflix")
    assert item is not None
    assert item["cadence"] == "monthly"


def test_variable_utility_amount(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    _add_series(conn, uid, "Helen Energia", cats["Utilities"],
                lambda k: 60 + (k % 4) * 10,
                date(2025, 6, 15), 10, 30)
    res = detect_recurring(conn, uid, today=TODAY)
    item = _by_store(res, "Helen Energia")
    assert item is not None  # amount varies but cadence is regular
    assert item["cadence"] == "monthly"


def test_stopped_cancelled_subscription(user_conn):
    # Last charge long before TODAY -> more than three expected charges never
    # arrived, so the series reads as stopped rather than merely late.
    conn, uid = user_conn
    cats = _cats(conn, uid)
    _add_series(conn, uid, "OldGymApp", cats["Entertainment"], 29.0,
                date(2024, 9, 1), 6, 30)
    res = detect_recurring(conn, uid, today=TODAY)
    item = _by_store(res, "OldGymApp")
    assert item is not None
    assert item["status"] == "stopped"
    assert item["missed_cycles"] >= 3


def test_one_late_charge_is_overdue_not_stopped(user_conn):
    # A single missed charge: still a live subscription, just late.
    conn, uid = user_conn
    cats = _cats(conn, uid)
    _add_series(conn, uid, "StreamCo", cats["Entertainment"], 12.0,
                date(2025, 7, 25), 9, 30)
    res = detect_recurring(conn, uid, today=TODAY)
    item = _by_store(res, "StreamCo")
    assert item is not None
    assert item["status"] == "overdue"
    assert item["missed_cycles"] < 3


def test_price_increase_flagged(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    # 8 months at 10, then a jump to 13 (30% increase).
    _add_series(conn, uid, "CloudStore", cats["Entertainment"],
                lambda k: 10 if k < 7 else 13,
                date(2025, 9, 1), 8, 30)
    res = detect_recurring(conn, uid, today=TODAY)
    item = _by_store(res, "CloudStore")
    assert item is not None
    assert item["status"] == "price_changed"


def test_salary_income_detected(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    _add_series(conn, uid, "Employer Oy", cats["Job"], 3200.0,
                date(2025, 6, 25), 11, 30, type_="income")
    res = detect_recurring(conn, uid, today=TODAY)
    item = _by_store(res, "Employer Oy")
    assert item is not None
    assert item["type"] == "income"
    # income must NOT inflate the expense-only monthly_total
    assert res["summary"]["monthly_total"] == 0


def test_non_recurring_noise_excluded(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    # Random one-off groceries on irregular days, varying amounts.
    for d, amt in [("2025-08-03", 12), ("2025-08-19", 40), ("2025-09-27", 7),
                   ("2026-01-05", 88), ("2026-03-14", 23)]:
        conn.execute(
            "INSERT INTO transactions "
            "(user_id, date, store, category_id, amount, type) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (uid, d, "RandomMart", cats["Groceries"], -amt, "expense"),
        )
    res = detect_recurring(conn, uid, today=TODAY)
    assert _by_store(res, "RandomMart") is None


def test_below_min_occurrences_excluded(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    _add_series(conn, uid, "TwiceOnly", cats["Entertainment"], 5.0,
                date(2026, 3, 1), 2, 30)
    res = detect_recurring(conn, uid, today=TODAY)
    assert _by_store(res, "TwiceOnly") is None


def test_duplicate_merchant_variants_merged(user_conn):
    # The same service (YouTube Premium) appears under two store strings that
    # differ only by a payment-gateway prefix ("GOOGLE *" vs plain "Google ").
    # On their own each variant fires every ~60 days (under the monthly bucket);
    # only when merged do they form one clean monthly series.
    conn, uid = user_conn
    cats = _cats(conn, uid)
    start = date(2025, 6, 5)
    for k in range(12):
        variant = ["GOOGLE *YouTubePremium",
                   "Google YouTubePremium"][k % 2]
        conn.execute(
            "INSERT INTO transactions "
            "(user_id, date, store, category_id, amount, type) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (uid, (start + timedelta(days=30 * k)).isoformat(), variant,
             cats["Entertainment"], -11.99, "expense"),
        )
    res = detect_recurring(conn, uid, today=TODAY)
    yt = [i for i in res["items"]
          if "youtube" in i["store"].lower()]
    assert len(yt) == 1, f"expected one merged YouTube series, got {len(yt)}"
    assert yt[0]["occurrences"] == 12
    assert yt[0]["cadence"] == "monthly"


def test_geo_suffix_variant_merged(user_conn):
    # "OURARING" and "OURARING, OULU, FI" are the same merchant; the geo suffix
    # must not split them into two series.
    conn, uid = user_conn
    cats = _cats(conn, uid)
    start = date(2025, 6, 1)
    for k in range(10):
        variant = ["OURARING", "OURARING, OULU, FI"][k % 2]
        conn.execute(
            "INSERT INTO transactions "
            "(user_id, date, store, category_id, amount, type) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (uid, (start + timedelta(days=30 * k)).isoformat(), variant,
             cats["Entertainment"], -6.0, "expense"),
        )
    res = detect_recurring(conn, uid, today=TODAY)
    oura = [i for i in res["items"] if "ouraring" in i["store"].lower()]
    assert len(oura) == 1, f"expected one merged Oura series, got {len(oura)}"
    assert oura[0]["occurrences"] == 10


def test_distinct_merchants_not_over_merged(user_conn):
    # Two genuinely different subscriptions must stay separate.
    conn, uid = user_conn
    cats = _cats(conn, uid)
    _add_series(conn, uid, "Spotify", cats["Entertainment"], 9.99,
                date(2025, 6, 1), 11, 30)
    _add_series(conn, uid, "Netflix", cats["Entertainment"], 15.99,
                date(2025, 6, 10), 11, 30)
    res = detect_recurring(conn, uid, today=TODAY)
    assert _by_store(res, "Spotify") is not None
    assert _by_store(res, "Netflix") is not None


def test_investment_transfer_excluded_from_total(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    # A regular subscription (counts toward the expense total)...
    _add_series(conn, uid, "Spotify", cats["Entertainment"], 10.0,
                date(2025, 6, 1), 11, 30)
    # ...and a monthly investment transfer (must NOT inflate the total).
    _add_series(conn, uid, "Nordnet", cats["Investments"], 500.0,
                date(2025, 6, 15), 11, 30)
    res = detect_recurring(conn, uid, today=TODAY)
    nordnet = _by_store(res, "Nordnet")
    spotify = _by_store(res, "Spotify")
    assert nordnet is not None and nordnet["is_transfer"] is True
    assert spotify is not None and spotify["is_transfer"] is False
    # The 500/mo transfer is excluded; total reflects only Spotify (~10/mo).
    assert res["summary"]["monthly_total"] < 50


def test_stopped_series_excluded_from_total(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    # A live subscription...
    _add_series(conn, uid, "Spotify", cats["Entertainment"], 10.0,
                date(2025, 6, 1), 11, 30)
    # ...and one that ended over a year ago. It still belongs in the list, but
    # the user does not pay it any more, so it must not inflate the headline.
    _add_series(conn, uid, "DeadGym", cats["Entertainment"], 200.0,
                date(2024, 9, 1), 6, 30)
    res = detect_recurring(conn, uid, today=TODAY)
    dead = _by_store(res, "DeadGym")
    assert dead is not None and dead["status"] == "stopped"
    assert res["summary"]["monthly_total"] < 50
    assert res["summary"]["active_count"] < res["summary"]["count"]


def test_debt_transfer_also_excluded(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    _add_series(conn, uid, "Nordea Loan", cats["Debt"], 300.0,
                date(2025, 6, 1), 11, 30)
    res = detect_recurring(conn, uid, today=TODAY)
    item = _by_store(res, "Nordea Loan")
    assert item is not None and item["is_transfer"] is True
    assert res["summary"]["monthly_total"] == 0


def test_dismissed_signature_filtered(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    _add_series(conn, uid, "Spotify", cats["Entertainment"], 9.99,
                date(2025, 6, 1), 11, 30)
    # First detection exposes the signature.
    res = detect_recurring(conn, uid, today=TODAY)
    item = _by_store(res, "Spotify")
    assert item is not None
    sig = item["signature"]
    assert sig == signature("Spotify", "monthly")
    # Persist a dismissal, then re-detect: the series should be gone.
    conn.execute(
        "INSERT INTO recurring_dismissed (user_id, signature) VALUES (%s, %s)",
        (uid, sig),
    )
    res2 = detect_recurring(conn, uid, today=TODAY)
    assert _by_store(res2, "Spotify") is None
    # Explicit dismissed-set argument should also filter.
    res3 = detect_recurring(conn, uid, today=TODAY, dismissed={sig})
    assert _by_store(res3, "Spotify") is None
