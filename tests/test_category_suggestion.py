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
