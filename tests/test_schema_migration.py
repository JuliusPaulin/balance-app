"""init_db() on a database from before mcp_audit keeps what is already there.

The file is a scratch database, not the suite's shared one, and not a real
expenses file. The connection is pointed back before the test returns.
"""

import config
from data import db
from data.schema import init_db, seed_local_user


def _rows(sql, params=()):
    with db.db_conn() as conn:
        return [tuple(row.values()) for row in conn.execute(sql, params).fetchall()]


def test_init_db_upgrades_an_older_file_without_rewriting_it(tmp_path):
    original = config.SQLITE_PATH
    path = str(tmp_path / "older.db")
    db.reset_pool(path)
    try:
        init_db()
        seed_local_user()
        with db.db_conn() as conn:
            conn.execute("DROP TABLE mcp_audit")
            conn.execute("ALTER TABLE transactions DROP COLUMN import_fx")
            groceries = conn.execute(
                "SELECT id FROM categories WHERE user_id = %s AND name = 'Groceries' "
                "AND type = 'expense'",
                (config.LOCAL_USER_ID,),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO transactions (user_id, date, store, category_id, amount, type) "
                "VALUES (%s, '2024-05-02', 'K-Market', %s, 12.4, 'expense')",
                (config.LOCAL_USER_ID, groceries),
            )
            account_id = conn.execute(
                "INSERT INTO accounts (user_id, name, type, is_archived) "
                "VALUES (%s, 'Old loan', 'liability', 1) RETURNING id",
                (config.LOCAL_USER_ID,),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO account_balances (user_id, account_id, as_of, balance) "
                "VALUES (%s, %s, '2024-01-01', 250)",
                (config.LOCAL_USER_ID, account_id),
            )

        before = _rows(
            "SELECT date, store, amount, type, category_id FROM transactions ORDER BY id"
        )
        init_db()

        assert _rows(
            "SELECT date, store, amount, type, category_id FROM transactions ORDER BY id"
        ) == before
        with db.db_conn() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)")}
            audit = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'mcp_audit'"
            ).fetchone()
            fx = conn.execute("SELECT import_fx FROM transactions").fetchone()["import_fx"]
        assert "import_fx" in columns
        assert audit is not None
        assert fx is None

        balances = _rows(
            "SELECT as_of, balance FROM account_balances ORDER BY as_of"
        )
        assert balances == [("2024-01-01", 250), ("2024-01-02", 0)]

        init_db()
        assert _rows(
            "SELECT date, store, amount, type, category_id FROM transactions ORDER BY id"
        ) == before
        assert _rows(
            "SELECT as_of, balance FROM account_balances ORDER BY as_of"
        ) == balances
        assert _rows("SELECT id FROM mcp_audit") == []
    finally:
        db.reset_pool(original)
