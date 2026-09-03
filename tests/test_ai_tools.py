"""The read-only tool layer the chat assistant calls.

The point of these tests is that the assistant's answers are the app's own
figures. So they seed transactions through the ordinary HTTP surface and then
check the tools report the same money back — and, above all, that the period
names resolve to the months a person would have meant.
"""

import os
import pathlib
from datetime import date

import pytest

import config

from ai import tools as ai_tools
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


def test_an_invented_period_is_refused_and_says_what_is_valid():
    """A model that makes up a period name has to be told, not humoured.

    This used to fall back to the current month. That reads as an answer and is
    not one: asked whether July beat June, the model invented `last_2_months`,
    was handed August without a word, and gave up on a question the data
    answers. The error names the list, so the next round can get it right.
    """
    with pytest.raises(ValueError) as excinfo:
        ai_tools.resolve_period("since_easter", today=TODAY)
    assert "since_easter" in str(excinfo.value)
    for period in ai_tools.PERIODS:
        assert period in str(excinfo.value)


def test_a_refused_period_reaches_the_model_as_an_error():
    """`run_tool` turns it into a result the model can act on, not a crash."""
    result = ai_tools.run_tool("monthly_summary", {"period": "since_easter"})
    assert "error" in result
    assert "last_3_months" in result["error"]


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


# ── Reaching the endpoints at all ─────────────────────────────────────────
# Every tool is a wrapper over one of the app's own GET routes. The wrappers
# were tested; the wiring underneath them was not, and it was broken: the
# blueprints go on in `app.py`, so anything importing `ai_tools` on its own —
# `scripts/ask.py`, the harness the model is judged with — dispatched against a
# Flask app with no routes and got a 404 for every question. The 404 became
# `None`, `None` became an empty result, and the assistant reported "0 €".

def test_the_tools_can_reach_the_routes_they_wrap():
    """The blueprints are on the app object the tools dispatch against."""
    ai_tools._ensure_routes()
    paths = {rule.rule for rule in ai_tools.app.url_map.iter_rules()}
    for path in ("/api/categories", "/api/transactions",
                 "/api/dashboard/category-breakdown",
                 "/api/dashboard/monthly-summary", "/api/recurring",
                 "/api/reports/annual", "/api/networth/summary"):
        assert path in paths, f"{path} is not registered"


def test_a_tool_works_in_a_process_that_never_imports_app():
    """The actual shape of the bug, which needs its own interpreter to see.

    Inside the suite something has always imported `app`, so the routes were on
    the app object before any tool ran and the wiring looked sound. `ask.py`
    imports `ai_chat` and nothing else. This runs that way on purpose.
    """
    import subprocess
    import sys

    source = (
        "from ai import tools;"
        "r = tools._call_api('/api/categories');"
        "assert isinstance(r, list) and r, 'no categories came back';"
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True, text=True,
        cwd=str(pathlib.Path(__file__).resolve().parent.parent),
        env={**os.environ, "SQLITE_PATH": config.SQLITE_PATH},
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_registering_the_blueprints_twice_is_harmless():
    """`app.py` registers them too; the tools must not care who got there first."""
    import routes
    routes.register(ai_tools.app)
    routes.register(ai_tools.app)


def test_a_failed_endpoint_raises_instead_of_reading_as_zero(seeded):
    """The one failure this module exists to prevent.

    A dispatch that does not return 200 used to come back as `None`, which the
    tool bodies turned into an empty result and the assistant read aloud as
    "0 €" — a false figure, stated confidently, about money.
    """
    with pytest.raises(ai_tools.ToolDispatchError):
        ai_tools._call_api("/api/no-such-endpoint")


# ── Subscriptions ─────────────────────────────────────────────────────────

@pytest.fixture
def with_a_subscription(client):
    """Six monthly charges from one merchant — enough for the detector."""
    entertainment = cat_id(client, "Entertainment")
    for month in range(3, 9):
        add_tx(client, f"2026-{month:02d}-05", "Spotify", entertainment, 11.99)
    return client


def test_subscriptions_carry_amounts_and_a_total(with_a_subscription):
    """The keys are the endpoint's own.

    This tool read `amount`, `monthly_total` and `next_charge`; the endpoint
    sends `monthly_cost`, a nested `summary` and `next_date`. Every figure came
    back `None`, and asked what its subscriptions cost, the assistant said it
    could not tell.
    """
    result = ai_tools.list_subscriptions()

    assert result["monthly_total"] is not None
    assert result["monthly_total_eur"] is not None
    assert result["annual_total_eur"] is not None
    assert result["counted_in_total"] >= 1

    spotify = next(s for s in result["subscriptions"] if s["merchant"] == "Spotify")
    assert spotify["monthly_cost"] == pytest.approx(11.99, abs=0.5)
    assert spotify["monthly_cost_eur"] is not None
    assert spotify["last_amount_eur"] == "12\xa0€"
    assert spotify["cadence"] == "monthly"
    assert spotify["counts_toward_total"] is True


def test_a_subscription_row_carries_no_yearly_figure(with_a_subscription):
    """Two amounts on a row, not three.

    Asked what the subscriptions cost each month the model wrote "1 253 € per
    month, that's 1 472 € for rent" — 1 472 € being nothing any tool returned,
    a mangling of a row reading 1 226 € a month, 14 718 € a year and 1 250 €
    last charge. It got Netflix right, where the three figures are 16 €, 188 €
    and 16 € and picking wrongly barely shows. The yearly total stays on the
    result; a yearly figure per row is one more thing to pick wrongly from.
    """
    result = ai_tools.list_subscriptions()
    spotify = next(s for s in result["subscriptions"] if s["merchant"] == "Spotify")

    assert "annual_cost" not in spotify
    assert "annual_cost_eur" not in spotify
    assert result["annual_total_eur"] is not None


def test_a_salary_is_detected_but_does_not_count_as_a_subscription(client):
    """The detector finds any monthly series, wages included.

    Left to weigh `type`, `is_transfer` and `status` itself, a small model will
    sooner or later announce that the largest subscription is the employer.
    """
    job = cat_id(client, "Job", "income")
    for month in range(3, 9):
        add_tx(client, f"2026-{month:02d}-13", "Payroll", job, 3200.00, "income")

    result = ai_tools.list_subscriptions()

    # Not merely flagged — kept out of the list entirely. A flag was not enough:
    # asked for its three biggest subscriptions the model sorted every row by
    # cost and led with the salary, labelled "(income)" and still wrong.
    assert all(s["merchant"] != "Payroll" for s in result["subscriptions"])
    payroll = next(s for s in result["also_recurring"] if s["merchant"] == "Payroll")
    assert payroll["type"] == "income"
    assert payroll["counts_toward_total"] is False
    assert "income" in payroll["not_a_subscription_because"]


def test_rent_is_not_offered_as_a_subscription(client):
    """The same mistake the page made, one layer down.

    Asked what the subscriptions cost, the assistant answered with the rent
    folded in — the tool handed it a list where a 1 250 EUR housing charge and a
    12 EUR streaming service sat under one heading with one total. It is a bill,
    and the row now says so in the field the model reads.
    """
    rent = cat_id(client, "Rent")
    ent = cat_id(client, "Entertainment")
    for month in range(3, 9):
        add_tx(client, f"2026-{month:02d}-01", "Vuokra Oy", rent, 1250.00)
        add_tx(client, f"2026-{month:02d}-05", "Spotify", ent, 11.99)

    result = ai_tools.list_subscriptions()

    assert [s["merchant"] for s in result["subscriptions"]] == ["Spotify"]
    vuokra = next(s for s in result["also_recurring"] if s["merchant"] == "Vuokra Oy")
    assert vuokra["kind"] == "bill"
    assert vuokra["counts_toward_total"] is False
    assert "bill" in vuokra["not_a_subscription_because"]
    # The headline is the subscription alone; 1 250 EUR of rent is nowhere near it.
    assert result["monthly_total"] < 100


def test_the_bills_carry_their_own_total(client):
    """Rule 2 again: a total the model would otherwise add up itself.

    The bills are out of the subscription figure and their rows are in
    `also_recurring`. Asked what the fixed costs come to, a model with only the
    rows would sum them, and it must never have to.
    """
    rent = cat_id(client, "Rent")
    for month in range(3, 9):
        add_tx(client, f"2026-{month:02d}-01", "Vuokra Oy", rent, 1250.00)

    result = ai_tools.list_subscriptions()
    assert result["bills_monthly_total_eur"] is not None
    assert result["bills_annual_total_eur"] is not None


# ── Totals the model must never work out itself ───────────────────────────

def test_monthly_summary_totals_the_period_itself(seeded):
    """Asked what it earned last year, the model added twelve figures up and
    was 705 € out. The sum belongs here, where it is arithmetic and not a guess.
    """
    result = ai_tools.monthly_summary(period="last_12_months")

    assert result["total_income"] == pytest.approx(
        sum(m["income"] for m in result["months"]))
    assert result["total_expense"] == pytest.approx(
        sum(m["expense"] for m in result["months"]))
    assert result["total_net_eur"] is not None
    assert result["total_income_eur"].endswith("€")


def test_annual_report_precomputes_the_year_on_year_change(seeded):
    """The euro strings used to be attached to keys the endpoint never sent, so
    the model quoted raw floats and subtracted them itself."""
    result = ai_tools.annual_report(year=2026)

    assert result["this_year"]["income_eur"].endswith("€")
    assert result["last_year"]["expense_eur"].endswith("€")

    change = result["change_vs_last_year"]
    assert change["income"] == pytest.approx(
        result["this_year"]["income"] - result["last_year"]["income"])
    assert change["income_eur"].endswith("€")
    assert change["expense_direction"] in ("up", "down", "flat")


def test_an_annual_report_says_which_year_it_is_about(seeded):
    """The panel reads the period of every lookup off this key and puts it on
    the summary line. An annual report showed a blank there — on the one answer
    whose whole meaning is which year it covers."""
    result = ai_tools.annual_report(year=2026)
    assert result["period"].startswith("2026")


def test_annual_report_compares_both_years_over_the_same_months(seeded):
    """A part-finished year against a full one reports a collapse that is
    really just the calendar."""
    result = ai_tools.annual_report(year=2026)
    assert result["compared_over"]
    assert result["previous_year"] == result["year"] - 1


# ── Net worth ─────────────────────────────────────────────────────────────

def test_net_worth_carries_a_euro_string_for_every_figure(client):
    """Including the change, whose key this tool used to guess wrong.

    It looked for "change" and "change_amount"; the endpoint sends
    "change_vs_prev". The figure went through as a bare float, which rule 2
    tells the model it may not quote.
    """
    res = client.post("/api/accounts", json={"name": "Savings", "type": "asset"})
    account_id = res.get_json()["id"]
    client.post(f"/api/accounts/{account_id}/balances",
                json={"as_of": "2026-07-01", "balance": 1000.0})
    client.post(f"/api/accounts/{account_id}/balances",
                json={"as_of": "2026-08-01", "balance": 1500.0})

    result = ai_tools.net_worth_summary()

    assert result["net_worth_eur"] == "1\xa0500\xa0€"
    assert result["assets_eur"] == "1\xa0500\xa0€"
    for key in ("net_worth", "assets", "liabilities", "change_since_last_month"):
        raw, formatted = result[key], result[f"{key}_eur"]
        assert (formatted is None) == (raw is None), f"{key} lost its euro string"

    savings = next(a for a in result["accounts"] if a["name"] == "Savings")
    assert savings["balance_eur"] == "1\xa0500\xa0€"
    # Ids and sort orders are not things to say out loud.
    assert "id" not in savings


def test_two_named_months_can_be_asked_for_outright(seeded):
    """The fix for a question no period could express.

    "Did I spend more in July than in June?" has no window that answers it —
    every period ends today. Given `last_2_months` to make it comfortable, the
    model asked for that, was handed July and August, and reported a figure for
    June anyway. So the months are named instead.
    """
    result = ai_tools.monthly_summary(months=["2026-05"])
    assert [m["month"] for m in result["months"]] == ["2026-05"]
    assert result["period"] == "2026-05"


def test_last_2_months_is_not_a_period(seeded):
    """It was added, it caused an invented figure, and it is not coming back."""
    assert "last_2_months" not in ai_tools.PERIODS
    assert "error" in ai_tools.run_tool("monthly_summary", {"period": "last_2_months"})


def test_search_hands_back_the_net_it_would_otherwise_be_asked_to_subtract(seeded):
    result = ai_tools.search_transactions(month="2026-05")
    assert result["sum_net"] == pytest.approx(
        result["sum_income"] - result["sum_expense"])
    assert result["sum_net_eur"] == "3\xa0067\xa0€"


def test_a_breakdown_states_the_direction_it_would_otherwise_be_guessed(client):
    """Given a month and its usual month, the model inverted about half of them.

    It filed Medical at 74 € against a usual 9 € under "saving money". Quoting a
    figure is a small model's strength; comparing two is not, so the comparison
    is made here.
    """
    groceries = cat_id(client, "Groceries")
    # Six months of history, then a month well above it.
    for month in range(2, 8):
        add_tx(client, f"2026-{month:02d}-10", "K-Market", groceries, 100.0)
    add_tx(client, "2026-08-10", "K-Market", groceries, 400.0)

    result = ai_tools.category_breakdown(month="2026-08")
    row = next(c for c in result["categories"] if c["category"] == "Groceries")

    assert row["usual_month"] == pytest.approx(100.0)
    assert row["direction"] == "above"
    assert row["vs_usual"] == pytest.approx(300.0)
    assert row["vs_usual_eur"] == "300\xa0€"
    assert row["reads_as"] == "above usual"


def test_ordinary_movement_reads_as_usual(client):
    """A wide band on purpose — at 10% everything is news and nothing is."""
    groceries = cat_id(client, "Groceries")
    for month in range(2, 8):
        add_tx(client, f"2026-{month:02d}-10", "K-Market", groceries, 100.0)
    add_tx(client, "2026-08-10", "K-Market", groceries, 108.0)

    row = next(c for c in ai_tools.category_breakdown(month="2026-08")["categories"]
               if c["category"] == "Groceries")
    assert row["direction"] == "above"
    assert row["reads_as"] == "as usual"


# ── Month names ───────────────────────────────────────────────────────────

def test_the_context_resolves_month_names_so_the_model_does_not(seeded):
    """"June" is calendar arithmetic, and that is what goes wrong.

    Asked what it spent in June the model never passed 2026-06 at all: it tried
    last_3_months, then last_6_months, then this_year, and reported a
    three-month total as one month's.
    """
    names = ai_tools.context_block()["months_by_name"]
    today = date.today()
    this_month = f"{today.year:04d}-{today.month:02d}"

    assert len(names) == 13
    # Newest first, so reading down finds the most recent one that has been.
    assert list(names.values())[0] == this_month
    assert all(len(key) == 7 and key[4] == "-" for key in names.values())
    # A label a person would type, mapped to what the tools want.
    label, key = next(iter(names.items()))
    assert key == this_month and label.endswith(str(today.year))


# ── The items inside a category ───────────────────────────────────────────

def test_a_single_month_carries_the_top_items_of_its_largest_categories(seeded):
    """"My biggest category and the top 3 things in it" is one lookup.

    As two it went wrong in a way that read as right: the model searched the
    whole month rather than inside the category, and listed the month's biggest
    charges under its biggest category's name.
    """
    result = ai_tools.category_breakdown(month="2026-05")
    top = result["categories"][0]

    assert top["category"] == "Groceries"
    assert [i["store"] for i in top["top_items"]] == ["K-Market", "S-Market"]
    assert top["top_items"][0]["amount_eur"] == "61\xa0€"


def test_a_period_carries_no_top_items(seeded):
    """They belong to a month. Over a range there is no usual month either."""
    result = ai_tools.category_breakdown(period="last_12_months")
    assert all("top_items" not in c for c in result["categories"])


# ── Reading a whole month ─────────────────────────────────────────────────

@pytest.fixture
def two_months(client):
    """One category over two months, moving in opposite directions."""
    groceries = cat_id(client, "Groceries")
    job = cat_id(client, "Job", "income")
    add_tx(client, "2026-04-10", "K-Market", groceries, 300.0)
    add_tx(client, "2026-05-10", "K-Market", groceries, 100.0)
    add_tx(client, "2026-05-11", "S-Market", groceries, 50.0)
    add_tx(client, "2026-05-25", "Payroll", job, 3000.0, "income")
    return client


def test_a_month_is_read_in_one_call(two_months):
    """The whole picture, so the model never has to decide what to fetch."""
    result = ai_tools.analyse_month(month="2026-05")

    assert result["month"] == "2026-05"
    assert result["compared_with"] == "2026-04"
    assert result["totals"]["expense_eur"] == "150\xa0€"
    assert result["totals"]["income_eur"] == "3\xa0000\xa0€"
    # Against the month before, with the direction already decided.
    assert result["totals"]["expense_vs_last_month"]["last_month_eur"] == "300\xa0€"
    assert result["totals"]["expense_vs_last_month"]["direction"] == "down"
    assert result["biggest_charges"][0]["store"] == "K-Market"


def test_each_category_says_which_way_it_moved_against_each_thing(two_months):
    """Two comparisons that disagree, and the model must not merge them.

    Given only the figures it did: Medical at 74 € against 335 € the month
    before came back as "spiked to 74 €, up from 335 €".
    """
    result = ai_tools.analyse_month(month="2026-05")
    row = next(c for c in result["categories"] if c["category"] == "Groceries")

    assert row["last_month_eur"] == "300\xa0€"
    assert row["vs_last_month_direction"] == "down"
    # Named, so neither direction can be attached to the wrong comparison.
    assert "direction" not in row
    assert "vs_usual_direction" in row or "usual_month_eur" not in row


def test_the_month_read_defaults_to_the_latest_one_holding_data(two_months):
    """Not this month: a month can be a day old and nearly empty."""
    assert ai_tools.analyse_month()["month"] == "2026-05"


def test_reading_a_month_drops_the_raw_floats(two_months):
    """It is the longest thing the model reads back, and the rules forbid it to
    do arithmetic on them anyway."""
    result = ai_tools.analyse_month(month="2026-05")
    for row in result["categories"] + result["biggest_charges"]:
        assert not {"total", "amount", "usual_month", "vs_usual"} & set(row)
