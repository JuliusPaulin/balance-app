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

    # An unknown period name is the model's mistake, not the user's. Fall back
    # to this month and say so in the label rather than failing the turn.
    return {"months": [this_month], "date_from": None, "date_to": None,
            "label": this_month}


def _call_api(path, params=None):
    """Dispatch one of the app's own GET endpoints in-process.

    Going through the real URL map rather than re-issuing the SQL is the whole
    point: the assistant cannot report a total the Dashboard would disagree
    with, because it is literally reading the Dashboard's endpoint. The
    before/after-request guards run too — CSRF only protects mutating methods,
    and every path here is a GET.
    """
    with app.test_request_context(path, query_string=params or {}):
        response = app.full_dispatch_request()
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
    return {
        "period": window["label"],
        "matched": data.get("total", 0),
        "showing": len(data.get("items", [])),
        "sum_expense": data.get("sum_expense", 0.0),
        "sum_expense_eur": _eur(data.get("sum_expense", 0.0)),
        "sum_income": data.get("sum_income", 0.0),
        "sum_income_eur": _eur(data.get("sum_income", 0.0)),
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


def monthly_summary(period="last_12_months"):
    """Income and expense per month — the bars on the Dashboard."""
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
    return {"period": window["label"], "months": months}


def list_subscriptions():
    """Detected recurring charges (see recurring.py).

    The totals deliberately exclude stopped series, transfers and investments,
    which is why they come from the endpoint rather than being summed here.
    """
    data = _call_api("/api/recurring") or {}
    items = data.get("items", data if isinstance(data, list) else [])
    return {
        "monthly_total": data.get("monthly_total"),
        "monthly_total_eur": _eur(data.get("monthly_total")),
        "annual_total": data.get("annual_total"),
        "annual_total_eur": _eur(data.get("annual_total")),
        "subscriptions": [
            {"merchant": s.get("merchant") or s.get("store"),
             "amount": s.get("amount"), "amount_eur": _eur(s.get("amount")),
             "cadence": s.get("cadence"), "status": s.get("status"),
             "next_charge": s.get("next_date") or s.get("next_charge")}
            for s in items
        ],
    }


def annual_report(year=None):
    """One year against the one before, held to the months this year has."""
    params = {"year": year} if year else {}
    data = _call_api("/api/reports/annual", params) or {}
    for key in ("total_income", "total_expense", "net", "prev_income", "prev_expense"):
        if isinstance(data.get(key), (int, float)):
            data[f"{key}_eur"] = _eur(data[key])
    return data


def net_worth_summary():
    """Current net worth: assets, liabilities and the latest total."""
    data = _call_api("/api/networth/summary") or {}
    for key in ("net_worth", "assets", "liabilities", "change", "change_amount"):
        if isinstance(data.get(key), (int, float)):
            data[f"{key}_eur"] = _eur(data[key])
    return data


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
        "description": "Income, expense and net for each month in a period. Use for "
                       "trends over time and month-to-month comparisons.",
        "input_schema": {
            "type": "object",
            "properties": {"period": _PERIOD_SCHEMA},
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
