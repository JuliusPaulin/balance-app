"""Categories over HTTP: the list, renaming, and what deleting one does.

Deleting is the part worth guarding. A category with transactions behind it
cannot simply go: the rows have to be moved somewhere first, and the app is
meant to say so rather than lose them.
"""

from helpers import add_tx, cat_id


def _names(client, type_=None):
    url = "/api/categories" + (f"?type={type_}" if type_ else "")
    return [c["name"] for c in client.get(url).get_json()]


def test_list_reports_usage(client, login, make_user):
    make_user()
    groceries = cat_id(client, "Groceries")
    add_tx(client, "2026-01-10", "K-Market", groceries, 20.0)
    add_tx(client, "2026-03-04", "Lidl",     groceries, 30.0)

    row = next(c for c in client.get("/api/categories").get_json()
               if c["id"] == groceries)
    assert row["tx_count"] == 2
    assert row["last_used"] == "2026-03-04"


def test_type_filter_splits_income_from_expense(client, login, make_user):
    make_user()
    assert "Job" in _names(client, "income")
    assert "Job" not in _names(client, "expense")


def test_create_and_rename(client, login, make_user):
    make_user()
    res = client.post("/api/categories", json={"name": "Sauna", "type": "expense"})
    assert res.status_code == 201
    new_id = res.get_json()["id"]
    assert "Sauna" in _names(client)

    renamed = client.put(f"/api/categories/{new_id}", json={"name": "Sauna & spa"})
    assert renamed.status_code == 200
    assert renamed.get_json()["name"] == "Sauna & spa"


def test_update_needs_something_to_update(client, login, make_user):
    make_user()
    res = client.put(f"/api/categories/{cat_id(client, 'Groceries')}", json={})
    assert res.status_code == 400


def test_update_and_delete_of_a_missing_category(client, login, make_user):
    make_user()
    assert client.put("/api/categories/999999", json={"name": "x"}).status_code == 404
    assert client.delete("/api/categories/999999").status_code == 404


def test_delete_moves_the_transactions_it_is_given_a_home_for(client, login, make_user):
    make_user()
    lunch     = cat_id(client, "Lunch")
    groceries = cat_id(client, "Groceries")
    add_tx(client, "2026-01-10", "Fafa's", lunch, 9.9)

    res = client.delete(f"/api/categories/{lunch}?reassign_to={groceries}")
    assert res.status_code == 204

    moved = client.get("/api/transactions").get_json()["items"][0]
    assert moved["category_id"] == groceries
    assert "Lunch" not in _names(client)


def test_delete_in_use_without_a_home_is_refused(client, login, make_user):
    """The rows would be orphaned, so the request loses rather than the data."""
    make_user()
    lunch = cat_id(client, "Lunch")
    add_tx(client, "2026-01-10", "Fafa's", lunch, 9.9)

    res = client.delete(f"/api/categories/{lunch}")
    assert res.status_code == 409
    assert "Reassign" in res.get_json()["error"]
    assert client.get("/api/transactions").get_json()["total"] == 1


def test_reassignment_target_must_exist(client, login, make_user):
    make_user()
    lunch = cat_id(client, "Lunch")
    add_tx(client, "2026-01-10", "Fafa's", lunch, 9.9)

    res = client.delete(f"/api/categories/{lunch}?reassign_to=999999")
    assert res.status_code == 400
    assert client.get("/api/transactions").get_json()["total"] == 1


def test_delete_takes_its_merchant_rules_with_it(client, login, make_user):
    """A rule pointing at a category that no longer exists can never fire."""
    make_user()
    lunch     = cat_id(client, "Lunch")
    groceries = cat_id(client, "Groceries")
    client.post("/api/merchant-rules",
                json={"pattern": "Fafa", "category_id": lunch, "match_type": "contains"})

    assert client.delete(
        f"/api/categories/{lunch}?reassign_to={groceries}").status_code == 204
    assert client.get("/api/merchant-rules").get_json() == []
