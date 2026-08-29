"""SQLite database layer (desktop mode) — the local, single-user engine.

This is the SQLite counterpart to the Postgres layer in :mod:`db`. It exposes
the **same public shape** so every query the rest of the app issues works
unchanged against either backend:

- ``db_conn()`` — context manager that opens a connection, commits on clean
  exit, rolls back on exception, and closes. Same contract as the Postgres pool.
- ``run_sql_script(sql)`` — execute a multi-statement DDL/SQL string.
- ``get_db()`` — a direct connection the caller commits + closes (old contract).
- ``close_pool()`` / ``reset_pool(path=None)`` — lifecycle/test helpers
  (no real pool here; provided for interface parity with :mod:`db`).

Compatibility with the Postgres-style SQL the app emits
--------------------------------------------------------
The app was written for psycopg, so its SQL uses ``%s`` placeholders and gets
``dict`` rows back. Two thin adapters make that work on SQLite:

1. **Placeholders** — every ``execute``/``executemany`` rewrites ``%s`` → ``?``
   (SQLite's positional marker) and downgrades ``ILIKE`` → ``LIKE`` (SQLite's
   ``LIKE`` is already case-insensitive for ASCII). The app uses no named
   (``%(name)s``) placeholders, so positional translation is sufficient.
2. **Rows as dicts** — the connection's ``row_factory`` returns plain ``dict``
   objects keyed by column name, matching psycopg's ``dict_row`` exactly, so
   ``row["id"]`` / ``dict(row)`` / ``jsonify`` all behave identically.

``RETURNING`` and ``ON CONFLICT ... DO UPDATE/NOTHING`` are used by the app and
are supported natively by the bundled SQLite (3.35+ / 3.24+ respectively).

A fresh connection is opened per ``db_conn()`` call (cheap for a single-user
desktop app) with ``check_same_thread=False`` so the Flask request thread and
the pywebview main thread can both touch the DB. WAL mode + a busy timeout keep
the occasional concurrent read/write from erroring.
"""

import json
import os
import re
import sqlite3
from contextlib import contextmanager

import config

# Backend-agnostic exception classes (mirrored by db_postgres) so callers can
# catch DB errors without importing a specific driver.
IntegrityError = sqlite3.IntegrityError
DatabaseError = sqlite3.Error


def Json(obj):
    """Adapt a Python value for a JSON column.

    SQLite has no native JSON type, so JSON columns are TEXT: serialize here.
    Mirrors psycopg's ``Json`` wrapper used in hosted mode (see db_postgres)."""
    return json.dumps(obj if obj is not None else None)


def load_json(value):
    """Decode a JSON column value read back from the DB.

    SQLite returns the stored TEXT, so parse it; ``None``/empty yields ``None``.
    In hosted mode JSONB is already parsed, so db_postgres.load_json is identity."""
    if value is None or value == "":
        return None
    if isinstance(value, (list, dict)):
        return value
    return json.loads(value)

# Matches the ``%s`` positional placeholder. No named placeholders are used by
# the app, and none of its SQL string literals contain a bare ``%s`` token, so a
# plain replace is safe. (SQLite date defaults use ``datetime('now')`` rather
# than ``strftime('%...')`` precisely to keep this translation trivial.)
_ILIKE_RE = re.compile(r"\bILIKE\b", re.IGNORECASE)
# Postgres' server-side current-timestamp function ``now()`` (used in a few
# ``updated_at = now()`` clauses) → SQLite's ``datetime('now')`` (UTC,
# "YYYY-MM-DD HH:MM:SS"), matching the schema's audit-timestamp form.
_NOW_RE = re.compile(r"\bnow\s*\(\s*\)", re.IGNORECASE)


def _translate(sql: str) -> str:
    """Rewrite psycopg-flavoured SQL into the SQLite dialect.

    ``%s`` → ``?`` (positional placeholder), ``ILIKE`` → ``LIKE`` (SQLite LIKE is
    already case-insensitive for ASCII), and ``now()`` → ``datetime('now')``.
    Everything else the app emits (``RETURNING``, ``ON CONFLICT … DO UPDATE``,
    ``excluded.col``) is already valid SQLite.
    """
    sql = sql.replace("%s", "?")
    sql = _ILIKE_RE.sub("LIKE", sql)
    sql = _NOW_RE.sub("datetime('now')", sql)
    return sql


def _dict_row(cursor, row):
    """row_factory producing a plain dict keyed by column name (≈ psycopg dict_row)."""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def _connect() -> sqlite3.Connection:
    """Open a configured SQLite connection (dict rows, FKs on, WAL, busy timeout).

    Creates the parent directory on first use so a fresh ``~/Library/Application
    Support/Balance`` path works out of the box."""
    parent = os.path.dirname(os.path.abspath(config.SQLITE_PATH))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(config.SQLITE_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = _dict_row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


class _Cursor:
    """Thin sqlite3 cursor wrapper: translates SQL and is a context manager.

    psycopg cursors support ``with conn.cursor() as cur:`` and translate their
    SQL; sqlite3 cursors do neither. This makes the few ``conn.cursor()`` call
    sites (e.g. ``executemany`` batches) behave the same on both backends.
    """

    def __init__(self, raw: sqlite3.Cursor):
        self._raw = raw

    def execute(self, sql, params=()):
        self._raw.execute(_translate(sql), params)
        return self

    def executemany(self, sql, seq):
        self._raw.executemany(_translate(sql), seq)
        return self

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._raw.close()
        return False


class _Conn:
    """Connection wrapper translating SQL on the way to a real sqlite3 connection.

    Mirrors the psycopg connection surface the app relies on: ``conn.execute()``
    returns a cursor whose ``fetchone()``/``fetchall()`` yield dict rows, and
    ``conn.cursor()`` returns a context-manager cursor. Anything else
    (``commit``, ``rollback``, ``close``, …) delegates straight through.
    """

    def __init__(self, raw: sqlite3.Connection):
        self._raw = raw

    def execute(self, sql, params=()):
        # sqlite3's Connection.execute returns a real cursor whose rows already
        # come back as dicts via the connection's row_factory.
        return self._raw.execute(_translate(sql), params)

    def executemany(self, sql, seq):
        return self._raw.executemany(_translate(sql), seq)

    def cursor(self):
        return _Cursor(self._raw.cursor())

    def __getattr__(self, name):
        return getattr(self._raw, name)


@contextmanager
def db_conn():
    """Open a connection; commit on success, roll back on error, always close.

    Same contract as :func:`db.db_conn`. A fresh connection per call keeps the
    desktop single-user case simple and thread-safe.
    """
    conn = _connect()
    try:
        yield _Conn(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_sql_script(sql: str) -> None:
    """Execute a multi-statement SQL string (schema DDL), committing atomically."""
    conn = _connect()
    try:
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()


def get_db() -> _Conn:
    """Return a direct connection the caller is responsible for committing + closing.

    Mirrors the OLD SQLite ``get_db()`` contract (and :func:`database.get_db`).
    """
    return _Conn(_connect())


# ── Lifecycle helpers (no real pool; kept for interface parity with db.py) ──
def close_pool() -> None:
    """No-op: SQLite opens a fresh connection per call, so there is no pool."""
    return None


def reset_pool(path: str | None = None) -> None:
    """Point the engine at a different SQLite file (used by tests).

    With ``path`` given, updates ``config.SQLITE_PATH`` so subsequent
    connections open that file; with no argument it is a no-op.
    """
    if path:
        config.SQLITE_PATH = path
