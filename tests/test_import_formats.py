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
# Per-user isolation of saved formats
# ---------------------------------------------------------------------------
def test_saved_format_isolated_per_user(client, login, make_user, fresh_conn):
    user_a = make_user()
    user_b = make_user()

    # User A teaches the format.
    login(client, user_a)
    _upload_mapped(
        client, UNKNOWN_CSV,
        date_col="0", amount_col="2", store_col="1",
        amount_sign="neg_expense", remember="1",
    )

    # User B uploads the same layout -> not learned for B -> still needs mapping.
    login(client, user_b)
    resp = _upload(client, UNKNOWN_CSV)
    assert resp.status_code == 200
    assert resp.get_json().get("needs_mapping") is True
    assert _count(fresh_conn, "import_formats", user_b) == 0


# ---------------------------------------------------------------------------
# GET / DELETE formats are user-scoped
# ---------------------------------------------------------------------------
def test_get_and_delete_formats_user_scoped(client, login, make_user):
    user_a = make_user()
    user_b = make_user()

    login(client, user_a)
    _upload_mapped(
        client, UNKNOWN_CSV,
        date_col="0", amount_col="2", store_col="1",
        amount_sign="neg_expense", remember="1",
    )
    listing = client.get("/api/import/formats").get_json()
    assert len(listing) == 1
    fmt_id = listing[0]["id"]
    assert listing[0]["date_col"] == 0

    # User B can't see or delete A's format.
    login(client, user_b)
    assert client.get("/api/import/formats").get_json() == []
    assert client.delete(f"/api/import/formats/{fmt_id}").status_code == 404

    # Owner deletes successfully.
    login(client, user_a)
    assert client.delete(f"/api/import/formats/{fmt_id}").status_code == 204
    assert client.get("/api/import/formats").get_json() == []


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
