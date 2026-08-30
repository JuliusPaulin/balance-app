"""Merchant rules: the patterns that decide a category from a store name.

The rules are what makes an import worth doing, so the three match types and
the "apply to history" button are the parts guarded here. Applying a rule
rewrites transactions in bulk, which is the one operation on this endpoint that
cannot be shrugged off if it goes wide.
"""

from helpers import add_tx, cat_id


def _rule(client, pattern, category_id, match_type="exact"):
    res = client.post("/api/merchant-rules", json={
        "pattern": pattern, "category_id": category_id, "match_type": match_type,
    })
    assert res.status_code == 201, res.get_json()
    return res.get_json()


def test_create_list_update_delete(client, login, make_user):
    make_user()
    groceries = cat_id(client, "Groceries")
    lunch     = cat_id(client, "Lunch")

    rule = _rule(client, "K-Market", groceries, "contains")
    assert rule["category_name"] == "Groceries"
    assert [r["pattern"] for r in client.get("/api/merchant-rules").get_json()] \
        == ["K-Market"]

    res = client.put(f"/api/merchant-rules/{rule['id']}", json={
        "pattern": "Fafa", "category_id": lunch, "match_type": "contains",
    })
    assert res.status_code == 200
    assert res.get_json()["category_name"] == "Lunch"

    assert client.delete(f"/api/merchant-rules/{rule['id']}").status_code == 204
    assert client.get("/api/merchant-rules").get_json() == []


def test_rule_needs_a_category_that_exists(client, login, make_user):
    make_user()
    res = client.post("/api/merchant-rules",
                      json={"pattern": "x", "category_id": 999999})
    assert res.status_code == 400


# ── Preview ─────────────────────────────────────────────────────────
def test_preview_counts_what_a_pattern_would_catch(client, login, make_user):
    make_user()
    groceries = cat_id(client, "Groceries")
    add_tx(client, "2026-01-01", "K-Market Kamppi", groceries, 10.0)
    add_tx(client, "2026-01-02", "K-Market Kamppi", groceries, 12.0)
    add_tx(client, "2026-01-03", "K-Market Töölö",  groceries, 14.0)
    add_tx(client, "2026-01-04", "Lidl",            groceries, 16.0)

    contains = client.get(
        "/api/merchant-rules/preview?pattern=k-market&match_type=contains").get_json()
    assert contains["match_count"] == 3
    assert contains["distinct_stores"] == 2

    exact = client.get(
        "/api/merchant-rules/preview?pattern=Lidl&match_type=exact").get_json()
    assert exact["match_count"] == 1


def test_preview_of_an_empty_pattern_matches_nothing(client, login, make_user):
    """Otherwise an empty box in the rule modal reads as "matches everything"."""
    make_user()
    add_tx(client, "2026-01-01", "Lidl", cat_id(client, "Groceries"), 10.0)
    got = client.get("/api/merchant-rules/preview?pattern=  ").get_json()
    assert got == {"matches": [], "match_count": 0, "distinct_stores": 0}


def test_preview_limit_caps_the_rows_not_the_count(client, login, make_user):
    make_user()
    groceries = cat_id(client, "Groceries")
    for day in range(1, 6):
        add_tx(client, f"2026-01-0{day}", "Lidl", groceries, 10.0)

    got = client.get(
        "/api/merchant-rules/preview?pattern=Lidl&limit=2").get_json()
    assert got["match_count"] == 5
    assert len(got["matches"]) == 2


# ── Apply to history ────────────────────────────────────────────────
def test_apply_recategorizes_the_matching_rows(client, login, make_user):
    make_user()
    other = cat_id(client, "Other")
    lunch = cat_id(client, "Lunch")
    add_tx(client, "2026-01-01", "Fafa's Kamppi", other, 9.9)
    add_tx(client, "2026-01-02", "Fafa's Töölö",  other, 8.9)
    add_tx(client, "2026-01-03", "Lidl",          other, 20.0)

    rule = _rule(client, "Fafa", lunch, "contains")
    res = client.post(f"/api/merchant-rules/{rule['id']}/apply")
    assert res.status_code == 200
    assert res.get_json()["updated"] == 2

    by_store = {t["store"]: t["category_name"]
                for t in client.get("/api/transactions").get_json()["items"]}
    assert by_store["Fafa's Kamppi"] == "Lunch"
    assert by_store["Lidl"] == "Other"


def test_apply_leaves_income_alone(client, login, make_user):
    """An expense rule must not drag an income row onto an expense category."""
    make_user()
    lunch      = cat_id(client, "Lunch")
    job        = cat_id(client, "Job", "income")
    add_tx(client, "2026-01-05", "Fafa Oy", job, 3000.0, "income")

    rule = _rule(client, "Fafa", lunch, "contains")
    assert client.post(f"/api/merchant-rules/{rule['id']}/apply").get_json()["updated"] == 0
    assert client.get("/api/transactions").get_json()["items"][0]["category_name"] == "Job"


def test_apply_counts_only_what_it_changed(client, login, make_user):
    """A row already in the right category is not an update."""
    make_user()
    lunch = cat_id(client, "Lunch")
    add_tx(client, "2026-01-01", "Fafa's", lunch, 9.9)

    rule = _rule(client, "Fafa", lunch, "contains")
    assert client.post(f"/api/merchant-rules/{rule['id']}/apply").get_json()["updated"] == 0


def test_apply_to_a_missing_rule_is_a_404(client, login, make_user):
    make_user()
    assert client.post("/api/merchant-rules/999999/apply").status_code == 404
