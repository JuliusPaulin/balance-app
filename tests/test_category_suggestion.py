"""Tests for routes.csv_import.suggest_category.

The suggestion drives the import review table, so what it declines to guess
matters as much as what it guesses: a row with no suggestion lands in "needs
review", which is the honest outcome for a store whose history disagrees with
itself. The threshold here is deliberately the one
scripts/generate_merchant_rules.py uses when deciding whether a store has
earned a rule at all.
"""

from routes.csv_import import suggest_category


def _cat(conn, uid, name, type_="expense"):
    row = conn.execute(
        "SELECT id FROM categories WHERE user_id = %s AND name = %s AND type = %s",
        (uid, name, type_),
    ).fetchone()
    if row:
        return row["id"]
    return conn.execute(
        "INSERT INTO categories (user_id, name, type) VALUES (%s, %s, %s) RETURNING id",
        (uid, name, type_),
    ).fetchone()["id"]


def _history(conn, uid, store, pairs):
    """Give ``store`` a history: ``pairs`` is [(category name, how many)]."""
    for name, n in pairs:
        cid = _cat(conn, uid, name)
        for _ in range(n):
            conn.execute(
                "INSERT INTO transactions (user_id, date, store, category_id, amount, type) "
                "VALUES (%s, '2026-01-01', %s, %s, 10.0, 'expense')",
                (uid, store, cid),
            )


def test_consistent_history_is_suggested(user_conn):
    conn, uid = user_conn
    _history(conn, uid, "Prisma", [("Groceries", 9), ("Other", 1)])
    assert suggest_category("Prisma", conn, uid) == "Groceries"


def test_history_at_the_threshold_is_suggested(user_conn):
    conn, uid = user_conn
    # Exactly 70% — the bar is inclusive, as it is in the rule generator.
    _history(conn, uid, "Lidl", [("Groceries", 7), ("Other", 3)])
    assert suggest_category("Lidl", conn, uid) == "Groceries"


def test_ambiguous_history_is_not_guessed(user_conn):
    conn, uid = user_conn
    # Two of four is a coin toss. Better to say nothing and let the row be
    # reviewed than to draw a guess like a rule.
    _history(conn, uid, "Verkkokauppa.com",
             [("Dog", 2), ("Other", 1), ("Electronics", 1)])
    assert suggest_category("Verkkokauppa.com", conn, uid) is None


def test_unknown_store_gets_nothing(user_conn):
    conn, uid = user_conn
    assert suggest_category("Never Seen Before Oy", conn, uid) is None


def test_a_rule_still_beats_the_history(user_conn):
    conn, uid = user_conn
    # The history is too split to speak, but an explicit rule is not a guess.
    _history(conn, uid, "Verkkokauppa.com", [("Dog", 2), ("Other", 2)])
    conn.execute(
        "INSERT INTO merchant_rules (user_id, pattern, category_id, match_type) "
        "VALUES (%s, %s, %s, 'exact')",
        (uid, "Verkkokauppa.com", _cat(conn, uid, "Electronics")),
    )
    assert suggest_category("Verkkokauppa.com", conn, uid) == "Electronics"


# ── The suggestion may not overturn the sign ────────────────────────────
#
# A suggestion is a bare category NAME, and the review table resolves that name
# against the category list to decide which category — and therefore which side
# of the ledger — the row imports under. Two names live on both sides: "Other",
# which is what an unrecognised store gets, and "Investments". A type-blind
# suggestion let the wrong one win, and the row's expense/income flipped with
# it, even though the amount and the sign on the statement were read correctly.
#
# So a suggestion is scoped to the type the row was read as. The same rule
# /api/merchant-rules/<id>/apply has always applied when re-categorising history.


def test_history_cannot_suggest_across_the_ledger(user_conn):
    conn, uid = user_conn
    # A grocer with an unambiguous expense history. The refund from that grocer
    # is income, and "Groceries" exists only as an expense — so there is nothing
    # to suggest, and the row goes to "needs review" rather than becoming
    # spending.
    _history(conn, uid, "Prisma", [("Groceries", 9)])
    assert suggest_category("Prisma", conn, uid, "expense") == "Groceries"
    assert suggest_category("Prisma", conn, uid, "income") is None


def test_a_name_on_both_sides_resolves_within_the_row_type(user_conn):
    conn, uid = user_conn
    # "Investments" is a seeded category of BOTH types. Asked as income, the
    # suggestion must come from the income side; asked as expense, the expense
    # side. Same name either way — which is exactly why the name alone was never
    # enough to decide the row's type.
    _history(conn, uid, "Nordnet", [("Investments", 9)])
    for asked in ("expense", "income"):
        name = suggest_category("Nordnet", conn, uid, asked)
        if name is None:
            continue
        row = conn.execute(
            "SELECT type FROM categories WHERE user_id = %s AND name = %s AND type = %s",
            (uid, name, asked),
        ).fetchone()
        assert row is not None, f"suggested {name!r} has no {asked} category"


def test_a_rule_cannot_suggest_across_the_ledger(user_conn):
    conn, uid = user_conn
    # An explicit rule is not a guess, but it is still type-blind: it says what
    # a store is called, not which way the money went. A card payment to a store
    # ruled into an income category must not import as earnings.
    conn.execute(
        "INSERT INTO merchant_rules (user_id, pattern, category_id, match_type) "
        "VALUES (%s, %s, %s, 'exact')",
        (uid, "Tyonantaja Oy", _cat(conn, uid, "Job", "income")),
    )
    assert suggest_category("Tyonantaja Oy", conn, uid, "income") == "Job"
    assert suggest_category("Tyonantaja Oy", conn, uid, "expense") is None
    # Unscoped, the old caller's behaviour is unchanged.
    assert suggest_category("Tyonantaja Oy", conn, uid) == "Job"
