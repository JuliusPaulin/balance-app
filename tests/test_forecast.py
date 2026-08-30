"""Unit tests for forecast.build_forecast, plus a smoke test of its route.

``today`` is pinned so the arithmetic is checked against a fixed calendar
rather than whatever day the suite runs on. The month buckets are the point of
the module, and they move under you otherwise.
"""
from datetime import date, timedelta

import pytest

from forecast import build_forecast
from tests.helpers import add_tx, cat_id

TODAY = date(2026, 6, 10)      # 21 days left of a 30-day month


def _cats(conn, user_id):
    """name -> id for the categories these tests reference."""
    names = [("Entertainment", "expense"), ("Groceries", "expense"),
             ("Job", "income"), ("Investments", "expense")]
    out = {}
    for name, type_ in names:
        row = conn.execute(
            "SELECT id FROM categories WHERE user_id = %s AND name = %s AND type = %s",
            (user_id, name, type_),
        ).fetchone()
        out[name] = row["id"]
    return out


def _series(conn, user_id, store, cat_id_, amount, start, count, step_days=30,
            type_="expense"):
    """Insert ``count`` charges ``step_days`` apart, oldest first."""
    d = start
    for _ in range(count):
        conn.execute(
            "INSERT INTO transactions (user_id, date, store, category_id, amount, type) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, d.isoformat(), store, cat_id_, amount, type_),
        )
        d += timedelta(days=step_days)


def _one_off(conn, user_id, d, store, cat_id_, amount, type_="expense"):
    conn.execute(
        "INSERT INTO transactions (user_id, date, store, category_id, amount, type) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (user_id, d.isoformat(), store, cat_id_, amount, type_),
    )


def _month(result, key):
    return next(m for m in result["months"] if m["month"] == key)


# ── Shape ────────────────────────────────────────────────────────────────
def test_returns_the_part_month_then_the_full_ones(user_conn):
    conn, uid = user_conn
    result = build_forecast(conn, uid, months_ahead=3, today=TODAY)

    assert [m["month"] for m in result["months"]] == [
        "2026-06", "2026-07", "2026-08", "2026-09"]
    first = result["months"][0]
    assert first["is_partial"] is True
    assert first["days_remaining"] == 21        # 10th of a 30-day month, today counts
    assert all(m["is_partial"] is False for m in result["months"][1:])


def test_months_ahead_is_clamped(user_conn):
    conn, uid = user_conn
    assert len(build_forecast(conn, uid, months_ahead=99, today=TODAY)["months"]) == 13
    assert len(build_forecast(conn, uid, months_ahead=0, today=TODAY)["months"]) == 2


def test_empty_database_forecasts_nothing_rather_than_failing(user_conn):
    conn, uid = user_conn
    result = build_forecast(conn, uid, today=TODAY)

    assert result["basis"]["has_history"] is False
    assert result["basis"]["variable_expense_monthly"] == 0
    assert all(m["net"] == 0 for m in result["months"])


# ── Recurring half ───────────────────────────────────────────────────────
def test_a_monthly_subscription_lands_once_in_each_month(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    _series(conn, uid, "Spotify", cats["Entertainment"], 11.99,
            date(2026, 1, 12), 6)              # Jan–Jun, next due ~10 Jul

    result = build_forecast(conn, uid, months_ahead=3, today=TODAY)

    for key in ("2026-07", "2026-08", "2026-09"):
        month = _month(result, key)
        assert month["recurring_expense"] == pytest.approx(11.99, abs=0.01), key
        assert [c["store"] for c in month["charges"]] == ["Spotify"]


def test_recurring_income_is_forecast_too(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    _series(conn, uid, "Employer", cats["Job"], 3000.0,
            date(2026, 1, 25), 6, type_="income")

    july = _month(build_forecast(conn, uid, months_ahead=2, today=TODAY), "2026-07")
    assert july["recurring_income"] == pytest.approx(3000.0, abs=0.01)
    assert july["net"] > 0


def test_a_stopped_series_is_not_forecast(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    # Six monthly charges ending in October 2025 — eight cycles missed by June.
    _series(conn, uid, "OldGym", cats["Entertainment"], 40.0, date(2025, 5, 3), 6)

    result = build_forecast(conn, uid, months_ahead=3, today=TODAY)
    charged = [c["store"] for m in result["months"] for c in m["charges"]]
    assert "OldGym" not in charged


def test_a_late_series_is_rolled_forward_not_charged_in_the_past(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    # Last charge 2 May, so its next date (1 Jun) has already gone by.
    _series(conn, uid, "Netflix", cats["Entertainment"], 15.0, date(2025, 12, 2), 6)

    result = build_forecast(conn, uid, months_ahead=2, today=TODAY)
    dates = [c["date"] for m in result["months"] for c in m["charges"]]
    assert dates, "a late series should still be expected to charge"
    assert all(d >= TODAY.isoformat() for d in dates)


def test_transfers_are_forecast_because_the_money_still_leaves(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    _series(conn, uid, "Nordnet", cats["Investments"], 500.0, date(2026, 1, 5), 6)

    july = _month(build_forecast(conn, uid, months_ahead=2, today=TODAY), "2026-07")
    assert july["recurring_expense"] == pytest.approx(500.0, abs=0.01)


# ── Variable half ────────────────────────────────────────────────────────
def test_variable_spend_is_the_median_of_completed_months(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    for month, amount in ((3, 300.0), (4, 500.0), (5, 400.0)):
        _one_off(conn, uid, date(2026, month, 8), f"Shop{month}",
                 cats["Groceries"], amount)
    # This month is unfinished, so it must not drag the baseline down.
    _one_off(conn, uid, date(2026, 6, 2), "Shop6", cats["Groceries"], 20.0)

    result = build_forecast(conn, uid, months_ahead=2, today=TODAY)
    assert result["basis"]["variable_expense_monthly"] == pytest.approx(400.0)
    assert result["basis"]["history_months"] == ["2026-03", "2026-04", "2026-05"]
    assert _month(result, "2026-07")["variable_expense"] == pytest.approx(400.0)


def test_the_part_month_is_scaled_to_the_days_left(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    for month in (3, 4, 5):
        _one_off(conn, uid, date(2026, month, 8), f"Shop{month}",
                 cats["Groceries"], 300.0)

    june = _month(build_forecast(conn, uid, today=TODAY), "2026-06")
    assert june["variable_expense"] == pytest.approx(300.0 * 21 / 30, abs=0.01)


def test_the_part_month_ignores_what_this_month_already_holds(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    for month in (3, 4, 5):
        _one_off(conn, uid, date(2026, month, 8), f"Shop{month}",
                 cats["Groceries"], 300.0)
    # Spending already recorded this month must not move the baseline: the
    # current month is unfinished, and a part month is not a month.
    _one_off(conn, uid, date(2026, 6, 4), "ShopJune", cats["Groceries"], 900.0)

    result = build_forecast(conn, uid, today=TODAY)
    assert result["basis"]["history_months"] == ["2026-03", "2026-04", "2026-05"]
    assert result["basis"]["variable_expense_monthly"] == pytest.approx(300.0)
    assert _month(result, "2026-06")["variable_expense"] == pytest.approx(
        300.0 * 21 / 30, abs=0.01)


def test_a_charge_already_paid_this_month_is_not_expected_again(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    # Charged on the 2nd of each month, so June's has already gone.
    _series(conn, uid, "Spotify", cats["Entertainment"], 11.99,
            date(2026, 1, 2), 6, step_days=30)

    june = _month(build_forecast(conn, uid, today=TODAY), "2026-06")
    assert june["recurring_expense"] == 0.0
    assert june["charges"] == []


def test_a_recurring_charge_is_not_counted_again_as_variable(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    _series(conn, uid, "Spotify", cats["Entertainment"], 11.99, date(2026, 1, 12), 6)
    for month in (3, 4, 5):
        _one_off(conn, uid, date(2026, month, 8), f"Shop{month}", cats["Groceries"], 300.0)

    result = build_forecast(conn, uid, months_ahead=2, today=TODAY)
    # 300 groceries only — the Spotify charge in each of those months belongs
    # to the recurring half and must not show up in the baseline as well.
    assert result["basis"]["variable_expense_monthly"] == pytest.approx(300.0)
    july = _month(result, "2026-07")
    assert july["expense_total"] == pytest.approx(311.99, abs=0.01)


def test_a_month_with_no_data_is_skipped_not_read_as_zero_spending(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    # April holds nothing — a statement not imported, not a month without spending.
    _one_off(conn, uid, date(2026, 3, 8), "Lidl", cats["Groceries"], 400.0)
    _one_off(conn, uid, date(2026, 5, 8), "Lidl", cats["Groceries"], 400.0)

    result = build_forecast(conn, uid, months_ahead=1, today=TODAY)
    assert result["basis"]["history_months"] == ["2026-03", "2026-05"]
    assert result["basis"]["variable_expense_monthly"] == pytest.approx(400.0)


# ── Totals ───────────────────────────────────────────────────────────────
def test_net_and_cumulative_add_up(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    _series(conn, uid, "Employer", cats["Job"], 3000.0,
            date(2026, 1, 25), 6, type_="income")
    for month in (3, 4, 5):
        _one_off(conn, uid, date(2026, month, 8), f"Shop{month}", cats["Groceries"], 900.0)

    result = build_forecast(conn, uid, months_ahead=3, today=TODAY)
    running = 0.0
    for month in result["months"]:
        assert month["net"] == pytest.approx(
            month["income_total"] - month["expense_total"], abs=0.01)
        running += month["net"]
        assert month["cumulative"] == pytest.approx(running, abs=0.05)
    assert result["summary"]["total_net"] == pytest.approx(running, abs=0.05)


def test_a_month_that_goes_negative_is_named(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    for month in (3, 4, 5):
        _one_off(conn, uid, date(2026, month, 8), f"Shop{month}", cats["Groceries"], 2000.0)

    summary = build_forecast(conn, uid, months_ahead=3, today=TODAY)["summary"]
    assert summary["negative_months"] == 3
    assert summary["worst_net"] < 0
    assert summary["average_net"] == pytest.approx(-2000.0, abs=0.01)


def test_the_average_ignores_the_part_month(user_conn):
    conn, uid = user_conn
    cats = _cats(conn, uid)
    for month in (3, 4, 5):
        _one_off(conn, uid, date(2026, month, 8), f"Shop{month}", cats["Groceries"], 600.0)

    result = build_forecast(conn, uid, months_ahead=2, today=TODAY)
    # The part-month spends only 21/30 of 600 and would pull a plain mean up.
    assert result["summary"]["average_net"] == pytest.approx(-600.0, abs=0.01)


# ── Route ────────────────────────────────────────────────────────────────
def test_the_route_answers_with_the_forecast(client):
    groceries = cat_id(client, "Groceries")
    add_tx(client, "2026-01-10", "Lidl", groceries, 100.0)

    res = client.get("/api/dashboard/forecast?months=2")
    assert res.status_code == 200
    body = res.get_json()
    assert len(body["months"]) == 3          # part-month + 2 full
    assert body["months"][0]["is_partial"] is True
    assert "variable_expense_monthly" in body["basis"]


def test_the_route_clamps_a_silly_horizon(client):
    res = client.get("/api/dashboard/forecast?months=500")
    assert res.status_code == 200
    assert res.get_json()["months_ahead"] == 12
