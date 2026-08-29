"""Database layer dispatcher — SQLite (desktop) or Postgres (hosted).

The whole app imports ``db`` and calls ``db.db_conn()`` / ``db.run_sql_script()``
without caring which backend is live. This module picks the engine from
:data:`config.USE_SQLITE` and re-exports its public surface:

- **Desktop (default)** → :mod:`db_sqlite`, a local single-user SQLite file. No
  network database, no psycopg dependency required.
- **Hosted** → :mod:`db_postgres`, the psycopg connection pool against Postgres.

Both engines expose the same contract (``db_conn`` context manager,
``run_sql_script``, ``close_pool``, ``reset_pool``), so everything downstream is
backend-agnostic.
"""

import config

if config.USE_SQLITE:
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
else:
    from db_postgres import (  # noqa: F401  (re-exported public API)
        db_conn,
        run_sql_script,
        close_pool,
        reset_pool,
        IntegrityError,
        DatabaseError,
        Json,
        load_json,
    )
