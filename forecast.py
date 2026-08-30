"""Cash-flow forecast — what the next few months are expected to cost and earn.

Pure analytics over ``transactions`` and :func:`recurring.detect_recurring`. No
schema changes, nothing stored: ask for it and it is worked out from the data
already there.

The forecast splits a month into two halves that always add back up to the
whole:

* **Recurring** — the series ``recurring.py`` already detects (plus the ones
  added by hand), each rolled forward on its own cadence to the dates it is
  expected to land on. This is the part with a date attached, so the card can
  say *what* is due and *when*.
* **Variable** — everything else. Not guessed per merchant, but taken as the
  median of what the last few completed months actually cost once their own
  recurring charges are taken out. Median rather than mean because one holiday
  should not become the new normal. In the part-finished current month it is
  scaled to the days left.

Netting the current month's actuals off a whole month's baseline was tried for
that last row and dropped. It reads better on paper — a salary already paid is
not forecast a second time — but it cannot tell "not spent yet" from "statement
not imported yet", and those look identical in the data. On the 30th of a month
whose statement had not been loaded it forecast a whole further month of salary
and spending. Scaling by the days left is the cruder answer and the safer one:
whatever it gets wrong, it can only get wrong by the share of the month still
to run.

The two halves are cut with the same knife: a transaction counts as recurring
here only if its store belongs to a detected series, and those same
transactions are the ones removed from the variable baseline. So nothing is
counted twice and nothing is dropped — Rent, which detection deliberately skips
as a generic name, still reaches the forecast through the variable half.

**Transfers count.** ``recurring.py`` keeps Investments and Debt out of its
"what I pay per month" total because they are movements rather than
consumption. A cash-flow forecast asks a different question — whether the money
is there — and money moved into an index fund has left the account like any
other. So transfers are forecast, and they are removed from the variable
baseline to match.

**Stopped series are not forecast**, because a service that ended will not
charge again. Their past charges *are* still removed from the variable baseline,
though: leaving them in would let a subscription cancelled in March inflate the
"variable" spend of every month before it.
"""
from __future__ import annotations

import calendar
import statistics
from datetime import date, timedelta

from recurring import detect_recurring, _normalize_store

# Completed months the variable baseline is drawn from. Six covers a season
# without reaching back to a salary or a rent that has since changed.
_HISTORY_MONTHS = 6

DEFAULT_MONTHS_AHEAD = 3
MAX_MONTHS_AHEAD = 12

# Cadence → days, for a series that reaches us without an interval (a
# hand-added subscription has no history to measure one from).
_CADENCE_DAYS = {"monthly": 30, "quarterly": 91, "yearly": 365}


def _add_months(year: int, month: int, n: int) -> tuple:
    """(year, month) n months on. n may be negative."""
    total = year * 12 + (month - 1) + n
    return total // 12, total % 12 + 1


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _last_day(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _series_interval(item: dict) -> int:
    """Days between charges for a series, however it reached us."""
    interval = item.get("interval_days")
    if interval and interval > 0:
        return int(interval)
    return _CADENCE_DAYS.get(item.get("cadence"), 30)


def _series_amount(item: dict) -> float:
    """What the next charge is expected to be.

    The average, except on a series flagged ``price_changed`` — there the whole
    point is that the price moved, so the latest charge is the one to expect.
    """
    if item.get("status") == "price_changed" and item.get("last_amount"):
        return abs(float(item["last_amount"]))
    return abs(float(item.get("avg_amount") or 0.0))


def _first_due(item: dict, today: date):
    """The first date this series is expected to charge on or after ``today``.

    A detected series has a ``next_date``. When it is in the past the series is
    late (``overdue``), not gone — detection would have called it ``stopped``
    otherwise — so the date is stepped forward by whole cadences until it
    reaches today rather than being written off.

    A hand-added subscription has no dates at all. It is anchored to the first
    of next month: there is nothing in the data to place it better, and putting
    it in the part-finished current month would charge the user for a day that
    may already have passed.
    """
    raw = item.get("next_date")
    if not raw:
        y, m = _add_months(today.year, today.month, 1)
        return date(y, m, 1)
    try:
        due = date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None
    interval = _series_interval(item)
    while due < today:
        due += timedelta(days=interval)
    return due


def _schedule(items: list, today: date, horizon_end: date) -> dict:
    """Roll every live series forward, bucketed by month key.

    Returns ``{"YYYY-MM": [charge, ...]}`` with each charge carrying its date,
    store, amount, type and category, so a month can show its own bill. Every
    charge falls on or after ``today`` — this month's charges that have already
    gone are the caller's history, not its forecast.
    """
    buckets: dict = {}
    for item in items:
        if item.get("status") == "stopped":
            continue
        amount = _series_amount(item)
        if amount <= 0:
            continue
        due = _first_due(item, today)
        if due is None:
            continue
        interval = _series_interval(item)
        while due <= horizon_end:
            buckets.setdefault(_month_key(due.year, due.month), []).append({
                "date": due.isoformat(),
                "store": item.get("store"),
                "category": item.get("category"),
                "type": item.get("type"),
                "amount": round(amount, 2),
                "cadence": item.get("cadence"),
                "is_manual": bool(item.get("is_manual")),
            })
            due += timedelta(days=interval)
    for charges in buckets.values():
        charges.sort(key=lambda c: (c["date"], -c["amount"]))
    return buckets


def _recurring_stores(items: list) -> set:
    """Every store string that belongs to a recurring series, normalized.

    Includes the stopped ones on purpose — see the module docstring.
    """
    stores = set()
    for item in items:
        for store in item.get("stores") or []:
            norm = _normalize_store(store)
            if norm:
                stores.add(norm)
    return stores


def _variable_baseline(conn, user_id: int, today: date, items: list) -> dict:
    """What a month costs and earns once its recurring charges are removed.

    The baseline is the median of the *completed* months before this one — the
    current month is part-finished and would drag the figure down. A month
    holding no data at all is skipped rather than counted as a zero: it is a
    statement not imported yet, not a month without spending.
    """
    first_history_y, first_history_m = _add_months(today.year, today.month, -_HISTORY_MONTHS)
    start = date(first_history_y, first_history_m, 1)
    this_month = _month_key(today.year, today.month)

    rows = conn.execute(
        """
        SELECT substr(t.date, 1, 7) AS month, t.type AS type, t.store AS store,
               SUM(ABS(t.amount)) AS total
        FROM transactions t
        WHERE t.user_id = %s AND t.date >= %s AND t.date <= %s
        GROUP BY substr(t.date, 1, 7), t.type, t.store
        """,
        (user_id, start.isoformat(), today.isoformat()),
    ).fetchall()

    recurring_stores = _recurring_stores(items)
    variable: dict = {}
    for r in rows:
        month = r["month"]
        totals = variable.setdefault(month, {"expense": 0.0, "income": 0.0})
        if _normalize_store(r["store"]) in recurring_stores:
            continue
        if r["type"] in totals:
            totals[r["type"]] += float(r["total"] or 0.0)

    variable.pop(this_month, None)
    months = sorted(variable)
    expense = [variable[m]["expense"] for m in months]
    income = [variable[m]["income"] for m in months]
    return {
        "months": months,
        "expense": round(statistics.median(expense), 2) if expense else 0.0,
        "income": round(statistics.median(income), 2) if income else 0.0,
    }


def build_forecast(conn, user_id: int, months_ahead: int = DEFAULT_MONTHS_AHEAD,
                   today: date = None, recurring: dict = None) -> dict:
    """The next ``months_ahead`` months of expected cash flow.

    The first row is always the rest of the *current* month — the part you can
    still do something about — pro-rated by the days left in it and marked
    ``is_partial``. The full months follow.
    """
    today = today or date.today()
    if months_ahead is None:
        months_ahead = DEFAULT_MONTHS_AHEAD
    months_ahead = max(1, min(int(months_ahead), MAX_MONTHS_AHEAD))

    if recurring is None:
        recurring = detect_recurring(conn, user_id, today=today)
    items = recurring.get("items", [])

    end_y, end_m = _add_months(today.year, today.month, months_ahead)
    horizon_end = _last_day(end_y, end_m)
    scheduled = _schedule(items, today, horizon_end)
    baseline = _variable_baseline(conn, user_id, today, items)

    months = []
    cumulative = 0.0
    for offset in range(months_ahead + 1):
        y, m = _add_months(today.year, today.month, offset)
        key = _month_key(y, m)
        days_in_month = calendar.monthrange(y, m)[1]
        partial = offset == 0
        # The rest of this month counts today itself — a charge dated today has
        # not necessarily hit the account yet.
        days_left = days_in_month - today.day + 1 if partial else days_in_month

        # Already on or after today — _schedule never looks back.
        charges = scheduled.get(key, [])

        rec_expense = sum(c["amount"] for c in charges if c["type"] == "expense")
        rec_income = sum(c["amount"] for c in charges if c["type"] == "income")
        share = days_left / days_in_month
        var_expense = baseline["expense"] * share
        var_income = baseline["income"] * share

        income_total = rec_income + var_income
        expense_total = rec_expense + var_expense
        net = income_total - expense_total
        cumulative += net

        months.append({
            "month": key,
            "is_partial": partial,
            "days_remaining": days_left,
            "days_in_month": days_in_month,
            "recurring_expense": round(rec_expense, 2),
            "variable_expense": round(var_expense, 2),
            "expense_total": round(expense_total, 2),
            "recurring_income": round(rec_income, 2),
            "variable_income": round(var_income, 2),
            "income_total": round(income_total, 2),
            "net": round(net, 2),
            "cumulative": round(cumulative, 2),
            "charges": charges,
        })

    full = [m for m in months if not m["is_partial"]]
    negative = [m for m in full if m["net"] < 0]
    worst = min(full, key=lambda m: m["net"]) if full else None

    return {
        "as_of": today.isoformat(),
        "months_ahead": months_ahead,
        "months": months,
        "basis": {
            # The completed months the variable figure was measured over. An
            # empty list means there is no finished month to measure, and the
            # variable half of every row below is zero — the forecast is then
            # only as good as the recurring charges it found.
            "history_months": baseline["months"],
            "variable_expense_monthly": baseline["expense"],
            "variable_income_monthly": baseline["income"],
            "recurring_series": sum(1 for i in items if i.get("status") != "stopped"),
            "has_history": bool(baseline["months"]),
        },
        "summary": {
            "total_net": round(sum(m["net"] for m in months), 2),
            # Averaged over the full months only: the part-month at the front
            # would drag a "per month" figure below what a month really costs.
            "average_net": round(statistics.fmean(m["net"] for m in full), 2) if full else 0.0,
            "negative_months": len(negative),
            "worst_month": worst["month"] if worst else None,
            "worst_net": worst["net"] if worst else None,
        },
    }
