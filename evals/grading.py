"""The graders. None of them needs a model, which is what makes them testable.

Four questions are asked of every answer, and they are the four things that
have actually gone wrong:

`tool_used`    did it look anything up, and the right thing?
`months_read`  did it read the months the question was about?
`grounded`     is every figure in the sentence one a tool handed it?
`says`         does the sentence carry what it has to — the month, the amount?

`grounded` is the one worth the trouble. Asked what it earned last year the
assistant added twelve monthly figures itself and answered 36 135 € against a
real 36 840 €: confident, right-looking, and 705 € out. No amount of reading
the reply catches that; comparing its figures against the tool results does.
"""

import re
import unicodedata
from collections import namedtuple

import ai_tools

# `advisory` marks a finding worth seeing that is not a wrong answer. A run
# whose headline number is dominated by punctuation is a run nobody reads, and
# the first version of the format check below turned every case red.
Check = namedtuple("Check", "name ok detail advisory")

def plain_spaces(text):
    """Every kind of Unicode space written as an ordinary one.

    The app separates thousands with U+00A0. Qwen 9b writes U+202F, a narrow
    no-break space, and 4b writes the ordinary one — all three are the same
    figure and none of them is visible. Without this the grader failed a
    correct net worth four times out of four on the character the model chose
    to put between the digits, which is the grader measuring itself.
    """
    return "".join(" " if unicodedata.category(c) == "Zs" else c
                   for c in (text or ""))


# A figure with a euro sign on either side of it. Nothing else is checked:
# dates, counts and "two or three" are not claims about money, and demanding a
# source for every digit would flag "the last 3 months".
_AMOUNT = r"\d[\d\s .,]*"
_EUR_FIGURE = re.compile(rf"(?:€\s*({_AMOUNT}))|(?:({_AMOUNT})\s*(?:€|EUR\b|euros?\b))",
                         re.IGNORECASE)
_PERCENT = re.compile(rf"({_AMOUNT})\s*%")

# How far a quoted figure may sit from the one the tool gave. The tools round to
# whole euros and a model may quote either the string or the raw float, so a
# euro of slack is honest; anything more is a different number.
TOLERANCE = 1.0


def parse_amount(text):
    """Read "1 234,50", "1,234.50" or "1234" as a number.

    The tools write Finnish — a non-breaking space for thousands — and a model
    retypes it with whatever separator it feels like. Neither is a wrong figure,
    so both have to read back to the same one.
    """
    cleaned = text.strip().replace(" ", "").replace(" ", "")
    cleaned = cleaned.rstrip(".,")
    if not cleaned:
        return None
    last_dot, last_comma = cleaned.rfind("."), cleaned.rfind(",")
    decimal = max(last_dot, last_comma)
    if decimal != -1 and len(cleaned) - decimal - 1 in (1, 2):
        whole = re.sub(r"[.,]", "", cleaned[:decimal])
        cleaned = f"{whole}.{cleaned[decimal + 1:]}"
    else:
        cleaned = re.sub(r"[.,]", "", cleaned)
    try:
        return float(cleaned)
    except ValueError:
        return None


def figures(text):
    """Every euro amount stated in a reply, as numbers."""
    found = []
    for match in _EUR_FIGURE.finditer(plain_spaces(text)):
        value = parse_amount(match.group(1) or match.group(2) or "")
        if value is not None:
            found.append(value)
    return found


def percentages(text):
    return [v for v in (parse_amount(m.group(1))
                        for m in _PERCENT.finditer(plain_spaces(text)))
            if v is not None]


def sources(output):
    """Every number a tool result offers, raw floats and `_eur` strings alike.

    Walked recursively rather than read off known keys, so a tool that grows a
    field does not quietly become a place the model may invent from.
    """
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            found.add(float(node))
        elif isinstance(node, str):
            for value in figures(node):
                found.add(value)
    walk(output)
    return found


def ungrounded(reply, outputs):
    """The figures in `reply` that no tool result contains.

    A rounding is not an invention: the tools round to whole euros, so 88,40 €
    quoted as 88 € is the same claim. A hundred euros out is not.
    """
    available = set()
    for output in outputs:
        available |= sources(output)
    rounded = {round(v) for v in available}
    loose = []
    for value in figures(reply):
        if value in available or round(value) in rounded:
            continue
        if any(abs(value - known) <= TOLERANCE for known in available):
            continue
        loose.append(value)
    return loose


def months_read(call):
    """The months one tool call actually covered.

    Read from the arguments rather than the `period` label, because a label
    like "2026-05 to 2026-07" does not contain the month in the middle of it
    and a case asking about June would be marked wrong for a call that read it.
    """
    name = call.get("tool")
    args = call.get("arguments") or {}
    if name in ("list_subscriptions", "net_worth_summary"):
        return []
    if name == "analyse_month":
        month = args.get("month") or (call.get("period") or None)
        return [month] if month else []
    if name == "annual_report":
        year = args.get("year")
        return [f"{int(year):04d}-{m:02d}" for m in range(1, 13)] if year else []
    if args.get("months"):
        return [str(m) for m in args["months"]]
    try:
        return ai_tools.resolve_period(
            period=args.get("period"), month=args.get("month"),
            date_from=args.get("date_from"), date_to=args.get("date_to"),
        )["months"]
    except ValueError:
        # An invented period name. The tool refused it, so nothing was read —
        # which is exactly what the case should see.
        return []


def check(case, result, outputs):
    """Grade one answered question. Returns `[(name, ok, detail), ...]`.

    Every check is reported, passing or not: a run that says only what failed
    cannot be read as evidence that the rest held.
    """
    reply = result.get("reply") or ""
    trace = result.get("tool_calls") or []
    called = [c["tool"] for c in trace]
    checks = []

    def add(name, ok, detail="", advisory=False):
        checks.append(Check(name, bool(ok), detail, advisory))

    if case.tools:
        ok = bool(called) and any(t in case.tools for t in called)
        add("tool", ok, f"called {called or ['nothing']}, wanted one of "
                        f"{sorted(case.tools)}")
    else:
        # A case with no tool named is one where looking nothing up is a fine
        # answer — "you cannot delete anything" reads no figures. Reported all
        # the same, because what it called is worth seeing either way.
        add("tool", True, f"called {called or ['nothing']}")

    if case.forbid_tools:
        stray = sorted(set(called) & set(case.forbid_tools))
        add("no stray lookups", not stray, f"also called {stray}" if stray else "")

    failed = [c["tool"] for c in trace if not c.get("ok")]
    add("lookups succeeded", not failed, f"failed: {failed}" if failed else "")

    if case.months is not None:
        read = set()
        for call in trace:
            read |= set(months_read(call))
        wanted = set(case.months)
        add("months", wanted <= read if wanted else not read,
            f"read {sorted(read) or 'nothing'}, wanted {sorted(wanted)}")

    loose = ungrounded(reply, outputs)
    add("grounded", not loose,
        f"figures no tool returned: {loose}" if loose else "")

    for phrase in case.must_say:
        add(f"says {phrase!r}", states(reply, phrase))
    for phrase in case.must_not_say:
        add(f"never says {phrase!r}", not states(reply, phrase))

    if case.max_chars:
        # The system prompt says two or three sentences, because this is a side
        # panel beside the figures being asked about. 9b answers a one-figure
        # question with a six-line bulleted report — every number right, and
        # not what the panel is for. Nothing else in the suite could see it.
        add("short enough for a side panel", len(reply) <= case.max_chars,
            f"{len(reply)} characters, cap is {case.max_chars}")

    misformatted = reformatted(reply)
    add("the app's own number format", not misformatted,
        f"wrote {misformatted}" if misformatted else "", advisory=True)

    return checks


def failures(checks):
    """The checks that make a run wrong, advisories left out."""
    return [c for c in checks if not c.ok and not c.advisory]


def states(reply, phrase):
    """Did the reply make this claim?

    An amount is graded as a number, not as a string. "€1,490" is the figure
    the tool gave, re-punctuated: the same claim about the same money, and a
    grader that calls it a miss buries the runs where the figure itself was
    wrong. Whether it should have been written that way is a separate check,
    below, so the two findings stay apart.
    """
    wanted = figures(phrase)
    if len(wanted) == 1 and not re.search(r"[A-Za-z]", phrase):
        return any(abs(v - wanted[0]) <= TOLERANCE for v in figures(reply))
    return _said(reply, phrase)


def reformatted(reply):
    """Amounts written in some style other than the app's own.

    The app prints "1 490 €" everywhere — sign last, non-breaking space for
    thousands, no decimals — and the assistant is told to copy the string it
    was handed rather than retype it. "€1,490" in a panel beside a Dashboard
    reading "1 490 €" is the same money looking like a different app. Small,
    and worth counting: it is the visible edge of the model retyping figures
    instead of quoting them, which is the habit that produces wrong ones.
    """
    def flat(text):
        """One ordinary space for any run of space. Which kind of space was
        typed is not a style a reader can see, and treating it as one flagged
        every answer in the suite — including the ones that were right."""
        return re.sub(r"\s+", " ", plain_spaces(text))

    off = []
    for match in _EUR_FIGURE.finditer(plain_spaces(reply)):
        # The regex takes the whole figure and whatever punctuation trails it;
        # the full stop ending the sentence is not a formatting choice.
        written = match.group(0).strip().rstrip(".,")
        value = parse_amount(match.group(1) or match.group(2) or "")
        if value is None:
            continue
        if flat(written) != flat(ai_tools._eur(value)):
            off.append(written)
    return off


def _said(reply, phrase):
    """Whitespace-insensitive, case-insensitive containment.

    The tools separate thousands with a non-breaking space and models retype it
    as an ordinary one. That is the same figure, and a grader that calls it a
    miss is measuring its own strictness.
    """
    def flat(text):
        return re.sub(r"[\s ]+", "", text).lower()
    return flat(phrase) in flat(reply)
