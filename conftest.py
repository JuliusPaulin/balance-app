"""Shared pytest fixtures — run the suite against a throwaway SQLite database.

Balance is a local, single-user app: one SQLite file, one fixed user
(``config.LOCAL_USER_ID``), no login. The suite runs against exactly that, so
what the tests exercise is what ships.

Two things matter here.

**Never touch the real database.** ``SQLITE_PATH`` is pointed at a temporary
file below, and it is set when this module is imported — before pytest collects
any test module. Several test modules ``import config`` / ``import db`` at the
top, and both read their settings once, on first import. Setting the path in a
fixture would be too late: the first module to import ``config`` would already
have pinned ``~/Library/Application Support/Balance/expenses.db``, and the suite
would write into real figures.

**Reset between tests.** Every table carries ``user_id`` with
``ON DELETE CASCADE``, so deleting the one user row wipes all its data. Each
test that asks for a user gets a freshly seeded one.
"""

import os
import tempfile

import pytest

# ---------------------------------------------------------------------------
# Point the app at a scratch database BEFORE anything imports config.
# ---------------------------------------------------------------------------
_TMP_DIR = tempfile.mkdtemp(prefix="balance-tests-")
TEST_SQLITE_PATH = os.path.join(_TMP_DIR, "expenses.db")
os.environ["SQLITE_PATH"] = TEST_SQLITE_PATH


@pytest.fixture(scope="session", autouse=True)
def _test_db():
    """Build the schema once per session in the scratch database."""
    import config
    import database

    assert config.SQLITE_PATH == TEST_SQLITE_PATH, (
        f"The suite must run against a scratch database, not {config.SQLITE_PATH}. "
        "Something imported config before conftest set SQLITE_PATH."
    )

    database.init_db()
    yield
    import db
    db.close_pool()


def _reset_local_user():
    """Wipe the local user's data and re-seed a clean one.

    Deleting the ``users`` row cascades through every table that references it,
    which is every table holding data. ``seed_local_user()`` then puts the row
    and its default categories back.
    """
    import config
    import database
    import db

    with db.db_conn() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (config.LOCAL_USER_ID,))
    database.seed_local_user()

    # Recurring detection is cached in memory against a version counter that
    # only the write routes bump. Wiping the user behind their back leaves the
    # previous test's result in that cache under the same key, so bump it here
    # too — otherwise a forecast or subscriptions test reads the last test's data.
    import core
    core.bump_data_version()

    return config.LOCAL_USER_ID


@pytest.fixture(autouse=True)
def _clean_db(_test_db):
    """Give every test an empty database, and leave one behind."""
    _reset_local_user()
    yield
    _reset_local_user()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def user_conn():
    """Yield ``(conn, user_id)`` on a live connection to the local user.

    There is only ever one user. ``_clean_db`` has already reset the database,
    so the user starts with default categories and no data.
    """
    import config
    import db

    with db.db_conn() as conn:
        yield conn, config.LOCAL_USER_ID


@pytest.fixture
def seeded_user_conn(user_conn):
    """Alias kept for the tests that ask for it — the local user is always seeded."""
    return user_conn


@pytest.fixture
def make_category():
    """Factory: insert a category and return its id.

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
# HTTP-level fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    """A Flask test client against the real app, with TESTING enabled."""
    import app as app_module

    app_module.app.config["TESTING"] = True
    # Most tests mutate without a CSRF token; the dedicated CSRF tests in
    # test_security.py flip enforcement back on per-test.
    app_module.app.config["CSRF_ENABLED"] = False
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def login():
    """No-op: there is no login. Kept so tests can read as they always did."""
    def _login(client, user_id=None, **_ignored):
        return None

    return _login


@pytest.fixture
def make_user():
    """Return the local user's id, freshly seeded.

    The app has exactly one user, so this hands back the same id every time —
    ``config.LOCAL_USER_ID``. Calling it twice in one test does NOT produce two
    users; tests that need two tenants no longer have anything to test.
    """
    def _make():
        return _reset_local_user()

    return _make


@pytest.fixture
def fresh_conn():
    """Yield a helper that runs a callable inside a committed transaction.

    ``fresh_conn(fn)`` opens a connection, calls ``fn(conn)``, and commits on a
    clean exit. Use it to set up or read back rows out-of-band from the HTTP
    requests the test client makes.
    """
    import db

    def _run(fn):
        with db.db_conn() as conn:
            return fn(conn)

    return _run
