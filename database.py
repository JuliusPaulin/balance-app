"""Schema + seeding, for both backends (SQLite desktop / Postgres hosted).

This module owns the **schema** and per-user category seeding. It supports two
backends selected by :data:`config.USE_SQLITE`:

- **Desktop (SQLite)** — a local single-user file. The app is still multi-tenant
  in shape (a ``users`` table, ``user_id`` on every table), but desktop mode
  auto-logs-in one fixed local user (``config.LOCAL_USER_ID``), so the same
  user-scoped queries run unchanged.
- **Hosted (Postgres)** — the original multi-user cloud schema.

Public shape (kept stable so app.py / recurring.py / networth.py / auth.py keep
importing it regardless of backend):

- ``db_conn``            — re-exported from :mod:`db` (the active engine's ctx mgr).
- ``get_db()``           — a direct connection the caller commits + closes.
- ``init_db()``          — build the full schema idempotently for the active engine.
- ``migrate_db()``       — upgrade an existing install in place (Postgres only;
                           SQLite builds the complete schema in ``init_db``).
- ``seed_categories_for_user(conn, user_id)`` — seed default categories for a user.
- ``seed_local_user()``  — ensure the single desktop user + its categories exist.
- ``backup_db`` / ``list_backups`` — real file-copy backups in SQLite mode;
                           no-ops in hosted Postgres mode.

Param style is ``%s`` and rows are dicts on **both** backends (the SQLite engine
translates ``%s`` → ``?`` and returns dict rows — see :mod:`db_sqlite`).
"""

import os
import shutil
from datetime import date, datetime, timedelta

import config
import db
from db import db_conn  # re-export so `from database import db_conn` keeps working

# Default categories — 28 expense + 6 income = 34 total. Single source of truth
# for per-user seeding on both backends.
EXPENSE_CATEGORIES = [
    "Car charging", "Car maintenance", "Car parking", "Car payment",
    "Clothing", "Condo fees", "Debt", "Dog", "Electronics",
    "Entertainment", "Exercise", "Gas", "Gifts", "Going out",
    "Groceries", "Home maintenance", "Insurance", "Investments",
    "Lunch", "Medical", "Other", "Public transportation", "Rent",
    "Restaurant", "Telecom", "Travel", "Utilities", "Work",
]

INCOME_CATEGORIES = [
    "Job", "Side project", "Kela", "Expense reimbursement",
    "Other", "Investments",
]


# ===========================================================================
# SQLite schema (desktop) — the local single-user build
# ===========================================================================
# Ported 1:1 from the Postgres schema below:
#   BIGINT GENERATED ... AS IDENTITY  -> INTEGER PRIMARY KEY AUTOINCREMENT
#   DOUBLE PRECISION                  -> REAL
#   TIMESTAMPTZ / JSONB               -> TEXT
#   DEFAULT to_char(now()...)         -> DEFAULT (datetime('now'))  (UTC, "YYYY-MM-DD HH:MM:SS")
# user_id columns + the users table are kept so the multi-user query layer runs
# unchanged; desktop just uses one fixed user. FK cascades need PRAGMA
# foreign_keys=ON, which db_sqlite sets on every connection.
_NOW_SQLITE = "(datetime('now'))"

SCHEMA_DDL_SQLITE = f"""
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    google_sub    TEXT NOT NULL UNIQUE,
    email         TEXT NOT NULL,
    name          TEXT,
    picture       TEXT,
    created_at    TEXT NOT NULL DEFAULT {_NOW_SQLITE},
    last_login_at TEXT,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','approved','denied')),
    role          TEXT NOT NULL DEFAULT 'user'
                  CHECK (role IN ('user','admin')),
    note          TEXT,
    decided_at    TEXT,
    decided_by    INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS categories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    type       TEXT NOT NULL CHECK (type IN ('expense', 'income')),
    is_default INTEGER NOT NULL DEFAULT 1,
    color      TEXT,
    created_at TEXT NOT NULL DEFAULT {_NOW_SQLITE},
    updated_at TEXT NOT NULL DEFAULT {_NOW_SQLITE}
);

CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date        TEXT NOT NULL,
    store       TEXT NOT NULL DEFAULT '',
    category_id INTEGER NOT NULL REFERENCES categories(id),
    amount      REAL NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('expense', 'income')),
    created_at  TEXT NOT NULL DEFAULT {_NOW_SQLITE},
    updated_at  TEXT NOT NULL DEFAULT {_NOW_SQLITE}
);

CREATE TABLE IF NOT EXISTS import_batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT {_NOW_SQLITE},
    status      TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS import_staging (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date               TEXT NOT NULL,
    store              TEXT NOT NULL DEFAULT '',
    suggested_category TEXT,
    amount             REAL NOT NULL,
    type               TEXT NOT NULL DEFAULT 'expense',
    confirmed          INTEGER NOT NULL DEFAULT 0,
    final_category_id  INTEGER REFERENCES categories(id),
    import_batch_id    INTEGER NOT NULL REFERENCES import_batches(id)
);

CREATE TABLE IF NOT EXISTS import_formats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    signature   TEXT NOT NULL,
    delimiter   TEXT NOT NULL,
    date_col    INTEGER NOT NULL,
    amount_col  INTEGER NOT NULL,
    store_col   INTEGER,
    amount_sign TEXT NOT NULL DEFAULT 'neg_expense'
                CHECK (amount_sign IN ('neg_expense','pos_expense')),
    created_at  TEXT NOT NULL DEFAULT {_NOW_SQLITE},
    UNIQUE (user_id, signature)
);

CREATE TABLE IF NOT EXISTS merchant_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    pattern     TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    match_type  TEXT NOT NULL DEFAULT 'exact' CHECK (match_type IN ('exact', 'contains', 'smart')),
    created_at  TEXT NOT NULL DEFAULT {_NOW_SQLITE}
);

CREATE TABLE IF NOT EXISTS month_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month      TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT {_NOW_SQLITE},
    UNIQUE (user_id, month)
);

CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('asset', 'liability')),
    sort_order  INTEGER NOT NULL DEFAULT 0,
    is_archived INTEGER NOT NULL DEFAULT 0,
    external_id TEXT,
    group_name  TEXT,
    created_at  TEXT NOT NULL DEFAULT {_NOW_SQLITE}
);

-- holdings — per-product leaf rows for imported investment accounts, snapshotted
-- by as_of so development can be tracked over time. The account total per as_of
-- lives in account_balances (= Σ value_eur). Reachable only via account_id (which
-- is user-scoped through accounts), so no user_id column is needed here.
CREATE TABLE IF NOT EXISTS holdings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    as_of       TEXT NOT NULL,
    name        TEXT NOT NULL,
    isin        TEXT,
    units       REAL,
    value_eur   REAL NOT NULL,
    return_pct  REAL,
    return_eur  REAL,
    currency    TEXT,
    created_at  TEXT NOT NULL DEFAULT {_NOW_SQLITE},
    UNIQUE (account_id, as_of, name)
);

CREATE TABLE IF NOT EXISTS recurring_dismissed (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    signature    TEXT NOT NULL,
    dismissed_at TEXT NOT NULL DEFAULT {_NOW_SQLITE},
    UNIQUE (user_id, signature)
);

CREATE TABLE IF NOT EXISTS manual_subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    store      TEXT NOT NULL,
    amount     REAL NOT NULL,
    cadence    TEXT NOT NULL DEFAULT 'monthly'
               CHECK (cadence IN ('monthly', 'quarterly', 'yearly')),
    category   TEXT,
    type       TEXT NOT NULL DEFAULT 'expense' CHECK (type IN ('expense', 'income')),
    created_at TEXT NOT NULL DEFAULT {_NOW_SQLITE}
);

CREATE TABLE IF NOT EXISTS account_balances (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    as_of      TEXT NOT NULL,
    balance    REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT {_NOW_SQLITE},
    UNIQUE (account_id, as_of)
);

CREATE TABLE IF NOT EXISTS bank_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id    TEXT NOT NULL,
    aspsp_name    TEXT,
    aspsp_country TEXT,
    valid_until   TEXT,
    accounts      TEXT,
    created_at    TEXT NOT NULL DEFAULT {_NOW_SQLITE},
    UNIQUE (user_id)
);

CREATE INDEX IF NOT EXISTS idx_bank_sessions_user ON bank_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
CREATE INDEX IF NOT EXISTS idx_categories_user ON categories(user_id);
CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_user_date ON transactions(user_id, date);
CREATE INDEX IF NOT EXISTS idx_transactions_user_category ON transactions(user_id, category_id);
CREATE INDEX IF NOT EXISTS idx_transactions_user_type ON transactions(user_id, type);
CREATE INDEX IF NOT EXISTS idx_merchant_rules_user_pattern ON merchant_rules(user_id, pattern);
CREATE INDEX IF NOT EXISTS idx_account_balances_user_asof ON account_balances(user_id, as_of);
CREATE INDEX IF NOT EXISTS idx_account_balances_account ON account_balances(account_id);
CREATE INDEX IF NOT EXISTS idx_manual_subscriptions_user ON manual_subscriptions(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_external_id
    ON accounts(user_id, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_holdings_account_asof ON holdings(account_id, as_of);
"""


# ===========================================================================
# Postgres schema (hosted) — unchanged from the cloud build
# ===========================================================================
_NOW_TEXT = "to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS')"

SCHEMA_DDL_PG = f"""
CREATE TABLE IF NOT EXISTS users (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    google_sub    TEXT NOT NULL UNIQUE,
    email         TEXT NOT NULL,
    name          TEXT,
    picture       TEXT,
    created_at    TEXT NOT NULL DEFAULT {_NOW_TEXT},
    last_login_at TEXT,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','approved','denied')),
    role          TEXT NOT NULL DEFAULT 'user'
                  CHECK (role IN ('user','admin')),
    note          TEXT,
    decided_at    TEXT,
    decided_by    BIGINT REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS categories (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    type       TEXT NOT NULL CHECK (type IN ('expense', 'income')),
    is_default INTEGER NOT NULL DEFAULT 1,
    color      TEXT,
    created_at TEXT NOT NULL DEFAULT {_NOW_TEXT},
    updated_at TEXT NOT NULL DEFAULT {_NOW_TEXT}
);

CREATE TABLE IF NOT EXISTS transactions (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date        TEXT NOT NULL,
    store       TEXT NOT NULL DEFAULT '',
    category_id BIGINT NOT NULL REFERENCES categories(id),
    amount      DOUBLE PRECISION NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('expense', 'income')),
    created_at  TEXT NOT NULL DEFAULT {_NOW_TEXT},
    updated_at  TEXT NOT NULL DEFAULT {_NOW_TEXT}
);

CREATE TABLE IF NOT EXISTS import_batches (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT {_NOW_TEXT},
    status      TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS import_staging (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id            BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date               TEXT NOT NULL,
    store              TEXT NOT NULL DEFAULT '',
    suggested_category TEXT,
    amount             DOUBLE PRECISION NOT NULL,
    type               TEXT NOT NULL DEFAULT 'expense',
    confirmed          INTEGER NOT NULL DEFAULT 0,
    final_category_id  BIGINT REFERENCES categories(id),
    import_batch_id    BIGINT NOT NULL REFERENCES import_batches(id)
);

CREATE TABLE IF NOT EXISTS import_formats (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    signature   TEXT NOT NULL,
    delimiter   TEXT NOT NULL,
    date_col    INT NOT NULL,
    amount_col  INT NOT NULL,
    store_col   INT,
    amount_sign TEXT NOT NULL DEFAULT 'neg_expense'
                CHECK (amount_sign IN ('neg_expense','pos_expense')),
    created_at  TEXT NOT NULL DEFAULT {_NOW_TEXT},
    UNIQUE (user_id, signature)
);

CREATE TABLE IF NOT EXISTS merchant_rules (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    pattern     TEXT NOT NULL,
    category_id BIGINT NOT NULL REFERENCES categories(id),
    match_type  TEXT NOT NULL DEFAULT 'exact' CHECK (match_type IN ('exact', 'contains', 'smart')),
    created_at  TEXT NOT NULL DEFAULT {_NOW_TEXT}
);

CREATE TABLE IF NOT EXISTS month_notes (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month      TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT {_NOW_TEXT},
    UNIQUE (user_id, month)
);

CREATE TABLE IF NOT EXISTS accounts (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('asset', 'liability')),
    sort_order  INTEGER NOT NULL DEFAULT 0,
    is_archived INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT {_NOW_TEXT}
);

CREATE TABLE IF NOT EXISTS recurring_dismissed (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    signature    TEXT NOT NULL,
    dismissed_at TEXT NOT NULL DEFAULT {_NOW_TEXT},
    UNIQUE (user_id, signature)
);

CREATE TABLE IF NOT EXISTS manual_subscriptions (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    store      TEXT NOT NULL,
    amount     DOUBLE PRECISION NOT NULL,
    cadence    TEXT NOT NULL DEFAULT 'monthly'
               CHECK (cadence IN ('monthly', 'quarterly', 'yearly')),
    category   TEXT,
    type       TEXT NOT NULL DEFAULT 'expense' CHECK (type IN ('expense', 'income')),
    created_at TEXT NOT NULL DEFAULT {_NOW_TEXT}
);

CREATE TABLE IF NOT EXISTS account_balances (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    as_of      TEXT NOT NULL,
    balance    DOUBLE PRECISION NOT NULL,
    created_at TEXT NOT NULL DEFAULT {_NOW_TEXT},
    UNIQUE (account_id, as_of)
);

CREATE TABLE IF NOT EXISTS bank_sessions (
    id            BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id    TEXT NOT NULL,
    aspsp_name    TEXT,
    aspsp_country TEXT,
    valid_until   TIMESTAMPTZ,
    accounts      JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id)
);
CREATE INDEX IF NOT EXISTS idx_bank_sessions_user ON bank_sessions(user_id);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_categories_user ON categories(user_id);
CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_user_date ON transactions(user_id, date);
CREATE INDEX IF NOT EXISTS idx_transactions_user_category ON transactions(user_id, category_id);
CREATE INDEX IF NOT EXISTS idx_transactions_user_type ON transactions(user_id, type);
CREATE INDEX IF NOT EXISTS idx_merchant_rules_user_pattern ON merchant_rules(user_id, pattern);
CREATE INDEX IF NOT EXISTS idx_account_balances_user_asof ON account_balances(user_id, as_of);
CREATE INDEX IF NOT EXISTS idx_account_balances_account ON account_balances(account_id);
CREATE INDEX IF NOT EXISTS idx_manual_subscriptions_user ON manual_subscriptions(user_id);
"""


# ---------------------------------------------------------------------------
# Direct (non-pooled) connection — caller commits + closes
# ---------------------------------------------------------------------------
def get_db():
    """Return a direct connection the caller is responsible for committing + closing.

    Desktop mode returns the SQLite engine's connection; hosted mode opens a
    fresh psycopg connection (autocommit off, ``dict_row`` rows). Both mirror the
    old SQLite ``get_db()`` contract.
    """
    if config.USE_SQLITE:
        return db.get_db()

    import psycopg
    from psycopg.rows import dict_row

    url = config.DATABASE_URL
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set — Postgres mode requires a connection "
            "string. Set DATABASE_URL (e.g. in .env) before using get_db()."
        )
    return psycopg.connect(url, row_factory=dict_row)


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------
def init_db():
    """Create the full schema for the active backend, idempotently.

    Everything is ``IF NOT EXISTS`` so this is safe to run repeatedly. Does NOT
    seed categories — categories are per-user (see :func:`seed_categories_for_user`
    / :func:`seed_local_user`).
    """
    db.run_sql_script(SCHEMA_DDL_SQLITE if config.USE_SQLITE else SCHEMA_DDL_PG)
    if config.USE_SQLITE:
        _ensure_sqlite_columns()


def _ensure_sqlite_columns():
    """Additive column migrations for existing SQLite installs.

    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, and SQLite
    has no ``ADD COLUMN IF NOT EXISTS`` — so check pragma info and add what's
    missing. Idempotent; runs on every startup.
    """
    with db.db_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(categories)").fetchall()}
        if "color" not in cols:
            conn.execute("ALTER TABLE categories ADD COLUMN color TEXT")
    _ensure_closed_accounts_zeroed()


def _ensure_closed_accounts_zeroed():
    """Give every already-closed account a zero balance so it leaves the totals.

    Net worth used to skip archived accounts outright, which rewrote the past:
    an account you closed in July vanished from last January as well. Totals now
    come from balances alone, and closing writes a zero at the closing date. Any
    account archived before that change has no such zero, so add one the day
    after its last recorded balance. Idempotent; runs on every startup.
    """
    with db.db_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.user_id,
                   (SELECT as_of FROM account_balances
                    WHERE account_id = a.id ORDER BY as_of DESC LIMIT 1) AS last_as_of,
                   (SELECT balance FROM account_balances
                    WHERE account_id = a.id ORDER BY as_of DESC LIMIT 1) AS last_balance
            FROM accounts a
            WHERE a.is_archived = 1
            """
        ).fetchall()
        for r in rows:
            if not r["last_as_of"] or not r["last_balance"]:
                continue  # never had a balance, or already ends at zero
            closed_on = (
                date.fromisoformat(r["last_as_of"]) + timedelta(days=1)
            ).isoformat()
            conn.execute(
                "INSERT INTO account_balances (user_id, account_id, as_of, balance) "
                "VALUES (%s, %s, %s, 0) ON CONFLICT (account_id, as_of) DO NOTHING",
                (r["user_id"], r["id"], closed_on),
            )


# ---------------------------------------------------------------------------
# Schema migration (hosted/Postgres: upgrade existing installs in place)
# ---------------------------------------------------------------------------
_MIGRATION_DDL = f"""
ALTER TABLE categories ADD COLUMN IF NOT EXISTS color TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user';
ALTER TABLE users ADD COLUMN IF NOT EXISTS note TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS decided_at TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS decided_by BIGINT REFERENCES users(id);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);

CREATE TABLE IF NOT EXISTS import_formats (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    signature   TEXT NOT NULL,
    delimiter   TEXT NOT NULL,
    date_col    INT NOT NULL,
    amount_col  INT NOT NULL,
    store_col   INT,
    amount_sign TEXT NOT NULL DEFAULT 'neg_expense'
                CHECK (amount_sign IN ('neg_expense','pos_expense')),
    created_at  TEXT NOT NULL DEFAULT {_NOW_TEXT},
    UNIQUE (user_id, signature)
);

CREATE TABLE IF NOT EXISTS bank_sessions (
    id            BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id    TEXT NOT NULL,
    aspsp_name    TEXT,
    aspsp_country TEXT,
    valid_until   TIMESTAMPTZ,
    accounts      JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id)
);
CREATE INDEX IF NOT EXISTS idx_bank_sessions_user ON bank_sessions(user_id);

CREATE TABLE IF NOT EXISTS manual_subscriptions (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    store      TEXT NOT NULL,
    amount     DOUBLE PRECISION NOT NULL,
    cadence    TEXT NOT NULL DEFAULT 'monthly'
               CHECK (cadence IN ('monthly', 'quarterly', 'yearly')),
    category   TEXT,
    type       TEXT NOT NULL DEFAULT 'expense' CHECK (type IN ('expense', 'income')),
    created_at TEXT NOT NULL DEFAULT {_NOW_TEXT}
);
CREATE INDEX IF NOT EXISTS idx_manual_subscriptions_user ON manual_subscriptions(user_id);
"""


def migrate_db():
    """Upgrade an existing install in place.

    SQLite (desktop) builds the complete schema in :func:`init_db`, so this is a
    no-op there. In hosted Postgres mode it idempotently adds the access-request
    columns + later tables to an existing DB and backfills admin/allowed emails.
    """
    if config.USE_SQLITE:
        return

    db.run_sql_script(_MIGRATION_DDL)

    admin_emails = list(config.ADMIN_EMAILS)
    allowed_emails = list(config.ALLOWED_EMAILS)

    with db.db_conn() as conn:
        if admin_emails:
            conn.execute(
                "UPDATE users SET role = 'admin', status = 'approved' "
                "WHERE lower(email) = ANY(%s)",
                (admin_emails,),
            )
        if allowed_emails:
            conn.execute(
                "UPDATE users SET status = 'approved' "
                "WHERE lower(email) = ANY(%s) AND status <> 'denied'",
                (allowed_emails,),
            )


# ---------------------------------------------------------------------------
# Per-user category seeding
# ---------------------------------------------------------------------------
def seed_categories_for_user(conn, user_id):
    """Seed the default categories for ``user_id`` if they have none.

    Accepts a live connection so the caller controls the transaction. Idempotent:
    a no-op if the user already has any category. 28 expense + 6 income = 34 rows.
    """
    row = conn.execute(
        "SELECT count(*) AS n FROM categories WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    if row["n"] != 0:
        return

    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO categories (user_id, name, type, is_default) "
            "VALUES (%s, %s, 'expense', 1)",
            [(user_id, name) for name in EXPENSE_CATEGORIES],
        )
        cur.executemany(
            "INSERT INTO categories (user_id, name, type, is_default) "
            "VALUES (%s, %s, 'income', 1)",
            [(user_id, name) for name in INCOME_CATEGORIES],
        )


def seed_categories(conn):
    """Removed: categories are per-user now. Use seed_categories_for_user()."""
    raise NotImplementedError(
        "Global seed_categories() was removed in the multi-user port. "
        "Use seed_categories_for_user(conn, user_id) instead."
    )


# ---------------------------------------------------------------------------
# Desktop single-user bootstrap
# ---------------------------------------------------------------------------
def seed_local_user():
    """Ensure the fixed desktop user (``config.LOCAL_USER_ID``) exists, approved.

    Desktop mode is single-user: it auto-logs-in one local user rather than going
    through Google OAuth. This creates that user row (id = LOCAL_USER_ID,
    approved admin) if absent and seeds its default categories. Idempotent —
    safe to call on every launch. No-op outside SQLite/desktop mode.
    """
    if not config.USE_SQLITE:
        return

    uid = config.LOCAL_USER_ID
    with db.db_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE id = %s", (uid,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO users (id, google_sub, email, name, status, role) "
                "VALUES (%s, 'local', 'local@localhost', 'Local User', 'approved', 'admin')",
                (uid,),
            )
        seed_categories_for_user(conn, uid)


# ---------------------------------------------------------------------------
# Backups — real file copy in SQLite mode; no-ops in hosted Postgres mode
# ---------------------------------------------------------------------------
def _backups_dir():
    """Directory that holds timestamped SQLite backups (next to the DB file)."""
    return os.path.join(os.path.dirname(os.path.abspath(config.SQLITE_PATH)), "backups")


def backup_db(reason: str = "manual"):
    """Copy the SQLite DB to ``backups/expenses-<ts>-<reason>.db`` and return its path.

    No-op (returns None) in hosted Postgres mode — managed backups / pg_dump are
    the provider's job there. In SQLite mode, returns None if the DB file does
    not exist yet (nothing to back up).
    """
    if not config.USE_SQLITE:
        return None
    src = config.SQLITE_PATH
    if not os.path.exists(src):
        return None
    dest_dir = _backups_dir()
    os.makedirs(dest_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_reason = "".join(c if c.isalnum() else "-" for c in reason)[:40] or "manual"
    dest = os.path.join(dest_dir, f"expenses-{ts}-{safe_reason}.db")
    shutil.copy2(src, dest)
    return dest


def list_backups() -> list[dict]:
    """List available SQLite backups (newest first); ``[]`` in hosted mode."""
    if not config.USE_SQLITE:
        return []
    dest_dir = _backups_dir()
    if not os.path.isdir(dest_dir):
        return []
    out = []
    for name in os.listdir(dest_dir):
        if not name.endswith(".db"):
            continue
        path = os.path.join(dest_dir, name)
        st = os.stat(path)
        out.append({
            "name": name,
            "path": path,
            "size": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    out.sort(key=lambda b: b["modified"], reverse=True)
    return out
