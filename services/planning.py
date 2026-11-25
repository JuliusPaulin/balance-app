"""Long-term cash flow for the Planning tab.

The browser runs the same rules in ``static/js/planning-math.js``. The two
have to stay in step: the tests check this module and, when Node is
installed, that the script returns the same rows.

One year, in order:

- Income before retirement grows at the income-growth rate. From retirement
  it is the pension, raised with inflation, and only once pension age is
  reached. The years between retirement and the pension come out of investments.
- Spending grows at the spending-growth rate and, after retirement, is
  multiplied by the retirement percentage. Rent is a part of that spending.
- A home purchase pays the down payment and the transfer tax out of
  investments that year, then a fixed annuity until the loan is gone. Upkeep
  rises with inflation. The home's value grows at its own rate. When the
  purchase stops the rent, that rent leaves spending from then on.
- A custom cost can be financed the same way, and can keep a value that
  counts in net worth. Replacing an "every N years" item sells the old one
  at whatever it is worth that year. Each repeat gets its own loan.
- A market crash is taken off the investments first. The year's return is
  then applied, but only when the balance is still positive, and the net
  cash flow is added after that.
- Net worth is investments + home value + other assets − the mortgage −
  other loans.
- Today's euros are the future amount divided by ``(1 + inflation) ^ years``.

The one-off down payment is cash out of the portfolio that year. It is not
part of the financial-independence target, which is the ongoing living and
housing cost divided by the safe withdrawal rate.
"""

from __future__ import annotations

import math
from datetime import date

from services.networth import summary as networth_summary

# What a new plan starts from. Age is not in the database, so 30 is a
# placeholder the user edits. The pension is 0 rather than a guessed income:
# the four "today" figures are overwritten from Balance.
ASSUMPTION_DEFAULTS = {
    "age": 30,
    "retAge": 65,
    "endAge": 90,
    "income": 0,
    "expenses": 0,
    "rent": 0,
    "incG": 3.0,
    "expG": 2.0,
    "infl": 2.0,
    "startInv": 0,
    "retPre": 6.0,
    "retPost": 4.0,
    "retPct": 80,
    "pension": 0,
    "pensionAge": 67,
    "swr": 3.5,
}

# Wider than the sliders. A number typed past the slider still has to save;
# a number that cannot be a percentage or an amount does not.
_BOUNDS = {
    "age": (0, 120),
    "retAge": (0, 120),
    "endAge": (0, 130),
    "income": (-1e8, 1e8),
    "expenses": (-1e8, 1e8),
    "rent": (-1e8, 1e8),
    "startInv": (-1e12, 1e12),
    "incG": (-50, 50),
    "expG": (-50, 50),
    "infl": (-20, 50),
    "retPre": (-50, 50),
    "retPost": (-50, 50),
    "retPct": (0, 300),
    "pension": (-1e8, 1e8),
    "pensionAge": (0, 120),
    "swr": (0, 30),
}

EVENT_TYPES = (
    "house", "car", "child", "sabbatical", "windfall",
    "extra", "oneoff", "shock", "custom",
)
_MAX_SCENARIOS = 12
_MAX_EVENTS = 80
_MAX_YEARS = 200


def annuity_payment(principal, annual_rate_pct, years):
    """The fixed yearly payment on a loan: the same amount every year.

    ``annual_rate_pct`` is the nominal yearly rate, so 3.5 means 3.5%. A zero
    rate is the principal split evenly across the term. The term is at least
    one year, matching ``Math.max(1, term)`` in the page.
    """
    try:
        principal = float(principal)
        rate = float(annual_rate_pct) / 100.0
        term = float(years)
    except (TypeError, ValueError):
        return 0.0
    if not _finite(principal) or not _finite(rate) or not _finite(term):
        return 0.0
    term = max(1.0, term)
    if principal == 0:
        return 0.0
    if rate == 0:
        return principal / term
    return principal * rate / (1 - (1 + rate) ** (-term))


def project(assumptions, events, start_year):
    """Project one scenario from the current age to the plan's last year.

    ``events`` that are not ``on`` are ignored. Returns ``rows``, ``ran_out``
    (the first age investments go negative, or None) and ``fi_age`` (the first
    age, before retirement, investments could pay that year's living and
    housing costs, or None).
    """
    assumptions = assumptions or {}
    events = list(events or [])
    age0 = _num(assumptions, "age")
    end = max(_num(assumptions, "endAge"), age0 + 1)
    inv = _num(assumptions, "startInv")
    ran_out = None

    enabled = [e for e in events if isinstance(e, dict) and e.get("on")]
    customs = []
    homes = []
    for event in enabled:
        if event.get("type") == "custom":
            custom = dict(event)
            custom["loans"] = []
            custom["assets"] = []
            customs.append(custom)
        elif event.get("type") == "house":
            home = dict(event)
            home["bought"] = False
            home["bal"] = 0.0
            home["value"] = 0.0
            homes.append(home)

    rows = []
    age = age0
    steps = 0
    while age <= end + 1e-9 and steps < _MAX_YEARS:
        steps += 1
        t = age - age0
        inflation = _num(assumptions, "infl") / 100
        defl = (1 + inflation) ** t
        retired = age >= _num(assumptions, "retAge")
        if retired:
            income = (
                _num(assumptions, "pension") * 12 * defl
                if age >= _num(assumptions, "pensionAge") else 0.0
            )
        else:
            income = _num(assumptions, "income") * 12 * (1 + _num(assumptions, "incG") / 100) ** t
        scale = _num(assumptions, "retPct") / 100 if retired else 1.0
        growth = (1 + _num(assumptions, "expG") / 100) ** t
        living = _num(assumptions, "expenses") * 12 * growth * scale
        rent_now = _num(assumptions, "rent") * 12 * growth * scale
        big = 0.0
        inflow = 0.0
        shock = 0.0
        marks = []
        minor = []

        for event in enabled:
            kind = event.get("type")
            event_age = _num(event, "age")
            if kind not in ("house", "custom") and age == event_age:
                marks.append(event.get("name") or "")
            years = max(1.0, _js_or(event.get("years"), 1))
            in_range = event_age <= age < event_age + years
            if kind == "sabbatical":
                if in_range and not retired:
                    income *= 1 - _num(event, "pct") / 100
            elif kind in ("child", "oneoff"):
                if in_range:
                    big += _num(event, "amount") * defl
            elif kind == "extra":
                if in_range:
                    inflow += _num(event, "amount") * defl
            elif kind == "windfall":
                if age == event_age:
                    inflow += _num(event, "amount") * defl
            elif kind == "car":
                every = _js_or(event.get("every"), 0)
                if every > 0:
                    hit = (
                        age >= event_age
                        and (age - event_age) % every == 0
                        and age <= _js_or(event.get("until"), end)
                    )
                else:
                    hit = age == event_age
                if hit:
                    big += _num(event, "amount") * defl
                    if age != event_age:
                        minor.append(event.get("name") or "")
            elif kind == "shock":
                if age == event_age:
                    shock = _num(event, "pct") / 100

        other_assets = 0.0
        other_debt = 0.0
        for custom in customs:
            every = max(1.0, _js_or(custom.get("every"), 1))
            years = max(1.0, _js_or(custom.get("years"), 1))
            event_age = _num(custom, "age")
            repeat = custom.get("repeat")
            if repeat == "yearly":
                hit = event_age <= age < event_age + years
            elif repeat == "every":
                hit = (
                    age >= event_age
                    and (age - event_age) % every == 0
                    and age <= _js_or(custom.get("until"), end)
                )
            else:
                hit = age == event_age
            factor = defl if custom.get("real") else 1.0
            growth_rate = _js_or(custom.get("assetG"), 0.0) / 100
            for asset in custom["assets"]:
                asset["value"] *= 1 + growth_rate
            if hit:
                if age == event_age:
                    marks.append(custom.get("name") or "")
                elif repeat == "every":
                    minor.append(custom.get("name") or "")
                amount = _num(custom, "amount") * factor
                if custom.get("dir") == "income":
                    inflow += amount
                else:
                    if custom.get("loan"):
                        down = _num(custom, "downPct")
                        rate_pct = _num(custom, "rate")
                        term = max(1.0, _num(custom, "term"))
                        principal = amount * (1 - down / 100)
                        big += amount * down / 100
                        custom["loans"].append({
                            "bal": principal,
                            "r": rate_pct / 100,
                            "pmt": annuity_payment(principal, rate_pct, term),
                        })
                    else:
                        big += amount
                    if custom.get("asset"):
                        if repeat == "every" and custom["assets"]:
                            inflow += sum(asset["value"] for asset in custom["assets"])
                            custom["assets"] = []
                        custom["assets"].append({
                            "value": amount * _num(custom, "assetPct") / 100,
                        })
            big += _pay_down(custom["loans"])
            running = _num(custom, "running")
            if custom.get("dir") != "income" and custom.get("asset") and custom["assets"] and running:
                big += running * factor
            other_assets += sum(asset["value"] for asset in custom["assets"])
            other_debt += sum(max(0.0, loan["bal"]) for loan in custom["loans"])

        housing = 0.0
        housing_upfront = 0.0
        equity = 0.0
        mortgage = 0.0
        home_value = 0.0
        for home in homes:
            if home["bought"]:
                home["value"] *= 1 + _num(home, "appr") / 100
            if age == _num(home, "age"):
                price = _num(home, "price") * defl
                down = _num(home, "downPct")
                rate_pct = _num(home, "rate")
                term = max(1.0, _num(home, "term"))
                home["bought"] = True
                home["value"] = price
                home["price_nom"] = price
                home["bal"] = price * (1 - down / 100)
                home["r"] = rate_pct / 100
                home["n"] = term
                home["pmt"] = annuity_payment(home["bal"], rate_pct, term)
                housing_upfront += price * (down + _num(home, "tax")) / 100
                marks.append(home.get("name") or "")
            if home["bought"]:
                housing += _pay_down([home])
                years_owned = age - _num(home, "age")
                housing += home.get("price_nom", 0.0) * _num(home, "costPct") / 100 * (
                    (1 + inflation) ** years_owned
                )
                if home.get("replacesRent"):
                    cut = min(rent_now, living)
                    living -= cut
                    rent_now -= cut
                equity += home["value"] - home["bal"]
                mortgage += max(0.0, home["bal"])
                home_value += home["value"]

        spend = living + housing + housing_upfront + big
        net = income + inflow - spend
        if shock and inv > 0:
            inv *= 1 - shock
        return_rate = (
            _num(assumptions, "retPost") if retired else _num(assumptions, "retPre")
        ) / 100
        inv = (inv * (1 + return_rate) if inv > 0 else inv) + net
        if inv < 0 and ran_out is None:
            ran_out = age
        swr = _num(assumptions, "swr") / 100
        fi_target = (living + housing) / swr if swr > 0 else float("inf")
        year = int(start_year) + t
        rows.append({
            "age": age,
            "year": int(year) if year == int(year) else year,
            "defl": defl,
            "retired": retired,
            "income": income + inflow,
            "salary": income,
            "inflow": inflow,
            "living": living,
            "housing": housing + housing_upfront,
            "big": big,
            "spend": spend,
            "net": net,
            "inv": inv,
            "equity": equity,
            "mortgage": mortgage,
            "home_value": home_value,
            "other_assets": other_assets,
            "other_debt": other_debt,
            "other_net": other_assets - other_debt,
            "loans": mortgage + other_debt,
            "nw": inv + equity + other_assets - other_debt,
            "fi_target": fi_target,
            "marks": marks,
            "minor": minor,
        })
        age += 1

    fi_age = None
    for row in rows:
        if row["inv"] >= row["fi_target"] and row["inv"] > 0 and not row["retired"]:
            fi_age = row["age"]
            break
    return {"rows": rows, "ran_out": ran_out, "fi_age": fi_age}


def summarize(result, assumptions, unit="real"):
    """The six summary cards, in the selected unit (``real`` or ``nominal``)."""
    rows = result["rows"]
    retirement_age = _num(assumptions, "retAge")
    ret = next((row for row in rows if row["age"] == retirement_age), rows[-1])
    first = rows[0]
    salary = first["salary"]
    expenses_year = _num(assumptions, "expenses") * 12
    savings_rate = (salary - expenses_year) / salary * 100 if salary > 0 else 0.0
    pension_now = _num(assumptions, "pensionAge") <= retirement_age
    pension_year = _num(assumptions, "pension") * 12 * ret["defl"] if pension_now else 0.0
    sustainable = (max(0.0, ret["inv"]) * _num(assumptions, "swr") / 100 + pension_year) / 12

    def _in_unit(row, value):
        return value / row["defl"] if unit == "real" else value

    peak = rows[0]
    for row in rows[1:]:
        if _in_unit(row, row["nw"]) > _in_unit(peak, peak["nw"]):
            peak = row
    monthly = sustainable / ret["defl"] if unit == "real" else sustainable
    return {
        "net_worth_at_retirement": _in_unit(ret, ret["nw"]),
        "retirement_year": ret["year"],
        "fi_age": result["fi_age"],
        "ran_out": result["ran_out"],
        "sustainable_monthly": monthly,
        "pension_already": pension_now,
        "savings_rate": savings_rate,
        "monthly_surplus": _num(assumptions, "income") - _num(assumptions, "expenses"),
        "peak_nw": _in_unit(peak, peak["nw"]),
        "peak_age": peak["age"],
    }


def default_assumptions(baseline):
    """Slider defaults, with the four Balance figures written over the zeros."""
    assumptions = dict(ASSUMPTION_DEFAULTS)
    assumptions["income"] = baseline["income"]
    assumptions["expenses"] = baseline["expenses"]
    assumptions["rent"] = baseline["rent"]
    assumptions["startInv"] = baseline["start_inv"]
    return assumptions


def read_baseline(conn, user_id, today=None):
    """The Planning tab's starting point, read from this user's own data.

    Income, spending and rent are a monthly average. Spending is every
    expense except the Investments category: that money is a transfer into
    the portfolio, and the projection invests whatever is left over, so
    counting it as spending would drop it twice. Rent is the part of spending
    in the Rent category.

    A history that already covers the whole window counts empty months as
    zero and divides by 12. A history that starts inside the window is
    divided by the months actually on record, so two months of tracking do
    not look like a twelfth of a year. Nothing on record stays at zero.

    Investments and savings start at the net worth the Net Worth tab shows.
    """
    today = today or date.today()
    window_start = _shift_month(date(today.year, today.month, 1), -11)
    rows = conn.execute(
        """
        SELECT t.date AS date, t.amount AS amount, t.type AS type, c.name AS category
        FROM transactions t
        JOIN categories c ON c.id = t.category_id
        WHERE t.user_id = %s AND t.date >= %s AND t.date <= %s
        """,
        (user_id, window_start.isoformat(), today.isoformat()),
    ).fetchall()
    first = conn.execute(
        "SELECT MIN(date) AS first_date FROM transactions WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    first_date = first["first_date"] if first else None

    income = expenses = rent = transfers = 0.0
    for row in rows:
        amount = float(row["amount"] or 0)
        if row["type"] == "income":
            income += amount
            continue
        if row["type"] != "expense":
            continue
        if (row["category"] or "").casefold() == "investments":
            transfers += amount
            continue
        expenses += amount
        if (row["category"] or "").casefold() == "rent":
            rent += amount

    if not first_date:
        coverage, divisor = "none", 0
    elif first_date < window_start.isoformat():
        coverage, divisor = "full", 12
    else:
        earliest = min(row["date"] for row in rows) if rows else first_date
        start = date.fromisoformat(earliest)
        divisor = _months_between(
            date(start.year, start.month, 1),
            date(today.year, today.month, 1),
        )
        coverage = "full" if divisor >= 12 else "partial"

    snap = networth_summary(conn, user_id, today)
    return {
        "income": _euros(income, divisor),
        "expenses": _euros(expenses, divisor),
        "rent": _euros(rent, divisor),
        "start_inv": int(round(snap["net_worth"])),
        "coverage": coverage,
        "months_averaged": divisor,
        "transfers_excluded": _euros(transfers, divisor),
        "net_worth": snap["net_worth"],
        "assets": snap["assets"],
        "liabilities": snap["liabilities"],
        "has_accounts": bool(snap["accounts"]),
        "has_transactions": first_date is not None,
    }


def clean_plan(payload):
    """Validate a saved plan. Returns ``(plan, None)`` or ``(None, error)``."""
    if not isinstance(payload, dict):
        return None, "The plan has to be an object."
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        return None, "The plan needs at least one scenario."
    if len(scenarios) > _MAX_SCENARIOS:
        return None, f"A plan can hold up to {_MAX_SCENARIOS} scenarios."

    cleaned = []
    seen_ids = set()
    next_id = 1
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            return None, "A scenario has to be an object."
        name = scenario.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
            return None, "A scenario needs a name of up to 40 characters."
        assumptions, error = _clean_assumptions(scenario.get("assumptions"))
        if error:
            return None, error
        events = scenario.get("events", [])
        if not isinstance(events, list):
            return None, "Events have to be a list."
        if len(events) > _MAX_EVENTS:
            return None, f"A scenario can hold up to {_MAX_EVENTS} events."
        cleaned_events = []
        for raw in events:
            event, error = _clean_event(raw)
            if error:
                return None, error
            event_id = event.get("id")
            if event_id is None or event_id in seen_ids:
                while next_id in seen_ids:
                    next_id += 1
                event_id = next_id
                next_id += 1
            else:
                next_id = max(next_id, event_id + 1)
            seen_ids.add(event_id)
            event["id"] = event_id
            cleaned_events.append(event)
        cleaned.append({
            "name": name.strip(),
            "assumptions": assumptions,
            "events": cleaned_events,
        })

    current = _whole(payload.get("current", 0))
    if current is None:
        return None, "The selected scenario has to be a number."
    current = max(0, min(current, len(cleaned) - 1))
    unit = payload.get("unit", "real")
    if unit not in ("real", "nominal"):
        return None, "The unit has to be today's euros or future euros."
    compare = payload.get("compare", False)
    if not isinstance(compare, bool):
        return None, "Compare has to be on or off."
    return {
        "scenarios": cleaned,
        "current": current,
        "unit": unit,
        "compare": compare,
    }, None


def _clean_assumptions(raw):
    if not isinstance(raw, dict):
        return None, "Assumptions have to be an object."
    cleaned = {}
    for key, (low, high) in _BOUNDS.items():
        number = _bounded(raw.get(key), low, high)
        if number is None:
            return None, f"{key} is missing or out of range."
        cleaned[key] = number
    return cleaned, None


def _clean_event(raw):
    if not isinstance(raw, dict):
        return None, "An event has to be an object."
    kind = raw.get("type")
    if kind not in EVENT_TYPES:
        return None, "That event type is not one this plan knows."
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        return None, "An event needs a name of up to 80 characters."
    if "on" in raw and not isinstance(raw.get("on"), bool):
        return None, "An event is either on or off."
    event = {
        "type": kind,
        "name": name.strip(),
        "on": raw.get("on", True),
    }
    event_id = raw.get("id")
    if event_id is not None:
        parsed = _positive_int(event_id)
        if parsed is None:
            return None, "An event id has to be a positive number."
        event["id"] = parsed
    if "dir" in raw and raw["dir"] not in ("cost", "income"):
        return None, "A custom event is either a cost or income."
    if "repeat" in raw and raw["repeat"] not in ("once", "yearly", "every"):
        return None, "A custom event repeats once, yearly, or every N years."
    for key, value in raw.items():
        if key in ("type", "name", "on", "id", "dir", "repeat"):
            continue
        if not isinstance(key, str) or len(key) > 40:
            return None, "An event has a field this plan cannot store."
        if isinstance(value, bool):
            event[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and _finite(value) and abs(value) <= 1e12:
            event[key] = float(value) if isinstance(value, float) else value
        elif isinstance(value, str) and len(value) <= 40:
            event[key] = value
        else:
            return None, f"The {key} field on an event cannot be stored."
    if "dir" in raw:
        event["dir"] = raw["dir"]
    if "repeat" in raw:
        event["repeat"] = raw["repeat"]
    return event, None


def _pay_down(loans):
    """Take one year's annuity off each loan. Returns the cash paid."""
    paid = 0.0
    for loan in loans:
        if loan["bal"] <= 0.5:
            continue
        interest = loan["bal"] * loan["r"]
        principal = min(loan["pmt"] - interest, loan["bal"])
        paid += interest + principal
        loan["bal"] -= principal
    return paid


def _euros(total, divisor):
    if not divisor:
        return 0
    return int(round(total / divisor))


def _months_between(start, end):
    """Inclusive count of calendar months from ``start`` through ``end``."""
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def _shift_month(month_start, delta):
    index = month_start.year * 12 + (month_start.month - 1) + delta
    year, month0 = divmod(index, 12)
    return date(year, month0 + 1, 1)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _num(source, key, default=0.0):
    """A finite number, with ``0`` kept. Missing or unreadable becomes ``default``."""
    if not isinstance(source, dict):
        return float(default)
    value = source.get(key, default)
    if isinstance(value, bool) or value is None or value == "":
        return float(default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(number):
        return float(default)
    return number


def _js_or(value, default):
    """JavaScript ``value || default`` for a number.

    ``0``, ``None`` and ``""`` fall through. A negative number is kept, so an
    asset can lose value.
    """
    if isinstance(value, bool) or value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number == 0:
        return default
    return number


def _bounded(value, low, high):
    if not _finite(value):
        return None
    number = float(value)
    if number < low or number > high:
        return None
    return number


def _whole(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _positive_int(value):
    number = _whole(value)
    if number is None or number <= 0:
        return None
    return number
