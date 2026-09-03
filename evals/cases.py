"""The questions, and what a right answer to each has to contain.

Every case here is a failure that happened, not one that might. The comment on
each says which — a case nobody can name a reason for is a case nobody will fix
when it goes red.

A case states as little as it can get away with. "The reply contains 421 €" is
checkable; "the reply is well written" is not, and a grader that guesses at it
turns a red run into a shrug.
"""

from dataclasses import dataclass, field


@dataclass
class Case:
    id: str
    question: str
    why: str                                  # the failure this case is for
    tools: set = field(default_factory=set)   # any one of these is right
    forbid_tools: set = field(default_factory=set)
    months: list = None                       # months the lookups must cover
    must_say: list = field(default_factory=list)
    must_not_say: list = field(default_factory=list)
    max_chars: int = None                     # "a side panel, not a report"
    # Everything the user says, in order. A case that leaves this alone is the
    # single question in `question`; a case that sets it is a conversation, and
    # only the last answer is graded — the earlier turns are there to be
    # remembered or forgotten.
    turns: list = None

    def conversation(self):
        return self.turns or [self.question]


def build(fx):
    """The suite, with every figure taken from the fixture that was written."""
    from ai import tools as ai_tools

    from evals import fixture

    # One figure is read from the tool rather than written down here, on
    # purpose. What a recurring series costs per month is `services/recurring.py`'s
    # answer — a monthly rent of 1 250 € normalises to 1 226 € over a 31-day
    # median gap — and re-deriving it here would make this file a second
    # implementation of it. `tests/test_recurring.py` is what holds that number
    # honest; this case only asks whether the model quotes it.
    subs = ai_tools.list_subscriptions()
    subscription_total = subs["monthly_total_eur"]

    def wrong_total(extra):
        """The monthly bill with something in it that does not belong."""
        return ai_tools._eur(subs["monthly_total"] + extra)

    last, this = fx.last_month, fx.this_month
    june = fx.months[-3]      # two months before last — a month with a name
    july = fx.months[-2]
    # The fixture reaches back twelve months, so part of last year is always in
    # it — enough for the one thing this case is about, which is whether the
    # model quotes the year's income or works it out from the monthly rows.
    last_year = str(int(this[:4]) - 1)
    if not any(m.startswith(last_year) for m in fx.months):
        last_year = None

    cases = [
        Case(
            id="groceries-last-month",
            question="what did I spend on groceries last month?",
            why="The answer that read 421 € with no month on it, beside a "
                "Dashboard showing 338 € for the month the user was looking at.",
            tools={"category_breakdown", "search_transactions"},
            months=[last],
            must_say=[fx.eur(fx.groceries_last_month), fx.name(last)],
        ),
        Case(
            id="named-month",
            question=f"how much did I spend on groceries in {fx.name(june)}?",
            why="Asked about June the model tried last_3_months, then "
                "last_6_months, then this_year, and reported a three-month "
                "total as one month's. months_by_name is in the context to "
                "stop exactly this.",
            tools={"category_breakdown", "search_transactions"},
            months=[june],
            must_say=[fx.eur(fx.groceries[june]), fx.name(june)],
            must_not_say=[fx.eur(fx.groceries[june] * 3)],
        ),
        Case(
            id="two-named-months",
            question=f"did I spend more in {fx.name(june)} or in {fx.name(july)}?",
            why="It invented a last_2_months period, was handed a window that "
                "did not contain June, and answered for the wrong months. "
                "monthly_summary takes the months by name instead.",
            tools={"monthly_summary", "category_breakdown"},
            months=[june, july],
            must_say=[fx.name(june), fx.name(july)],
        ),
        Case(
            id="biggest-expense",
            question="what was my single biggest expense last month?",
            why="Sorting without type=expense puts the salary at the top. And "
                "the whole use of naming one charge is being told which shop.",
            tools={"search_transactions", "analyse_month"},
            months=[last],
            must_say=["Verkkokauppa.com", fx.eur(1490.00)],
            must_not_say=["Acme Oy palkka"],
        ),
        Case(
            id="subscriptions",
            question="what are my subscriptions costing me each month?",
            why="It sorted every recurring row by cost and led with the salary, "
                "labelled '(income)'. And a service that stopped is not part of "
                "what this person pays each month. Nor is the rent: the tool "
                "handed over a list where 1 250 EUR of housing and a 12 EUR "
                "streaming service sat under one heading with one total.",
            tools={"list_subscriptions"},
            months=[],
            must_say=[subscription_total],
            # Twice now this case has been written as "never mention the gym" —
            # first its name, then its cost — and twice it marked a right
            # answer wrong. A stopped service belongs in the reply, with the
            # reason, and out of the total; 9b named it, priced it and filed it
            # under "doesn't count", which is the best answer anyone has given.
            # The falsifiable claim is the total itself, so what is forbidden
            # is a total with the gym, the salary or the rent folded into it.
            must_not_say=[wrong_total(fixture.GYM), wrong_total(3200),
                          wrong_total(fixture.RENT)],
        ),
        Case(
            id="analyse-month",
            question="analyse last month for me — anything unusual?",
            why="An open-ended question needs the whole month in one call. "
                "Fetching the pieces separately is four lookups and a small "
                "model losing the thread between them.",
            tools={"analyse_month"},
            forbid_tools={"category_breakdown", "monthly_summary",
                          "search_transactions"},
            months=[last],
            must_say=["Travel"],
            must_not_say=["Verkkokauppa.com is the largest category"],
        ),
        Case(
            id="net-worth",
            question="what is my net worth?",
            why="A negative net worth is the one people actually have, and "
                "reporting the assets alone is the comfortable way to get it "
                "wrong.",
            tools={"net_worth_summary"},
            months=[],
            must_say=[fx.eur(fx.net_worth)],
        ),
        Case(
            id="empty-month",
            question="how much have I spent on groceries this month?",
            why="The month in progress is thin. An assistant that reads it as "
                "a full month reports a collapse in spending that did not "
                "happen — and one that quietly answers for last month instead "
                "is worse.",
            tools={"category_breakdown", "search_transactions"},
            months=[this],
            must_not_say=[fx.eur(fx.groceries_last_month)],
        ),
        Case(
            id="read-only",
            question="delete my rent transaction from last month",
            why="The app has no undo for a hand-edited row, so the assistant "
                "is read-only. A 2B model reached for a delete_transaction "
                "tool that does not exist; the dispatcher refused it, but the "
                "answer must refuse it too.",
            must_not_say=["deleted", "I have removed"],
        ),
    ]

    cases += [
        Case(
            id="answer-length",
            question="what did I spend on rent last month?",
            why="A one-figure question answered as a six-line bulleted report. "
                "The panel sits beside the figures it is talking about, and "
                "the prompt says two or three sentences. Every number can be "
                "right and the answer still be the wrong shape.",
            tools={"category_breakdown", "search_transactions"},
            months=[last],
            must_say=[fx.eur(1250)],
            max_chars=400,
        ),
        Case(
            id="largest-category",
            question="what do I spend the most on?",
            why="The biggest category and the biggest single charge are "
                "different rows on purpose: Travel is 1 800 € across two "
                "charges, and the largest one charge is 1 490 € at "
                "Verkkokauppa. Answering the second question when the first "
                "was asked looks entirely right.",
            tools={"category_breakdown", "analyse_month"},
            must_say=[fx.biggest_category_last_month],
        ),
        Case(
            id="unknown-merchant",
            question="how much did I spend at Prisma last month?",
            why="Nothing in the database is called Prisma. An empty result "
                "reads exactly like 'you spent nothing', and the tempting "
                "answer is a figure from whatever did come back.",
            months=[last],
            must_not_say=[fx.eur(fx.total_expense_last_month),
                          fx.eur(fx.groceries_last_month)],
            max_chars=400,
        ),
        Case(
            id="no-data-year",
            question="what did I spend in 2019?",
            why="The database starts twelve months ago. A year it has never "
                "seen must come back as nothing known — not as this year's "
                "figures under a year nobody has data for, which is the same "
                "fault the 2025 answer had.",
            must_not_say=[fx.eur(fx.total_expense_last_month)],
        ),
        Case(
            id="mixed-direction",
            question="how did my medical spending go last month?",
            why="Medical is 74 € against 335 € the month before and near "
                "nothing usually, so it is below one comparison and above the "
                "other at the same time. Handed both figures the assistant "
                "wrote 'spiked to 74 €, up from 335 €' — a false sentence "
                "built out of two true numbers.",
            tools={"category_breakdown", "analyse_month", "search_transactions"},
            months=[last],
            must_say=[fx.eur(74)],
            must_not_say=[f"up from {fx.eur(335)}", f"rose from {fx.eur(335)}",
                          f"increased from {fx.eur(335)}"],
        ),
        Case(
            id="follow-up",
            question="what did I spend on groceries last month?",
            turns=["what did I spend on groceries last month?",
                   "and the month before that?"],
            why="A follow-up carries none of its own nouns. 'The month before "
                "that' means nothing without the turn above it, and a model "
                "that loses the thread answers about last month again — with "
                "a right figure, under the wrong question.",
            tools={"category_breakdown", "search_transactions"},
            months=[july],
            must_say=[fx.eur(fx.groceries[july]), fx.name(july)],
        ),
    ]

    if last_year:
        cases.append(Case(
            id="last-year-income",
            question=f"what did I earn in {last_year}?",
            why="It added twelve monthly income figures up itself and answered "
                "36 135 € against a real 36 840 €. This is the case the "
                "grounding check exists for.",
            tools={"annual_report", "monthly_summary"},
            months=[m for m in fx.months if m.startswith(last_year)],
            # The run that prompted this check: it read `this_year`, found
            # 22 400 €, and answered "in 2025, you earned 22 400 €". Grounded,
            # sourced, and about the wrong year — which is why the months a
            # lookup covered is a check of its own and not a detail.
            must_say=[last_year],
            must_not_say=[fx.eur(fx.income_in(this[:4]))],
        ))

    return cases
