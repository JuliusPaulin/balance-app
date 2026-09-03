"""The database: one SQLite file, reached through one import point.

``db``      what every caller imports. It re-exports the driver's public
            API — ``db_conn``, ``IntegrityError``, ``DatabaseError``,
            ``Json``, ``load_json`` — so route code never imports ``sqlite3``
            itself and a driver change stays inside this package.
``sqlite``  the engine. The SQL throughout the app is psycopg-flavoured
            (``%s`` placeholders, ``RETURNING``, ``ON CONFLICT``, dict rows)
            and this translates it on the way to the driver, which is why
            route code reads the way it does.
``schema``  the DDL, the seeding and the backups.

Import ``db``, not ``sqlite``.
"""
