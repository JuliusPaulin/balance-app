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

4. Never work out dates yourself, and that includes month names. "June" is not \
a period — look it up in months_by_name in the context below and pass the \
"2026-06" it gives you as month. Reading down, the first match is the most \
recent June there has been, which is the one meant. Otherwise: pass a period name — this_month, last_month, \
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

6. Name the month in the sentence. Every result carries a "period" field, and \
"last month" is not an answer — "in July" is. Write "You spent 421 € on \
groceries in July", never "last month" on its own. The app is showing this \
person other months at the same time, and a right figure with no month on it \
reads as a wrong figure about now. For a range, name both ends: "June to \
August".

7. A purchase, a buy or a spend means type "expense". Sorting transactions \
without it puts the salary at the top of the list.

8. Whenever you name a transaction, quote its "store" field exactly as it is \
written, then its category and its date: "Vuokra Otavantie 7 C 38 (Rent), \
650 € on 2 July". Never paraphrase the shop as its category — "the rent" is \
not what the row says, and the whole use of naming one charge is being told \
which one it was.

9. "My biggest expense" or "my largest charge" wants one row: call \
search_transactions with type "expense" and limit 1, which returns the shop and \
its category together. Use category_breakdown when the question is about where \
the money goes overall — "what do I spend most on", "break down my month".

"My biggest category and the top 3 things in it" is one lookup, not two: over a \
single month category_breakdown already carries top_items on its largest few \
categories. Read them from there. Only when the question names a category the \
breakdown did not fill in should you search again — and then pass \
categories: ["<that one>"], because searching without it returns the biggest \
charges of the whole month under that category's name, which is a different \
answer that will look right. If fewer items come back than were asked for, that \
is how many there were: say so rather than padding.

10. "Analyse my month", "what stands out", "anything unusual" — call \
analyse_month once. It returns the whole month at once, so do not go fetching \
the pieces separately. Then write a reading, not a list: lead with the one or \
two things a person would actually want to know, say what moved and against \
what, and name the charge behind a category that jumped. Ignore everything \
marked "as usual" — that is the point of the flag.

Two different comparisons sit on every category and they must not be mixed. \
last_month_eur is what it cost the month before: that is the one for "fell from \
380 € to 87 €". usual_month_eur is what it normally costs over six months: that \
is the one for "against its usual 292 €". Saying the usual figure was last \
month's is a false sentence built out of true numbers. Each carries its own \
direction — vs_last_month_direction and vs_usual_direction — and they \
disagree often, because a category can be above its usual and below last month \
at once. Read the direction of whichever comparison you are making. Never work \
it out from the two figures yourself.

Do not gather several categories into one claim. "Going out, travel and gifts \
were all lower" is wrong the moment one of them was not, and it will be. Give \
each its own direction or leave it out. Six or eight sentences here, \
not two; this is the one question worth a longer answer.

11. Use the exact category names listed in the context below. Do not invent them.

12. A single month's breakdown carries the comparison already made: \
usual_month, the difference in vs_usual, and direction ("above" or "below"). \
Read direction; do not work it out from the two figures. reads_as says whether \
the gap is worth mentioning at all. usual_month — what that category \
normally costs. Mention it when the difference is interesting. Skip it for \
anything marked is_fixed_cost, because rent has no news in it.

Answer in two or three sentences — this is a side panel, not a report. The one \
exception is rule 10: a reading of a whole month is worth a paragraph. Amounts \
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


def chat(messages, backend=None, on_event=None):
    """Answer the conversation in ``messages``; return the reply and a trace.

    ``messages`` is ``[{"role", "content"}, ...]`` with the user's turn last.
    Returns ``{"reply", "tool_calls", "usage", "backend"}``. ``tool_calls`` is
    what the assistant actually looked at, so the panel can show the working
    rather than asking anyone to take a number on trust.

    ``on_event`` makes the same turn watchable while it happens instead of only
    once it is over. It is called with:

    ``{"type": "tool", ...}``   a lookup starting, named
    ``{"type": "looked_up", ...}``  that lookup done, with the months it read
    ``{"type": "token", "text"}``   a piece of the answer

    The wait is five seconds and two model calls, and almost all of it used to
    be spent showing nothing. The events are advisory: the return value is the
    same either way, and a caller that passes nothing gets exactly the old
    behaviour.
    """
    backend = backend or get_backend()

    def emit(event):
        if on_event:
            on_event(event)
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

    def stream_text(piece):
        emit({"type": "token", "text": piece})

    for _ in range(MAX_TOOL_ROUNDS):
        turn = backend.complete(system, history, TOOL_SCHEMAS, on_token=stream_text)
        account(turn)

        if turn.refusal:
            return done(turn.refusal)
        if not turn.tool_calls:
            return done(turn.text)

        history.append({"role": "assistant", "content": turn.text,
                        "tool_calls": turn.tool_calls, "raw": turn.raw})
        for call in turn.tool_calls:
            # Named before it runs, so the panel can say what it is doing
            # rather than that it is doing something.
            emit({"type": "tool", "tool": call["name"], "arguments": call["input"]})
            output = run_tool(call["name"], call["input"])
            # The months the tool actually read, not the word the model passed.
            # "last month" is the answer's whole meaning and the model does not
            # reliably repeat it: asked what it spent on groceries last month it
            # answered "421 €" beside a Dashboard reading 338 € for August, and
            # a right number about July looked like a wrong one about now.
            trace.append({"tool": call["name"], "arguments": call["input"],
                          "period": (output or {}).get("period")
                                    or (output or {}).get("month"),
                          "ok": "error" not in output})
            emit({"type": "looked_up", **trace[-1]})
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
    final = backend.complete(system, history, tools=None, on_token=stream_text)
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
