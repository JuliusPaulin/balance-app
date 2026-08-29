"""One-time migration: old single-user SQLite DB -> new local multi-user schema.

The desktop app used to run on a flat, single-user SQLite schema (no ``user_id``
columns). The current app keeps a multi-tenant shape even locally — every table
has a ``user_id`` and there is a ``users`` table — but desktop mode uses one
fixed local user (``config.LOCAL_USER_ID``). This script copies an existing old
database into a fresh new-schema database, attaching every row to that local
user and preserving primary-key ids (so transactions keep pointing at the right
categories/accounts).

Usage::

    python3 migrate_to_local_sqlite.py SOURCE_OLD.db [DEST_NEW.db]

DEST defaults to ``config.SQLITE_PATH`` (``expenses.db`` next to the code). The
destination must NOT already exist (refuses to overwrite). Tables that don't
exist in the new schema (``holdings``) and transient import tables
(``import_batches`` / ``import_staging``) are intentionally skipped.
"""

import os
import sqlite3
import sys

import config
import database

# Tables copied from the old DB, in FK-dependency order. Each row gets user_id =
# LOCAL_USER_ID; for every table we copy only the columns that exist in BOTH the
# old and the new schema (so investment-only extras like accounts.external_id /
# accounts.group_name are dropped, and holdings — absent from the new schema — is
# skipped entirely along with the transient import_* staging tables).
_TABLES_IN_ORDER = [
    "categories",
    "accounts",
    "transactions",
    "account_balances",
    "merchant_rules",
    "month_notes",
    "recurring_dismissed",
    "holdings",
]


def _columns(conn, table):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def migrate(source_path, dest_path):
    if not os.path.exists(source_path):
        raise SystemExit(f"Source DB not found: {source_path}")
    if os.path.exists(dest_path):
        raise SystemExit(
            f"Destination already exists: {dest_path}\n"
            "Refusing to overwrite. Move/delete it first if you want a clean import."
        )

    # 1) Build the fresh new schema at the destination.
    config.SQLITE_PATH = dest_path
    database.init_db()

    src = sqlite3.connect(source_path)
    dst = sqlite3.connect(dest_path)
    uid = config.LOCAL_USER_ID
    counts = {}
    try:
        dst.execute("PRAGMA foreign_keys = OFF")  # bulk load; verified at the end

        # 2) The local user row (id = LOCAL_USER_ID), approved admin. We do NOT
        #    seed default categories — the old DB's categories are copied verbatim
        #    (preserving ids) so transaction.category_id references stay valid.
        dst.execute(
            "INSERT OR IGNORE INTO users (id, google_sub, email, name, status, role) "
            "VALUES (?, 'local', 'local@localhost', 'Local User', 'approved', 'admin')",
            (uid,),
        )

        # 3) Copy each table, column-intersection + injected user_id.
        for table in _TABLES_IN_ORDER:
            if not _table_exists(src, table):
                counts[table] = "(absent in source)"
                continue
            old_cols = _columns(src, table)
            new_cols = _columns(dst, table)
            # Columns present in both, minus user_id (we inject it ourselves for
            # tables that have it). ``holdings`` has no user_id — it's reached via
            # its (user-scoped) account_id — so it is copied verbatim.
            has_user_id = "user_id" in new_cols
            shared = [c for c in old_cols if c in new_cols and c != "user_id"]
            rows = src.execute(
                f"SELECT {', '.join(shared)} FROM {table}"
            ).fetchall()

            if has_user_id:
                insert_cols = ["user_id"] + shared
                values = [(uid, *row) for row in rows]
            else:
                insert_cols = shared
                values = [tuple(row) for row in rows]
            placeholders = ", ".join(["?"] * len(insert_cols))
            sql = (
                f"INSERT INTO {table} ({', '.join(insert_cols)}) "
                f"VALUES ({placeholders})"
            )
            dst.executemany(sql, values)
            counts[table] = len(rows)

        # 4) Bump AUTOINCREMENT counters past the imported max ids so new inserts
        #    never collide with copied rows.
        for table in ["users", *_TABLES_IN_ORDER]:
            if not _table_exists(dst, table):
                continue
            mx = dst.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
            if mx:
                # sqlite_sequence is an internal table with no UNIQUE constraint,
                # so upsert by delete+insert. A row exists for any table we just
                # inserted into (AUTOINCREMENT created it).
                dst.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
                dst.execute(
                    "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                    (table, mx),
                )

        dst.commit()

        # 5) Integrity check now that everything is loaded.
        dst.execute("PRAGMA foreign_keys = ON")
        violations = dst.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise SystemExit(f"Foreign-key violations after import: {violations}")
    finally:
        src.close()
        dst.close()

    print(f"Migrated old DB -> {dest_path}")
    for table, n in counts.items():
        print(f"  {table}: {n}")
    print("Done. Foreign-key check passed.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    source = sys.argv[1]
    dest = sys.argv[2] if len(sys.argv) > 2 else config.SQLITE_PATH
    migrate(source, dest)
