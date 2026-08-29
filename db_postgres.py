"""Postgres database layer (hosted mode) — psycopg3 + connection pool.

This is the Postgres counterpart to the desktop SQLite layer in
``database.py``. It mirrors that module's public shape so the SQL/route
conversions in Steps 1.2–1.8 are mechanical:

- ``db_conn()`` — context manager that borrows a pooled connection, commits on
  clean exit, rolls back on exception, and returns the connection to the pool.
  Same contract as ``database.db_conn()``.
- ``run_sql_script(sql)`` — execute a multi-statement DDL/SQL string (used by
  ``database.py`` in Step 1.2 to create the schema).
- ``close_pool()`` / ``reset_pool(url=None)`` — lifecycle helpers for clean
  shutdown and for tests to point the pool at ``expense_test``.

Param style & rows (IMPORTANT for the conversion steps)
-------------------------------------------------------
psycopg uses ``%s`` placeholders, NOT SQLite's ``?``. Every converted query
must use ``%s`` (and ``%(name)s`` for named params).

Connections yield rows via ``psycopg.rows.dict_row``: rows are dicts keyed by
column name (``row["id"]``). Positional access (``row[0]``) is NOT available,
so any SQLite code relying on ``cursor.fetchone()[0]`` must be rewritten to use
a column name or an explicit alias (e.g. ``SELECT count(*) AS n``).

The pool is created lazily on first use, so ``import db`` never opens a
connection. This keeps desktop mode (empty ``DATABASE_URL``) importable.
"""

from contextlib import contextmanager
from threading import Lock

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json  # noqa: F401  (re-exported JSON adapter)
from psycopg_pool import ConnectionPool

import config

# Backend-agnostic exception classes (mirrored by db_sqlite) so callers can
# catch DB errors without importing a specific driver.
IntegrityError = psycopg.errors.IntegrityError
DatabaseError = psycopg.Error


def load_json(value):
    """Decode a JSON column value. psycopg already parses JSONB, so identity."""
    return value

# Pool sizing: Render free + Supabase free are small, so keep max_size modest.
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 5

# The pool is created lazily (see _get_pool) so importing this module never
# requires a database. A lock guards the one-time construction against races
# under gunicorn threaded workers.
_pool: ConnectionPool | None = None
_pool_lock = Lock()


def _make_pool(url: str) -> ConnectionPool:
    """Build (and open) a ConnectionPool for the given URL with dict_row."""
    return ConnectionPool(
        conninfo=url,
        min_size=_POOL_MIN_SIZE,
        max_size=_POOL_MAX_SIZE,
        # dict_row applies to every connection handed out by the pool.
        kwargs={"row_factory": dict_row},
        open=True,
    )


def _get_pool() -> ConnectionPool:
    """Return the process-wide pool, creating it lazily on first use.

    Raises RuntimeError if DATABASE_URL is not configured.
    """
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            url = config.DATABASE_URL
            if not url:
                raise RuntimeError(
                    "DATABASE_URL is not set — Postgres mode requires a "
                    "connection string. Set DATABASE_URL (e.g. in .env) before "
                    "using db.db_conn()/run_sql_script()."
                )
            _pool = _make_pool(url)
    return _pool


def close_pool() -> None:
    """Close and dispose the pool (clean shutdown / test teardown)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def reset_pool(url: str | None = None) -> None:
    """Dispose the current pool and (optionally) rebuild it against ``url``.

    Intended for tests: a fixture can call ``reset_pool("postgresql://localhost/
    expense_test")`` to point the pool at the test database. With no argument it
    just disposes the pool, so the next use lazily recreates it from
    ``config.DATABASE_URL``.
    """
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None
        if url:
            _pool = _make_pool(url)


@contextmanager
def db_conn():
    """Borrow a pooled connection; commit on success, rollback on error.

    Mirrors ``database.db_conn()``: the connection is yielded for use, committed
    on a clean exit, rolled back if the body raises, and always returned to the
    pool. Rows come back as dicts (``dict_row``). psycopg connections support
    both ``conn.execute(...)`` and ``conn.cursor()``.
    """
    pool = _get_pool()
    with pool.connection() as conn:
        # ConnectionPool.connection() already commits on clean exit and rolls
        # back on exception before returning the connection to the pool, so the
        # commit/rollback/close contract is satisfied by this context manager.
        yield conn


def run_sql_script(sql: str) -> None:
    """Execute a multi-statement SQL string (e.g. schema DDL).

    psycopg can send multiple semicolon-separated statements in a single
    ``execute`` call, which works for DDL. Runs inside a single ``db_conn()`` so
    the whole script commits atomically (or rolls back on error).
    """
    with db_conn() as conn:
        conn.execute(sql)
