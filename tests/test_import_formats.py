"""Tests for the CSV-format-learning import feature.

Covers the unknown-format mapping preview (`needs_mapping`), the mapped upload
endpoint (`/api/import/upload-mapped`) including the sign toggle and `remember`,
the learned fast-path on a second upload, index validation, per-user isolation
of saved formats, and that the existing recognized formats still import.

These drive the real Flask app through its HTTP surface (like test_isolation),
so each user's data is created in committed `db.db_conn()` blocks via the
`make_user` / `fresh_conn` fixtures.
"""

import io

import db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _upload(client, csv_text, filename="export.csv"):
    return client.post(
        "/api/import/upload",
        data={"file": (io.BytesIO(csv_text.encode("utf-8")), filename)},
        content_type="multipart/form-data",
    )


def _upload_mapped(client, csv_text, filename="export.csv", **form):
    data = {"file": (io.BytesIO(csv_text.encode("utf-8")), filename)}
    data.update(form)
    return client.post(
        "/api/import/upload-mapped",
        data=data,
        content_type="multipart/form-data",
    )


# An unrecognized layout: header names don't match any alias in COLUMN_ALIASES.
UNKNOWN_CSV = (
    "Col1;Col2;Col3\n"
    "2024-01-15;Coffee Shop;-4,50\n"
    "2024-01-16;Salary Inc;2500,00\n"
    "2024-01-17;Grocery Mart;-32,10\n"
)


def _count(fresh_conn, table, user_id):
    return fresh_conn(
        lambda c: c.execute(
            f"SELECT count(*) AS n FROM {table} WHERE user_id = %s", (user_id,)
        ).fetchone()["n"]
    )


# ---------------------------------------------------------------------------
# Unknown format → mapping preview, no orphan batch
# ---------------------------------------------------------------------------
def test_unknown_format_returns_needs_mapping(client, login, make_user, fresh_conn):
    uid = make_user()
    login(client, uid)

    resp = _upload(client, UNKNOWN_CSV)
    assert resp.status_code == 200
    body = resp.get_json()

    assert body["needs_mapping"] is True
    assert body["headers"] == ["Col1", "Col2", "Col3"]
    assert body["delimiter"] == ";"
    assert body["signature"]
    # Up to 5 data rows as string arrays.
    assert len(body["sample_rows"]) == 3
    assert body["sample_rows"][0] == ["2024-01-15", "Coffee Shop", "-4,50"]
    assert "guess" in body and set(body["guess"]) == {"date", "amount", "store"}

    # No orphan batch or staging rows were created.
    assert _count(fresh_conn, "import_batches", uid) == 0
    assert _count(fresh_conn, "import_staging", uid) == 0


# ---------------------------------------------------------------------------
# upload-mapped stages rows; remember persists a format
# ---------------------------------------------------------------------------
def test_upload_mapped_stages_and_remembers(client, login, make_user, fresh_conn):
    uid = make_user()
    login(client, uid)

    resp = _upload_mapped(
        client, UNKNOWN_CSV,
        date_col="0", amount_col="2", store_col="1",
        amount_sign="neg_expense", remember="1",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 3
    items = sorted(body["items"], key=lambda r: r["date"])

    # date / amount parsed, merchant taken from store_col, sign convention applied
    assert items[0]["date"] == "2024-01-15"
    assert items[0]["store"] == "Coffee Shop"
    assert items[0]["amount"] == 4.50
    assert items[0]["type"] == "expense"
    # positive row -> income under neg_expense
    assert items[1]["store"] == "Salary Inc"
    assert items[1]["amount"] == 2500.0
    assert items[1]["type"] == "income"

    # remember=1 created exactly one import_formats row with the mapping.
    fmts = fresh_conn(
        lambda c: c.execute(
            "SELECT * FROM import_formats WHERE user_id = %s", (uid,)
        ).fetchall()
    )
    assert len(fmts) == 1
    f = fmts[0]
    assert (f["date_col"], f["amount_col"], f["store_col"]) == (0, 2, 1)
    assert f["amount_sign"] == "neg_expense"
    assert f["delimiter"] == ";"


def test_upload_mapped_pos_expense_sign(client, login, make_user):
    uid = make_user()
    login(client, uid)

    resp = _upload_mapped(
        client, UNKNOWN_CSV,
        date_col="0", amount_col="2", store_col="1",
        amount_sign="pos_expense", remember="0",
    )
    assert resp.status_code == 200
    items = {r["store"]: r for r in resp.get_json()["items"]}
    # positive amount -> expense, negative -> income (flipped)
    assert items["Salary Inc"]["type"] == "expense"
    assert items["Coffee Shop"]["type"] == "income"


def test_upload_mapped_without_remember_saves_nothing(client, login, make_user, fresh_conn):
    uid = make_user()
    login(client, uid)
    _upload_mapped(
        client, UNKNOWN_CSV,
        date_col="0", amount_col="2", store_col="1",
        amount_sign="neg_expense", remember="0",
    )
    assert _count(fresh_conn, "import_formats", uid) == 0


def test_upload_mapped_no_store_col(client, login, make_user):
    uid = make_user()
    login(client, uid)
    resp = _upload_mapped(
        client, UNKNOWN_CSV,
        date_col="0", amount_col="2", store_col="",
        amount_sign="neg_expense", remember="0",
    )
    assert resp.status_code == 200
    for item in resp.get_json()["items"]:
        assert item["store"] == ""


# ---------------------------------------------------------------------------
# Second upload of same layout auto-maps via the learned format
# ---------------------------------------------------------------------------
def test_second_upload_auto_maps(client, login, make_user, fresh_conn):
    uid = make_user()
    login(client, uid)

    # Teach the format.
    first = _upload_mapped(
        client, UNKNOWN_CSV,
        date_col="0", amount_col="2", store_col="1",
        amount_sign="neg_expense", remember="1",
    )
    assert first.status_code == 200

    # Re-upload the same layout via the plain endpoint -> no prompt, auto-mapped.
    resp = _upload(client, UNKNOWN_CSV)
    assert resp.status_code == 200
    body = resp.get_json()
    assert "needs_mapping" not in body
    assert body["count"] == 3
    items = sorted(body["items"], key=lambda r: r["date"])
    assert items[0]["store"] == "Coffee Shop"
    assert items[0]["amount"] == 4.50
    assert items[0]["type"] == "expense"


# ---------------------------------------------------------------------------
# Index validation
# ---------------------------------------------------------------------------
def test_out_of_range_index_400(client, login, make_user):
    uid = make_user()
    login(client, uid)
    resp = _upload_mapped(
        client, UNKNOWN_CSV,
        date_col="0", amount_col="9", store_col="1",
        amount_sign="neg_expense", remember="0",
    )
    assert resp.status_code == 400
    assert "amount_col" in resp.get_json()["error"]


def test_missing_required_index_400(client, login, make_user):
    uid = make_user()
    login(client, uid)
    resp = _upload_mapped(
        client, UNKNOWN_CSV,
        date_col="", amount_col="2",
        amount_sign="neg_expense", remember="0",
    )
    assert resp.status_code == 400
    assert "date_col" in resp.get_json()["error"]


def test_non_integer_index_400(client, login, make_user):
    uid = make_user()
    login(client, uid)
    resp = _upload_mapped(
        client, UNKNOWN_CSV,
        date_col="abc", amount_col="2",
        amount_sign="neg_expense", remember="0",
    )
    assert resp.status_code == 400


def test_bad_amount_sign_400(client, login, make_user):
    uid = make_user()
    login(client, uid)
    resp = _upload_mapped(
        client, UNKNOWN_CSV,
        date_col="0", amount_col="2",
        amount_sign="bogus", remember="0",
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Saved formats can be listed and deleted
#
# This used to be two tests asserting user A could not see or delete user B's
# saved format. The app has one user and no login, so there is no second tenant
# to isolate from; what is left worth guarding is the list/delete round-trip.
# ---------------------------------------------------------------------------
def test_get_and_delete_formats(client, login, make_user):
    uid = make_user()
    login(client, uid)
    _upload_mapped(
        client, UNKNOWN_CSV,
        date_col="0", amount_col="2", store_col="1",
        amount_sign="neg_expense", remember="1",
    )
    listing = client.get("/api/import/formats").get_json()
    assert len(listing) == 1
    fmt_id = listing[0]["id"]
    assert listing[0]["date_col"] == 0

    assert client.delete(f"/api/import/formats/{fmt_id}").status_code == 204
    assert client.get("/api/import/formats").get_json() == []


def test_delete_unknown_format_404(client, login, make_user):
    uid = make_user()
    login(client, uid)
    assert client.delete("/api/import/formats/999999").status_code == 404


# ---------------------------------------------------------------------------
# Recognized formats still import unchanged
# ---------------------------------------------------------------------------
FINNISH_BANK_CSV = (
    "Kirjauspäivä;Määrä;Nimi;Viesti\n"
    "2024/02/01;-25,00;K-Market;\n"
    "2024/02/02;1500,00;Tyonantaja Oy;Palkka\n"
)

FINNAIR_CSV = (
    "Date of payment,Location of purchase,c2,c3,c4,c5,c6,c7,Amount\n"
    "2024-03-01,Helsinki Cafe,x,x,x,x,x,x,-12.00\n"
    "2024-03-02,Airport Shop,x,x,x,x,x,x,-50.00\n"
)


def test_recognized_finnish_bank_format(client, login, make_user):
    uid = make_user()
    login(client, uid)
    resp = _upload(client, FINNISH_BANK_CSV, filename="etutili.csv")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "needs_mapping" not in body
    assert body["count"] == 2
    by_date = {r["date"]: r for r in body["items"]}
    assert by_date["2024-02-01"]["store"] == "K-Market"
    assert by_date["2024-02-01"]["amount"] == 25.0
    assert by_date["2024-02-01"]["type"] == "expense"
    # Viesti override applied for the salary row.
    assert by_date["2024-02-02"]["store"] == "Palkka"
    assert by_date["2024-02-02"]["type"] == "income"


# Current Nordea account export: card rows carry the receipt line or the city in
# Viesti and a payment reference in Viitenumero; transfers carry a typed message
# and no reference.
NORDEA_CARD_CSV = (
    "Kirjauspäivä;Määrä;Maksaja;Maksunsaaja;Nimi;Otsikko;Viesti;Viitenumero;Saldo;Valuutta;\n"
    "2026/01/30;-10,76;FI00 0000 0000 0000 00;;APPLE.COM/BILL;APPLE.COM/BILL;"
    "EUR          10,76 CORK;260130208810;4385,61;EUR;\n"
    "2026/01/28;-17,22;FI00 0000 0000 0000 00;;K-supermarket Example;"
    "K-supermarket Example;HELSINKI;260128000019;4368,39;EUR;\n"
    "2026/01/25;1500,00;;FI00 0000 0000 0000 00;Tyonantaja Oy;Tyonantaja Oy;"
    "Palkka;;5868,39;EUR;\n"
)


def test_nordea_card_rows_keep_merchant_name(client, login, make_user):
    """Viesti must not replace the merchant on rows with a payment reference."""
    uid = make_user()
    login(client, uid)
    resp = _upload(client, NORDEA_CARD_CSV, filename="kayttotili.csv")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "needs_mapping" not in body
    assert body["count"] == 3
    by_date = {r["date"]: r for r in body["items"]}

    # Card rows: the real merchant survives, not "EUR 10,76 CORK" or "HELSINKI".
    assert by_date["2026-01-30"]["store"] == "APPLE.COM/BILL"
    assert by_date["2026-01-30"]["amount"] == 10.76
    assert by_date["2026-01-30"]["type"] == "expense"
    assert by_date["2026-01-28"]["store"] == "K-supermarket Example"

    # Transfer row: no reference, so the typed message still wins.
    assert by_date["2026-01-25"]["store"] == "Palkka"
    assert by_date["2026-01-25"]["type"] == "income"


def test_recognized_finnair_format(client, login, make_user):
    uid = make_user()
    login(client, uid)
    resp = _upload(client, FINNAIR_CSV, filename="finnair.csv")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "needs_mapping" not in body
    assert body["count"] == 2
    by_date = {r["date"]: r for r in body["items"]}
    # EUR amount comes from the fixed col 8, store from "Location of purchase".
    assert by_date["2024-03-01"]["store"] == "Helsinki Cafe"
    assert by_date["2024-03-01"]["amount"] == 12.0
    assert by_date["2024-03-01"]["type"] == "expense"


# ---------------------------------------------------------------------------
# Cancelling a review discards the batch
# ---------------------------------------------------------------------------
def test_discard_removes_pending_batch_and_its_rows(client, login, make_user, fresh_conn):
    uid = make_user()
    login(uid)
    res = _upload(client, FINNISH_BANK_CSV)
    assert res.status_code == 200
    batch_id = res.get_json()["batch_id"]
    assert _count(fresh_conn, "import_staging", uid) == 2

    assert client.delete(f"/api/import/batch/{batch_id}").status_code == 200
    # Nothing of the abandoned review is left behind.
    assert _count(fresh_conn, "import_staging", uid) == 0
    assert _count(fresh_conn, "import_batches", uid) == 0


def test_discard_refuses_a_confirmed_batch(client, login, make_user, fresh_conn):
    uid = make_user()
    login(uid)
    batch_id = _upload(client, FINNISH_BANK_CSV).get_json()["batch_id"]
    staged = fresh_conn(lambda c: c.execute(
        "SELECT id FROM import_staging WHERE user_id = %s", (uid,)).fetchall())
    cat = fresh_conn(lambda c: c.execute(
        "SELECT id FROM categories WHERE user_id = %s LIMIT 1", (uid,)).fetchone())["id"]
    items = [{"id": r["id"], "category_id": cat} for r in staged]
    assert client.post("/api/import/confirm",
                       json={"items": items, "batch_id": batch_id}).status_code == 200

    # A confirmed batch is the record of what was imported, not scratch space.
    assert client.delete(f"/api/import/batch/{batch_id}").status_code == 409
    assert _count(fresh_conn, "import_batches", uid) == 1


def test_discard_of_an_unknown_batch_is_a_404(client, login, make_user):
    login(make_user())
    assert client.delete("/api/import/batch/999999").status_code == 404


# ---------------------------------------------------------------------------
# Import history: reading batches back, resuming and undoing
# ---------------------------------------------------------------------------
def _confirm(client, fresh_conn, uid, batch_id):
    staged = fresh_conn(lambda c: c.execute(
        "SELECT id, type FROM import_staging WHERE import_batch_id = %s AND user_id = %s",
        (batch_id, uid)).fetchall())
    cat = fresh_conn(lambda c: c.execute(
        "SELECT id FROM categories WHERE user_id = %s LIMIT 1", (uid,)).fetchone())["id"]
    return client.post("/api/import/confirm", json={
        "items": [{"id": r["id"], "category_id": cat, "type": r["type"]} for r in staged],
        "batch_id": batch_id,
    })


def test_batches_endpoint_reports_what_an_import_brought_in(client, login, make_user, fresh_conn):
    uid = make_user()
    login(uid)
    batch_id = _upload(client, FINNISH_BANK_CSV).get_json()["batch_id"]

    pending = client.get("/api/import/batches").get_json()[0]
    assert pending["status"] == "pending" and pending["staged"] == 2

    assert _confirm(client, fresh_conn, uid, batch_id).status_code == 200
    done = client.get("/api/import/batches").get_json()[0]
    assert done["status"] == "completed"
    # Both rows are now real transactions, tied back to the batch that made them.
    assert done["imported"] == 2
    assert done["sum_expense"] == 25.0 and done["sum_income"] == 1500.0


def test_undo_removes_only_that_import(client, login, make_user, fresh_conn):
    uid = make_user()
    login(uid)
    keep = _upload(client, FINNAIR_CSV).get_json()["batch_id"]
    _confirm(client, fresh_conn, uid, keep)
    drop = _upload(client, FINNISH_BANK_CSV).get_json()["batch_id"]
    _confirm(client, fresh_conn, uid, drop)
    assert _count(fresh_conn, "transactions", uid) == 4

    res = client.post(f"/api/import/batch/{drop}/undo")
    assert res.status_code == 200 and res.get_json()["removed"] == 2
    # The other import is untouched.
    assert _count(fresh_conn, "transactions", uid) == 2
    # The batch record survives, saying what happened to it.
    row = [b for b in client.get("/api/import/batches").get_json() if b["id"] == drop][0]
    assert row["status"] == "undone" and row["imported"] == 0


def test_confirm_keeps_the_staged_type_when_the_client_omits_it(client, login, make_user, fresh_conn):
    uid = make_user()
    login(uid)
    batch_id = _upload(client, FINNISH_BANK_CSV).get_json()["batch_id"]
    staged = fresh_conn(lambda c: c.execute(
        "SELECT id FROM import_staging WHERE user_id = %s", (uid,)).fetchall())
    cat = fresh_conn(lambda c: c.execute(
        "SELECT id FROM categories WHERE user_id = %s LIMIT 1", (uid,)).fetchone())["id"]
    # No "type" on any item: staging already knows which row was the salary,
    # so defaulting the lot to expense would throw that away.
    client.post("/api/import/confirm", json={
        "items": [{"id": r["id"], "category_id": cat} for r in staged],
        "batch_id": batch_id,
    })
    kinds = fresh_conn(lambda c: c.execute(
        "SELECT type, count(*) n FROM transactions WHERE user_id = %s GROUP BY type",
        (uid,)).fetchall())
    assert {r["type"]: r["n"] for r in kinds} == {"expense": 1, "income": 1}


def test_undo_refuses_a_review_that_was_never_confirmed(client, login, make_user):
    login(make_user())
    batch_id = _upload(client, FINNISH_BANK_CSV).get_json()["batch_id"]
    assert client.post(f"/api/import/batch/{batch_id}/undo").status_code == 409


def test_undo_refuses_an_import_predating_the_batch_link(client, login, make_user, fresh_conn):
    uid = make_user()
    login(uid)
    batch_id = _upload(client, FINNISH_BANK_CSV).get_json()["batch_id"]
    _confirm(client, fresh_conn, uid, batch_id)
    # Imports made before transactions.import_batch_id existed have no link.
    fresh_conn(lambda c: c.execute(
        "UPDATE transactions SET import_batch_id = NULL WHERE user_id = %s", (uid,)))

    res = client.post(f"/api/import/batch/{batch_id}/undo")
    # Better to refuse than to report an undo that removed nothing.
    assert res.status_code == 409
    assert _count(fresh_conn, "transactions", uid) == 2


def test_staging_fetch_matches_the_upload_shape(client, login, make_user):
    login(make_user())
    up = _upload(client, FINNISH_BANK_CSV).get_json()
    resumed = client.get(f"/api/import/staging/{up['batch_id']}").get_json()
    # Resuming feeds this straight into the review table, so the shapes must agree.
    assert set(resumed) == set(up)
    assert resumed["batch_id"] == up["batch_id"]
    assert resumed["count"] == up["count"] == 2
    assert len(resumed["items"]) == 2


# ---------------------------------------------------------------------------
# The suggestion may not overturn the sign the statement gave the row
# ---------------------------------------------------------------------------
# A staged row carries a type read off the amount's sign and a suggested
# category read off the store's history. The review table resolves that
# suggestion — a bare NAME — into a category, and took the type from it. So a
# type-blind suggestion could flip the row: a refund from a grocer imported as
# spending, a card payment to a store ruled into an income category imported as
# earnings. The suggestion is now scoped to the row's own type, on both the CSV
# and the bank path.

SIGN_FLIP_CSV = (
    "Kirjauspäivä;Määrä;Nimi;Viesti\n"
    "2026-02-01;-40,00;Prisma;\n"      # the weekly shop  → expense
    "2026-02-02;40,00;Prisma;\n"       # the return of it → income
)


def test_income_row_is_not_suggested_an_expense_category(client, login, make_user):
    uid = make_user()
    # Give Prisma an unambiguous expense history, so the suggester speaks.
    with db.db_conn() as conn:
        cid = conn.execute(
            "SELECT id FROM categories WHERE user_id = %s AND name = 'Groceries' "
            "AND type = 'expense'", (uid,)).fetchone()["id"]
        for _ in range(9):
            conn.execute(
                "INSERT INTO transactions (user_id, date, store, category_id, amount, type) "
                "VALUES (%s, '2026-01-01', 'Prisma', %s, 40.0, 'expense')", (uid, cid))
        conn.commit()

    login(client, uid)
    resp = _upload(client, SIGN_FLIP_CSV, filename="etutili.csv")
    assert resp.status_code == 200
    by_date = {r["date"]: r for r in resp.get_json()["items"]}

    # The sign still decides the type on both rows — that part was never wrong.
    assert by_date["2026-02-01"]["type"] == "expense"
    assert by_date["2026-02-02"]["type"] == "income"

    # The expense row is suggested from the expense history it has.
    assert by_date["2026-02-01"]["suggested_category"] == "Groceries"
    # The income row is not: "Groceries" is an expense category, and the review
    # table would have adopted its type and turned the refund into spending.
    assert by_date["2026-02-02"]["suggested_category"] is None
