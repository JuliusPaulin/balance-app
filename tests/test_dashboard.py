"""What the Dashboard and the annual report read.

Two things here are worth more than the rest. The breakdown card answers over
whichever period it is asked for, and the annual report holds every
year-on-year figure to the months the chosen year actually has — a year seven
months in, measured against a full twelve, reports a collapse that never
happened.
"""

from helpers import add_tx, cat_id


def test_monthly_summary_groups_by_month_and_type(client, login, make_user):
    make_user()
    groceries = cat_id(client, "Groceries")
    job       = cat_id(client, "Job", "income")
    add_tx(client, "2026-01-10", "Lidl",     groceries,   20.0)
    add_tx(client, "2026-01-20", "K-Market", groceries,   30.0)
    add_tx(client, "2026-02-01", "Employer", job,       3000.0, "income")

    rows = client.get("/api/dashboard/monthly-summary").get_json()
    got = {(r["month"], r["type"]): r["total"] for r in rows}
    assert got[("2026-01", "expense")] == 50.0
    assert got[("2026-02", "income")] == 3000.0


def test_monthly_summary_counts_investing_separately(client, login, make_user):
    """Money moved into investments is spending in the ledger and saving in
    life, so the summary reports it under both."""
    make_user()
    investments = cat_id(client, "Investments")
    add_tx(client, "2026-01-15", "Nordnet", investments, 500.0)

    rows = client.get("/api/dashboard/monthly-summary").get_json()
    got = {(r["month"], r["type"]): r["total"] for r in rows}
    assert got[("2026-01", "expense")] == 500.0
    assert got[("2026-01", "investment")] == 500.0


# ── Category breakdown ──────────────────────────────────────────────
def _breakdown(client, query=""):
    got = client.get("/api/dashboard/category-breakdown" + query).get_json()
    return got, {i["name"]: i["total"] for i in got["items"]}


def _seed_two_months(client):
    groceries = cat_id(client, "Groceries")
    lunch     = cat_id(client, "Lunch")
    job       = cat_id(client, "Job", "income")
    add_tx(client, "2026-01-10", "Lidl",     groceries,   20.0)
    add_tx(client, "2026-02-10", "K-Market", groceries,   30.0)
    add_tx(client, "2026-02-11", "Fafa's",   lunch,       10.0)
    add_tx(client, "2026-02-25", "Employer", job,       3000.0, "income")


def test_breakdown_defaults_to_the_latest_month_of_expenses(client, login, make_user):
    make_user()
    _seed_two_months(client)
    got, totals = _breakdown(client)
    assert got["month"] == "2026-02"
    assert totals == {"Groceries": 30.0, "Lunch": 10.0}


def test_breakdown_over_named_months(client, login, make_user):
    make_user()
    _seed_two_months(client)
    got, totals = _breakdown(client, "?months=2026-01,2026-02")
    assert got["months"] == ["2026-01", "2026-02"]
    assert totals["Groceries"] == 50.0


def test_breakdown_over_a_year(client, login, make_user):
    make_user()
    _seed_two_months(client)
    _, totals = _breakdown(client, "?year=2026")
    assert totals["Groceries"] == 50.0


def test_breakdown_answers_for_income_too(client, login, make_user):
    """Both cards come from this one endpoint, so income is not a special case."""
    make_user()
    _seed_two_months(client)
    got, totals = _breakdown(client, "?type=income&month=2026-02")
    assert got["type"] == "income"
    assert totals == {"Job": 3000.0}


def test_breakdown_falls_back_to_expense_on_a_type_it_cannot_read(client, login, make_user):
    make_user()
    _seed_two_months(client)
    got, _ = _breakdown(client, "?type=nonsense&month=2026-02")
    assert got["type"] == "expense"


def test_breakdown_of_an_empty_ledger(client, login, make_user):
    make_user()
    got, totals = _breakdown(client)
    assert got["month"] is None and totals == {}


# ── Top expenses and trends ─────────────────────────────────────────
def test_top_expenses_reads_the_latest_month(client, login, make_user):
    make_user()
    groceries = cat_id(client, "Groceries")
    lunch     = cat_id(client, "Lunch")
    add_tx(client, "2026-01-10", "Lidl",   groceries, 20.0)
    add_tx(client, "2026-02-10", "Lidl",   groceries, 30.0)
    add_tx(client, "2026-02-11", "Fafa's", lunch,     50.0)

    got = client.get("/api/dashboard/top-expenses").get_json()
    assert got["latest_month"] == "2026-02"
    assert [c["name"] for c in got["categories"]] == ["Lunch", "Groceries"]
    # The trend runs over every month those categories appear in, not just the
    # latest one — that is what makes it a trend.
    assert {t["month"] for t in got["trends"]} == {"2026-01", "2026-02"}


def test_category_trends_needs_ids_and_wants_integers(client, login, make_user):
    make_user()
    assert client.get("/api/dashboard/category-trends").get_json() == \
        {"categories": [], "trends": []}
    assert client.get(
        "/api/dashboard/category-trends?ids=abc").status_code == 400


# ── Heatmap ─────────────────────────────────────────────────────────
def test_heatmap_totals_a_day_and_lists_the_years(client, login, make_user):
    make_user()
    groceries = cat_id(client, "Groceries")
    job       = cat_id(client, "Job", "income")
    add_tx(client, "2026-03-04", "Lidl",     groceries,  20.0)
    add_tx(client, "2026-03-04", "K-Market", groceries,  15.0)
    add_tx(client, "2026-03-04", "Employer", job,      3000.0, "income")
    add_tx(client, "2025-07-01", "Lidl",     groceries,  10.0)

    got = client.get("/api/dashboard/heatmap?year=2026").get_json()
    days = {r["date"]: r["total"] for r in got["items"]}
    # Income is not spending, so it stays out of the day's square.
    assert days == {"2026-03-04": 35.0}
    assert got["available_years"] == [2026, 2025]


# ── Annual report ───────────────────────────────────────────────────
def test_annual_report_compares_only_the_months_the_year_has(client, login, make_user):
    """A part-finished year against a full one is not a comparison.

    2026 here holds January and February. 2025 held those two months and a
    fat December besides. Measuring against the whole of 2025 would report
    income falling through the floor; against the same two months it rose.
    """
    make_user()
    job = cat_id(client, "Job", "income")
    add_tx(client, "2025-01-25", "Employer", job, 2000.0, "income")
    add_tx(client, "2025-02-25", "Employer", job, 2000.0, "income")
    add_tx(client, "2025-12-25", "Employer", job, 9000.0, "income")
    add_tx(client, "2026-01-25", "Employer", job, 2500.0, "income")
    add_tx(client, "2026-02-25", "Employer", job, 2500.0, "income")

    got = client.get("/api/reports/annual?year=2026").get_json()
    assert got["compare_months"] == ["01", "02"]
    assert got["totals"]["income"] == 5000.0
    assert got["prev_totals"]["income"] == 4000.0   # not 13000


def test_annual_report_holds_category_deltas_to_the_same_months(client, login, make_user):
    make_user()
    groceries = cat_id(client, "Groceries")
    add_tx(client, "2025-01-10", "Lidl", groceries, 100.0)
    add_tx(client, "2025-11-10", "Lidl", groceries, 900.0)
    add_tx(client, "2026-01-10", "Lidl", groceries, 120.0)

    got = client.get("/api/reports/annual?year=2026").get_json()
    assert got["prev_categories"]["Groceries"] == 100.0


def test_annual_report_of_a_year_with_nothing_in_it(client, login, make_user):
    """No months means nothing to compare against — and no SQL built from an
    empty list of placeholders."""
    make_user()
    got = client.get("/api/reports/annual?year=2026").get_json()
    assert got["compare_months"] == []
    assert got["prev_totals"] == {}
    assert got["categories"] == []
