"""Every route the app serves, one module per area.

Each module owns a Flask blueprint named after itself. ``ALL`` is the list
``app.py`` registers, and the only place a new area has to be added.

The modules lean on each other in one direction only, so there are no import
cycles: everything imports ``core``; ``csv_import`` borrows the rule rebuilder
from ``merchant_rules``; ``bank_import`` borrows the staging helpers from
``csv_import``.
"""

from routes import (bank_import, categories, csv_import, dashboard,
                    merchant_rules, net_worth, notes, subscriptions, system,
                    transactions)

ALL = [
    system.bp,
    categories.bp,
    merchant_rules.bp,
    notes.bp,
    transactions.bp,
    dashboard.bp,
    subscriptions.bp,
    net_worth.bp,
    csv_import.bp,
    bank_import.bp,
]
