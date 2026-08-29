"""Shared pytest fixtures — run the suite against the real Postgres test DB.

The desktop suite used to build its own ``sqlite3.connect(":memory:")`` schema.
The multi-user port moved every analytics function onto Postgres and made
``user_id`` mandatory, so the tests now run against the local ``expense_test``
database with proper per-test isolation.

Design
------
- A **session-scoped autouse** fixture points the psycopg pool at
  ``expense_test`` and builds the schema once (idempotently), tearing the pool
  down at the end of the session.
- A function-scoped ``user_conn`` fixture yields ``(conn, user_id)`` for a fresh
  ``users`` row. On teardown it ``DELETE``s that user; every user-owned table
  has ``ON DELETE CASCADE`` on its ``user_id`` FK, so the delete wipes all the
  user's data and tests stay isolated even though they share one database.
- ``seeded_user_conn`` additionally seeds the user's default categories.
- ``make_category`` is a helper factory: identity PKs mean tests can't hardcode
  category ids, so tests create the categories they need and map name -> id.

Because ``db.db_conn()`` commits on a clean exit and rolls back on exception, a
failing test still leaves the DB in a clean state; the ``user_conn`` teardown
then removes the user row regardless.
"""

import os
import uuid

import pytest

TEST_DATABASE_URL = "postgresql://localhost/expense_test"

# ---------------------------------------------------------------------------
# Pick the backend at IMPORT time, not fixture time.
#
# pytest imports this file before it collects any test module. Several test
# modules do `import db` / `import config` at module level, and `db` binds to
# an engine the moment it is first imported, from `config.USE_SQLITE` — which
# `config` computes from APP_MODE when IT is first imported.
#
# Setting these inside the session fixture was too late: the fixture runs after
# collection, so whichever module imported `db` first had already pinned the
# desktop default (SQLite) for the whole run. A test then got Postgres or
# SQLite depending only on which files were collected alongside it, which is
# why the suite failed differently run to run and why psycopg's `Json` adapter
# ended up being handed to sqlite3.
# ---------------------------------------------------------------------------
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("APP_MODE", "hosted")


@pytest.fixture(scope="session", autouse=True)
def _test_db():
    """Point the pool at expense_test and create the schema once per session."""
    import config
    config.DATABASE_URL = TEST_DATABASE_URL

    import db
    import database

    assert not config.USE_SQLITE, (
        "The suite must run against Postgres (expense_test). config.USE_SQLITE "
        "is True, so something imported config before APP_MODE was set."
    )

    db.reset_pool(TEST_DATABASE_URL)
    # init_db() creates the base schema; migrate_db() then idempotently adds the
    # access-request columns + status index (a no-op on a fresh schema that
    # already has them, and the upgrade path for an existing users table that
    # predates them). This mirrors the prod upgrade order.
    database.init_db()
    database.migrate_db()
    try:
        yield
    finally:
        db.close_pool()


def _insert_user(conn, status="approved", role="user"):
    """Insert a fresh users row with a unique google_sub; return its id.

    Defaults to an APPROVED user so the existing isolation/security/auth tests
    (which predate the access-request workflow and exercise the app as a normal
    logged-in user) keep passing past the new status gate. Access-specific tests
    pass ``status="pending"`` / ``"denied"`` explicitly.
    """
    sub = f"test-{uuid.uuid4()}"
    row = conn.execute(
        "INSERT INTO users (google_sub, email, name, status, role) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (sub, f"{sub}@example.test", "Test User", status, role),
    ).fetchone()
    return row["id"]


@pytest.fixture
def user_conn(_test_db):
    """Yield ``(conn, user_id)`` for a fresh user; cascade-delete on teardown.

    Opens a pooled connection, inserts a unique ``users`` row, and yields the
    live connection plus the new user id. On teardown the user is ``DELETE``d
    (cascading to all their data) so each test is fully isolated.

    The cleanup must run on a connection that can *see* the test's data. Pooled
    connections don't share an uncommitted transaction, so the DELETE is issued
    on a fresh connection only AFTER the test's connection has closed (and thus
    committed its data). If the test raised, ``db_conn()`` already rolled the
    test connection back, leaving nothing for the cascade to remove — the user
    row is then deleted on its own. Either way the test leaves no rows behind.
    """
    import db

    user_id = None
    try:
        with db.db_conn() as conn:
            user_id = _insert_user(conn)
            yield conn, user_id
    finally:
        # The test connection has now closed: it committed on a clean exit or
        # rolled back if the test raised. Either way, clean up on a fresh
        # connection that can see whatever was committed. (A failing test rolls
        # back ALL its data, including the user insert, so there's nothing left;
        # a passing test committed everything and the cascade clears it here.)
        if user_id is not None:
            with db.db_conn() as cleanup:
                cleanup.execute("DELETE FROM users WHERE id = %s", (user_id,))


@pytest.fixture
def seeded_user_conn(user_conn):
    """Like ``user_conn`` but with the user's default categories seeded."""
    import database

    conn, user_id = user_conn
    database.seed_categories_for_user(conn, user_id)
    return conn, user_id


@pytest.fixture
def make_category():
    """Factory: insert a category for a user and return its id.

    Identity PKs mean tests can't hardcode category ids, so tests that reference
    categories by id create them through this helper and map name -> id.

        cat_id = make_category(conn, user_id, "Entertainment")
    """
    def _make(conn, user_id, name, type_="expense"):
        row = conn.execute(
            "INSERT INTO categories (user_id, name, type) "
            "VALUES (%s, %s, %s) RETURNING id",
            (user_id, name, type_),
        ).fetchone()
        return row["id"]

    return _make


# ---------------------------------------------------------------------------
# HTTP-level fixtures (data isolation tests)
# ---------------------------------------------------------------------------
# The fixtures below drive the Flask app through its real HTTP surface via the
# test client. Unlike ``user_conn`` — whose connection is held open for the whole
# test and therefore commits only on teardown — these tests must *commit* a
# user's data before an HTTP request runs, because the test client borrows a
# DIFFERENT pooled connection that can't see another connection's uncommitted
# transaction. So data is created in short-lived ``db.db_conn()`` blocks (each
# commits on exit) and the user rows are cascade-deleted on teardown.


def _delete_user(user_id):
    """Cascade-delete a user (and all their data) on a fresh connection."""
    import db

    with db.db_conn() as cleanup:
        cleanup.execute("DELETE FROM users WHERE id = %s", (user_id,))


@pytest.fixture
def client(_test_db):
    """A Flask test client against the real app, with TESTING enabled."""
    import app as app_module

    app_module.app.config["TESTING"] = True
    # The existing suite mutates without a CSRF token; disable enforcement here
    # so those tests pass unchanged. The dedicated CSRF tests (test_security.py)
    # flip CSRF_ENABLED back on per-test.
    app_module.app.config["CSRF_ENABLED"] = False
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def login():
    """Return a helper that logs a client in as a given user id.

    There is no login route yet (Phase 3), so the session is injected directly,
    exactly as the real auth flow will eventually set it.

        login(client, user_id)
    """
    def _login(client, user_id, status="approved", role="user"):
        with client.session_transaction() as s:
            s["user_id"] = user_id
            # Mirror what the real auth flow sets so the status gate lets the
            # (approved-by-default) test user through.
            s["status"] = status
            s["role"] = role

    return _login


@pytest.fixture
def make_user():
    """Factory creating a fresh, category-seeded user; auto-deleted on teardown.

    Returns the new user's id. Each call commits the user + seeded categories so
    the test client (on its own pooled connection) can see them. Every created
    user is cascade-deleted at the end of the test.
    """
    import db
    import database

    created = []

    def _make():
        with db.db_conn() as conn:
            user_id = _insert_user(conn)
            database.seed_categories_for_user(conn, user_id)
        created.append(user_id)
        return user_id

    try:
        yield _make
    finally:
        for uid in created:
            _delete_user(uid)


@pytest.fixture
def fresh_conn():
    """Yield a short-lived helper that runs a callable inside a committed txn.

    ``fresh_conn(fn)`` opens a pooled connection, calls ``fn(conn)``, and commits
    on clean exit (the pool's context manager handles the commit). Use it to set
    up or read back a specific user's rows out-of-band from the HTTP requests.
    """
    import db

    def _run(fn):
        with db.db_conn() as conn:
            return fn(conn)

    return _run
