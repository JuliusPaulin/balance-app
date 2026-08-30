"""Small helpers shared by the HTTP-level route tests.

The local user is seeded with the default categories, so tests look their
category ids up by name rather than creating duplicates of "Groceries".
"""


def cat_id(client, name, type_="expense"):
    """The seeded category's id. Name AND type — "Other" exists on both sides."""
    for c in client.get("/api/categories").get_json():
        if c["name"] == name and c["type"] == type_:
            return c["id"]
    raise AssertionError(f"no seeded {type_} category named {name!r}")


def add_tx(client, date, store, category_id, amount, type_="expense"):
    """POST a transaction and return the created row."""
    res = client.post("/api/transactions", json={
        "date": date, "store": store, "category_id": category_id,
        "amount": amount, "type": type_,
    })
    assert res.status_code == 201, res.get_json()
    return res.get_json()


def stores(payload):
    """The store names in a /api/transactions response, in the order returned."""
    return [t["store"] for t in payload["items"]]
