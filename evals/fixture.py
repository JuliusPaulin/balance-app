"""A database whose every answer is known before the model is asked.

The real database is the honest thing to judge a model against and the useless
thing to judge it with: the figures move every time a statement is imported, so
"it answered 421 €" cannot be written down as a pass. This builds a small one
with the same shape — a salary, a rent, a weekly shop, some subscriptions, one
service that stopped, and one month with a spike in it — and hands back the
totals it wrote, formatted the way the tools would.

Months are laid down relative to today, because "last month" has to mean
something. Amounts are fixed, never random: a case says the answer is
`fx.eur(fx.groceries[fx.last_month])` and that is a number, not a range.
"""

from datetime import date

import ai_tools


def _month_add(month, delta):
    index = int(month[:4]) * 12 + int(month[5:7]) - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December")

# Twelve full months behind the current one, which is deliberately left thin:
# the month in progress is thin in a real database too, and an assistant that
# reads it as a full month is the bug that makes "this month" look like a
# collapse in spending.
HISTORY_MONTHS = 12

SALARY = 3200.00
RENT = 1250.00
# The weekly shop, four rows a month. The last month is heavier on purpose so
# "how did last month compare" has an answer that is not zero.
GROCERY_ROWS = (88.40, 63.10, 104.75, 71.30)
GROCERY_ROWS_LAST_MONTH = (88.40, 63.10, 104.75, 71.30, 96.55)
SPOTIFY = 11.99
NETFLIX = 15.99
GYM = 39.90              # stopped four months ago
# Above the rent, which is the other big round number in the month — a
# fixture where the biggest charge is the rent tests nothing, because the rent
# is the one charge the assistant is told to skip as having no news in it.
BIG_PURCHASE = 1490.00
# The spike is two charges, not one, so the biggest *category* and the biggest
# single *charge* are different rows. A fixture where they are the same row
# cannot tell an assistant that confuses the two questions from one that does
# not — and confusing them is the ordinary mistake.
TRAVEL_SPIKE = (940.00, 860.00)

# Net worth is kept by hand in this app, so the fixture keeps some by hand too.
# A mortgage is in there because a net worth that is a positive number is the
# easy case, and "how am I doing" against a negative one is the real one.
ACCOUNTS = (
    ("Nordea käyttötili", "asset", 4200.00),
    ("Nordnet salkku", "asset", 18500.00),
    ("Asuntolaina", "liability", 145000.00),
)


class Fixture:
    """What was written, and the strings a right answer would quote."""

    def __init__(self, today=None):
        self.today = today or date.today()
        self.this_month = self.today.strftime("%Y-%m")
        self.last_month = _month_add(self.this_month, -1)
        self.months = [_month_add(self.this_month, -n)
                       for n in range(HISTORY_MONTHS, 0, -1)]
        self.groceries = {}
        self.expense = {}
        self.income = {}
        self.rows = []

    # ── naming ────────────────────────────────────────────────────────────
    def name(self, month):
        """"July" — what rule 6 says every answer has to carry."""
        return _MONTH_NAMES[int(month[5:7]) - 1]

    def eur(self, amount):
        """The string the tools hand the model, which it is told to copy."""
        return ai_tools._eur(amount)

    # ── the money ─────────────────────────────────────────────────────────
    @property
    def groceries_last_month(self):
        return self.groceries[self.last_month]

    @property
    def usual_groceries(self):
        """The median of the six months before last — the baseline a
        breakdown reports, and the figure a model likes to confuse with
        last month's."""
        window = [self.groceries[m] for m in self.months[-7:-1]]
        window.sort()
        return round((window[2] + window[3]) / 2, 2)

    @property
    def total_expense_last_month(self):
        return self.expense[self.last_month]

    @property
    def net_last_month(self):
        return round(self.income[self.last_month] - self.expense[self.last_month], 2)

    @property
    def assets(self):
        return round(sum(b for _, kind, b in ACCOUNTS if kind == "asset"), 2)

    @property
    def liabilities(self):
        return round(sum(b for _, kind, b in ACCOUNTS if kind == "liability"), 2)

    @property
    def net_worth(self):
        return round(self.assets - self.liabilities, 2)

    def income_in(self, year):
        """What was earned in one calendar year of the fixture.

        Part-years are the normal case here — the fixture reaches back twelve
        months, so last year is the months of it that fall inside. That is the
        point: quoting this year's total as last year's is the mistake.
        """
        return round(sum(v for m, v in self.income.items()
                         if m.startswith(str(year))), 2)

    @property
    def stopped_subscription(self):
        """The gym. Detection still finds it; what it must not do is charge
        the user for it every month."""
        return "Elixia Tapiola"


def build(today=None):
    """Write the fixture into whatever database is configured; return it.

    The caller is responsible for pointing `SQLITE_PATH` somewhere throwaway
    before importing config — the same rule the test suite runs under.
    """
    import config
    import db

    fx = Fixture(today)
    uid = config.LOCAL_USER_ID
    stopped_from = fx.months[-4]   # the gym's last charge

    with db.db_conn() as conn:
        cats = {(r["name"], r["type"]): r["id"] for r in conn.execute(
            "SELECT id, name, type FROM categories WHERE user_id = %s", (uid,)
        ).fetchall()}

        def add(month, day, store, category, amount, kind="expense"):
            cid = cats[(category, kind)]
            when = f"{month}-{day:02d}"
            conn.execute(
                "INSERT INTO transactions (user_id, date, store, category_id,"
                " amount, type) VALUES (%s, %s, %s, %s, %s, %s)",
                (uid, when, store, cid, amount, kind),
            )
            fx.rows.append({"date": when, "store": store, "category": category,
                            "amount": amount, "type": kind})
            if kind == "expense":
                fx.expense[month] = round(fx.expense.get(month, 0) + amount, 2)
                if category == "Groceries":
                    fx.groceries[month] = round(
                        fx.groceries.get(month, 0) + amount, 2)
            else:
                fx.income[month] = round(fx.income.get(month, 0) + amount, 2)

        for month in fx.months:
            add(month, 25, "Acme Oy palkka", "Job", SALARY, "income")
            add(month, 1, "Vuokra Otavantie 7 C 38", "Rent", RENT)
            shop = (GROCERY_ROWS_LAST_MONTH if month == fx.last_month
                    else GROCERY_ROWS)
            for i, amount in enumerate(shop):
                add(month, 3 + i * 6, "K-Supermarket Iso Omena", "Groceries", amount)
            add(month, 8, "Spotify AB", "Entertainment", SPOTIFY)
            add(month, 14, "Netflix.com", "Entertainment", NETFLIX)
            if month <= stopped_from:
                add(month, 2, "Elixia Tapiola", "Exercise", GYM)

        # The two things that make "what stands out" a question with an answer.
        add(fx.last_month, 17, "Verkkokauppa.com", "Electronics", BIG_PURCHASE)
        add(fx.last_month, 21, "Finnair", "Travel", TRAVEL_SPIKE[0])
        add(fx.last_month, 22, "Hotel Kämp", "Travel", TRAVEL_SPIKE[1])

        for order, (name, kind, balance) in enumerate(ACCOUNTS):
            row = conn.execute(
                "INSERT INTO accounts (user_id, name, type, sort_order)"
                " VALUES (%s, %s, %s, %s) RETURNING id",
                (uid, name, kind, order),
            ).fetchone()
            conn.execute(
                "INSERT INTO account_balances (user_id, account_id, as_of,"
                " balance) VALUES (%s, %s, %s, %s)",
                (uid, row["id"], f"{fx.last_month}-28", balance),
            )

    return fx
