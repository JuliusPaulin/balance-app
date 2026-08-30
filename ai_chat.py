"""The agent loop behind the chat assistant.

Hand-written rather than any SDK's tool runner, and it knows nothing about
which model is answering: it asks a backend from :mod:`ai_backends` for a turn,
runs whatever tools that turn asked for, and hands the results back. Swapping
Ollama for something else is a change in that file, not this one.

What lives here: the system prompt and the tool-call cycle. What does not: any
knowledge of the database, of what a category is, or of a provider's wire format.
"""

import json

import config
from ai_backends import BackendUnavailable, get_backend
from ai_tools import TOOL_SCHEMAS, context_block, run_tool

# How many times the model may call tools before we stop and answer with what
# we have. Every question this assistant can be asked is answerable in one or
# two calls; beyond that it is going in circles, and small models circle more
# readily than large ones.
MAX_TOOL_ROUNDS = 4

SYSTEM_PROMPT = """You are the assistant inside Balance., a personal expense \
tracker that runs entirely on the user's own Mac. You answer questions about \
their spending, income, subscriptions and net worth.

Call a tool for every question. The rules below are not style preferences — the \
app exists to tell this person the truth about their money, and a number you \
invented is worse than no answer at all.

1. Never state a figure that did not come from a tool result. No totals you \
worked out, no percentages you estimated, no "roughly". If you need a number \
you do not have, call a tool. If no tool can give it, say so.

2. Every amount comes back twice: a raw number, and a string like "612 €" in a \
field ending in _eur. Copy the _eur string across character for character, \
spacing included. Do not retype it, round it, or change the separators. Use the \
raw numbers only to decide what to say — which category is largest, whether \
something went up or down.

3. Never add, subtract or compare amounts yourself. If you want a total, a \
year-on-year change or a share, it is already in the result: monthly_summary \
carries total_income and total_expense for the whole period, and annual_report \
carries change_vs_last_year with the direction worked out. If the figure you \
want is genuinely not there, say what you can see instead of computing it.

4. Never work out dates yourself. Pass a period name — this_month, last_month, \
last_3_months, last_6_months, last_12_months, this_year, last_year, ytd, \
all_time — and the tool resolves it. Pass an explicit month like "2026-05" when \
the user named that month, or a date_from/date_to pair for anything the list \
does not cover — a season, a holiday, "since March". Never invent a period \
name: the tool will refuse it and tell you the list. When the question names \
months — "June against July" — pass them to monthly_summary as \
months: ["2026-06", "2026-07"]. A period is always a window ending today and \
cannot say that.

5. Report only the months the result actually contains. Check the "period" \
field and the month of every row before you write a figure against it. Asked \
whether July beat June, and handed back July and August, the honest answer is \
that June was not in the result — not a figure for June.

6. Say which period you answered for. Every result carries a "period" field. \
"Spring" and "recently" mean nothing exact, so name the months you actually \
read — "June to August" — and let the user correct you.

7. A purchase, a buy or a spend means type "expense". Sorting transactions \
without it puts the salary at the top of the list.

8. Use the exact category names listed in the context below. Do not invent them.

9. A single month's breakdown carries the comparison already made: \
usual_month, the difference in vs_usual, and direction ("above" or "below"). \
Read direction; do not work it out from the two figures. reads_as says whether \
the gap is worth mentioning at all. usual_month — what that category \
normally costs. Mention it when the difference is interesting. Skip it for \
anything marked is_fixed_cost, because rent has no news in it.

Answer in two or three sentences. This is a side panel, not a report. Amounts \
are euros. You are read-only: you cannot add, edit or delete anything, so if \
asked to, say so."""


def _system_prompt():
    """The prompt plus the facts the model would otherwise have to guess.

    Today's date, the months that hold data and the real category names. Small
    models invent category names freely, and an invented name returns an empty
    result that reads exactly like "you spent nothing on that".
    """
    return (SYSTEM_PROMPT + "\n\nContext for this conversation:\n"
            + json.dumps(context_block(), indent=2, ensure_ascii=False))


def chat(messages, backend=None):
    """Answer the conversation in ``messages``; return the reply and a trace.

    ``messages`` is ``[{"role", "content"}, ...]`` with the user's turn last.
    Returns ``{"reply", "tool_calls", "usage", "backend"}``. ``tool_calls`` is
    what the assistant actually looked at, so the panel can show the working
    rather than asking anyone to take a number on trust.
    """
    backend = backend or get_backend()
    system = _system_prompt()

    history = [{"role": m["role"], "content": m["content"]} for m in messages]
    trace = []
    usage = {"input_tokens": 0, "output_tokens": 0}

    def account(turn):
        usage["input_tokens"] += turn.usage.get("input_tokens", 0)
        usage["output_tokens"] += turn.usage.get("output_tokens", 0)

    def done(reply):
        return {"reply": reply, "tool_calls": trace, "usage": usage,
                "backend": backend.name}

    for _ in range(MAX_TOOL_ROUNDS):
        turn = backend.complete(system, history, TOOL_SCHEMAS)
        account(turn)

        if turn.refusal:
            return done(turn.refusal)
        if not turn.tool_calls:
            return done(turn.text)

        history.append({"role": "assistant", "content": turn.text,
                        "tool_calls": turn.tool_calls, "raw": turn.raw})
        for call in turn.tool_calls:
            output = run_tool(call["name"], call["input"])
            trace.append({"tool": call["name"], "arguments": call["input"],
                          "ok": "error" not in output})
            history.append({
                "role": "tool", "id": call["id"], "name": call["name"],
                "content": json.dumps(output, default=str, ensure_ascii=False),
                "is_error": "error" in output,
            })

    # Out of rounds. Ask once more with the tools withdrawn, so a stuck
    # conversation ends in a sentence rather than a fifth identical lookup.
    history.append({
        "role": "user",
        "content": "Answer now using only what the tools already returned. "
                   "If that is not enough, say what you could not find out.",
    })
    final = backend.complete(system, history, tools=None)
    account(final)
    return done(final.refusal or final.text)


def status():
    """What the panel needs to decide whether to show itself, and why not."""
    try:
        return get_backend().status()
    except BackendUnavailable as exc:
        return {"backend": config.AI_BACKEND, "model": None, "reachable": False,
                "model_installed": False, "installed_models": [],
                "detail": str(exc)}
