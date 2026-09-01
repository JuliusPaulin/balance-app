"""The model validation harness, checked without a model.

`evals/` judges the assistant; this judges `evals/`. Two things have to hold or
a green eval run means nothing:

**The fixture is what the cases claim.** A case says the right answer is
`fx.eur(fx.groceries_last_month)`; the tools have to actually return that
figure for the month, or the suite is measuring the model against a number
nobody wrote.

**The graders catch the answers that shipped.** Every failure in CLAUDE.md is
replayed here as a scripted reply — the invented year total, the salary sold as
a subscription, the figure with no month on it — and the grader has to mark
each one wrong. A checker that passes everything is worse than none, because it
reads as evidence.
"""

import pytest

import ai_tools
from evals import cases as case_module
from evals import fixture as fixture_module
from evals import grading


@pytest.fixture
def fx(client):
    """The eval fixture, in the suite's own scratch database.

    Takes `client` for its side effect: the app has to exist for the tools to
    dispatch into, and `_clean_db` has already re-seeded the categories the
    fixture writes against.
    """
    return fixture_module.build()


# ── Reading a figure out of a sentence ────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("1 234,50", 1234.50),      # what the app writes
    ("1 234,50", 1234.50), # ...with the non-breaking space it really uses
    ("1,234.50", 1234.50),      # what a model retypes it as
    ("1234", 1234.0),
    ("612", 612.0),
    ("36 840", 36840.0),
])
def test_an_amount_reads_the_same_however_it_is_written(text, expected):
    assert grading.parse_amount(text) == expected


@pytest.mark.parametrize("reply, expected", [
    ("You spent 612 € on groceries.", [612.0]),
    ("€1 249 at Verkkokauppa.com", [1249.0]),
    ("27,98 EUR a month", [27.98]),
    ("It came to 3 523 € against 3 598 €.", [3523.0, 3598.0]),
])
def test_every_euro_figure_in_a_reply_is_found(reply, expected):
    assert grading.figures(reply) == expected


@pytest.mark.parametrize("space, what", [
    ("\u00a0", "no-break space — what the app writes"),
    ("\u202f", "narrow no-break space — what Qwen 9b writes"),
    (" ", "an ordinary space — what Qwen 4b writes"),
    ("\u2009", "a thin space"),
])
def test_any_invisible_space_between_the_digits_is_the_same_figure(space, what):
    """The grader failed a correct net worth four times out of four on the
    character the model chose to put between the digits. None of them is
    visible, all of them are the same money, and a grader that fails on one is
    measuring itself."""
    reply = f"Your net worth is -122{space}300{space}€."
    assert grading.figures(reply) == [122300.0], what
    assert grading.states(reply, ai_tools._eur(-122300)) is True
    assert grading.reformatted(reply) == []


def test_a_number_that_is_not_money_is_not_a_claim_about_money():
    """"the last 3 months" needs no source. Demanding one for every digit is
    how a grader ends up ignored."""
    assert grading.figures("Over the last 3 months, across 12 categories.") == []


# ── Grounding: the check the whole harness is for ─────────────────────────

def test_a_total_the_model_worked_out_itself_is_caught():
    """The recorded failure: twelve monthly figures added up by hand, answered
    as 36 135 € against a real 36 840 €."""
    outputs = [{"months": [{"income": 3070.0, "income_eur": "3 070 €"}],
                "total_income": 36840.0, "total_income_eur": "36 840 €"}]
    assert grading.ungrounded("You earned 36 135 € last year.", outputs) == [36135.0]


def test_quoting_the_figure_the_tool_gave_is_grounded():
    outputs = [{"total_income_eur": "36 840 €"}]
    assert grading.ungrounded("You earned 36 840 € last year.", outputs) == []


def test_retyping_the_separator_is_not_an_invention():
    """The tools group thousands with a non-breaking space; models use whatever
    they like. Same figure, and a grader that says otherwise is measuring its
    own strictness."""
    outputs = [{"total_eur": ai_tools._eur(36840)}]
    assert grading.ungrounded("36,840 € last year", outputs) == []
    assert grading.ungrounded("36840 euros last year", outputs) == []


def test_rounding_the_cents_off_is_not_an_invention():
    outputs = [{"amount": 88.40, "amount_eur": "88 €"}]
    assert grading.ungrounded("A shop at 88 €.", outputs) == []


def test_a_figure_from_a_deeper_row_still_counts_as_read():
    """Walked recursively rather than off known keys, so a tool that grows a
    field does not become a place to invent from."""
    outputs = [{"categories": [{"top_items": [{"amount_eur": "1 490 €"}]}]}]
    assert grading.ungrounded("Your biggest was 1 490 €.", outputs) == []


def test_an_answer_with_no_lookup_behind_it_has_nothing_to_stand_on():
    assert grading.ungrounded("You spent about 400 € on groceries.", []) == [400.0]


# ── The right figure, written a different way ─────────────────────────────

def test_a_figure_repunctuated_is_still_the_figure():
    """The model wrote "€1,490" where the tool said "1 490 €". Same money. A
    grader that calls that a miss buries the runs where the number itself was
    wrong, which is the only thing this suite is really watching for."""
    assert grading.states("a €1,490 purchase from Verkkokauppa.com",
                          ai_tools._eur(1490)) is True
    assert grading.states("a €1,940 purchase", ai_tools._eur(1490)) is False


def test_the_wrong_shop_is_still_the_wrong_shop():
    """Only amounts are graded loosely. A name is a name."""
    assert grading.states("bought at Verkkokauppa.com", "Verkkokauppa.com") is True
    assert grading.states("bought at the electronics shop", "Verkkokauppa.com") is False


def test_writing_an_amount_in_another_style_is_reported_on_its_own():
    """Reported, not folded into the figure check: the app prints "1 490 €"
    everywhere, and a panel saying "€1,490" beside a Dashboard saying
    "1 490 €" is the same money looking like a different app."""
    assert grading.reformatted(f"It was {ai_tools._eur(1490)}.") == []
    assert grading.reformatted("It was €1,490.") == ["€1,490"]
    # Which kind of space was typed is not a style anyone can see, and treating
    # it as one turned every case in the suite red.
    assert grading.reformatted("It was 22 400 €.") == []


def test_a_note_about_style_never_fails_a_run():
    """It is a note on the answer, not a verdict on it. A headline number
    dominated by punctuation is one nobody reads."""
    result = {"reply": "You spent €1,490.", "tool_calls": []}
    case = case_module.Case(id="x", question="?", why="?")
    style = next(c for c in grading.check(case, result, [{"total": 1490}])
                 if "format" in c.name)
    assert (style.ok, style.advisory) == (False, True)
    assert style not in grading.failures(grading.check(case, result, [{"total": 1490}]))


# ── Which months a lookup covered ─────────────────────────────────────────

def test_months_are_read_from_the_arguments_not_the_label():
    """A label like "2026-05 to 2026-07" does not contain the month in the
    middle of it, and a case asking about June would be marked wrong for a
    lookup that read it."""
    call = {"tool": "category_breakdown", "arguments": {"period": "last_3_months"},
            "period": "2026-05 to 2026-07"}
    assert len(grading.months_read(call)) == 3


def test_named_months_are_taken_as_given():
    call = {"tool": "monthly_summary",
            "arguments": {"months": ["2026-06", "2026-07"]}}
    assert grading.months_read(call) == ["2026-06", "2026-07"]


def test_an_invented_period_read_nothing():
    """The tool refused it, so no month was read — which is what the case
    should see, rather than a fallback that looks like an answer."""
    call = {"tool": "category_breakdown", "arguments": {"period": "last_2_months"}}
    assert grading.months_read(call) == []


@pytest.mark.parametrize("tool", ["list_subscriptions", "net_worth_summary"])
def test_a_tool_about_no_month_reads_no_month(tool):
    assert grading.months_read({"tool": tool, "arguments": {}}) == []


# ── The fixture is what the cases say it is ───────────────────────────────

def test_the_tools_report_the_groceries_the_fixture_wrote(fx):
    result = ai_tools.category_breakdown(month=fx.last_month)
    groceries = next(c for c in result["categories"] if c["category"] == "Groceries")
    assert groceries["total_eur"] == fx.eur(fx.groceries_last_month)


def test_the_fixture_month_in_progress_is_empty(fx):
    """The case about "this month" only means something if the month really is
    thin — otherwise it is a second copy of the last-month case."""
    result = ai_tools.category_breakdown(month=fx.this_month)
    assert result["categories"] == []


def test_the_biggest_charge_is_the_one_the_case_names(fx):
    result = ai_tools.search_transactions(month=fx.last_month, type="expense", limit=1)
    top = result["transactions"][0]
    assert (top["store"], top["amount_eur"]) == ("Verkkokauppa.com", fx.eur(1490.00))


def test_net_worth_is_assets_less_the_mortgage(fx):
    assert ai_tools.net_worth_summary()["net_worth_eur"] == fx.eur(fx.net_worth)
    assert fx.net_worth < 0


def test_the_stopped_service_is_not_part_of_the_monthly_bill(fx):
    """The gym stopped four months ago. Detection still finds it; what it must
    not do is charge the user for it every month."""
    result = ai_tools.list_subscriptions()
    counted = [s["merchant"] for s in result["subscriptions"]]
    stopped = [s["merchant"] for s in result["also_recurring"]]
    assert "Elixia Tapiola" not in counted
    assert "Acme Oy palkka" not in counted
    assert "Elixia Tapiola" in stopped
    assert "Spotify AB" in counted


def test_a_case_asks_for_a_figure_the_fixture_can_actually_return(fx):
    """Every `must_say` figure has to exist somewhere in the fixture. A case
    demanding a number nobody wrote fails for ever and gets muted."""
    known = {fx.eur(row["amount"]) for row in fx.rows}
    known |= {fx.eur(v) for v in (fx.groceries_last_month, fx.net_worth,
                                  fx.assets, fx.usual_groceries)}
    known |= {fx.eur(fx.groceries[m]) for m in fx.months}
    # The one figure a case reads from a tool rather than writing down — what a
    # series costs per month is recurring.py's answer, not this file's.
    known.add(ai_tools.list_subscriptions()["monthly_total_eur"])
    for case in case_module.build(fx):
        for phrase in case.must_say:
            if phrase.endswith("€"):
                assert phrase in known, f"{case.id} wants {phrase!r}, nothing has it"


# ── The harder cases, and what they rest on ───────────────────────────────

def test_medical_moves_both_ways_at_once(fx):
    """The case only means something if the two comparisons really disagree:
    74 € is below the 335 € of the month before and above a usual of nearly
    nothing. A fixture where they agree cannot see the inverted sentence."""
    month = ai_tools.analyse_month(month=fx.last_month)
    medical = next(c for c in month["categories"] if c["category"] == "Medical")

    assert medical["vs_last_month_direction"] == "down"
    assert medical["vs_usual_direction"] == "above"


def test_the_biggest_category_is_not_the_biggest_charge(fx):
    """Travel is 1 800 € across two charges; the largest single charge is
    1 490 € at one shop. Answering the second when the first was asked looks
    entirely right, and a fixture where they coincide cannot tell them apart.
    """
    breakdown = ai_tools.category_breakdown(month=fx.last_month)
    top_charge = ai_tools.search_transactions(
        month=fx.last_month, type="expense", limit=1)["transactions"][0]

    assert breakdown["categories"][0]["category"] == fx.biggest_category_last_month
    assert top_charge["category"] == "Electronics"


def test_nothing_in_the_fixture_is_called_prisma(fx):
    """The unknown-merchant case is a claim about the database, so hold the
    database to it."""
    assert ai_tools.search_transactions(q="Prisma", period="all_time")["matched"] == 0


def test_a_long_report_fails_the_side_panel_check(fx):
    """9b answers a one-figure question with a six-line bulleted list. Every
    number right, and not what a side panel is for — and nothing else in the
    suite could see it."""
    case = _case(fx, "answer-length")
    trace = [{"tool": "category_breakdown", "arguments": {"period": "last_month"},
              "period": fx.last_month, "ok": True}]
    outputs = [ai_tools.category_breakdown(month=fx.last_month)]

    short = f"You paid {fx.eur(1250)} in rent in {fx.name(fx.last_month)}."
    report = short + "\n" + "\n".join(
        f"- **Item {n}**: some detail about the row and what it cost" for n in range(12))

    assert _grade(case, short, trace, outputs)["short enough for a side panel"]
    assert _grade(case, report, trace, outputs)["short enough for a side panel"] is False


def test_a_case_with_no_turns_is_a_single_question():
    plain = case_module.Case(id="x", question="what did I spend?", why="?")
    assert plain.conversation() == ["what did I spend?"]


def test_a_follow_up_is_graded_on_its_own_answer(fx):
    """Only the last turn is graded. The turn above it exists to be remembered
    — 'the month before that' has no nouns of its own."""
    case = _case(fx, "follow-up")
    july = fx.months[-2]
    assert case.conversation()[-1] == "and the month before that?"

    trace = [{"tool": "category_breakdown", "arguments": {"month": july},
              "period": july, "ok": True}]
    outputs = [ai_tools.category_breakdown(month=july)]

    right = (f"In {fx.name(july)} you spent {fx.eur(fx.groceries[july])} "
             "on groceries.")
    lost_the_thread = (f"In {fx.name(fx.last_month)} you spent "
                       f"{fx.eur(fx.groceries_last_month)} on groceries.")

    assert all(_grade(case, right, trace, outputs).values())
    assert _grade(case, lost_the_thread, trace, outputs)[
        f"says {fx.name(july)!r}"] is False


# ── The graders replayed against the answers that shipped ─────────────────

def _grade(case, reply, trace, outputs):
    """Every check by name, advisories left out — those are notes on a run, not
    the verdict on it."""
    result = {"reply": reply, "tool_calls": trace}
    return {c.name: c.ok for c in grading.check(case, result, outputs)
            if not c.advisory}


def _case(fx, case_id):
    return next(c for c in case_module.build(fx) if c.id == case_id)


def test_a_right_answer_passes_every_check(fx):
    case = _case(fx, "groceries-last-month")
    outputs = [ai_tools.category_breakdown(month=fx.last_month)]
    trace = [{"tool": "category_breakdown", "arguments": {"period": "last_month"},
              "period": fx.last_month, "ok": True}]
    reply = (f"You spent {fx.eur(fx.groceries_last_month)} on groceries in "
             f"{fx.name(fx.last_month)}.")
    assert all(_grade(case, reply, trace, outputs).values())


def test_a_figure_with_no_month_on_it_is_marked_wrong(fx):
    """The answer that read "421 €" beside a Dashboard showing another month.
    Right number, and it looked like a wrong one."""
    case = _case(fx, "groceries-last-month")
    outputs = [ai_tools.category_breakdown(month=fx.last_month)]
    trace = [{"tool": "category_breakdown", "arguments": {"period": "last_month"},
              "period": fx.last_month, "ok": True}]
    reply = f"You spent {fx.eur(fx.groceries_last_month)} on groceries last month."
    assert _grade(case, reply, trace, outputs)[f"says {fx.name(fx.last_month)!r}"] is False


def test_a_three_month_total_reported_as_one_month_is_marked_wrong(fx):
    """Asked about a named month it reached for last_3_months and answered with
    the window's total. Both checks have to fire: the wrong months, and the
    figure that goes with them."""
    case = _case(fx, "named-month")
    june = fx.months[-3]
    outputs = [ai_tools.category_breakdown(period="last_3_months")]
    trace = [{"tool": "category_breakdown", "arguments": {"period": "last_3_months"},
              "period": "a window", "ok": True}]
    reply = (f"You spent {fx.eur(fx.groceries[june] * 3)} on groceries in "
             f"{fx.name(june)}.")
    grades = _grade(case, reply, trace, outputs)
    assert grades["months"] is False
    assert grades[f"never says {fx.eur(fx.groceries[june] * 3)!r}"] is False


def test_the_salary_folded_into_the_monthly_bill_is_marked_wrong(fx):
    """It sorted every recurring row by cost and led with the salary, labelled
    "(income)". A flag on a row is not a boundary."""
    case = _case(fx, "subscriptions")
    result = ai_tools.list_subscriptions()
    outputs = [result]
    trace = [{"tool": "list_subscriptions", "arguments": {}, "period": None,
              "ok": True}]
    inflated = ai_tools._eur(result["monthly_total"] + 3200)
    reply = f"Your recurring charges come to {inflated} a month."
    assert _grade(case, reply, trace, outputs)[f"never says {inflated!r}"] is False


def test_naming_a_stopped_service_and_its_cost_is_not_a_failure(fx):
    """Twice this case was written as "never mention the gym" — first its
    name, then its cost — and twice it marked a right answer wrong. A stopped
    service belongs in the reply, with the reason, and out of the total. The
    9b model named it, priced it and filed it under "doesn't count", which is
    the best answer anything has given this question.
    """
    case = _case(fx, "subscriptions")
    result = ai_tools.list_subscriptions()
    trace = [{"tool": "list_subscriptions", "arguments": {}, "period": None,
              "ok": True}]
    reply = (f"Your subscriptions cost {result['monthly_total_eur']} a month. "
             f"A stopped Elixia Tapiola membership ({fx.eur(fixture_module.GYM)}) "
             "is not counted — the service ended.")
    assert all(_grade(case, reply, trace, [result]).values())


def test_answering_with_no_lookup_at_all_is_marked_wrong(fx):
    case = _case(fx, "net-worth")
    grades = _grade(case, "Your net worth is about 120 000 €.", [], [])
    assert grades["tool"] is False
    assert grades["grounded"] is False


def test_a_failed_lookup_is_not_a_pass_however_the_sentence_reads(fx):
    """A dispatch that does not answer used to read as "0 €". The answer can
    sound perfectly reasonable; the run must not be green."""
    case = _case(fx, "net-worth")
    trace = [{"tool": "net_worth_summary", "arguments": {}, "period": None,
              "ok": False}]
    grades = _grade(case, "I could not read your accounts.", trace,
                    [{"error": "net_worth_summary failed"}])
    assert grades["lookups succeeded"] is False


def test_taking_a_month_apart_by_hand_is_marked_wrong(fx):
    """Rule 10: one call. Four lookups is a small model losing the thread
    between them, and it reads as a pass if nobody counts."""
    case = _case(fx, "analyse-month")
    trace = [{"tool": "analyse_month", "arguments": {"month": fx.last_month},
              "period": fx.last_month, "ok": True},
             {"tool": "category_breakdown", "arguments": {"month": fx.last_month},
              "period": fx.last_month, "ok": True}]
    outputs = [ai_tools.analyse_month(month=fx.last_month),
               ai_tools.category_breakdown(month=fx.last_month)]
    grades = _grade(case, "Travel stands out.", trace, outputs)
    assert grades["tool"] is True
    assert grades["no stray lookups"] is False


def test_claiming_to_have_deleted_something_is_marked_wrong(fx):
    """Read-only on purpose: the app has no undo for a hand-edited row."""
    case = _case(fx, "read-only")
    good = _grade(case, "I can't change anything — I can only read your figures.",
                  [], [])
    bad = _grade(case, "I have removed the rent transaction for you.", [], [])
    assert all(good.values())
    assert bad["never says 'I have removed'"] is False
