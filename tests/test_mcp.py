"""The local MCP server, against the throwaway database.

Synthetic CSV only. The real expenses file is never opened: conftest points
SQLITE_PATH at a temp file before anything imports config.
"""

import asyncio
import inspect
from datetime import date

import pytest

import config
from balance_mcp.errors import BalanceMcpError
from balance_mcp import service
from data import db


FINNISH = (
    "Kirjauspäivä;Määrä;Nimi;Viesti\n"
    "2026/03/01;-12,40;K-Market;\n"
    "2026/03/02;2500,00;Employer Oy;Palkka\n"
)

NORDEA = (
    "Kirjauspäivä;Määrä;Maksaja;Maksunsaaja;Nimi;Otsikko;Viesti;Viitenumero;Saldo;Valuutta;\n"
    "2026/03/01;-12,40;;;K-Market Kamppi;K-Market Kamppi;HELSINKI;1001;100,00;EUR;\n"
    "2026/03/02;2500,00;;;Employer Oy;Employer Oy;Palkka;;3500,00;EUR;\n"
)

UNKNOWN = (
    "Col1;Col2;Col3\n"
    "2024-01-15;Coffee Shop;-4,50\n"
    "2024-01-16;Salary Inc;2500,00\n"
)

READ_ONLY = {
    "list_accounts", "net_worth", "search_transactions", "spending_by_category",
    "cash_flow", "list_categories", "list_category_rules", "preview_bank_import",
    "preview_recategorise", "suggest_categories", "list_audit_log",
}
TOOLS = READ_ONLY | {
    "commit_bank_import", "set_transaction_category", "create_category",
    "rename_category", "create_category_rule", "apply_recategorise",
    "apply_category_suggestions", "undo_change",
}


def _cid(name, type_="expense"):
    with db.db_conn() as conn:
        return conn.execute(
            "SELECT id FROM categories WHERE user_id = %s AND name = %s AND type = %s",
            (config.LOCAL_USER_ID, name, type_),
        ).fetchone()["id"]


def _add(date_, store, category, amount, type_="expense"):
    cid = category if isinstance(category, int) else _cid(category, type_)
    with db.db_conn() as conn:
        return conn.execute(
            "INSERT INTO transactions (user_id, date, store, category_id, amount, type) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (config.LOCAL_USER_ID, date_, store, cid, amount, type_),
        ).fetchone()["id"]


def _count(table):
    with db.db_conn() as conn:
        return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def _write(tmp_path, text, name="statement.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _pairs(rows):
    return {(row["date"], row["store"], round(float(row["amount"]), 2), row["type"]) for row in rows}


def _add_account(name, type_, balance, as_of):
    with db.db_conn() as conn:
        account_id = conn.execute(
            "INSERT INTO accounts (user_id, name, type) VALUES (%s, %s, %s) RETURNING id",
            (config.LOCAL_USER_ID, name, type_),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO account_balances (user_id, account_id, as_of, balance) "
            "VALUES (%s, %s, %s, %s)",
            (config.LOCAL_USER_ID, account_id, as_of, balance),
        )
        return account_id


def test_empty_reads_are_empty():
    assert service.list_accounts() == {"accounts": []}
    worth = service.net_worth()
    assert worth["net_worth"] == 0
    assert worth["assets"] == 0
    assert worth["liabilities"] == 0
    assert service.search_transactions()["total"] == 0
    spending = service.spending_by_category()
    assert spending["items"] == []
    assert spending["total"] == 0
    flow = service.cash_flow()
    assert flow["months"] == []
    assert flow["net"] == 0
    assert service.list_category_rules() == {"rules": []}
    assert service.list_audit_log() == {"entries": []}
    names = {row["name"] for row in service.list_categories()["categories"]}
    assert "Groceries" in names
    assert "Job" in names


def test_accounts_balances_and_net_worth():
    today = date.today().isoformat()
    checking = _add_account("Checking", "asset", 1000, today)
    _add_account("Loan", "liability", 400, today)
    listed = {row["name"]: row for row in service.list_accounts()["accounts"]}
    assert listed["Checking"]["balance"] == 1000
    assert listed["Checking"]["type"] == "asset"
    assert listed["Loan"]["balance"] == 400
    one = service.list_accounts(checking)
    assert one["accounts"][0]["balances"] == [{"as_of": today, "balance": 1000}]
    worth = service.net_worth(months=2)
    assert worth["assets"] == 1000
    assert worth["liabilities"] == 400
    assert worth["net_worth"] == 600
    assert worth["history"][-1]["net_worth"] == 600


def test_search_filters_merchant_separately_from_category():
    _add("2026-03-01", "Lunch place", "Groceries", 12.5)
    _add("2026-03-02", "Employer Oy", "Job", 2500, "income")
    _add("2026-04-01", "K-Market", "Groceries", 40)
    assert service.search_transactions(merchant="Groceries")["total"] == 0
    lunch = service.search_transactions(merchant="lunch")
    assert lunch["total"] == 1
    assert lunch["items"][0]["store"] == "Lunch place"
    assert lunch["sum_expense"] == 12.5
    groceries = service.search_transactions(category="Groceries", category_type="expense")
    assert groceries["total"] == 2
    march = service.search_transactions(month="2026-03", txn_type="expense")
    assert march["total"] == 1
    ranged = service.search_transactions(date_from="2026-03-02", date_to="2026-04-01")
    assert ranged["total"] == 2
    assert service.search_transactions(amount_min=30, txn_type="expense")["total"] == 1
    with pytest.raises(BalanceMcpError, match="both"):
        service.search_transactions(category="Other")


def test_spending_and_cash_flow_split_investments():
    _add("2026-01-15", "Old shop", "Groceries", 5)
    _add("2026-03-02", "K-Market", "Groceries", 10)
    _add("2026-03-03", "Fund", "Investments", 40)
    _add("2026-03-04", "Employer Oy", "Job", 100, "income")
    latest = service.spending_by_category()
    assert latest["month"] == "2026-03"
    assert latest["total"] == 50
    march = service.spending_by_category(month="2026-03")
    assert {item["category"]: item["total"] for item in march["items"]} == {
        "Groceries": 10, "Investments": 40,
    }
    year = service.spending_by_category(year="2026")
    assert year["total"] == 55
    income = service.spending_by_category(txn_type="income", month="2026-03")
    assert income["total"] == 100
    flow = service.cash_flow(date_from="2026-03-01", date_to="2026-03-31")
    assert len(flow["months"]) == 1
    month = flow["months"][0]
    assert month["income"] == 100
    assert month["expense"] == 50
    assert month["investments"] == 40
    assert month["net"] == 50
    assert flow["investments"] == 40


def test_connection_is_wal_with_a_busy_timeout():
    with db.db_conn() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()
    assert list(mode.values()) == ["wal"]
    assert list(timeout.values()) == [5000]


def test_init_db_adds_the_audit_table_without_touching_rows():
    transaction_id = _add("2026-03-01", "K-Market", "Groceries", 10)
    with db.db_conn() as conn:
        conn.execute("DROP TABLE mcp_audit")
    from data.schema import init_db
    init_db()
    with db.db_conn() as conn:
        still = conn.execute(
            "SELECT id FROM transactions WHERE id = %s", (transaction_id,)
        ).fetchone()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(mcp_audit)")}
    assert still["id"] == transaction_id
    assert {"action", "undo_kind", "undo_payload", "undone"} <= columns


def test_preview_matches_the_app_parser(tmp_path, client, make_user):
    make_user()
    finnish = _write(tmp_path, FINNISH, "etutili.csv")
    nordea = _write(tmp_path, NORDEA, "nordea.csv")
    for path, filename in ((finnish, "etutili.csv"), (nordea, "nordea.csv")):
        with open(path, "rb") as handle:
            response = client.post(
                "/api/import/upload",
                data={"file": (handle, filename)},
                content_type="multipart/form-data",
            )
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        preview = service.preview_bank_import(path)
        assert preview["needs_mapping"] is False
        assert preview["dry_run"] is True
        assert _pairs(preview["samples"]) == _pairs(body["items"])
    nordea_preview = service.preview_bank_import(nordea)
    by_date = {row["date"]: row for row in nordea_preview["samples"]}
    assert by_date["2026-03-01"]["store"] == "K-Market Kamppi"
    assert by_date["2026-03-01"]["type"] == "expense"
    assert by_date["2026-03-02"]["store"] == "Palkka"
    assert by_date["2026-03-02"]["type"] == "income"


def test_preview_writes_nothing(tmp_path):
    path = _write(tmp_path, FINNISH)
    before = (_count("transactions"), _count("import_batches"), _count("mcp_audit"))
    preview = service.preview_bank_import(path)
    assert preview["to_import"] == 2
    assert preview["duplicates"] == 0
    assert (_count("transactions"), _count("import_batches"), _count("mcp_audit")) == before


def test_commit_skips_duplicates_and_undoes(tmp_path):
    _add("2026-03-01", "K-Market", "Groceries", 12.40)
    path = _write(tmp_path, FINNISH + "2026/03/01;-12,40;K-Market;\n")
    preview = service.preview_bank_import(path)
    assert preview["parsed"] == 3
    assert preview["duplicates"] == 2
    assert preview["to_import"] == 1
    assert preview["samples"][0]["store"] == "Palkka"
    with pytest.raises(BalanceMcpError, match="would import 1"):
        service.commit_bank_import(path, expected_new=2)
    assert _count("transactions") == 1
    assert _count("mcp_audit") == 0

    committed = service.commit_bank_import(path, expected_new=1)
    assert committed["committed"] == 1
    assert committed["skipped_duplicates"] == 2
    assert service.search_transactions(merchant="Palkka")["total"] == 1
    assert _count("transactions") == 2

    undone = service.undo_change(committed["audit_id"])
    assert undone["result"]["removed"] == 1
    assert service.search_transactions()["total"] == 1
    with db.db_conn() as conn:
        status = conn.execute(
            "SELECT status FROM import_batches WHERE id = %s", (committed["batch_id"],)
        ).fetchone()["status"]
    assert status == "undone"
    with pytest.raises(BalanceMcpError, match="already undone"):
        service.undo_change(committed["audit_id"])


def test_include_duplicates_imports_the_second_copy(tmp_path):
    text = (
        "Kirjauspäivä;Määrä;Nimi;Viesti\n"
        "2026/03/01;-8,00;Cafe;\n"
        "2026/03/01;-8,00;Cafe;\n"
    )
    path = _write(tmp_path, text)
    skipped = service.preview_bank_import(path)
    assert skipped["to_import"] == 1
    assert skipped["duplicates"] == 1
    kept = service.preview_bank_import(path, include_duplicates=True)
    assert kept["to_import"] == 2
    service.commit_bank_import(path, expected_new=2, include_duplicates=True)
    assert service.search_transactions(merchant="Cafe")["total"] == 2


def test_unknown_layout_needs_a_mapping_then_imports(tmp_path):
    path = _write(tmp_path, UNKNOWN)
    preview = service.preview_bank_import(path)
    assert preview["needs_mapping"] is True
    assert preview["headers"] == ["Col1", "Col2", "Col3"]
    assert len(preview["sample_rows"]) == 2
    assert _count("import_batches") == 0
    with pytest.raises(BalanceMcpError, match="column mapping"):
        service.commit_bank_import(path, expected_new=2)
    committed = service.commit_bank_import(
        path, expected_new=2, date_col=0, amount_col=2, store_col=1, remember_format=True,
    )
    assert committed["committed"] == 2
    assert committed["format_saved"] is True
    rows = {row["store"]: row for row in service.search_transactions()["items"]}
    assert rows["Coffee Shop"]["type"] == "expense"
    assert rows["Coffee Shop"]["amount"] == 4.5
    assert rows["Salary Inc"]["type"] == "income"
    service.undo_change(committed["audit_id"])
    assert _count("transactions") == 0
    assert _count("import_formats") == 0


def test_positive_amounts_can_be_expenses(tmp_path):
    path = _write(tmp_path, "When;Who;Amt\n15.1.2024;Cafe;4,50\n")
    preview = service.preview_bank_import(
        path, date_col=0, amount_col=2, store_col=1, amount_sign="pos_expense",
    )
    assert preview["samples"][0]["type"] == "expense"
    assert preview["samples"][0]["amount"] == 4.5


def test_commit_does_not_wipe_hand_written_rules(tmp_path):
    created = service.create_category_rule(
        "unrelated shop", "exact", category="Groceries", category_type="expense",
    )
    path = _write(tmp_path, FINNISH)
    preview = service.preview_bank_import(path)
    service.commit_bank_import(path, expected_new=preview["to_import"])
    rules = service.list_category_rules()["rules"]
    assert [rule["id"] for rule in rules] == [created["id"]]


def test_uncached_foreign_currency_does_not_call_out(tmp_path, monkeypatch):
    calls = {"n": 0}

    def boom(*_args, **_kwargs):
        calls["n"] += 1
        raise AssertionError("the MCP server fetched a rate")

    monkeypatch.setattr("services.exchange_rates.requests.get", boom)
    path = _write(tmp_path, "Kirjauspäivä;Määrä;Nimi;Valuutta\n2024/06/01;-100,00;Cafe;SEK\n")
    with pytest.raises(BalanceMcpError, match="does not fetch"):
        service.preview_bank_import(path)
    assert calls["n"] == 0
    assert _count("transactions") == 0
    assert _count("import_batches") == 0


def test_saved_rate_converts_without_a_request(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("fetched")

    monkeypatch.setattr("services.exchange_rates.requests.get", boom)
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO import_exchange_rates "
            "(user_id, currency, requested_date, rate_date, rate) "
            "VALUES (%s, 'SEK', '2024-06-01', '2024-06-01', '0.1')",
            (config.LOCAL_USER_ID,),
        )
    path = _write(tmp_path, "Kirjauspäivä;Määrä;Nimi;Valuutta\n2024/06/01;-100,00;Cafe;SEK\n")
    preview = service.preview_bank_import(path)
    assert preview["to_import"] == 1
    assert preview["samples"][0]["amount"] == 10
    service.commit_bank_import(path, expected_new=1)
    assert service.search_transactions(merchant="Cafe")["items"][0]["amount"] == 10


def test_refuses_the_database_file_and_a_url():
    with pytest.raises(BalanceMcpError, match="database"):
        service.preview_bank_import(config.SQLITE_PATH)
    with pytest.raises(BalanceMcpError, match="local file"):
        service.preview_bank_import("https://example.com/statement.csv")


def test_create_rename_and_set_category_round_trip():
    created = service.create_category("Pets", "expense")
    with pytest.raises(BalanceMcpError, match="already exists"):
        service.create_category("pets", "expense")
    renamed = service.rename_category(created["id"], "Animals")
    assert renamed["name"] == "Animals"
    transaction_id = _add("2026-03-01", "Pet shop", "Other", 20)
    with pytest.raises(BalanceMcpError, match="income"):
        service.set_transaction_category(transaction_id, category="Job", category_type="income")
    assert _count("mcp_audit") == 2
    changed = service.set_transaction_category(
        transaction_id, category="Animals", category_type="expense",
    )
    assert changed["transaction"]["category"] == "Animals"
    same = service.set_transaction_category(transaction_id, category_id=created["id"])
    assert same["changed"] is False
    service.undo_change(changed["audit_id"])
    assert service.search_transactions(merchant="Pet")["items"][0]["category"] == "Other"
    service.undo_change(renamed["audit_id"])
    names = {row["name"] for row in service.list_categories(category_type="expense")["categories"]}
    assert "Pets" in names and "Animals" not in names
    service.undo_change(created["audit_id"])
    names = {row["name"] for row in service.list_categories(category_type="expense")["categories"]}
    assert "Pets" not in names


def test_recategorise_previews_before_apply_and_skips_the_other_type():
    expense_id = _add("2026-03-01", "Netflix", "Other", 9.99)
    income_id = _add("2026-03-01", "Netflix", "Job", 15, "income")
    with pytest.raises(BalanceMcpError, match="3 characters"):
        service.create_category_rule("ne", "contains", category="Entertainment", category_type="expense")
    rule = service.create_category_rule(
        "netflix", "contains", category="Entertainment", category_type="expense",
    )
    preview = service.preview_recategorise(rule_id=rule["id"])
    assert preview["would_update"] == 1
    assert preview["skipped_other_type"] == 1
    assert preview["samples"][0]["id"] == expense_id
    with pytest.raises(BalanceMcpError, match="would change 1"):
        service.apply_recategorise(expected_updates=5, rule_id=rule["id"])
    applied = service.apply_recategorise(expected_updates=1, rule_id=rule["id"])
    rows = {row["id"]: row for row in service.search_transactions(merchant="Netflix")["items"]}
    assert rows[expense_id]["category"] == "Entertainment"
    assert rows[income_id]["category"] == "Job"
    service.undo_change(applied["audit_id"])
    rows = {row["id"]: row for row in service.search_transactions(merchant="Netflix")["items"]}
    assert rows[expense_id]["category"] == "Other"
    assert service.list_category_rules()["rules"][0]["id"] == rule["id"]
    service.undo_change(rule["audit_id"])
    assert service.list_category_rules()["rules"] == []


def test_suggestions_come_from_history_or_a_rule_and_apply_is_explicit():
    for _ in range(3):
        _add("2026-02-01", "Prisma", "Groceries", 20)
    other_id = _add("2026-03-01", "Prisma", "Other", 22)
    for _ in range(2):
        _add("2026-02-01", "Mixed Shop", "Dog", 10)
    ambiguous = _add("2026-03-01", "Mixed Shop", "Other", 10)
    suggestions = {
        row["transaction_id"]: row for row in service.suggest_categories()["suggestions"]
    }
    assert suggestions[other_id]["category"] == "Groceries"
    assert suggestions[other_id]["source"] == "history"
    assert ambiguous not in suggestions

    _add("2026-02-01", "Verkkokauppa", "Dog", 10)
    _add("2026-02-02", "Verkkokauppa", "Other", 10)
    ruled = _add("2026-03-01", "Verkkokauppa", "Other", 30)
    service.create_category_rule(
        "Verkkokauppa", "exact", category="Electronics", category_type="expense",
    )
    again = {row["transaction_id"]: row for row in service.suggest_categories()["suggestions"]}
    assert again[ruled]["category"] == "Electronics"
    assert again[ruled]["source"] == "rule"

    applied = service.apply_category_suggestions([other_id, ruled, ambiguous])
    assert applied["applied"] == 2
    assert {row["id"] for row in applied["skipped"]} == {ambiguous}
    rows = {row["id"]: row["category"] for row in service.search_transactions()["items"]}
    assert rows[other_id] == "Groceries"
    assert rows[ruled] == "Electronics"
    assert rows[ambiguous] == "Other"
    service.undo_change(applied["audit_id"])
    rows = {row["id"]: row["category"] for row in service.search_transactions()["items"]}
    assert rows[other_id] == "Other"
    assert rows[ruled] == "Other"


def test_undo_of_a_missing_category_rolls_back():
    transaction_id = _add("2026-03-01", "Pet shop", "Groceries", 20)
    groceries = _cid("Groceries")
    pets = service.create_category("Pets", "expense")
    changed = service.set_transaction_category(transaction_id, category_id=pets["id"])
    with db.db_conn() as conn:
        conn.execute(
            "DELETE FROM categories WHERE id = %s AND user_id = %s",
            (groceries, config.LOCAL_USER_ID),
        )
    with pytest.raises(BalanceMcpError, match="Nothing was undone"):
        service.undo_change(changed["audit_id"])
    with db.db_conn() as conn:
        row = conn.execute(
            "SELECT undone FROM mcp_audit WHERE id = %s", (changed["audit_id"],)
        ).fetchone()
        category_id = conn.execute(
            "SELECT category_id FROM transactions WHERE id = %s", (transaction_id,)
        ).fetchone()["category_id"]
    assert row["undone"] == 0
    assert category_id == pets["id"]


def test_server_lists_the_tools_and_a_read_returns_json():
    from balance_mcp.server import build_server

    server = build_server()
    assert inspect.signature(server.run).parameters["transport"].default == "stdio"
    tools = asyncio.run(server.list_tools())
    by_name = {tool.name: tool for tool in tools}
    assert set(by_name) == TOOLS

    def read_only(tool):
        annotations = tool.annotations
        if annotations is None:
            return False
        if isinstance(annotations, dict):
            return bool(annotations.get("readOnlyHint") or annotations.get("read_only_hint"))
        return bool(getattr(annotations, "read_only_hint", False))

    assert {name for name, tool in by_name.items() if read_only(tool)} == READ_ONLY
    result = asyncio.run(server.call_tool("list_categories", {}))
    assert result.is_error is False
    assert "Groceries" in result.content[0].text
    from mcp.server.mcpserver.exceptions import ToolError
    with pytest.raises(ToolError, match="not a file"):
        asyncio.run(server.call_tool(
            "commit_bank_import", {"path": "/no/such/file.csv", "expected_new": 1},
        ))
