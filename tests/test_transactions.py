"""The transaction list and its filters, over HTTP.

/api/transactions is the endpoint the app reads most and the one nothing
tested. Every filter here is one the Transactions page offers, and the header
figures (`total`, `sum_expense`, `sum_income`) come from the same aggregate
pass as the rows, so a filter that stops narrowing one of them is a filter that
lies about the money.
"""

from helpers import add_tx, cat_id, stores


def _seed(client):
    """A small ledger spanning three months, both types, three categories."""
    groceries = cat_id(client, "Groceries")
    lunch     = cat_id(client, "Lunch")
    job       = cat_id(client, "Job", "income")

    add_tx(client, "2026-01-10", "K-Market",  groceries, 20.00)
    add_tx(client, "2026-01-20", "Lidl",      groceries, 35.50)
    add_tx(client, "2026-02-05", "Fafa's",    lunch,      9.90)
    add_tx(client, "2026-02-15", "K-Market",  groceries, 60.00)
    add_tx(client, "2026-03-01", "Employer",  job,      3000.00, "income")
    return {"groceries": groceries, "lunch": lunch, "job": job}


# ── CRUD ────────────────────────────────────────────────────────────
def test_create_read_update_delete(client, login, make_user):
    make_user()
    groceries = cat_id(client, "Groceries")
    lunch     = cat_id(client, "Lunch")

    created = add_tx(client, "2026-04-01", "Alepa", groceries, 12.30)
    assert created["category_name"] == "Groceries"

    listed = client.get("/api/transactions").get_json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == created["id"]

    res = client.put(f"/api/transactions/{created['id']}", json={
        "date": "2026-04-02", "store": "Alepa Kamppi",
        "category_id": lunch, "amount": 15.00, "type": "expense",
    })
    assert res.status_code == 200
    assert res.get_json()["store"] == "Alepa Kamppi"
    assert res.get_json()["category_name"] == "Lunch"

    assert client.delete(f"/api/transactions/{created['id']}").status_code == 204
    assert client.get("/api/transactions").get_json()["total"] == 0


def test_unknown_category_is_refused(client, login, make_user):
    make_user()
    res = client.post("/api/transactions", json={
        "date": "2026-04-01", "store": "Alepa",
        "category_id": 999999, "amount": 1.0, "type": "expense",
    })
    assert res.status_code == 400
    assert res.get_json()["error"] == "Category not found"


def test_missing_transaction_is_a_404(client, login, make_user):
    make_user()
    groceries = cat_id(client, "Groceries")
    put = client.put("/api/transactions/999999", json={
        "date": "2026-04-01", "store": "x",
        "category_id": groceries, "amount": 1.0, "type": "expense",
    })
    assert put.status_code == 404
    assert client.delete("/api/transactions/999999").status_code == 404


# ── Filters ─────────────────────────────────────────────────────────
def test_month_filter(client, login, make_user):
    make_user()
    _seed(client)
    got = client.get("/api/transactions?month=2026-01").get_json()
    assert got["total"] == 2
    assert set(stores(got)) == {"K-Market", "Lidl"}


def test_months_filter_takes_several(client, login, make_user):
    make_user()
    _seed(client)
    got = client.get("/api/transactions?months=2026-01,2026-03").get_json()
    assert got["total"] == 3
    assert "Fafa's" not in stores(got)


def test_type_filter(client, login, make_user):
    make_user()
    _seed(client)
    got = client.get("/api/transactions?type=income").get_json()
    assert stores(got) == ["Employer"]


def test_search_matches_store_or_category_ignoring_case(client, login, make_user):
    make_user()
    _seed(client)

    by_store = client.get("/api/transactions?q=k-market").get_json()
    assert by_store["total"] == 2

    # The same search also reaches the category name, which is why the query
    # joins categories rather than filtering on the store alone.
    by_category = client.get("/api/transactions?q=lunch").get_json()
    assert stores(by_category) == ["Fafa's"]


def test_date_range_filter(client, login, make_user):
    make_user()
    _seed(client)
    got = client.get(
        "/api/transactions?date_from=2026-01-15&date_to=2026-02-10").get_json()
    assert set(stores(got)) == {"Lidl", "Fafa's"}


def test_amount_range_filter(client, login, make_user):
    make_user()
    _seed(client)
    got = client.get("/api/transactions?amount_min=20&amount_max=40").get_json()
    assert set(stores(got)) == {"K-Market", "Lidl"}
    assert got["total"] == 2


def test_category_ids_filter(client, login, make_user):
    make_user()
    ids = _seed(client)
    got = client.get(
        f"/api/transactions?category_ids={ids['lunch']},{ids['job']}").get_json()
    assert set(stores(got)) == {"Fafa's", "Employer"}


# ── The header figures ──────────────────────────────────────────────
def test_totals_follow_the_filter(client, login, make_user):
    make_user()
    _seed(client)

    everything = client.get("/api/transactions").get_json()
    assert everything["total"] == 5
    assert everything["sum_expense"] == 125.40
    assert everything["sum_income"] == 3000.00

    january = client.get("/api/transactions?month=2026-01").get_json()
    assert january["sum_expense"] == 55.50
    assert january["sum_income"] == 0


# ── Order and paging ────────────────────────────────────────────────
def test_sort_by_amount_and_by_store(client, login, make_user):
    make_user()
    _seed(client)

    dearest = client.get(
        "/api/transactions?type=expense&sort=amount&dir=desc").get_json()
    assert stores(dearest)[0] == "K-Market"      # 60.00
    assert stores(dearest)[-1] == "Fafa's"       # 9.90

    by_store = client.get(
        "/api/transactions?type=expense&sort=store&dir=asc").get_json()
    assert stores(by_store)[0] == "Fafa's"


def test_dates_sort_chronologically_not_by_year(client, login, make_user):
    """Dates are ISO strings; sorting them as text has to order the days too.

    Casting them to a date on SQLite collapses every value to its year, and the
    id tiebreak then decides the order — which puts the list in insertion order
    while claiming to be sorted by date.
    """
    make_user()
    groceries = cat_id(client, "Groceries")
    add_tx(client, "2026-01-05", "first",  groceries, 1.0)
    add_tx(client, "2026-11-30", "last",   groceries, 1.0)
    add_tx(client, "2026-06-15", "middle", groceries, 1.0)

    got = client.get("/api/transactions?sort=date&dir=asc").get_json()
    assert stores(got) == ["first", "middle", "last"]


def test_paging(client, login, make_user):
    make_user()
    _seed(client)

    page1 = client.get("/api/transactions?per_page=2&page=1").get_json()
    page2 = client.get("/api/transactions?per_page=2&page=2").get_json()

    assert page1["total"] == page2["total"] == 5   # the count is of the filter
    assert len(page1["items"]) == len(page2["items"]) == 2
    assert page2["page"] == 2
    assert not set(stores(page1)) & set(stores(page2))


# ── Facet counts (the filter rail) ──────────────────────────────────
# The rail beside the list shows, against every filter value, the number of
# rows it would give. The rule that makes it honest: a facet's counts apply
# every OTHER filter but not its own, because they answer "what would I get
# if I clicked this" — counting with its own selection applied would report
# the list you are already looking at.
def _facets(client, query=""):
    return client.get("/api/transactions/facets" + query).get_json()


def _seed_facets(client):
    groceries = cat_id(client, "Groceries")
    lunch     = cat_id(client, "Lunch")
    job       = cat_id(client, "Job", "income")
    add_tx(client, "2026-01-10", "Lidl",     groceries,  20.0)
    add_tx(client, "2026-01-20", "K-Market", groceries,  30.0)
    add_tx(client, "2026-02-10", "Lidl",     groceries,  25.0)
    add_tx(client, "2026-02-11", "Fafa's",   lunch,      10.0)
    add_tx(client, "2026-02-25", "Employer", job,      3000.0, "income")
    return groceries, lunch, job


def test_facets_count_every_value(client, login, make_user):
    make_user()
    _seed_facets(client)
    got = _facets(client)

    assert {c["name"]: c["n"] for c in got["categories"]} == {
        "Groceries": 3, "Lunch": 1, "Job": 1}
    assert {t["type"]: t["n"] for t in got["types"]} == {"expense": 4, "income": 1}
    assert {m["month"]: m["n"] for m in got["months"]} == {"2026-01": 2, "2026-02": 3}


def test_categories_come_back_most_used_first(client, login, make_user):
    """The rail is meant to be scanned. Alphabetical order would bury the
    categories you live in under the ones you touch twice a year."""
    make_user()
    _seed_facets(client)
    got = _facets(client)
    assert [c["name"] for c in got["categories"]][0] == "Groceries"
    assert [c["n"] for c in got["categories"]] == sorted(
        [c["n"] for c in got["categories"]], reverse=True)


def test_a_facet_ignores_its_own_filter(client, login, make_user):
    """Picking Groceries must not collapse the category counts to Groceries
    alone — you still need to see what adding Lunch would give."""
    make_user()
    groceries, lunch, _ = _seed_facets(client)
    got = _facets(client, f"?category_ids={groceries}")

    counts = {c["name"]: c["n"] for c in got["categories"]}
    assert counts["Groceries"] == 3
    assert counts["Lunch"] == 1          # unchanged by the category filter


def test_a_facet_honours_every_other_filter(client, login, make_user):
    """The type counts DO narrow under a category filter — that one is not
    their own, so it applies."""
    make_user()
    groceries, _, _ = _seed_facets(client)
    got = _facets(client, f"?category_ids={groceries}")

    assert {t["type"]: t["n"] for t in got["types"]} == {"expense": 3}
    assert {m["month"]: m["n"] for m in got["months"]} == {"2026-01": 2, "2026-02": 1}


def test_period_covers_months_and_the_date_range_together(client, login, make_user):
    """The rail shows month picks and the date boxes as one section, and each
    undoes the other, so the month counts drop both."""
    make_user()
    _seed_facets(client)
    got = _facets(client, "?months=2026-01&date_from=2026-02-01")

    # Neither the month pick nor the range narrows the month counts...
    assert {m["month"]: m["n"] for m in got["months"]} == {"2026-01": 2, "2026-02": 3}
    # ...but both still narrow the list itself.
    listed = client.get("/api/transactions?months=2026-01&date_from=2026-02-01").get_json()
    assert listed["total"] == 0


def test_facets_narrow_under_the_search_box(client, login, make_user):
    make_user()
    _seed_facets(client)
    got = _facets(client, "?q=lidl")
    assert {c["name"]: c["n"] for c in got["categories"]} == {"Groceries": 2}


def test_facets_are_scoped_to_the_user(client, login, make_user):
    make_user()
    _seed_facets(client)
    assert sum(c["n"] for c in _facets(client)["categories"]) == 5
    make_user()                                    # fresh user, same client
    assert _facets(client)["categories"] == []
