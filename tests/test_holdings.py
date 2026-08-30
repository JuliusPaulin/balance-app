"""HTTP tests for the investment import and the holdings drill-down.

`/api/networth/import-investments/preview` + `/confirm` and
`/api/networth/holdings` are the path from a broker export to a number in the
net-worth history. Two things are worth guarding beyond "it wrote a row":

* **The account total is the sum of its holdings**, written into
  `account_balances`, which is what `networth.py` carries forward. A holdings
  write that does not move the balance shows the right drill-down under the
  wrong total.
* **A snapshot is (account_id, as_of)** and re-importing the same day must land
  on the same snapshot. Anything else quietly doubles a portfolio.
"""
import io
from datetime import date

import pytest

from helpers import nordea_xlsx_bytes, nordnet_csv_bytes

STOCKS_FILE = "Osaketaulukko salkkunro 18318444 24.5.2026.csv"
AS_OF = "2026-05-24"

CUSTODY_ROWS = [
    ["Custody", "Nordea salkku 123", "FI0009000681", "EUR", "Nokia",
     100, 1000.0, None, 200.0, 800.0],
]
CASH_ROW = ["CashAccount", "FI21 1234 5600 0007 85", "", "EUR", "",
            None, None, 1500.0, None, None]


# ── helpers ──────────────────────────────────────────────────────────
def _preview(client, files):
    """POST files (list of (filename, bytes)) to the preview endpoint."""
    data = {"files": [(io.BytesIO(raw), name) for name, raw in files]}
    return client.post("/api/networth/import-investments/preview",
                       data=data, content_type="multipart/form-data")


def _confirm(client, accounts):
    return client.post("/api/networth/import-investments/confirm",
                       json={"accounts": accounts})


def _account_payload(**over):
    """One reviewed account, in the shape the review UI posts back."""
    payload = {
        "include": True,
        "as_of": AS_OF,
        "external_id": "csv:nordnet:18318444",
        "name": "Nordnet 18318444",
        "group_name": "Nordnet",
        "kind": "investment",
        "type": "asset",
        "holdings": [
            {"name": "Nokia", "units": 100, "value_eur": 450.50,
             "return_pct": 2.5, "return_eur": 11.0, "currency": "EUR",
             "isin": "FI0009000681"},
            {"name": "Sampo", "units": 12.5, "value_eur": 1234.56,
             "return_pct": -3.1, "return_eur": -39.5, "currency": "EUR"},
        ],
    }
    payload.update(over)
    return payload


def _holdings(client, account_id, as_of=None):
    q = f"?account_id={account_id}" + (f"&as_of={as_of}" if as_of else "")
    res = client.get("/api/networth/holdings" + q)
    assert res.status_code == 200, res.get_json()
    return res.get_json()


def _history(client):
    """The net-worth series keyed by month.

    ``compute_history`` counts back from *today*, and these snapshots are dated,
    so the window is worked out here rather than hardcoded — a fixed ``months=3``
    would pass this year and quietly stop covering the fixture the next.
    """
    today = date.today()
    snapshot = date.fromisoformat(AS_OF)
    months = (today.year - snapshot.year) * 12 + (today.month - snapshot.month) + 1
    res = client.get(f"/api/networth/history?months={max(months, 2)}")
    assert res.status_code == 200, res.get_json()
    return {p["month"]: p for p in res.get_json()["series"]}


def _balance(fresh_conn, account_id, as_of):
    return fresh_conn(lambda c: c.execute(
        "SELECT balance FROM account_balances WHERE account_id = %s AND as_of = %s",
        (account_id, as_of),
    ).fetchone())


@pytest.fixture
def imported(client):
    """One confirmed Nordnet snapshot: returns the created account's row."""
    res = _confirm(client, [_account_payload()])
    assert res.status_code == 200, res.get_json()
    return res.get_json()["accounts"][0]


# ── preview ──────────────────────────────────────────────────────────
def test_preview_returns_the_hierarchy_and_writes_nothing(client):
    res = _preview(client, [(STOCKS_FILE, nordnet_csv_bytes([
        ["Nokia", "100", "450,50", "2,5", "11,00", "EUR"],
    ]))])
    assert res.status_code == 200
    (f,) = res.get_json()["files"]
    assert f["filename"] == STOCKS_FILE
    assert (f["source"], f["as_of"], f["warnings"]) == ("nordnet_stocks", AS_OF, [])
    (acct,) = f["accounts"]
    assert acct["external_id"] == "csv:nordnet:18318444"
    assert acct["total_eur"] == 450.50
    assert acct["holdings"][0]["name"] == "Nokia"
    # A preview is a read. Nothing may exist yet.
    assert client.get("/api/accounts").get_json() == []


def test_preview_takes_several_files_at_once(client):
    res = _preview(client, [
        (STOCKS_FILE, nordnet_csv_bytes([["Nokia", "100", "450,50", "2,5", "11,00", "EUR"]])),
        ("Omistukset.xlsx", nordea_xlsx_bytes([*CUSTODY_ROWS, CASH_ROW])),
    ])
    files = res.get_json()["files"]
    assert [f["source"] for f in files] == ["nordnet_stocks", "nordea_xlsx"]
    assert [a["kind"] for a in files[1]["accounts"]] == ["investment", "cash"]


def test_preview_without_files_is_refused(client):
    res = client.post("/api/networth/import-investments/preview",
                      data={}, content_type="multipart/form-data")
    assert res.status_code == 400
    assert "No files" in res.get_json()["error"]


def test_preview_names_the_file_it_could_not_read(client):
    res = _preview(client, [("portfolio.pdf", b"%PDF-1.4")])
    assert res.status_code == 400
    body = res.get_json()
    assert body["filename"] == "portfolio.pdf"
    assert "portfolio.pdf" in body["error"]


def test_preview_suggests_an_existing_account_by_external_id(client, fresh_conn):
    import config
    acc_id = fresh_conn(lambda c: c.execute(
        "INSERT INTO accounts (user_id, name, type, external_id) "
        "VALUES (%s, 'Nordnet', 'asset', 'csv:nordnet:18318444') RETURNING id",
        (config.LOCAL_USER_ID,),
    ).fetchone()["id"])

    res = _preview(client, [(STOCKS_FILE, nordnet_csv_bytes([
        ["Nokia", "100", "450,50", "2,5", "11,00", "EUR"]]))])
    match = res.get_json()["files"][0]["accounts"][0]["match"]
    assert match == {"existing_account_id": acc_id, "by": "external_id"}


def test_preview_suggests_a_cash_account_by_iban(client, fresh_conn):
    import config
    acc_id = fresh_conn(lambda c: c.execute(
        "INSERT INTO accounts (user_id, name, type, external_id) VALUES "
        "(%s, 'Käyttötili', 'asset', 'bank:FI2112345600000785') RETURNING id",
        (config.LOCAL_USER_ID,),
    ).fetchone()["id"])

    res = _preview(client, [("Omistukset.xlsx", nordea_xlsx_bytes([CASH_ROW]))])
    match = res.get_json()["files"][0]["accounts"][0]["match"]
    assert match == {"existing_account_id": acc_id, "by": "iban"}


def test_preview_suggests_by_name_and_never_merges_on_its_own(client):
    """A name match is a hint for the review screen, not a decision."""
    client.post("/api/accounts", json={"name": "Nordnet", "type": "asset"})
    res = _preview(client, [("export.csv", nordnet_csv_bytes([
        ["Nokia", "100", "450,50", "2,5", "11,00", "EUR"]]))])
    acct = res.get_json()["files"][0]["accounts"][0]
    assert acct["match"]["by"] == "name"
    # Still nothing imported — no balances, no holdings.
    assert client.get("/api/networth/summary").get_json()["assets"] == 0


def test_preview_reports_no_match_when_there_is_none(client):
    res = _preview(client, [(STOCKS_FILE, nordnet_csv_bytes([
        ["Nokia", "100", "450,50", "2,5", "11,00", "EUR"]]))])
    assert res.get_json()["files"][0]["accounts"][0]["match"] == {
        "existing_account_id": None, "by": None}


# ── confirm ──────────────────────────────────────────────────────────
def test_confirm_creates_the_account_holdings_and_balance(client, fresh_conn):
    res = _confirm(client, [_account_payload()])
    assert res.status_code == 200
    body = res.get_json()
    assert (body["updated"], body["as_of"]) == (1, AS_OF)
    (acct,) = body["accounts"]
    assert (acct["matched"], acct["holdings_count"]) == ("created", 2)
    assert acct["total"] == 1685.06        # 450.50 + 1234.56
    assert (acct["name"], acct["group_name"]) == ("Nordnet 18318444", "Nordnet")

    # The total is what net worth reads, so it has to be in account_balances.
    assert _balance(fresh_conn, acct["id"], AS_OF)["balance"] == 1685.06
    assert client.get("/api/networth/summary").get_json()["assets"] == 1685.06


def test_confirm_is_idempotent_on_the_same_day(client):
    """Re-importing the same export must land on the same snapshot — not add a
    second account, and not double the portfolio."""
    first = _confirm(client, [_account_payload()]).get_json()["accounts"][0]
    second = _confirm(client, [_account_payload()]).get_json()["accounts"][0]

    assert second["id"] == first["id"]
    assert second["matched"] == "existing"
    assert second["total"] == first["total"]
    assert len(client.get("/api/accounts").get_json()) == 1
    assert len(_holdings(client, first["id"])["holdings"]) == 2


def test_confirm_merges_two_files_for_one_account_and_date(client):
    """Stocks and funds are separate exports of the same portfolio. Posted
    together they union; the second must not overwrite the first."""
    stocks = _account_payload(holdings=[
        {"name": "Nokia", "value_eur": 450.50, "units": 100}])
    funds = _account_payload(holdings=[
        {"name": "Nordea Suomi Passiivinen", "value_eur": 8010.0, "units": 512.3456}])

    body = _confirm(client, [stocks, funds]).get_json()
    assert body["updated"] == 1
    (acct,) = body["accounts"]
    assert acct["holdings_count"] == 2
    assert acct["total"] == 8460.50
    assert {h["name"] for h in _holdings(client, acct["id"])["holdings"]} == {
        "Nokia", "Nordea Suomi Passiivinen"}


def test_confirm_adopts_an_existing_account_the_user_picked(client, fresh_conn):
    created = client.post("/api/accounts", json={"name": "Osakesalkku", "type": "asset"})
    target = created.get_json()["id"]

    (acct,) = _confirm(client, [
        _account_payload(target_account_id=target)]).get_json()["accounts"]
    assert (acct["id"], acct["matched"]) == (target, "adopted")
    assert acct["name"] == "Osakesalkku"       # the user's name is kept
    # ...and the account picks up the external id, so the next import matches it.
    ext = fresh_conn(lambda c: c.execute(
        "SELECT external_id FROM accounts WHERE id = %s", (target,)).fetchone())
    assert ext["external_id"] == "csv:nordnet:18318444"
    assert len(client.get("/api/accounts").get_json()) == 1


def test_confirm_a_cash_account_uses_its_total_and_holds_nothing(client):
    (acct,) = _confirm(client, [_account_payload(
        kind="cash", external_id="csv:nordea:FI2112345600000785",
        name="Käyttötili", holdings=[], total_eur=1500.0,
    )]).get_json()["accounts"]
    assert (acct["holdings_count"], acct["total"]) == (0, 1500.0)
    assert _holdings(client, acct["id"])["holdings"] == []
    assert client.get("/api/networth/summary").get_json()["assets"] == 1500.0


def test_confirm_writes_a_second_snapshot_without_touching_the_first(client, imported):
    """Two dates = two snapshots. The history is the point of the feature."""
    _confirm(client, [_account_payload(
        as_of="2026-06-24",
        holdings=[{"name": "Nokia", "units": 100, "value_eur": 500.0}],
    )])
    assert _holdings(client, imported["id"], AS_OF)["holdings"][0]["value_eur"] == 1234.56
    later = _holdings(client, imported["id"], "2026-06-24")
    assert [(h["name"], h["value_eur"]) for h in later["holdings"]] == [("Nokia", 500.0)]

    hist = _history(client)
    assert hist["2026-05"]["assets"] == 1685.06
    assert hist["2026-06"]["assets"] == 500.0


def test_confirm_skips_excluded_accounts(client):
    body = _confirm(client, [
        _account_payload(include=False),
        _account_payload(external_id="csv:nordnet:999", name="Nordnet 999"),
    ]).get_json()
    assert body["updated"] == 1
    assert body["accounts"][0]["name"] == "Nordnet 999"


def test_confirm_drops_duplicate_holding_names_within_one_account(client):
    (acct,) = _confirm(client, [_account_payload(holdings=[
        {"name": "Nokia", "value_eur": 450.50},
        {"name": "Nokia", "value_eur": 450.50},
        {"name": "", "value_eur": 10.0},
        {"name": "Broken", "value_eur": "not a number"},
    ])]).get_json()["accounts"]
    assert acct["holdings_count"] == 1
    assert acct["total"] == 450.50


def test_confirm_with_nothing_selected_is_refused(client):
    assert _confirm(client, []).status_code == 400
    res = _confirm(client, [_account_payload(include=False)])
    assert res.status_code == 400
    assert "No accounts selected" in res.get_json()["error"]


@pytest.mark.parametrize("bad", ["", "24.5.2026", "2026-5-24", "not a date"])
def test_confirm_refuses_a_date_it_cannot_trust(client, bad):
    """The snapshot date keys everything. A bad one has to stop the import, not
    create a snapshot nothing can find again."""
    res = _confirm(client, [_account_payload(as_of=bad)])
    assert res.status_code == 400
    assert "as_of must be YYYY-MM-DD" in res.get_json()["error"]
    assert client.get("/api/accounts").get_json() == []


def test_confirm_refuses_an_unknown_target_account(client):
    res = _confirm(client, [_account_payload(target_account_id=9999)])
    assert res.status_code == 400
    assert "9999" in res.get_json()["error"]


def test_preview_then_confirm_round_trip(client):
    """The review screen posts back what preview handed it, plus a date."""
    preview = _preview(client, [(STOCKS_FILE, nordnet_csv_bytes([
        ["Nokia", "100", "450,50", "2,5", "11,00", "EUR"],
        ["Sampo", "12,5", "1 234,56", "-3,1", "-39,50", "EUR"],
    ]))]).get_json()
    f = preview["files"][0]
    accounts = [dict(a, as_of=f["as_of"], name=a["label"], include=True)
                for a in f["accounts"]]

    (acct,) = _confirm(client, accounts).get_json()["accounts"]
    assert acct["total"] == 1685.06
    holdings = _holdings(client, acct["id"])["holdings"]
    assert [h["name"] for h in holdings] == ["Sampo", "Nokia"]   # largest first
    assert holdings[0]["return_pct"] == -3.1


# ── the holdings drill-down ──────────────────────────────────────────
def test_holdings_default_to_the_latest_snapshot(client, imported):
    _confirm(client, [_account_payload(
        as_of="2026-06-24", holdings=[{"name": "Nokia", "value_eur": 500.0}])])
    latest = _holdings(client, imported["id"])
    assert latest["as_of"] == "2026-06-24"
    assert [h["name"] for h in latest["holdings"]] == ["Nokia"]


def test_holdings_are_sorted_largest_first(client, imported):
    rows = _holdings(client, imported["id"])["holdings"]
    assert [h["value_eur"] for h in rows] == [1234.56, 450.50]
    assert rows[0]["units"] == 12.5 and rows[1]["isin"] == "FI0009000681"


def test_holdings_requires_an_account(client):
    res = client.get("/api/networth/holdings")
    assert res.status_code == 400
    assert "account_id is required" in res.get_json()["error"]


def test_holdings_of_an_unknown_account_are_not_found(client):
    assert client.get("/api/networth/holdings?account_id=9999").status_code == 404


def test_holdings_of_an_account_with_none_are_empty_not_an_error(client):
    """A hand-kept account (a savings balance you type in) has no holdings —
    the drill-down opens empty rather than failing."""
    acc = client.post("/api/accounts", json={"name": "Savings", "type": "asset"}).get_json()
    assert _holdings(client, acc["id"]) == {
        "account_id": acc["id"], "as_of": None, "holdings": []}


# ── deleting a holding ───────────────────────────────────────────────
def test_deleting_a_holding_recomputes_the_snapshot_total(client, imported, fresh_conn):
    nokia = next(h for h in _holdings(client, imported["id"])["holdings"]
                 if h["name"] == "Nokia")
    res = client.delete(f"/api/networth/holdings/{nokia['id']}")
    assert res.status_code == 200
    body = res.get_json()
    assert (body["holdings_left"], body["total"]) == (1, 1234.56)
    assert body["as_of"] == AS_OF

    # The account balance follows, or the drill-down and the total disagree.
    assert _balance(fresh_conn, imported["id"], AS_OF)["balance"] == 1234.56
    assert client.get("/api/networth/summary").get_json()["assets"] == 1234.56


def test_deleting_the_last_holding_leaves_a_zero_not_a_gap(client, imported):
    for h in _holdings(client, imported["id"])["holdings"]:
        res = client.delete(f"/api/networth/holdings/{h['id']}")
    assert res.get_json() == {"account_id": imported["id"], "as_of": AS_OF,
                              "holdings_left": 0, "total": 0}
    assert client.get("/api/networth/summary").get_json()["assets"] == 0


def test_deleting_from_an_old_snapshot_leaves_the_latest_alone(client, imported):
    _confirm(client, [_account_payload(
        as_of="2026-06-24",
        holdings=[{"name": "Nokia", "value_eur": 500.0},
                  {"name": "Sampo", "value_eur": 1300.0}])])
    old_nokia = next(h for h in _holdings(client, imported["id"], AS_OF)["holdings"]
                     if h["name"] == "Nokia")
    client.delete(f"/api/networth/holdings/{old_nokia['id']}")

    assert len(_holdings(client, imported["id"], "2026-06-24")["holdings"]) == 2
    hist = _history(client)
    assert hist["2026-05"]["assets"] == 1234.56    # the old month was corrected
    assert hist["2026-06"]["assets"] == 1800.0     # the new one untouched


def test_deleting_a_holding_never_invents_a_balance(client, imported, fresh_conn):
    """A snapshot with no balance row must not gain one on delete — that would
    add a data point to the net-worth history out of a deletion."""
    fresh_conn(lambda c: c.execute(
        "DELETE FROM account_balances WHERE account_id = %s", (imported["id"],)))
    h = _holdings(client, imported["id"])["holdings"][0]
    assert client.delete(f"/api/networth/holdings/{h['id']}").status_code == 200
    assert _balance(fresh_conn, imported["id"], AS_OF) is None


def test_deleting_an_unknown_holding_is_not_found(client):
    res = client.delete("/api/networth/holdings/9999")
    assert res.status_code == 404
    assert "holding not found" in res.get_json()["error"]


def test_deleting_an_account_takes_its_holdings_with_it(client, imported):
    assert client.delete(f"/api/accounts/{imported['id']}").status_code == 204
    assert client.get(f"/api/networth/holdings?account_id={imported['id']}").status_code == 404
