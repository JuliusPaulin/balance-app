"""Saving a plan, and reading it back next to the Balance baseline.

The figures are made up. The point is that a plan survives a reload, a bad
save does not wipe a good one, and an older database gains the table without
losing the rows it already had.
"""

from datetime import date

import config
from data import db
from data.schema import init_db
from helpers import add_tx, cat_id


def _assumptions(**overrides):
    base = {
        "age": 27, "retAge": 65, "endAge": 90,
        "income": 3800, "expenses": 2500, "rent": 950,
        "incG": 3, "expG": 2, "infl": 2,
        "startInv": 25000, "retPre": 6, "retPost": 4,
        "retPct": 80, "pension": 1900, "pensionAge": 67, "swr": 3.5,
    }
    base.update(overrides)
    return base


def _event(kind, **overrides):
    event = {
        "house": {"name": "Apartment", "age": 32, "price": 320000, "downPct": 15,
                  "tax": 1.5, "rate": 3.5, "term": 25, "costPct": 1.5, "appr": 2,
                  "replacesRent": True},
        "car": {"name": "Car", "age": 30, "amount": 25000, "every": 8, "until": 70},
        "child": {"name": "First child", "age": 33, "amount": 9000, "years": 18},
        "sabbatical": {"name": "Sabbatical", "age": 40, "years": 1, "pct": 50},
        "windfall": {"name": "Inheritance", "age": 55, "amount": 60000},
        "extra": {"name": "Side income", "age": 30, "amount": 6000, "years": 10},
        "oneoff": {"name": "Wedding", "age": 30, "amount": 20000, "years": 1},
        "shock": {"name": "Market crash", "age": 45, "pct": 30},
        "custom": {"name": "Summer cottage", "dir": "cost", "age": 45, "amount": 120000,
                   "repeat": "once", "years": 5, "every": 10, "until": 70, "real": True,
                   "loan": True, "downPct": 25, "rate": 4.5, "term": 15,
                   "asset": True, "assetPct": 100, "assetG": 1, "running": 3000},
    }[kind]
    return {"type": kind, "on": True, **event, **overrides}


def _plan(events=None, name="Base plan"):
    return {
        "scenarios": [{
            "name": name,
            "assumptions": _assumptions(),
            "events": events if events is not None else [_event("house")],
        }],
        "current": 0,
        "unit": "real",
        "compare": False,
    }


def test_a_new_database_has_nothing_saved_and_starts_from_zero(client, login, make_user):
    make_user()
    body = client.get("/api/planning").get_json()
    assert body["plan"] is None
    assert body["updated_at"] is None
    assert body["start_year"] == date.today().year
    assert body["baseline"]["coverage"] == "none"
    assert body["baseline"]["income"] == 0
    assert body["baseline"]["start_inv"] == 0
    assert body["defaults"]["income"] == 0
    assert body["defaults"]["retAge"] == 65
    assert body["defaults"]["pension"] == 0


def test_the_baseline_reads_the_same_figures_the_rest_of_the_app_does(client, login, make_user):
    make_user()
    today = date.today()
    job = cat_id(client, "Job", "income")
    rent = cat_id(client, "Rent")
    groceries = cat_id(client, "Groceries")
    investments = cat_id(client, "Investments")
    for delta in range(-11, 1):
        index = today.year * 12 + (today.month - 1) + delta
        year, month0 = divmod(index, 12)
        when = date(year, month0 + 1, 1).isoformat()
        add_tx(client, when, "Employer", job, 4000, "income")
        add_tx(client, when, "Landlord", rent, 900, "expense")
        add_tx(client, when, "Market", groceries, 600, "expense")
        add_tx(client, when, "Broker", investments, 400, "expense")
    account = client.post("/api/accounts", json={"name": "Savings", "type": "asset"})
    assert account.status_code == 201
    client.post(
        f"/api/accounts/{account.get_json()['id']}/balances",
        json={"as_of": today.isoformat(), "balance": 25000},
    )

    baseline = client.get("/api/planning").get_json()["baseline"]
    worth = client.get("/api/networth/summary").get_json()
    assert baseline["income"] == 4000
    assert baseline["expenses"] == 1500
    assert baseline["rent"] == 900
    assert baseline["transfers_excluded"] == 400
    assert baseline["start_inv"] == worth["net_worth"] == 25000


def test_a_plan_round_trips_with_every_event_type(client, login, make_user):
    make_user()
    events = [_event(kind, on=(kind not in ("sabbatical", "shock"))) for kind in (
        "house", "car", "child", "sabbatical", "custom", "windfall", "extra",
        "oneoff", "shock",
    )]
    plan = _plan(events)
    plan["scenarios"].append({
        "name": "Keep renting",
        "assumptions": _assumptions(),
        "events": [{**event, "on": False} if event["type"] == "house" else event for event in events],
    })
    plan["compare"] = True
    plan["unit"] = "nominal"
    plan["current"] = 1

    saved = client.put("/api/planning", json=plan)
    assert saved.status_code == 200, saved.get_json()
    body = saved.get_json()
    assert body["current"] == 1
    assert body["unit"] == "nominal"
    assert body["compare"] is True
    assert [event["type"] for event in body["scenarios"][0]["events"]] == [
        "house", "car", "child", "sabbatical", "custom", "windfall", "extra",
        "oneoff", "shock",
    ]
    cottage = next(event for event in body["scenarios"][0]["events"] if event["type"] == "custom")
    assert cottage["loan"] is True
    assert cottage["asset"] is True
    assert cottage["amount"] == 120000
    assert cottage["downPct"] == 25
    assert cottage["running"] == 3000
    assert body["scenarios"][1]["events"][0]["on"] is False

    again = client.get("/api/planning").get_json()
    assert again["plan"]["scenarios"][1]["name"] == "Keep renting"
    assert again["plan"]["scenarios"][0]["events"][4]["name"] == "Summer cottage"
    assert again["updated_at"]


def test_a_rejected_save_leaves_the_stored_plan_alone(client, login, make_user):
    make_user()
    assert client.put("/api/planning", json=_plan()).status_code == 200

    rejected = client.put("/api/planning", json={"scenarios": []})
    assert rejected.status_code == 400
    assert "scenario" in rejected.get_json()["error"]

    unknown = client.put("/api/planning", json=_plan([
        {"type": "lottery", "on": True, "name": "Jackpot", "age": 40},
    ]))
    assert unknown.status_code == 400

    nameless = client.put("/api/planning", json=_plan([
        {"type": "car", "on": True, "name": "  ", "age": 30, "amount": 1},
    ]))
    assert nameless.status_code == 400

    stored = client.get("/api/planning").get_json()["plan"]
    assert stored["scenarios"][0]["events"][0]["type"] == "house"
    assert stored["scenarios"][0]["assumptions"]["income"] == 3800


def test_saving_a_plan_does_not_touch_transactions(client, login, make_user):
    make_user()
    add_tx(client, "2026-01-15", "Market", cat_id(client, "Groceries"), 12.5, "expense")
    before = client.get("/api/transactions?per_page=50").get_json()["items"]

    assert client.put("/api/planning", json=_plan()).status_code == 200

    after = client.get("/api/transactions?per_page=50").get_json()["items"]
    assert [(row["store"], row["amount"]) for row in after] == [
        (row["store"], row["amount"]) for row in before
    ]


def test_init_db_adds_the_planning_table_without_rewriting_what_is_there(tmp_path):
    """An existing database gains the table. The transaction that was already
    there is still the same transaction afterwards."""
    from data.schema import seed_local_user

    original = config.SQLITE_PATH
    db.reset_pool(str(tmp_path / "older.db"))
    try:
        init_db()
        seed_local_user()
        with db.db_conn() as conn:
            conn.execute("DROP TABLE planning_plans")
            groceries = conn.execute(
                "SELECT id FROM categories WHERE user_id = %s AND name = 'Groceries' "
                "AND type = 'expense'",
                (config.LOCAL_USER_ID,),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO transactions (user_id, date, store, category_id, amount, type) "
                "VALUES (%s, '2024-05-02', 'K-Market', %s, 12.4, 'expense')",
                (config.LOCAL_USER_ID, groceries),
            )
        init_db()
        with db.db_conn() as conn:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'planning_plans'"
            ).fetchone()
            kept = conn.execute("SELECT store, amount FROM transactions").fetchone()
        assert table is not None
        assert (kept["store"], kept["amount"]) == ("K-Market", 12.4)
    finally:
        db.reset_pool(original)
