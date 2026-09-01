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


def build(fx):
    """The suite, with every figure taken from the fixture that was written."""
    import ai_tools

    from evals import fixture

    # One figure is read from the tool rather than written down here, on
    # purpose. What a recurring series costs per month is `recurring.py`'s
    # answer — a monthly rent of 1 250 € normalises to 1 226 € over a 31-day
    # median gap — and re-deriving it here would make this file a second
    # implementation of it. `tests/test_recurring.py` is what holds that number
    # honest; this case only asks whether the model quotes it.
    subscription_total = ai_tools.list_subscriptions()["monthly_total_eur"]

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
                "what this person pays each month.",
            tools={"list_subscriptions"},
            months=[],
            must_say=[subscription_total],
            # The gym's cost, not the gym's name. Forbidding the name marked
            # "Elixia Tapiola has stopped recurring" as a failure, which is the
            # assistant doing exactly the right thing: a stopped series belongs
            # in the answer, out of the total. What must not appear is its
            # money, and the salary's.
            must_not_say=[fx.eur(fixture.GYM), fx.eur(3200)],
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
