"""Revolut rows, dated conversion, saved defaults and atomic failures."""

import csv
import io
from decimal import Decimal
from unittest.mock import Mock

import pytest
import requests

from data import db
from helpers import cat_id
from services import exchange_rates as fx


HEADER = "Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\n"


def statement(*rows):
    out = io.StringIO()
    out.write(HEADER)
    writer = csv.writer(out)
    for row in rows:
        values = {
            "kind": "Card Payment", "day": "2024-09-13", "completed": "2024-09-16 08:00:00",
            "store": "Coop", "amount": "-100.00", "fee": "0.00", "currency": "SEK",
            "state": "COMPLETED",
        } | row
        writer.writerow([values["kind"], "Current", values["day"] + " 16:45:33",
                         values["completed"], values["store"], values["amount"],
                         values["fee"], values["currency"], values["state"], "900.00"])
    return out.getvalue()


def upload(client, text, **fields):
    return client.post("/api/import/upload", data={
        "file": (io.BytesIO(text.encode()), "statement.csv"), **fields,
    })


@pytest.fixture
def rates(monkeypatch):
    def response(url, params, timeout):
        base = url.split("/")[-2].upper()
        day = params["date"]
        return Mock(json=lambda: {
            "date": "2024-09-13" if day in ("2024-09-14", "2024-09-15") else day,
            "base": base, "quote": "EUR", "rate": 0.09 if base == "SEK" else 0.90,
        })
    getter = Mock(side_effect=response)
    monkeypatch.setattr(fx.requests, "get", getter)
    return getter


def test_revolut_dates_states_and_exchange_rows(client, rates):
    response = upload(client, statement(
        {"day": "2024-09-11", "amount": "-469.00", "store": "Bolt"},
        {"day": "2024-09-14", "amount": "-44.95"},
        {"kind": "Exchange", "amount": "11218.41"},
        {"state": "PENDING", "completed": ""},
        {"state": "REVERTED", "completed": ""},
    ))
    assert response.status_code == 200, response.json
    body = response.json
    assert body["count"] == 2
    by_store = {r["store"]: r for r in body["items"]}
    assert by_store["Bolt"]["date"] == "2024-09-11"
    assert by_store["Bolt"]["amount"] == 42.21
    assert by_store["Coop"]["amount"] == 4.05
    assert by_store["Coop"]["import_fx"]["rate_date"] == "2024-09-13"
    assert all(r["type"] == "expense" for r in body["items"])
    assert all(r["suggested_category"] is None for r in body["items"])
    assert body["summary"] == {"converted": 2, "revolut": True,
                                "skipped_states": 2, "skipped_exchanges": 1}
    assert rates.call_count == 2
    # No merchant, amount or full file goes to the rate provider.
    assert rates.call_args.kwargs == {"params": {"date": "2024-09-14"}, "timeout": (5, 15)}


def test_mixed_currencies_refund_and_fee(client, rates):
    body = upload(client, statement(
        {"store": "Refund", "amount": "100", "fee": "2"},
        {"store": "Cash", "amount": "-100", "fee": "2"},
        {"store": "Dollars", "currency": "USD"},
        {"store": "Euros", "currency": "EUR"},
    )).json
    rows = {r["store"]: r for r in body["items"]}
    assert (rows["Refund"]["amount"], rows["Refund"]["type"]) == (8.82, "income")
    assert rows["Cash"]["amount"] == 9.18
    assert rows["Cash"]["import_fx"]["fee"] == "2"
    assert rows["Dollars"]["amount"] == 90
    assert rows["Euros"]["amount"] == 100
    assert rows["Euros"]["import_fx"] is None
    assert rates.call_count == 2


def test_cached_rate_resume_confirm_and_undo(client, rates):
    source = statement({})
    body = upload(client, source).json
    first_fx = body["items"][0]["import_fx"]
    rates.side_effect = requests.ConnectionError("offline")
    assert upload(client, source).status_code == 200
    assert rates.call_count == 1
    resumed = client.get(f'/api/import/staging/{body["batch_id"]}').json
    assert resumed["items"][0]["import_fx"] == first_fx
    assert resumed["summary"] == body["summary"]
    item = body["items"][0]
    result = client.post("/api/import/confirm", json={"batch_id": body["batch_id"], "items": [{
        "id": item["id"], "category_id": cat_id(client, "Groceries"), "amount": 4.5,
        "import_fx": {"rate": "999"},
    }]})
    assert result.status_code == 200
    transactions = client.get("/api/transactions").json
    assert transactions["sum_expense"] == 4.5
    assert db.load_json(transactions["items"][0]["import_fx"]) == first_fx
    result = client.post(f'/api/import/batch/{body["batch_id"]}/undo')
    assert result.status_code == 200
    assert client.get("/api/transactions").json["total"] == 0


def test_default_currency_and_explicit_column(client, rates):
    assert client.get("/api/import/preferences").json["default_currency"] == "EUR"
    assert client.put("/api/import/preferences", json={"default_currency": "sek"}).status_code == 200
    assert client.get("/api/import/preferences").json["default_currency"] == "SEK"


    text = "Date,Description,Amount\n2024-09-13,Shop,-100\n"
    assert upload(client, text).json["items"][0]["amount"] == 9
    assert upload(client, text, default_currency="EUR").json["items"][0]["amount"] == 100
    explicit = "Date,Description,Amount,Currency\n2024-09-13,Shop,-100,EUR\n"
    assert upload(client, explicit).json["items"][0]["amount"] == 100
    assert client.put("/api/import/preferences", json={"default_currency": "bad/code"}).status_code == 400
    assert client.get("/api/import/preferences").json["default_currency"] == "SEK"


def test_invalid_preferences_are_refused(client):
    assert client.put("/api/import/preferences", json=["SEK"]).status_code == 400
    assert client.put("/api/import/preferences", json={}).status_code == 400


def test_mapped_and_learned_currency(client, rates):
    source = "When,Who,How much,Currency\n2024-09-13,Shop,-100,SEK\n"
    response = client.post("/api/import/upload-mapped", data={
        "file": (io.BytesIO(source.encode()), "custom.csv"),
        "date_col": "0", "store_col": "1", "amount_col": "2", "remember": "1",
    })
    assert response.status_code == 200
    assert response.json["items"][0]["amount"] == 9
    assert upload(client, source).json["items"][0]["amount"] == 9


def test_learned_mapping_keeps_omitted_store_omitted(client, rates):
    source = "When,Description,How much,Currency\n2024-09-13,Ignore this,-100,SEK\n"
    response = client.post("/api/import/upload-mapped", data={
        "file": (io.BytesIO(source.encode()), "custom.csv"),
        "date_col": "0", "amount_col": "2", "remember": "1", "store_col": "",
    })
    assert response.json["items"][0]["store"] == ""
    assert upload(client, source).json["items"][0]["store"] == ""


def test_finnair_eur_column_never_converts(client, rates):
    client.put("/api/import/preferences", json={"default_currency": "SEK"})
    source = ('Date of payment,Location of purchase,Amount,Currency,A,B,C,D,EUR amount\n'
              '2024-09-13,Shop,-100,SEK,,,,,-9\n')
    response = upload(client, source)
    assert response.status_code == 200
    assert response.json["items"][0]["amount"] == 9
    rates.assert_not_called()


@pytest.mark.parametrize("change", [
    {"currency": ""}, {"amount": "NaN"}, {"fee": "-1"},
    {"day": "2024-99-11"}, {"currency": "SE/K"},
])
def test_invalid_revolut_row_rolls_back_whole_batch(client, rates, change):
    response = upload(client, statement({}, change))
    assert response.status_code == 400, response.json
    with db.db_conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM import_staging").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM import_batches").fetchone()["n"] == 0


@pytest.mark.parametrize("payload", [
    {"date": "2024-09-16", "base": "SEK", "quote": "EUR", "rate": 0.09},
    {"date": "2024-08-01", "base": "SEK", "quote": "EUR", "rate": 0.09},
    {"date": "2024-09-13", "base": "EUR", "quote": "SEK", "rate": 11},
    {"date": "2024-09-13", "base": "SEK", "quote": "EUR", "rate": 0},
    {"date": "2024-09-13", "base": "SEK", "quote": "EUR", "rate": "NaN"},
    {}, [],
])
def test_bad_rate_never_becomes_money(client, rates, payload):
    rates.side_effect = None
    rates.return_value = Mock(json=lambda: payload)
    response = upload(client, statement({"currency": "EUR"}, {}))
    assert response.status_code == 400
    assert "Could not get an ECB" in response.json["error"]
    with db.db_conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM import_staging").fetchone()["n"] == 0


def test_rate_timeout_leaves_nothing_imported(client, rates):
    rates.side_effect = requests.Timeout()
    response = upload(client, statement({}))
    assert response.status_code == 400
    assert "No transactions were imported" in response.json["error"]
    assert client.get("/api/transactions").json["total"] == 0


def test_decimal_rounding_and_no_network_for_eur(user_conn, rates):
    conn, uid = user_conn
    value, _ = fx.convert(conn, uid, Decimal("1.005"), "EUR", "2024-09-13", {})
    assert value == 1.01
    rates.assert_not_called()


def test_additive_schema_preserves_existing_amounts(client):
    from data.schema import init_db
    with db.db_conn() as conn:
        conn.execute("INSERT INTO transactions (user_id, date, store, category_id, amount, type) "
                     "VALUES (1, '2024-09-13', 'Existing', %s, 123.45, 'expense')",
                     (cat_id(client, "Groceries"),))
        conn.execute("ALTER TABLE transactions DROP COLUMN import_fx")
        conn.execute("ALTER TABLE import_staging DROP COLUMN import_fx")
        conn.execute("ALTER TABLE import_batches DROP COLUMN import_summary")
    init_db()
    init_db()
    assert client.get("/api/transactions").json["sum_expense"] == 123.45
