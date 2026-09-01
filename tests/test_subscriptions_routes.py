"""The Subscriptions page's endpoints, driven over HTTP.

Three of these cover a door that only opened one way. `recurring_dismissed` was
written by a ✕ on every row and read by nothing the user could reach: detection
dropped the series without a word and took its cost out of the headline with it,
so a misclick removed a real charge from the app's own count of what is paid
each month, invisibly and for good. The un-hide endpoint had existed all along
with nothing calling it.
"""
from datetime import date, timedelta

from recurring import GROUP_BILL, GROUP_SUBSCRIPTION, signature
from tests.helpers import cat_id


def _series(client, store, category_id, amount, months, day=4):
    """Post `months` monthly charges ending last month, so the series is live."""
    start = date.today().replace(day=1) - timedelta(days=30 * months)
    for k in range(months):
        d = start + timedelta(days=30 * k)
        r = client.post("/api/transactions", json={
            "date": d.isoformat(), "store": store, "category_id": category_id,
            "amount": -abs(amount), "type": "expense"})
        assert r.status_code in (200, 201), r.data


# ── What the page reads ─────────────────────────────────────────────────
def test_recurring_reports_groups_and_a_subscription_only_headline(client):
    _series(client, "Netflix", cat_id(client, "Entertainment"), 16.0, 10)
    _series(client, "Vuokra Oy", cat_id(client, "Rent"), 1250.0, 10)

    data = client.get("/api/recurring").get_json()
    by_store = {i["store"]: i for i in data["items"]}
    assert by_store["Netflix"]["group"] == GROUP_SUBSCRIPTION
    assert by_store["Vuokra Oy"]["group"] == GROUP_BILL

    s = data["summary"]
    assert s["monthly_total"] < 100          # the rent is not in it
    assert s["groups"][GROUP_BILL]["monthly_total"] > 1000
    assert s["active_count"] == 1


def test_recurring_reports_what_is_hidden(client):
    """Hidden series ride along with the visible ones, or nothing names them."""
    _series(client, "Netflix", cat_id(client, "Entertainment"), 16.0, 10)
    sig = signature("Netflix", "monthly")

    before = client.get("/api/recurring").get_json()
    assert before["dismissed"] == []

    assert client.post("/api/recurring/dismiss",
                       json={"signature": sig}).status_code == 201
    after = client.get("/api/recurring").get_json()
    assert [i["store"] for i in after["items"]] == []      # gone from the table
    assert len(after["dismissed"]) == 1                    # and named as hidden
    assert after["dismissed"][0]["signature"] == sig
    assert after["dismissed"][0]["store"] == "netflix"
    assert after["dismissed"][0]["cadence"] == "monthly"


def test_a_hidden_series_can_come_back(client):
    _series(client, "Netflix", cat_id(client, "Entertainment"), 16.0, 10)
    sig = signature("Netflix", "monthly")
    client.post("/api/recurring/dismiss", json={"signature": sig})
    assert client.get("/api/recurring").get_json()["summary"]["monthly_total"] == 0

    assert client.delete(f"/api/recurring/dismiss/{sig}").status_code == 204
    back = client.get("/api/recurring").get_json()
    assert [i["store"] for i in back["items"]] == ["Netflix"]
    assert back["dismissed"] == []
    assert back["summary"]["monthly_total"] > 0


# ── Moving a series between groups ──────────────────────────────────────
def test_group_override_round_trip(client):
    _series(client, "Kuntokeskus", cat_id(client, "Exercise"), 34.0, 10)
    sig = signature("Kuntokeskus", "monthly")

    assert client.get("/api/recurring").get_json()["summary"]["monthly_total"] > 30

    r = client.put("/api/recurring/group",
                   json={"signature": sig, "group": GROUP_BILL})
    assert r.status_code == 200
    moved = client.get("/api/recurring").get_json()
    item = moved["items"][0]
    assert item["group"] == GROUP_BILL and item["moved"] is True
    assert moved["summary"]["monthly_total"] == 0
    assert moved["summary"]["groups"][GROUP_BILL]["monthly_total"] > 30

    # Clearing hands the row back to the category's own answer.
    assert client.put("/api/recurring/group",
                      json={"signature": sig, "group": None}).status_code == 200
    back = client.get("/api/recurring").get_json()
    assert back["items"][0]["group"] == GROUP_SUBSCRIPTION
    assert back["items"][0]["moved"] is False
    assert back["summary"]["monthly_total"] > 30


def test_group_override_rejects_an_invented_group(client):
    r = client.put("/api/recurring/group",
                   json={"signature": "x|monthly", "group": "whatever"})
    assert r.status_code == 400
    assert "group must be one of" in r.get_json()["error"]


def test_group_override_needs_a_signature(client):
    assert client.put("/api/recurring/group",
                      json={"group": GROUP_BILL}).status_code == 400


# ── What subscriptions cost, month by month ─────────────────────────────
def test_history_sums_the_real_charges_not_todays_total(client):
    """The point of the chart: a price rise has to step, not flatten.

    Projecting the current monthly cost backwards would draw twelve identical
    bars and answer nothing. Each month is the transactions that actually landed
    in it.
    """
    ent = cat_id(client, "Entertainment")
    start = date.today().replace(day=1) - timedelta(days=30 * 10)
    for k in range(10):
        d = start + timedelta(days=30 * k)
        client.post("/api/transactions", json={
            "date": d.isoformat(), "store": "Netflix", "category_id": ent,
            "amount": -(10.0 if k < 5 else 20.0), "type": "expense"})

    data = client.get("/api/recurring/history?months=12").get_json()
    assert len(data["months"]) == 12
    assert [m["month"] for m in data["months"]] == sorted(m["month"] for m in data["months"])
    charged = [m["total"] for m in data["months"] if m["total"] > 0]
    assert charged, "the series should show up in the months it charged"
    assert min(charged) < 15 < max(charged), "the price rise must step"


def test_history_ignores_bills_and_income(client):
    """The chart is named after the subscriptions, so it counts only those."""
    _series(client, "Vuokra Oy", cat_id(client, "Rent"), 1250.0, 10)
    data = client.get("/api/recurring/history?months=12").get_json()
    assert all(m["total"] == 0 for m in data["months"])


def test_history_months_are_clamped(client):
    assert len(client.get("/api/recurring/history?months=0").get_json()["months"]) == 1
    assert len(client.get("/api/recurring/history?months=999").get_json()["months"]) == 36
    assert len(client.get("/api/recurring/history?months=nope").get_json()["months"]) == 12
