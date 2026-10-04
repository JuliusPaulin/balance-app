"""Focused portfolio reconciliation, settings, flows, and import tests."""
import math

from config import LOCAL_USER_ID


def _account(conn, name="Nordnet", kind="asset"):
    return conn.execute("INSERT INTO accounts (user_id, name, type) VALUES (%s, %s, %s) RETURNING id", (LOCAL_USER_ID, name, kind)).fetchone()["id"]


def test_portfolio_uses_holdings_and_keeps_residual_unknown(client, fresh_conn):
    def seed(conn):
        aid = _account(conn)
        conn.execute("INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s,%s,'2026-10-01',100)", (LOCAL_USER_ID, aid))
        conn.execute("INSERT INTO holdings (account_id, as_of, name, value_eur) VALUES (%s,'2026-10-01','Apple Inc.',60)", (aid,))
    seed_result = fresh_conn(seed)
    body = client.get("/api/portfolio").get_json()
    assert body["total"] == 100
    assert {h["name"] for h in body["holdings"]} == {"Apple Inc.", "Nordnet — unclassified"}


def test_portfolio_excludes_cash_account_by_default(client, fresh_conn):
    def seed(conn):
        aid = _account(conn, "Checking")
        conn.execute("INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s,%s,'2026-10-01',5000)", (LOCAL_USER_ID, aid))
    fresh_conn(seed)
    body = client.get("/api/portfolio").get_json()
    assert body["total"] == 0
    assert body["accounts"][0]["emergency"] is True


def test_portfolio_settings_validate_target_totals(client):
    response = client.put("/api/portfolio/settings", json={"targets": {"mandate": {"World": 0.4}}})
    assert response.status_code == 400


def test_targets_cannot_mix_country_and_fund_mandate_bases(client):
    assert client.put('/api/portfolio/settings', json={'targets': {'country': {'World': 1}}}).status_code == 400
    assert client.put('/api/portfolio/settings', json={'targets': {'region': {'Apple': 1}}}).status_code == 400
    assert client.put('/api/portfolio/settings', json={'targets': {'country': {'United States': .8, 'Unknown': .2}}}).status_code == 200


def test_default_modes_include_holdings_and_exclude_personal_cash_everywhere(client, fresh_conn):
    def seed(conn):
        broker = _account(conn, "Brokerage")
        cash = _account(conn, "Personal cash")
        conn.execute("INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s,%s,'2026-01-01',100)", (LOCAL_USER_ID, broker))
        conn.execute("INSERT INTO holdings (account_id, as_of, name, value_eur) VALUES (%s,'2026-01-01','Apple Inc.',100)", (broker,))
        conn.execute("INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s,%s,'2026-01-01',5000)", (LOCAL_USER_ID, cash))
    fresh_conn(seed)

    body = client.get("/api/portfolio").get_json()
    assert body["total"] == 100
    assert {row["name"] for row in body["holdings"]} == {"Apple Inc."}
    accounts = {row["name"]: row for row in body["accounts"]}
    assert accounts["Brokerage"]["included"] is True
    assert accounts["Personal cash"]["included"] is False
    assert accounts["Personal cash"]["emergency"] is True
    assert all(item["name"] != "Personal cash" for item in body["holdings"])


def test_balance_is_authoritative_when_holdings_differ_only_by_rounding(client, fresh_conn):
    def seed(conn):
        aid = _account(conn, "Brokerage")
        conn.execute("INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s,%s,'2026-01-01',100)", (LOCAL_USER_ID, aid))
        conn.execute("INSERT INTO holdings (account_id, as_of, name, value_eur) VALUES (%s,'2026-01-01','Apple Inc.',40.005)", (aid,))
        conn.execute("INSERT INTO holdings (account_id, as_of, name, value_eur) VALUES (%s,'2026-01-01','Nordnet Suomi Indeksi',60.005)", (aid,))
    fresh_conn(seed)

    body = client.get("/api/portfolio").get_json()
    assert body["total"] == 100
    assert round(sum(row["value"] for row in body["holdings"]), 2) == 100


def test_partial_country_metadata_allocates_residual_to_unknown_and_region(client, fresh_conn):
    def seed(conn):
        aid = _account(conn, "Brokerage")
        conn.execute("INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s,%s,'2026-01-01',100)", (LOCAL_USER_ID, aid))
        conn.execute("INSERT INTO holdings (account_id, as_of, name, value_eur) VALUES (%s,'2026-01-01','Nordea Maailma Enhanced A',100)", (aid,))
    fresh_conn(seed)

    body = client.get("/api/portfolio").get_json()
    countries = {row["name"]: row["value"] for row in body["allocation"]["country"]}
    regions = {row["name"]: row["value"] for row in body["allocation"]["region"]}
    assert countries == {"United States": 24.16, "Unknown": 75.84}
    assert regions == {"North America": 24.16, "Unknown": 75.84}
    assert math.isclose(sum(row["weight"] for row in body["allocation"]["country"]), 1)


def test_closed_account_keeps_historical_value_then_carries_zero_forward(client, fresh_conn):
    def seed(conn):
        aid = _account(conn, "Brokerage")
        conn.execute("INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s,%s,'2026-01-15',100)", (LOCAL_USER_ID, aid))
        conn.execute("INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s,%s,'2026-03-01',0)", (LOCAL_USER_ID, aid))
        conn.execute("INSERT INTO holdings (account_id, as_of, name, value_eur) VALUES (%s,'2026-01-15','Apple Inc.',100)", (aid,))
        conn.execute("UPDATE accounts SET is_archived = 1 WHERE id = %s", (aid,))
    fresh_conn(seed)

    body = client.get("/api/portfolio").get_json()
    history = {row["date"]: row["value"] for row in body["history"]}
    assert history["2026-01-15"] == 100
    assert history["2026-03-01"] == 0


def test_settings_reject_nonfinite_values_and_malformed_bodies(client):
    cases = [
        {"targets": {"mandate": {"World": float("nan"), "US": 0.0}}},
        {"model": {"returns": {"World": float("inf")}}},
        {"targets": []},
        {"flow_complete_from": "2026-02-30"},
    ]
    for payload in cases:
        assert client.put("/api/portfolio/settings", json=payload).status_code == 400
    assert client.put("/api/portfolio/settings", data="[]", content_type="application/json").status_code == 400


def test_preview_targets_unique_holding_key_is_pure_and_repeated_steps_are_stable(client, fresh_conn):
    def seed(conn):
        first = _account(conn, "Brokerage A")
        second = _account(conn, "Brokerage B")
        for aid, value in ((first, 100), (second, 50)):
            conn.execute("INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s,%s,'2026-01-01',%s)", (LOCAL_USER_ID, aid, value))
            conn.execute("INSERT INTO holdings (account_id, as_of, name, value_eur) VALUES (%s,'2026-01-01','Apple Inc.',%s)", (aid, value))
    fresh_conn(seed)

    before = client.get("/api/portfolio").get_json()
    key = next(row["key"] for row in before["holdings"] if row["account_name"] == "Brokerage B")
    first = client.post("/api/portfolio/preview", json={"key": key, "amount": 10, "steps": 3})
    second = client.post("/api/portfolio/preview", json={"key": key, "amount": 10, "steps": 3})
    assert first.status_code == second.status_code == 200
    assert first.get_json() == second.get_json()
    assert first.get_json()["after"]["total"] == 180
    assert client.get("/api/portfolio/flows").get_json() == []
    assert client.get("/api/portfolio").get_json()["total"] == 150
    for amount in (float("nan"), float("inf"), -1):
        assert client.post("/api/portfolio/preview", json={"key": key, "amount": amount}).status_code == 400


def test_manual_flows_require_valid_dates_exclude_emergency_and_enable_zero_gain_mwr(client, fresh_conn):
    def seed(conn):
        aid = _account(conn, "Brokerage")
        cash = _account(conn, "Checking")
        for d, value in (("2026-01-01", 100), ("2026-02-01", 150)):
            conn.execute("INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s,%s,%s,%s)", (LOCAL_USER_ID, aid, d, value))
            conn.execute("INSERT INTO holdings (account_id, as_of, name, value_eur) VALUES (%s,%s,'Apple Inc.',%s)", (aid, d, value))
        conn.execute("INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s,%s,'2026-01-01',1000)", (LOCAL_USER_ID, cash))
    fresh_conn(seed)
    assert client.post("/api/portfolio/flows", json={"account_id": 1, "date": "nope", "amount": 50}).status_code == 400
    # Account IDs are insertion-order stable in this isolated database only through lookup.
    accounts = {row["name"]: row["id"] for row in client.get("/api/portfolio").get_json()["accounts"]}
    assert client.post("/api/portfolio/flows", json={"account_id": accounts["Checking"], "date": "2026-02-01", "amount": 50}).status_code == 400
    assert client.post("/api/portfolio/flows", json={"account_id": accounts["Brokerage"], "date": "2026-02-01", "amount": 50}).status_code == 201
    assert client.put("/api/portfolio/settings", json={"flow_complete_from": "2026-01-01", "flow_complete_to": "2026-02-01"}).status_code == 200
    performance = client.get("/api/portfolio").get_json()["performance"]
    assert performance["available"] is True
    assert performance["start"] == "2026-01-01"
    assert performance["end"] == "2026-02-01"
    assert performance["opening_value"] == 100
    assert performance["closing_value"] == 150
    assert performance["net_flows"] == 50
    assert performance["gains"] == 0
    assert abs(performance["mwr"]) < 1e-8


def test_performance_is_unavailable_without_exact_boundary_valuations(client, fresh_conn):
    def seed(conn):
        aid = _account(conn, "Brokerage")
        conn.execute("INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s,%s,'2026-01-01',100)", (LOCAL_USER_ID, aid))
        conn.execute("INSERT INTO holdings (account_id, as_of, name, value_eur) VALUES (%s,'2026-01-01','Apple Inc.',100)", (aid,))
    fresh_conn(seed)
    assert client.put("/api/portfolio/settings", json={"flow_complete_from": "2026-01-01", "flow_complete_to": "2026-02-01"}).status_code == 200
    performance = client.get("/api/portfolio").get_json()["performance"]
    assert performance["available"] is False
    assert "actual valuation" in performance["reason"]


def test_start_date_flow_is_excluded_from_end_of_day_return(client, fresh_conn):
    def seed(conn):
        aid = _account(conn, "Brokerage")
        for d in ("2026-01-01", "2026-02-01"):
            conn.execute("INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s,%s,%s,100)", (LOCAL_USER_ID, aid, d))
            conn.execute("INSERT INTO holdings (account_id, as_of, name, value_eur) VALUES (%s,%s,'Apple Inc.',100)", (aid, d))
    fresh_conn(seed)
    aid = client.get("/api/portfolio").get_json()["accounts"][0]["id"]
    assert client.post("/api/portfolio/flows", json={"account_id": aid, "date": "2026-01-01", "amount": 50}).status_code == 201
    assert client.put("/api/portfolio/settings", json={"flow_complete_from": "2026-01-01", "flow_complete_to": "2026-02-01"}).status_code == 200
    performance = client.get("/api/portfolio").get_json()["performance"]
    assert performance["available"] is True
    assert performance["net_flows"] == 0
    assert performance["gains"] == 0


def test_manual_flows_reject_accounts_excluded_from_portfolio(client, fresh_conn):
    def seed(conn):
        aid = _account(conn, "Miscellaneous asset")
        conn.execute("INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s,%s,'2026-01-01',1000)", (LOCAL_USER_ID, aid))
    fresh_conn(seed)
    aid = client.get("/api/portfolio").get_json()["accounts"][0]["id"]
    response = client.post("/api/portfolio/flows", json={"account_id": aid, "date": "2026-01-02", "amount": 50})
    assert response.status_code == 400


def test_investment_import_keeps_future_snapshot_when_older_snapshot_is_added(client):
    def account(as_of, value):
        return {"include": True, "as_of": as_of, "external_id": "csv:test:future", "name": "Future broker", "kind": "investment", "type": "asset", "holdings": [{"name": "Apple Inc.", "value_eur": value, "units": 1, "currency": "EUR"}]}

    assert client.post("/api/networth/import-investments/confirm", json={"accounts": [account("2026-10-01", 200)]}).status_code == 200
    assert client.post("/api/networth/import-investments/confirm", json={"accounts": [account("2026-01-01", 100)]}).status_code == 200
    rows = client.get("/api/portfolio").get_json()["history"]
    values = {row["date"]: row["value"] for row in rows}
    assert values["2026-01-01"] == 100
    assert values["2026-10-01"] == 200


def test_partial_country_coverage_reports_only_verified_fraction(client, fresh_conn):
    def seed(conn):
        aid = _account(conn, "Brokerage")
        conn.execute("INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s,%s,'2026-01-01',100)", (LOCAL_USER_ID, aid))
        conn.execute("INSERT INTO holdings (account_id, as_of, name, value_eur) VALUES (%s,'2026-01-01','Nordea Maailma Enhanced A',100)", (aid,))
    fresh_conn(seed)
    body = client.get("/api/portfolio").get_json()
    assert body["coverage"]["country"] == 0.2416


def test_duplicate_instrument_names_aggregate_concentration(client, fresh_conn):
    def seed(conn):
        for name in ("Brokerage A", "Brokerage B"):
            aid = _account(conn, name)
            conn.execute("INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s,%s,'2026-01-01',50)", (LOCAL_USER_ID, aid))
            conn.execute("INSERT INTO holdings (account_id, as_of, name, value_eur) VALUES (%s,'2026-01-01','Apple Inc.',50)", (aid,))
    fresh_conn(seed)
    concentration = client.get("/api/portfolio").get_json()["concentration"]
    assert concentration["largest"] == 1
    assert concentration["holding_hhi"] == 1
    assert concentration["effective_holdings"] == 1


def test_preview_rejects_unclassified_destination(client, fresh_conn):
    def seed(conn):
        aid = _account(conn, "Brokerage")
        conn.execute("INSERT INTO account_balances (user_id, account_id, as_of, balance) VALUES (%s,%s,'2026-01-01',100)", (LOCAL_USER_ID, aid))
        conn.execute("INSERT INTO holdings (account_id, as_of, name, value_eur) VALUES (%s,'2026-01-01','Apple Inc.',60)", (aid,))
    fresh_conn(seed)
    holding = next(row for row in client.get("/api/portfolio").get_json()["holdings"] if not row["classified"])
    response = client.post("/api/portfolio/preview", json={"key": holding["key"], "amount": 10})
    assert response.status_code == 400


def test_settings_roundtrip_and_model_caps_validation(client):
    targets = {"mandate": {"World": 1.0}}
    assert client.put("/api/portfolio/settings", json={"targets": targets}).status_code == 200
    saved = client.get("/api/portfolio").get_json()["settings"]
    assert saved["targets"] == targets
    response = client.put("/api/portfolio/settings", json={"model": {"max_weights": {group: 0.1 for group in ("World", "US", "Europe", "EM", "Finland", "Norway", "Sweden", "Apple", "Bonds")}}})
    assert response.status_code == 400


def test_risk_and_model_use_server_market_cache(monkeypatch, client, fresh_conn):
    import routes.portfolio as route
    market = {"returns": {"World": {f"2024-{m:02d}": 0.01 for m in range(1, 13)} | {f"2025-{m:02d}": 0.01 for m in range(1, 13)}}}
    monkeypatch.setattr(route, "load_market", lambda conn: market)
    seen = {}
    monkeypatch.setattr(route, "analyze_risk", lambda weights, supplied, risk_free, months: seen.update({"market": supplied}) or {"available": False, "coverage": 0})
    body = client.get("/api/portfolio", query_string={"market": "client-value"})
    assert body.status_code == 200
    assert seen["market"] is market
    monkeypatch.setattr(route, "frontier", lambda weights, supplied, model: seen.update({"frontier_market": supplied}) or {"available": False, "points": []})
    response = client.post("/api/portfolio/model", json={"market": {"World": "client-value"}})
    assert response.status_code == 200
    assert seen["frontier_market"] is market


def test_negative_investment_balance_disables_allocation_and_risk(client,fresh_conn):
    def seed(conn):
        a=_account(conn,'Fund');b=_account(conn,'Personnel fund')
        for aid,value in ((a,100),(b,-50)):
            conn.execute("INSERT INTO account_balances (user_id,account_id,as_of,balance) VALUES (%s,%s,'2026-01-01',%s)",(LOCAL_USER_ID,aid,value))
        conn.execute("INSERT INTO holdings (account_id,as_of,name,value_eur) VALUES (%s,'2026-01-01','Apple Inc.',100)",(a,))
    fresh_conn(seed)
    data=client.get('/api/portfolio').get_json()
    assert data['invalid_reason']
    assert all(not v for v in data['allocation'].values())
    assert not data['risk']['available']
    assert not client.get('/api/portfolio/model').get_json()['available']
    assert client.post('/api/portfolio/preview',json={'key':data['holdings'][0]['key'],'amount':100}).status_code==400


def test_editing_confirmed_period_flow_requires_reconfirmation(client,fresh_conn):
    def seed(conn):
        aid=_account(conn,'Fund')
        for d,value in (('2026-01-01',100),('2026-02-01',150)):
            conn.execute('INSERT INTO account_balances (user_id,account_id,as_of,balance) VALUES (%s,%s,%s,%s)',(LOCAL_USER_ID,aid,d,value))
        return aid
    aid=fresh_conn(seed)
    flow=client.post('/api/portfolio/flows',json={'account_id':aid,'date':'2026-01-15','amount':50}).get_json()
    period={'flow_complete_from':'2026-01-01','flow_complete_to':'2026-02-01'}
    client.put('/api/portfolio/settings',json=period)
    assert client.get('/api/portfolio').get_json()['performance']['available']
    client.delete('/api/portfolio/flows/'+str(flow['id']))
    assert not client.get('/api/portfolio').get_json()['performance']['available']
    client.put('/api/portfolio/settings',json=period)
    response=client.post('/api/portfolio/flows',json={'account_id':aid,'date':'2026-01-15','amount':50})
    assert response.get_json()['completeness_reset']
    assert not client.get('/api/portfolio').get_json()['performance']['available']
