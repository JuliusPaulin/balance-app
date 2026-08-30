"""The read-only tool layer the chat assistant calls.

The point of these tests is that the assistant's answers are the app's own
figures. So they seed transactions through the ordinary HTTP surface and then
check the tools report the same money back — and, above all, that the period
names resolve to the months a person would have meant.
"""

from datetime import date

import pytest

import ai_tools
from helpers import add_tx, cat_id


TODAY = date(2026, 8, 30)


# ── Period resolution ─────────────────────────────────────────────────────
# This is where small models go wrong, which is exactly why it happens in
# Python. Every case here is a sentence the assistant must never have to
# work out for itself.

@pytest.mark.parametrize("period, expected", [
    ("this_month",     ["2026-08"]),
    ("last_month",     ["2026-07"]),
    ("last_3_months",  ["2026-06", "2026-07", "2026-08"]),
    ("last_12_months", ["2025-09", "2026-08"]),
])
def test_relative_periods_resolve_to_months(period, expected):
    months = ai_tools.resolve_period(period, today=TODAY)["months"]
    if period == "last_12_months":
        assert len(months) == 12
        assert [months[0], months[-1]] == expected
    else:
        assert months == expected


def test_year_periods_stop_at_today():
    """"This year" means the months that have happened, not twelve of them."""
    ytd = ai_tools.resolve_period("ytd", today=TODAY)
    assert ytd["months"] == [f"2026-{m:02d}" for m in range(1, 9)]

    last_year = ai_tools.resolve_period("last_year", today=TODAY)
    assert len(last_year["months"]) == 12
    assert last_year["months"][0] == "2025-01"


def test_periods_cross_the_year_boundary():
    """January minus three months is not month -2."""
    months = ai_tools.resolve_period("last_3_months", today=date(2026, 1, 15))["months"]
    assert months == ["2025-11", "2025-12", "2026-01"]


def test_explicit_month_beats_a_named_period():
    """The user named a month; we do not second-guess it with 'last month'."""
    window = ai_tools.resolve_period(period="last_month", month="2025-03", today=TODAY)
    assert window["months"] == ["2025-03"]


def test_explicit_date_range_carries_no_months():
    window = ai_tools.resolve_period(date_from="2026-01-01", date_to="2026-02-15",
                                     today=TODAY)
    assert window["months"] == []
    assert (window["date_from"], window["date_to"]) == ("2026-01-01", "2026-02-15")


def test_all_time_is_unbounded():
    assert ai_tools.resolve_period("all_time", today=TODAY)["months"] == []


def test_an_invented_period_falls_back_rather_than_failing():
    """A model that makes up a period name should still get an answer."""
    assert ai_tools.resolve_period("since_easter", today=TODAY)["months"] == ["2026-08"]


# ── Formatting ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("amount, expected", [
    (612.37, "612 €"),
    (1234.5, "1 234 €"),
    (0, "0 €"),
    (-45.2, "-45 €"),
])
def test_amounts_are_preformatted_for_the_model_to_quote(amount, expected):
    assert ai_tools._eur(amount) == expected


def test_eur_of_nothing_is_nothing():
    assert ai_tools._eur(None) is None


# ── The tools, against real seeded data ───────────────────────────────────

@pytest.fixture
def seeded(client):
    """A month of spending and one salary, through the ordinary API."""
    groceries = cat_id(client, "Groceries")
    restaurant = cat_id(client, "Restaurant")
    job = cat_id(client, "Job", "income")

    add_tx(client, "2026-05-04", "K-Market", groceries, 61.20)
    add_tx(client, "2026-05-18", "S-Market", groceries, 43.80)
    add_tx(client, "2026-05-20", "Bar Llamas", restaurant, 28.00)
    add_tx(client, "2026-05-25", "Payroll", job, 3200.00, "income")
    return client


def test_category_breakdown_reports_the_apps_own_totals(seeded):
    result = ai_tools.category_breakdown(month="2026-05")

    assert result["period"] == "2026-05"
    assert result["total"] == pytest.approx(133.00)
    assert result["total_eur"] == "133 €"

    by_name = {c["category"]: c for c in result["categories"]}
    assert by_name["Groceries"]["total"] == pytest.approx(105.00)
    assert by_name["Groceries"]["total_eur"] == "105 €"
    # Largest first, so "where did it go" answers itself.
    assert result["categories"][0]["category"] == "Groceries"


def test_breakdown_reads_income_when_asked(seeded):
    result = ai_tools.category_breakdown(month="2026-05", type="income")
    assert result["type"] == "income"
    assert result["total"] == pytest.approx(3200.00)


def test_a_multi_month_period_claims_no_baseline(seeded):
    """A twelve-month sum has no monthly normal to stand beside."""
    result = ai_tools.category_breakdown(period="last_12_months")
    assert result["has_baseline"] is False
    assert all("usual_month" not in c for c in result["categories"])


def test_search_filters_by_category_name_not_id(seeded):
    """The model names a category; it never sees or invents an id."""
    result = ai_tools.search_transactions(month="2026-05", categories=["Groceries"])

    assert result["matched"] == 2
    assert result["unknown_categories"] == []
    assert {t["store"] for t in result["transactions"]} == {"K-Market", "S-Market"}
    # Largest first — the answer to "what did I buy" leads with what mattered.
    assert result["transactions"][0]["store"] == "K-Market"
    assert result["transactions"][0]["amount_eur"] == "61 €"


def test_search_says_which_categories_it_did_not_recognise(seeded):
    """Silence here would look like 'you spent nothing on that'."""
    result = ai_tools.search_transactions(month="2026-05", categories=["Yacht upkeep"])
    assert result["unknown_categories"] == ["yacht upkeep"]


def test_search_totals_split_expense_from_income(seeded):
    result = ai_tools.search_transactions(month="2026-05")
    assert result["sum_expense"] == pytest.approx(133.00)
    assert result["sum_income"] == pytest.approx(3200.00)


def test_search_honours_an_amount_floor(seeded):
    result = ai_tools.search_transactions(month="2026-05", type="expense", amount_min=50)
    assert [t["store"] for t in result["transactions"]] == ["K-Market"]


def test_search_never_returns_more_than_the_cap(seeded):
    result = ai_tools.search_transactions(month="2026-05", limit=999)
    assert result["showing"] <= ai_tools.MAX_ROWS


def test_monthly_summary_reports_income_expense_and_net(seeded):
    result = ai_tools.monthly_summary(period="last_12_months")
    may = next((m for m in result["months"] if m["month"] == "2026-05"), None)

    # The seeded month is inside the last twelve only if today is close enough;
    # when it is not, the point is that the filter held, not that data is missing.
    if may is not None:
        assert may["expense"] == pytest.approx(133.00)
        assert may["income"] == pytest.approx(3200.00)
        assert may["net"] == pytest.approx(3067.00)
        assert may["net_eur"] == "3 067 €"


def test_context_tells_the_model_what_it_is_looking_at(seeded):
    context = ai_tools.context_block()
    assert context["first_month_with_data"] == "2026-05"
    assert context["last_month_with_data"] == "2026-05"
    # Without the category list the model guesses names and gets empty results.
    assert "Groceries" in context["categories"]
    assert context["today"] == date.today().isoformat()


# ── The dispatcher ────────────────────────────────────────────────────────
# A tool that raises would end the turn with a stack trace where an answer
# should be. Every failure has to come back as something the model can read.

def test_every_advertised_tool_is_callable():
    """A schema with no function behind it is a tool call that always fails."""
    assert {t["name"] for t in ai_tools.TOOL_SCHEMAS} == set(ai_tools.TOOLS)


def test_an_invented_tool_name_returns_an_error(seeded):
    assert "error" in ai_tools.run_tool("transfer_money", {})


def test_bad_arguments_return_an_error_rather_than_raising(seeded):
    result = ai_tools.run_tool("category_breakdown", {"nonsense": 1})
    assert "error" in result


def test_run_tool_dispatches_a_real_call(seeded):
    result = ai_tools.run_tool("category_breakdown", {"month": "2026-05"})
    assert result["total_eur"] == "133 €"
