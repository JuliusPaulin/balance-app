"""Database layer — the local SQLite engine.

Balance keeps everything in one SQLite file on your own disk. This module is
the single import point for the rest of the app: everything calls
``db.db_conn()`` / ``db.run_sql_script()`` and never touches the driver.

The SQL throughout the app is written psycopg-flavoured — ``%s`` placeholders,
``RETURNING``, ``ON CONFLICT``, dict rows — a leftover from a hosted Postgres
port that has since been removed. :mod:`db_sqlite` translates that on the way
to sqlite3, so the query layer needed no rewrite. Backend-specific bits still
funnel through here: ``IntegrityError``, ``DatabaseError``, ``Json``,
``load_json``.
"""

from db_sqlite import (  # noqa: F401  (re-exported public API)
    db_conn,
    run_sql_script,
    get_db,
    close_pool,
    reset_pool,
    IntegrityError,
    DatabaseError,
    Json,
    load_json,
)
