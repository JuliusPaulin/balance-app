"""The read-only tools the chat assistant is allowed to call.

The assistant does not query the database and it does not do arithmetic. Every
tool here is a thin wrapper over an endpoint the app already serves, so the
number the assistant reports is the same number the Dashboard draws — computed
by the same SQL, through the same ``_filter_clauses``, scoped to the same user.

Three rules hold this together, and each of them exists because the obvious
alternative fails in an app about money:

**No SQL from the model.** Handing a model the schema and letting it write
queries is where small models fall off a cliff: a plausible query with the join
or the sign wrong reports a number that is simply false, confidently. Picking
one of six functions and filling two parameters is a much easier task, and it
is the task that has to survive being run against a 4B model on the user's own
machine later.

**No arithmetic from the model.** Every result carries a preformatted
``*_eur`` string beside its raw float, so the assistant can quote a figure
verbatim instead of rounding one itself. The raw floats are there for
comparisons; the strings are what belongs in a sentence.

**No calendar arithmetic from the model.** "Last month", "since summer" and
"this time last year" are where small models actually go wrong — not tool
choice. So no tool takes a relative date: they take a ``period`` name from a
fixed list, and :func:`resolve_period` turns it into explicit months here, in
Python, where it can be tested.

Read-only on purpose. Letting the assistant write to ``transactions`` is a
different risk conversation, and the app has no undo for a hand-edited row.
"""

from datetime import date

from core import app, current_user_id
from database import db_conn

# The relative periods a question can name. The model picks one of these by
# name; it never computes a date. Anything outside the list has to be given as
# an explicit `month` (YYYY-MM) or a `date_from`/`date_to` pair.
# `last_2_months` was tried here and taken out again. The model reached for the
# name constantly, so it looked like a gap worth filling — but "the last two
# months" is July and August, and it was asking in order to compare June with
# July. Handed a window that looked right and did not hold June, it reported
# June at 3 598 € against a real 3 523 €: the one failure this module exists to
# prevent, caused by making the model comfortable. Two named months are asked
# for by name — see `months` on monthly_summary.
PERIODS = (
    "this_month", "last_month", "last_3_months", "last_6_months",
    "last_12_months", "this_year", "last_year", "ytd", "all_time",
)

# How many transaction rows one search may return. The assistant is answering a
# question, not paginating a table — beyond this the answer wants a breakdown,
# and the rows are just context the model has to pay for.
MAX_ROWS = 50


def _eur(amount):
    """Format like the UI's ``fmt()`` — fi-FI, euro, no decimals.

    The assistant quotes this string rather than formatting the float itself.
    A rounding it does not perform is a rounding it cannot get wrong.
    """
    try:
        whole = round(float(amount))
    except (TypeError, ValueError):
        return None
    # fi-FI groups with a non-breaking space and puts the symbol last.
    return f"{whole:,}".replace(",", " ") + " €"


def _month_add(month, delta):
    """"2026-05" plus a number of months, as "YYYY-MM"."""
    index = int(month[:4]) * 12 + int(month[5:7]) - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def resolve_period(period=None, month=None, date_from=None, date_to=None, today=None):
    """Turn whatever the assistant asked for into explicit months and dates.

    Returns ``{"months": [...], "date_from": ..., "date_to": ..., "label": ...}``
    where ``months`` is empty for an all-time or explicit-date-range request.

    ``today`` is injectable so the tests do not drift with the wall clock — the
    whole point of resolving periods here is that it is testable, and a helper
    that reads ``date.today()`` internally is not.
    """
    today = today or date.today()
    this_month = today.strftime("%Y-%m")

    # An explicit month or date range always wins over a named period: the user
    # said something specific and we should not second-guess it.
    if month:
        return {"months": [month], "date_from": None, "date_to": None, "label": month}
    if date_from or date_to:
        return {
            "months": [],
            "date_from": date_from,
            "date_to": date_to,
            "label": f"{date_from or 'the beginning'} to {date_to or 'today'}",
        }

    def back(n):
        """The n most recent months, including this one, newest last."""
        return [_month_add(this_month, -d) for d in range(n - 1, -1, -1)]

    if period in (None, "this_month"):
        return {"months": [this_month], "date_from": None, "date_to": None,
                "label": this_month}
    if period == "last_month":
        previous = _month_add(this_month, -1)
        return {"months": [previous], "date_from": None, "date_to": None,
                "label": previous}
    if period in ("last_3_months", "last_6_months", "last_12_months"):
        n = int(period.split("_")[1])
        months = back(n)
        return {"months": months, "date_from": None, "date_to": None,
                "label": f"{months[0]} to {months[-1]}"}
    if period in ("this_year", "ytd"):
        months = [f"{today.year:04d}-{m:02d}" for m in range(1, today.month + 1)]
        return {"months": months, "date_from": None, "date_to": None,
                "label": f"{today.year} so far"}
    if period == "last_year":
        year = today.year - 1
        months = [f"{year:04d}-{m:02d}" for m in range(1, 13)]
        return {"months": months, "date_from": None, "date_to": None,
                "label": str(year)}
    if period == "all_time":
        return {"months": [], "date_from": None, "date_to": None, "label": "all time"}

    # An unknown period name is the model's mistake, and it has to be told so.
    # Falling back to this month was the worst of both: asked "did I spend more
    # in July than in June?" the model invented `last_2_months`, quietly got
    # August, and gave up on a question the data answers easily. An error naming
    # the list is something it can act on, and `run_tool` hands it straight back.
    raise ValueError(
        f"Unknown period {period!r}. Use one of: {', '.join(PERIODS)}."
        " For anything else, pass an explicit month as YYYY-MM,"
        " or a date_from/date_to pair."
    )


class ToolDispatchError(RuntimeError):
    """An endpoint the tools read did not answer.

    Raised rather than returned, so :func:`run_tool` turns it into an ``error``
    the model is handed. The alternative is what this used to do: hand back
    ``None``, let the tool body's ``or {}`` make it an empty result, and report
    a 404 to the user as "0 €". A tool that cannot reach its endpoint has to say
    so — silence that reads as a number is the one failure this whole module is
    built to prevent.
    """


def _ensure_routes():
    """Attach the blueprints to the app object if nothing else has.

    ``core`` holds the bare Flask app; the routes go on in :func:`routes.register`,
    which ``app.py`` calls on start-up. A process that reaches the tools without
    going through ``app.py`` — ``scripts/ask.py``, or anything importing
    :mod:`ai_chat` on its own — would otherwise dispatch against an app with no
    routes and 404 on every call.

    Imported here rather than at the top of the file because ``routes.chat``
    imports ``ai_chat``, which imports this module: at module level that is a
    cycle, and by the time this runs it is just a lookup in ``sys.modules``.
    """
    import routes
    routes.register(app)


def _call_api(path, params=None):
    """Dispatch one of the app's own GET endpoints in-process.

    Going through the real URL map rather than re-issuing the SQL is the whole
    point: the assistant cannot report a total the Dashboard would disagree
    with, because it is literally reading the Dashboard's endpoint. The
    before/after-request guards run too — CSRF only protects mutating methods,
    and every path here is a GET.
    """
    _ensure_routes()
    with app.test_request_context(path, query_string=params or {}):
        response = app.full_dispatch_request()
    if response.status_code != 200:
        raise ToolDispatchError(
            f"{path} returned HTTP {response.status_code}"
        )
    return response.get_json()


def _category_ids(names):
    """Map category names the assistant used to the ids the API wants.

    The model never sees or invents an id. Matching is on name alone and so
    spans both types on purpose — "Other" and "Investments" exist as an expense
    and an income category, and a question about "Investments" means both.
    """
    if not names:
        return [], []
    wanted = {n.strip().lower() for n in names if n and n.strip()}
    ids, matched = [], set()
    for category in _call_api("/api/categories") or []:
        if category["name"].lower() in wanted:
            ids.append(str(category["id"]))
            matched.add(category["name"].lower())
    return ids, sorted(wanted - matched)


# ── The tools ─────────────────────────────────────────────────────────────

def search_transactions(period=None, month=None, date_from=None, date_to=None,
                        categories=None, type=None, q=None,
                        amount_min=None, amount_max=None, limit=20):
    """Individual transactions matching a filter — the /api/transactions rail."""
    window = resolve_period(period, month, date_from, date_to)
    ids, unknown = _category_ids(categories)

    params = {"per_page": min(int(limit or 20), MAX_ROWS), "sort": "amount", "dir": "desc"}
    if window["months"]:
        params["months"] = ",".join(window["months"])
    if window["date_from"]:
        params["date_from"] = window["date_from"]
    if window["date_to"]:
        params["date_to"] = window["date_to"]
    if ids:
        params["category_ids"] = ",".join(ids)
    if type in ("expense", "income"):
        params["type"] = type
    if q:
        params["q"] = q
    if amount_min is not None:
        params["amount_min"] = amount_min
    if amount_max is not None:
        params["amount_max"] = amount_max

    data = _call_api("/api/transactions", params) or {}
    _net = round(data.get("sum_income", 0.0) - data.get("sum_expense", 0.0), 2)
    return {
        "period": window["label"],
        "matched": data.get("total", 0),
        "showing": len(data.get("items", [])),
        "sum_expense": data.get("sum_expense", 0.0),
        "sum_expense_eur": _eur(data.get("sum_expense", 0.0)),
        "sum_income": data.get("sum_income", 0.0),
        "sum_income_eur": _eur(data.get("sum_income", 0.0)),
        # Both sums and not the difference is an invitation to subtract, and
        # asked whether spring had been expensive the model duly did.
        "sum_net": _net,
        "sum_net_eur": _eur(_net),
        # Largest first, and only the columns a sentence could use. The full
        # row carries ids and timestamps the assistant has no business quoting.
        "transactions": [
            {"date": t["date"], "store": t["store"], "category": t["category_name"],
             "amount": t["amount"], "amount_eur": _eur(t["amount"]), "type": t["type"]}
            for t in data.get("items", [])
        ],
        "unknown_categories": unknown,
    }


def category_breakdown(period=None, month=None, type="expense"):
    """Totals per category, with the baseline that says whether it is a lot.

    A single month carries ``median`` (that category's usual month, over the
    six before) and ``fixed``. That is the difference between "Groceries 612 €"
    and "Groceries 612 €, about a fifth above your usual 510 €" — and the
    second one is the only version worth having an assistant for.
    """
    window = resolve_period(period, month)
    params = {"type": type if type in ("expense", "income") else "expense"}
    if window["months"]:
        params["months"] = ",".join(window["months"])
    else:
        # All-time has no month list to send; the endpoint defaults to the
        # latest month, so name the year instead of silently narrowing.
        params["year"] = str(date.today().year)

    data = _call_api("/api/dashboard/category-breakdown", params) or {}
    items = data.get("items", [])
    total = sum(i.get("total", 0.0) for i in items)

    out = []
    for item in items:
        row = {"category": item["name"], "total": item["total"],
               "total_eur": _eur(item["total"])}
        median = item.get("median")
        if median is not None:
            row["usual_month"] = median
            row["usual_month_eur"] = _eur(median)
            row["is_fixed_cost"] = bool(item.get("fixed"))
            # The comparison, made here rather than left to the model. Handed a
            # month and a usual month it read the figures out correctly and then
            # filed Medical at 74 € against a usual 9 € under "saving money" —
            # about half the list came back with the direction inverted. It is
            # the same lesson as the year-on-year change: quoting a number is a
            # small model's strength, and comparing two is not.
            difference = round(item["total"] - median, 2)
            row["vs_usual"] = difference
            row["vs_usual_eur"] = _eur(difference)
            row["direction"] = ("above" if difference > 0
                                else "below" if difference < 0 else "same")
            # The Dashboard's own band, so the assistant and the bars agree on
            # what counts as news (QUIET_BAND in static/js/app.js).
            row["reads_as"] = (
                "as usual" if not median or abs(difference / median) < 0.25
                else f"{row['direction']} usual"
            )
        out.append(row)

    return {
        "period": window["label"],
        "type": params["type"],
        "total": total,
        "total_eur": _eur(total),
        # Only a single month has a monthly normal to stand beside. Over a
        # period the endpoint sends no median, and neither do we.
        "has_baseline": len(window["months"]) == 1,
        "categories": out,
    }


def monthly_summary(period="last_12_months", months=None):
    """Income and expense per month — the bars on the Dashboard.

    ``months`` names them outright (``["2026-06", "2026-07"]``). Comparing two
    months is one of the commonest questions asked here and there was no way to
    ask it: every period is a window ending today, so "June against July" meant
    fetching a wider one and reading two rows out of it, or fetching each month
    in a separate call. The model did neither. It picked the window whose name
    sounded right, got July and August, and made June's figure up.
    """
    if months:
        window = {"months": sorted(months),
                  "label": (f"{min(months)} to {max(months)}"
                            if len(months) > 1 else months[0])}
    else:
        window = resolve_period(period)
    wanted = set(window["months"])
    rows = _call_api("/api/dashboard/monthly-summary") or []

    by_month = {}
    for row in rows:
        if wanted and row["month"] not in wanted:
            continue
        entry = by_month.setdefault(row["month"], {"month": row["month"]})
        entry[row["type"]] = row["total"]

    months = []
    for month in sorted(by_month):
        entry = by_month[month]
        expense = entry.get("expense", 0.0)
        income = entry.get("income", 0.0)
        months.append({
            "month": month,
            "expense": expense, "expense_eur": _eur(expense),
            "income": income, "income_eur": _eur(income),
            "net": income - expense, "net_eur": _eur(income - expense),
            "invested": entry.get("investment", 0.0),
        })
    # The totals belong here, not in the model's head. Asked what it earned
    # last year, the model added up twelve income figures itself and answered
    # 36 135 € against a real 36 840 € — confident, and 705 € out. Every other
    # tool hands back the sum it is likely to be asked for; this one did not,
    # so summing was the only thing left to it.
    total_income = round(sum(m["income"] for m in months), 2)
    total_expense = round(sum(m["expense"] for m in months), 2)
    total_net = round(total_income - total_expense, 2)
    return {
        "period": window["label"],
        "total_income": total_income, "total_income_eur": _eur(total_income),
        "total_expense": total_expense, "total_expense_eur": _eur(total_expense),
        "total_net": total_net, "total_net_eur": _eur(total_net),
        "months": months,
    }


def list_subscriptions():
    """Detected recurring charges (see recurring.py).

    The totals deliberately exclude stopped series, transfers and investments,
    which is why they come from the endpoint rather than being summed here.

    Every row carries ``counts_toward_total``, already worked out with the same
    rule the endpoint sums by: an expense, not a transfer, not stopped. The
    detector also finds the salary — it is a monthly series like any other — and
    a model left to combine three flags itself will sooner or later announce
    that the user's largest subscription is their employer.
    """
    data = _call_api("/api/recurring") or {}
    items = data.get("items") or []
    summary = data.get("summary") or {}

    subscriptions, also_recurring = [], []
    for s in items:
        counts = (s.get("type") == "expense"
                  and not s.get("is_transfer")
                  and s.get("status") != "stopped")
        if s.get("type") != "expense":
            why = "this is income, not a subscription"
        elif s.get("is_transfer"):
            why = "a transfer or investment, not money spent"
        elif s.get("status") == "stopped":
            why = "stopped — the service ended"
        else:
            why = None
        (subscriptions if counts else also_recurring).append({
            "merchant": s.get("store"),
            "category": s.get("category"),
            "type": s.get("type"),
            # What it costs per month, whatever its cadence — the figure the
            # totals are built from. `last_amount` is what actually left the
            # account last time, which is the answer to "how much is Netflix".
            "monthly_cost": s.get("monthly_cost"),
            "monthly_cost_eur": _eur(s.get("monthly_cost")),
            "annual_cost": s.get("annual_cost"),
            "annual_cost_eur": _eur(s.get("annual_cost")),
            "last_amount": s.get("last_amount"),
            "last_amount_eur": _eur(s.get("last_amount")),
            "cadence": s.get("cadence"),
            "status": s.get("status"),
            "next_charge": s.get("next_date"),
            "counts_toward_total": counts,
            **({"not_a_subscription_because": why} if why else {}),
        })

    return {
        "monthly_total": summary.get("monthly_total"),
        "monthly_total_eur": _eur(summary.get("monthly_total")),
        "annual_total": summary.get("annual_total"),
        "annual_total_eur": _eur(summary.get("annual_total")),
        # The table is longer than the total is built from, and saying so is
        # what stops "13 subscriptions costing 771 €/mo", which is two true
        # numbers making one false sentence.
        "counted_in_total": summary.get("active_count"),
        "detected_in_all": summary.get("count"),
        "subscriptions": subscriptions,
        # Kept apart rather than flagged in one list. A flag was not enough:
        # asked for the three biggest subscriptions the model sorted the whole
        # list by cost and led with the salary, correctly labelled "(income)"
        # and still the wrong answer. Two lists cannot be sorted across.
        "also_recurring": also_recurring,
    }


_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _span_label(month_numbers):
    """"01".."08" → "Jan-Aug". The months the two years are compared over."""
    if not month_numbers:
        return None
    names = [_MONTH_NAMES[int(m) - 1] for m in sorted(month_numbers)]
    return names[0] if len(names) == 1 else f"{names[0]}-{names[-1]}"


def annual_report(year=None):
    """One year against the one before, held to the months this year has.

    The endpoint answers a whole report page — two years of monthly rows, both
    category lists, the top ten transactions with their ids. Handed over whole
    it cost 9 000 tokens a question and gave the model nothing it could quote:
    the totals sit in nested ``totals``/``prev_totals`` dicts, so the euro
    strings this used to attach went onto keys that were never there. The model
    read the raw floats instead, printed "28,542.27 €", and worked out the
    year-on-year gap by subtracting them itself.

    So the differences are computed here, in Python, and every figure comes
    with the string to quote.
    """
    params = {"year": year} if year else {}
    data = _call_api("/api/reports/annual", params) or {}

    totals = data.get("totals") or {}
    prev = data.get("prev_totals") or {}
    span = _span_label(data.get("compare_months") or [])

    def figures(source):
        income = source.get("income", 0.0)
        expense = source.get("expense", 0.0)
        return {
            "income": income, "income_eur": _eur(income),
            "expense": expense, "expense_eur": _eur(expense),
            "net": round(income - expense, 2),
            "net_eur": _eur(round(income - expense, 2)),
        }

    this_year = figures(totals)
    last_year = figures(prev)

    change = {}
    for key in ("income", "expense", "net"):
        delta = round(this_year[key] - last_year[key], 2)
        change[key] = delta
        change[f"{key}_eur"] = _eur(delta)
        # Signed, so "spending is up" never has to be inferred from two totals.
        change[f"{key}_direction"] = ("up" if delta > 0
                                      else "down" if delta < 0 else "flat")

    def categories(rows):
        return [{"category": r["name"], "total": r["total"],
                 "total_eur": _eur(r["total"]), "transactions": r.get("count")}
                for r in (rows or [])[:8]]

    return {
        "year": data.get("year"),
        # Both years are measured over the same months, or a part-finished year
        # would report a collapse in income that is really the calendar.
        "compared_over": span,
        "previous_year": (data.get("year") - 1) if data.get("year") else None,
        "this_year": this_year,
        "last_year": last_year,
        "change_vs_last_year": change,
        "top_expense_categories": categories(data.get("categories")),
        "top_income_categories": categories(data.get("income_categories")),
    }


def net_worth_summary():
    """Current net worth: assets, liabilities and the latest total.

    Shaped rather than passed through. The endpoint answers a page: every
    account with its id, sort order, archived flag and full IBAN in the name.
    None of that belongs in a sentence, and the same guessing at key names that
    left this tool's "change" without a euro string — the endpoint calls it
    change_vs_prev — is what put the other tools' amounts through as None.
    """
    data = _call_api("/api/networth/summary") or {}

    def amount(key):
        value = data.get(key)
        return (value, _eur(value)) if isinstance(value, (int, float)) else (None, None)

    net_worth, net_worth_eur = amount("net_worth")
    assets, assets_eur = amount("assets")
    liabilities, liabilities_eur = amount("liabilities")
    change, change_eur = amount("change_vs_prev")

    return {
        "net_worth": net_worth, "net_worth_eur": net_worth_eur,
        "assets": assets, "assets_eur": assets_eur,
        "liabilities": liabilities, "liabilities_eur": liabilities_eur,
        # Named for what `networth.summary` actually measures — this month's
        # net worth against last month's. Called "change_since_previous" it
        # invited the obvious misreading, and the 4b model duly reported it as
        # the change since last year.
        "change_since_last_month": change,
        "change_since_last_month_eur": change_eur,
        "accounts": [
            {"name": a.get("name"), "group": a.get("group_name"),
             "type": a.get("type"),
             "balance": a.get("latest_balance"),
             "balance_eur": _eur(a.get("latest_balance")),
             "as_of": a.get("latest_as_of")}
            for a in (data.get("accounts") or [])
            # A closed account keeps a zero so past months stay true, but it is
            # not part of what the user has now.
            if not a.get("is_archived")
        ],
    }


# ── What the model is told it can call ────────────────────────────────────
#
# One schema list, provider-neutral JSON Schema. Anthropic takes it as
# `input_schema`; Ollama and the OpenAI-compatible servers take the same object
# under `parameters`. Keeping it in one place is what makes swapping the
# backend a change of forty lines rather than a rewrite.

_PERIOD_SCHEMA = {
    "type": "string",
    "enum": list(PERIODS),
    "description": "A relative period. Use this for anything like 'last month' "
                   "or 'this year' — never work the dates out yourself.",
}

TOOL_SCHEMAS = [
    {
        "name": "search_transactions",
        "description": "Individual transactions matching a filter, largest first, "
                       "with the totals for the whole match. Use for 'what did I "
                       "buy at X', 'anything over 200 €', or listing purchases.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": _PERIOD_SCHEMA,
                "month": {"type": "string", "description": "An explicit month, YYYY-MM. Use only when the user named one."},
                "date_from": {"type": "string", "description": "YYYY-MM-DD."},
                "date_to": {"type": "string", "description": "YYYY-MM-DD."},
                "categories": {"type": "array", "items": {"type": "string"},
                               "description": "Category names exactly as listed in the context."},
                "type": {"type": "string", "enum": ["expense", "income"]},
                "q": {"type": "string", "description": "Substring match on store name or category."},
                "amount_min": {"type": "number"},
                "amount_max": {"type": "number"},
                "limit": {"type": "integer", "description": f"Rows to return, at most {MAX_ROWS}."},
            },
            "required": [],
        },
    },
    {
        "name": "category_breakdown",
        "description": "Spending or income per category over a period. For a single "
                       "month each category also carries the usual month it is being "
                       "compared against. Use for 'what did I spend on X', 'where did "
                       "my money go', 'is that a lot'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": _PERIOD_SCHEMA,
                "month": {"type": "string", "description": "An explicit month, YYYY-MM."},
                "type": {"type": "string", "enum": ["expense", "income"], "default": "expense"},
            },
            "required": [],
        },
    },
    {
        "name": "monthly_summary",
        "description": "Income, expense and net for each month in a period, plus "
                       "the totals for the whole period. To compare named months, "
                       "pass `months` — a period is always a window ending today, "
                       "so it cannot express 'June against July'. Use for "
                       "trends over time and month-to-month comparisons.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": _PERIOD_SCHEMA,
                "months": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Explicit months as YYYY-MM, e.g. "
                                   "[\"2026-06\", \"2026-07\"]. Use this whenever "
                                   "the question names the months.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "list_subscriptions",
        "description": "Detected recurring charges and what they cost per month and "
                       "per year. Use for anything about subscriptions or regular bills.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "annual_report",
        "description": "A year against the previous one, fairly compared over the "
                       "months this year actually has.",
        "input_schema": {
            "type": "object",
            "properties": {"year": {"type": "integer", "description": "Defaults to this year."}},
            "required": [],
        },
    },
    {
        "name": "net_worth_summary",
        "description": "Current net worth: assets, liabilities and the latest total.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]

TOOLS = {
    "search_transactions": search_transactions,
    "category_breakdown": category_breakdown,
    "monthly_summary": monthly_summary,
    "list_subscriptions": list_subscriptions,
    "annual_report": annual_report,
    "net_worth_summary": net_worth_summary,
}


def run_tool(name, arguments):
    """Execute one tool call. Never raises — a failed tool is a result too.

    A tool that raises would end the turn with a stack trace where an answer
    should be. Handing the model the error instead lets it say what went wrong,
    or try a different tool, which is what a person would do.
    """
    func = TOOLS.get(name)
    if func is None:
        return {"error": f"No such tool: {name}"}
    try:
        return func(**(arguments or {}))
    except TypeError as exc:
        return {"error": f"Bad arguments for {name}: {exc}"}
    except Exception as exc:  # pragma: no cover - defensive
        app.logger.warning("chat tool %s failed: %s", name, exc)
        return {"error": f"{name} failed: {exc}"}


def context_block():
    """The facts the assistant needs before it can ask a sensible question.

    Today's date, the months that actually hold data, and the category names.
    Without the category list the model guesses names and gets empty results;
    without the month list it asks about months the database has never seen.
    """
    uid = current_user_id()
    with db_conn() as conn:
        span = conn.execute(
            "SELECT MIN(substr(date, 1, 7)) AS first, MAX(substr(date, 1, 7)) AS last"
            " FROM transactions WHERE user_id = %s", (uid,)
        ).fetchone()
        names = conn.execute(
            "SELECT DISTINCT name FROM categories WHERE user_id = %s ORDER BY name",
            (uid,)
        ).fetchall()
    return {
        "today": date.today().isoformat(),
        "current_month": date.today().strftime("%Y-%m"),
        "first_month_with_data": span["first"] if span else None,
        "last_month_with_data": span["last"] if span else None,
        "categories": [r["name"] for r in names],
    }
