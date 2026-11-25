"""The planning projection, and the Balance figures it starts from.

The numbers are made up. Nothing here is a real account or a real salary.
"""

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from services.planning import (
    annuity_payment,
    default_assumptions,
    project,
    read_baseline,
    summarize,
)


def _assumptions(**overrides):
    base = {
        "age": 40, "retAge": 65, "endAge": 42,
        "income": 0, "expenses": 0, "rent": 0,
        "incG": 0, "expG": 0, "infl": 0,
        "startInv": 0, "retPre": 0, "retPost": 0,
        "retPct": 100, "pension": 0, "pensionAge": 67, "swr": 4,
    }
    base.update(overrides)
    return base


def _row(assumptions, events, age, start_year=2026):
    result = project(assumptions, events, start_year)
    found = [row for row in result["rows"] if row["age"] == age]
    assert found, f"no row at age {age}"
    return result, found[0]


def _cat(conn, uid, name, type_):
    return conn.execute(
        "SELECT id FROM categories WHERE user_id = %s AND name = %s AND type = %s",
        (uid, name, type_),
    ).fetchone()["id"]


def _tx(conn, uid, category_id, when, amount, type_):
    conn.execute(
        "INSERT INTO transactions (user_id, date, store, category_id, amount, type) "
        "VALUES (%s, %s, 'Example', %s, %s, %s)",
        (uid, when, category_id, amount, type_),
    )


# ── The loan payment ─────────────────────────────────────────────────

def test_annuity_is_the_same_payment_every_year_and_clears_the_loan():
    """90 000 at 4.5% over 15 years is the cottage in the event dialog.

    The yearly payment rounds to 8 380, and paying it fifteen times costs
    about 35 700 in interest. A zero rate is just the principal split evenly.
    """
    payment = annuity_payment(90000, 4.5, 15)
    assert round(payment) == 8380
    assert round(payment * 15 - 90000) == 35704

    balance = 90000.0
    for _ in range(15):
        interest = balance * 0.045
        principal = min(payment - interest, balance)
        balance -= principal
    assert balance == 0 or abs(balance) < 0.5

    assert annuity_payment(80000, 0, 10) == 8000
    assert annuity_payment(0, 5, 10) == 0


def test_a_zero_rate_loan_inside_a_projection_pays_down_to_nothing():
    assumptions = _assumptions(age=40, endAge=42, startInv=100000)
    events = [{
        "type": "custom", "on": True, "name": "Boat", "dir": "cost",
        "age": 40, "amount": 100000, "repeat": "once", "real": False,
        "loan": True, "downPct": 20, "rate": 0, "term": 2,
        "asset": False,
    }]
    result, first = _row(assumptions, events, 40)
    # 20 000 of own money, then half of the 80 000 loan, in the first year.
    assert first["big"] == 20000 + 40000
    assert first["other_debt"] == 40000
    _, last = _row(assumptions, events, 42)
    assert last["other_debt"] == 0
    assert result["rows"][2]["big"] == 0


# ── Inflation, retirement, and the gap before the pension ────────────

def test_todays_euros_divide_out_inflation_and_a_matching_raise_cancels_it():
    assumptions = _assumptions(
        age=40, endAge=41, income=1000, expenses=100,
        incG=10, expG=10, infl=10, swr=4,
    )
    _, later = _row(assumptions, [], 41)
    assert later["income"] == 12000 * 1.1
    assert later["living"] == 1200 * 1.1
    assert later["defl"] == 1.1
    assert later["income"] / later["defl"] == 12000
    assert later["living"] / later["defl"] == 1200


def test_the_years_between_retirement_and_the_pension_are_paid_from_savings():
    """Retire at 65, pension at 67. Those two years have no income, and the
    pension arrives in the third, raised with inflation when there is any.
    """
    assumptions = _assumptions(
        age=65, retAge=65, endAge=67, pensionAge=67, pension=1000,
        expenses=500, startInv=20000, infl=0,
    )
    result, first = _row(assumptions, [], 65)
    assert first["salary"] == 0
    assert first["living"] == 6000
    assert first["inv"] == 14000
    _, gap = _row(assumptions, [], 66)
    assert gap["salary"] == 0
    assert gap["inv"] == 8000
    _, pension = _row(assumptions, [], 67)
    assert pension["salary"] == 12000
    assert pension["inv"] == 14000
    assert result["ran_out"] is None

    inflated = _assumptions(
        age=65, retAge=65, endAge=67, pensionAge=67, pension=1000,
        expenses=0, infl=10,
    )
    _, at_pension = _row(inflated, [], 67)
    assert at_pension["salary"] == 12000 * (1.1 ** 2)


def test_spending_in_retirement_is_a_percentage_of_what_came_before():
    assumptions = _assumptions(
        age=64, retAge=65, endAge=65, expenses=1000, retPct=80, income=0,
    )
    _, working = _row(assumptions, [], 64)
    _, retired = _row(assumptions, [], 65)
    assert working["living"] == 12000
    assert retired["living"] == 9600


def test_a_negative_balance_earns_no_return():
    """Returns apply only while there is still money. A shortfall does not
    get charged interest, and it does not shrink by itself either.
    """
    assumptions = _assumptions(
        age=40, endAge=41, expenses=1000, startInv=0, retPre=10,
    )
    _, first = _row(assumptions, [], 40)
    assert first["inv"] == -12000
    _, second = _row(assumptions, [], 41)
    assert second["inv"] == -24000
    assert second["net"] == -12000


# ── Financial independence and running out ───────────────────────────

def test_financial_independence_is_the_first_age_before_retirement():
    # 12 000 of spending at a 4% withdrawal needs 300 000 invested.
    assumptions = _assumptions(
        age=40, retAge=65, endAge=42, expenses=1000, startInv=400000, swr=4,
    )
    result, first = _row(assumptions, [], 40)
    assert first["fi_target"] == 300000
    assert first["inv"] == 388000
    assert result["fi_age"] == 40

    short = _assumptions(
        age=40, retAge=65, endAge=41, expenses=1000, startInv=100000, swr=4,
    )
    short_result, _ = _row(short, [], 40)
    assert short_result["fi_age"] is None

    already_retired = _assumptions(
        age=70, retAge=65, endAge=71, expenses=1000, startInv=1000000, swr=4,
    )
    retired_result, _ = _row(already_retired, [], 70)
    assert retired_result["fi_age"] is None


def test_money_lasts_until_the_first_year_investments_go_negative():
    assumptions = _assumptions(
        age=40, endAge=43, expenses=1000, startInv=18000, swr=4,
    )
    result, _ = _row(assumptions, [], 40)
    # 18 000 becomes 6 000, then goes negative at 41.
    assert [row["inv"] for row in result["rows"]] == [6000, -6000, -18000, -30000]
    assert result["ran_out"] == 41

    safe = _assumptions(age=40, endAge=50, startInv=1000, expenses=0, income=0)
    safe_result, _ = _row(safe, [], 40)
    assert safe_result["ran_out"] is None


def test_summary_cards_follow_the_unit_and_quote_the_pension_only_once_it_has_started():
    assumptions = _assumptions(
        age=65, retAge=65, endAge=65, pensionAge=67, pension=1900,
        expenses=0, startInv=120000, swr=4, infl=0,
    )
    result = project(assumptions, [], 2026)
    cards = summarize(result, assumptions, "real")
    # No pension yet, so the card is the withdrawal alone: 4% of 120 000 / 12.
    assert cards["pension_already"] is False
    assert cards["sustainable_monthly"] == 400
    assert cards["retirement_year"] == 2026
    assert cards["ran_out"] is None

    started = _assumptions(
        age=65, retAge=65, endAge=65, pensionAge=65, pension=1000,
        expenses=1000, startInv=0, swr=4, infl=0,
    )
    started_cards = summarize(project(started, [], 2026), started, "nominal")
    assert started_cards["pension_already"] is True
    assert started_cards["sustainable_monthly"] == 1000


# ── A home ───────────────────────────────────────────────────────────

def test_buying_a_home_takes_the_cash_up_front_and_stops_the_rent():
    """Price 100 000, 20% down, 2% transfer tax, no interest, two-year loan.

    The rent was the whole of spending, so living costs go to zero from the
    purchase year. The down payment and the tax leave the portfolio; the
    loan principal becomes equity instead.
    """
    assumptions = _assumptions(
        age=30, endAge=31, expenses=500, rent=500, startInv=100000,
    )
    home = {
        "type": "house", "on": True, "name": "Apartment", "age": 30,
        "price": 100000, "downPct": 20, "tax": 2, "rate": 0, "term": 2,
        "costPct": 0, "appr": 0, "replacesRent": True,
    }
    result, bought = _row(assumptions, [home], 30)
    assert bought["living"] == 0
    assert bought["housing"] == 22000 + 40000
    assert bought["home_value"] == 100000
    assert bought["mortgage"] == 40000
    assert bought["equity"] == 60000
    assert bought["inv"] == 100000 - 62000
    assert "Apartment" in bought["marks"]

    _, next_year = _row(assumptions, [home], 31)
    assert next_year["mortgage"] == 0
    assert next_year["home_value"] == 100000
    assert next_year["housing"] == 40000
    assert result["rows"][0]["nw"] == (100000 - 62000) + 60000


def test_upkeep_rises_with_inflation_and_the_home_grows_at_its_own_rate():
    assumptions = _assumptions(age=30, endAge=31, infl=10)
    home = {
        "type": "house", "on": True, "name": "Apartment", "age": 30,
        "price": 100000, "downPct": 100, "tax": 0, "rate": 0, "term": 1,
        "costPct": 1, "appr": 5, "replacesRent": False,
    }
    _, bought = _row(assumptions, [home], 30)
    # Paying the whole price up front, plus 1% upkeep. No loan is left.
    assert bought["housing"] == 100000 + 1000
    assert bought["mortgage"] == 0
    _, later = _row(assumptions, [home], 31)
    assert later["home_value"] == 100000 * 1.05
    assert later["housing"] == 1000 * 1.1


def test_a_home_that_is_switched_off_changes_nothing():
    assumptions = _assumptions(age=30, endAge=30, rent=500, expenses=500, startInv=1000)
    home = {
        "type": "house", "on": False, "name": "Apartment", "age": 30,
        "price": 100000, "downPct": 20, "tax": 0, "rate": 0, "term": 10,
        "costPct": 0, "appr": 0, "replacesRent": True,
    }
    _, row = _row(assumptions, [home], 30)
    assert row["living"] == 6000
    assert row["housing"] == 0
    assert row["home_value"] == 0


# ── The other event types, including a custom loan and asset ─────────

def test_a_custom_purchase_keeps_its_value_and_its_debt_in_net_worth():
    """Pay 20% yourself, borrow the rest over two years at no interest, and
    keep 80% of the price as an asset that grows 10% a year.
    """
    assumptions = _assumptions(age=45, endAge=46, startInv=50000)
    cottage = {
        "type": "custom", "on": True, "name": "Summer cottage",
        "dir": "cost", "age": 45, "amount": 100000, "repeat": "once",
        "real": True, "loan": True, "downPct": 20, "rate": 0, "term": 2,
        "asset": True, "assetPct": 80, "assetG": 10, "running": 1000,
    }
    _, bought = _row(assumptions, [cottage], 45)
    assert bought["big"] == 20000 + 40000 + 1000
    assert bought["other_assets"] == 80000
    assert bought["other_debt"] == 40000
    assert bought["other_net"] == 40000
    assert bought["inv"] == 50000 - 61000
    assert bought["nw"] == bought["inv"] + 80000 - 40000

    _, later = _row(assumptions, [cottage], 46)
    assert later["other_assets"] == 88000
    assert later["other_debt"] == 0
    assert later["big"] == 40000 + 1000
    # A one-off is marked on the year it happens, and not again the year after.
    assert "Summer cottage" not in later["marks"]


def test_replacing_a_custom_asset_sells_the_old_one_at_its_current_value():
    assumptions = _assumptions(age=45, endAge=47, startInv=0)
    boat = {
        "type": "custom", "on": True, "name": "Boat", "dir": "cost",
        "age": 45, "amount": 10000, "repeat": "every", "every": 2, "until": 47,
        "real": False, "loan": False, "asset": True, "assetPct": 100,
        "assetG": 0, "running": 0,
    }
    result, first = _row(assumptions, [boat], 45)
    assert first["big"] == 10000
    assert first["other_assets"] == 10000
    _, kept = _row(assumptions, [boat], 46)
    assert kept["big"] == 0
    assert kept["other_assets"] == 10000
    _, replaced = _row(assumptions, [boat], 47)
    # The old boat is sold for 10 000 and the new one costs 10 000.
    assert replaced["inflow"] == 10000
    assert replaced["big"] == 10000
    assert replaced["net"] == 0
    assert replaced["other_assets"] == 10000
    assert result["rows"][2]["minor"] == ["Boat"]


def test_a_custom_amount_stays_fixed_when_it_is_not_in_todays_euros():
    assumptions = _assumptions(age=40, endAge=41, infl=10, startInv=0)
    gift = {
        "type": "custom", "on": True, "name": "Side work", "dir": "income",
        "age": 40, "amount": 5000, "repeat": "yearly", "years": 2, "real": False,
    }
    _, first = _row(assumptions, [gift], 40)
    _, second = _row(assumptions, [gift], 41)
    assert first["inflow"] == 5000
    assert second["inflow"] == 5000

    rising = dict(gift, real=True)
    _, later = _row(assumptions, [rising], 41)
    assert later["inflow"] == 5000 * 1.1


def test_the_other_event_types_hit_the_year_they_say_they_do():
    assumptions = _assumptions(age=30, endAge=34, income=2000, startInv=100000, retPre=0)
    events = [
        {"type": "car", "on": True, "name": "Car", "age": 30, "amount": 8000, "every": 2, "until": 34},
        {"type": "child", "on": True, "name": "First child", "age": 31, "amount": 1000, "years": 2},
        {"type": "sabbatical", "on": True, "name": "Time off", "age": 32, "years": 1, "pct": 50},
        {"type": "windfall", "on": True, "name": "Inheritance", "age": 33, "amount": 20000},
        {"type": "extra", "on": True, "name": "Side income", "age": 33, "amount": 1000, "years": 1},
        {"type": "oneoff", "on": True, "name": "Wedding", "age": 34, "amount": 5000, "years": 1},
        {"type": "shock", "on": True, "name": "Market crash", "age": 30, "pct": 25},
    ]
    result = project(assumptions, events, 2026)
    by_age = {row["age"]: row for row in result["rows"]}

    # The crash is taken before the (zero) return. The car is bought the same year.
    # The crash lands before the return. Salary still comes in, and the car goes out.
    assert by_age[30]["inv"] == 100000 * 0.75 + (24000 - 8000)
    assert by_age[30]["big"] == 8000
    assert "Car" in by_age[30]["marks"] and "Market crash" in by_age[30]["marks"]

    assert by_age[31]["big"] == 1000  # the child's first year, no car
    assert by_age[32]["big"] == 8000 + 1000  # car again, child again
    assert "Car" in by_age[32]["minor"]
    assert by_age[32]["salary"] == 24000 * 0.5

    assert by_age[33]["inflow"] == 20000 + 1000
    assert by_age[33]["big"] == 0  # the child was two years, 31 and 32
    assert by_age[34]["big"] == 8000 + 5000


def test_an_event_that_is_off_is_kept_out_of_the_numbers():
    assumptions = _assumptions(age=40, endAge=40, income=1000, startInv=1000)
    crash = {"type": "shock", "on": False, "name": "Market crash", "age": 40, "pct": 50}
    _, row = _row(assumptions, [crash], 40)
    assert row["inv"] == 1000 + 12000
    assert row["marks"] == []


# ── What Balance fills in ────────────────────────────────────────────

def test_baseline_averages_a_full_twelve_months_and_uses_net_worth(user_conn):
    conn, uid = user_conn
    today = date(2026, 11, 25)
    job = _cat(conn, uid, "Job", "income")
    rent = _cat(conn, uid, "Rent", "expense")
    groceries = _cat(conn, uid, "Groceries", "expense")
    investments = _cat(conn, uid, "Investments", "expense")
    for delta in range(-11, 1):
        when = _month(today, delta).isoformat()
        _tx(conn, uid, job, when, 3800, "income")
        _tx(conn, uid, rent, when, 950, "expense")
        _tx(conn, uid, groceries, when, 1550, "expense")
        _tx(conn, uid, investments, when, 500, "expense")
    account = conn.execute(
        "INSERT INTO accounts (user_id, name, type) VALUES (%s, 'Savings', 'asset') RETURNING id",
        (uid,),
    ).fetchone()["id"]
    loan = conn.execute(
        "INSERT INTO accounts (user_id, name, type) VALUES (%s, 'Car loan', 'liability') RETURNING id",
        (uid,),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s, %s, %s, 40000)",
        (uid, account, today.isoformat()),
    )
    conn.execute(
        "INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s, %s, %s, 15000)",
        (uid, loan, today.isoformat()),
    )

    baseline = read_baseline(conn, uid, today)
    assert baseline["coverage"] == "full"
    assert baseline["months_averaged"] == 12
    assert baseline["income"] == 3800
    assert baseline["expenses"] == 2500
    assert baseline["rent"] == 950
    # The 500 a month sent to investments is not spending.
    assert baseline["transfers_excluded"] == 500
    assert baseline["start_inv"] == 25000
    assert baseline["has_accounts"] is True
    assert baseline["has_transactions"] is True

    defaults = default_assumptions(baseline)
    assert defaults["income"] == 3800
    assert defaults["startInv"] == 25000
    assert defaults["age"] == 30
    assert defaults["pension"] == 0


def test_a_short_history_is_averaged_over_the_months_on_record(user_conn):
    """Two months of tracking must not be divided by twelve."""
    conn, uid = user_conn
    today = date(2026, 11, 25)
    job = _cat(conn, uid, "Job", "income")
    _tx(conn, uid, job, "2026-10-02", 3000, "income")
    _tx(conn, uid, job, "2026-11-02", 3000, "income")

    baseline = read_baseline(conn, uid, today)
    assert baseline["coverage"] == "partial"
    assert baseline["months_averaged"] == 2
    assert baseline["income"] == 3000
    assert baseline["start_inv"] == 0
    assert baseline["has_accounts"] is False


def test_empty_months_count_as_zero_once_the_history_covers_the_window(user_conn):
    conn, uid = user_conn
    today = date(2026, 11, 25)
    job = _cat(conn, uid, "Job", "income")
    _tx(conn, uid, job, "2024-01-15", 100, "income")
    _tx(conn, uid, job, "2026-11-02", 12000, "income")

    baseline = read_baseline(conn, uid, today)
    assert baseline["coverage"] == "full"
    assert baseline["months_averaged"] == 12
    assert baseline["income"] == 1000


def test_no_data_at_all_stays_at_zero(user_conn):
    conn, uid = user_conn
    baseline = read_baseline(conn, uid, date(2026, 11, 25))
    assert baseline["coverage"] == "none"
    assert baseline["months_averaged"] == 0
    assert baseline["income"] == baseline["expenses"] == baseline["rent"] == 0
    assert baseline["start_inv"] == 0
    assert baseline["has_transactions"] is False
    assert baseline["has_accounts"] is False


def test_the_page_uses_the_same_projection_as_this_module():
    """static/js/planning-math.js is the copy the tab runs. When Node is on
    the machine, the two have to agree on every row, not just the shape.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    cases = [
        {"assumptions": _assumptions(age=40, endAge=42, startInv=100000),
         "events": [{"type": "custom", "on": True, "name": "Boat", "dir": "cost",
                     "age": 40, "amount": 100000, "repeat": "once", "real": False,
                     "loan": True, "downPct": 20, "rate": 0, "term": 2, "asset": False}],
         "start_year": 2026, "unit": "real"},
        {"assumptions": _assumptions(age=40, endAge=41, income=1000, expenses=100,
                                     incG=10, expG=10, infl=10),
         "events": [], "start_year": 2026, "unit": "nominal"},
        {"assumptions": _assumptions(age=65, retAge=65, endAge=67, pensionAge=67,
                                     pension=1000, expenses=500, startInv=20000),
         "events": [], "start_year": 2026, "unit": "real"},
        {"assumptions": _assumptions(age=30, endAge=31, expenses=500, rent=500, startInv=100000),
         "events": [{"type": "house", "on": True, "name": "Apartment", "age": 30,
                     "price": 100000, "downPct": 20, "tax": 2, "rate": 0, "term": 2,
                     "costPct": 1, "appr": 5, "replacesRent": True}],
         "start_year": 2026, "unit": "real"},
        {"assumptions": _assumptions(age=45, endAge=47, startInv=50000, infl=10),
         "events": [{"type": "custom", "on": True, "name": "Summer cottage", "dir": "cost",
                     "age": 45, "amount": 120000, "repeat": "every", "every": 2, "until": 47,
                     "real": True, "loan": True, "downPct": 25, "rate": 4.5, "term": 15,
                     "asset": True, "assetPct": 100, "assetG": 1, "running": 3000}],
         "start_year": 2026, "unit": "real"},
        {"assumptions": _assumptions(age=30, endAge=34, income=2000, startInv=100000),
         "events": [
             {"type": "car", "on": True, "name": "Car", "age": 30, "amount": 8000, "every": 2, "until": 34},
             {"type": "shock", "on": True, "name": "Crash", "age": 30, "pct": 25},
             {"type": "child", "on": True, "name": "First child", "age": 31, "amount": 1000, "years": 2},
             {"type": "windfall", "on": True, "name": "Inheritance", "age": 33, "amount": 20000},
         ],
         "start_year": 2026, "unit": "real"},
    ]
    expected = []
    for case in cases:
        result = project(case["assumptions"], case["events"], case["start_year"])
        expected.append({
            "result": result,
            "summary": summarize(result, case["assumptions"], case["unit"]),
        })
    script = r"""
const fs = require("fs");
const vm = require("vm");
const context = vm.createContext();
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const out = cases.map(c => {
  const result = context.planningProject(c.assumptions, c.events, c.start_year);
  return { result, summary: context.planningSummarize(result, c.assumptions, c.unit || "real") };
});
process.stdout.write(JSON.stringify(out));
"""
    math_file = Path(__file__).resolve().parents[1] / "static" / "js" / "planning-math.js"
    run = subprocess.run(
        [node, "-e", script, str(math_file)],
        input=json.dumps(cases), text=True, capture_output=True, check=False,
    )
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    assert len(got) == len(expected)
    for index, (left, right) in enumerate(zip(expected, got)):
        _same(left, right, f"case {index}")


def _same(left, right, path):
    if isinstance(left, dict):
        assert isinstance(right, dict) and set(left) == set(right), path
        for key in left:
            _same(left[key], right[key], f"{path}.{key}")
        return
    if isinstance(left, list):
        assert isinstance(right, list) and len(left) == len(right), path
        for index, (item, other) in enumerate(zip(left, right)):
            _same(item, other, f"{path}[{index}]")
        return
    if isinstance(left, float) or isinstance(right, float):
        assert left == pytest.approx(right, rel=1e-9, abs=1e-6), f"{path}: {left} != {right}"
        return
    assert left == right, f"{path}: {left!r} != {right!r}"


def _month(today, delta):
    index = today.year * 12 + (today.month - 1) + delta
    year, month0 = divmod(index, 12)
    return date(year, month0 + 1, 1)
