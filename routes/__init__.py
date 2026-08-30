"""Every route the app serves, one module per area.

Each module owns a Flask blueprint named after itself. ``ALL`` is the list
:func:`register` attaches, and the only place a new area has to be added.

The modules lean on each other in one direction only, so there are no import
cycles: everything imports ``core``; ``csv_import`` borrows the rule rebuilder
from ``merchant_rules``; ``bank_import`` borrows the staging helpers from
``csv_import``.
"""

from routes import (bank_import, categories, chat, csv_import, dashboard,
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
    chat.bp,
]


def register(flask_app):
    """Attach every blueprint to ``flask_app``. Safe to call twice.

    ``app.py`` calls this on start-up, and :mod:`ai_tools` calls it before it
    dispatches an endpoint in-process: the chat tools read the app's own routes,
    and a process that imports ``ai_tools`` without importing ``app`` would
    otherwise hold a Flask app with no routes on it at all.
    """
    for blueprint in ALL:
        if blueprint.name not in flask_app.blueprints:
            flask_app.register_blueprint(blueprint)
