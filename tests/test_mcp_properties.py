"""Property checks for parsing, importing every row, and undoing a change.

Synthetic rows only. Each example resets the throwaway database first.
"""

from datetime import date
from pathlib import Path

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

import config
from balance_mcp import service
from conftest import _reset_local_user
from data import db
from routes.csv_import import parse_amount, parse_date, suggest_category_detail


_DATES = st.dates(min_value=date(2018, 1, 1), max_value=date(2026, 12, 28))
_STORES = st.from_regex(r"[A-Za-z][A-Za-z]{2,14}", fullmatch=True)
_CENTS = st.integers(min_value=1, max_value=250_000)
_ROW = st.fixed_dictionaries({"day": _DATES, "store": _STORES, "cents": _CENTS})


def _finnish(rows) -> str:
    lines = ["Kirjauspäivä;Määrä;Nimi;Viesti"]
    for row in rows:
        day = row["day"]
        euros, frac = divmod(row["cents"], 100)
        lines.append(
            f"{day.year}/{day.month:02d}/{day.day:02d};-{euros},{frac:02d};{row['store']};"
        )
    return "\n".join(lines) + "\n"


def _write(directory: Path, text: str) -> str:
    path = directory / "statement.csv"
    path.write_text(text, encoding="utf-8")
    return str(path)


@given(day=_DATES, cents=_CENTS)
@settings(max_examples=40, deadline=None, derandomize=True)
def test_explicit_dates_and_finnish_amounts_parse_back(day, cents):
    iso = day.isoformat()
    assert parse_date(iso) == iso
    assert parse_date(f"{day.year}/{day.month}/{day.day}") == iso
    assert parse_date(f"{day.day}.{day.month}.{day.year}") == iso
    euros, frac = divmod(cents, 100)
    assert parse_amount(f"{euros},{frac:02d}") == euros + frac / 100
    assert parse_amount(f"-{euros},{frac:02d}") == euros + frac / 100


@settings(
    max_examples=12,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(rows=st.lists(_ROW, min_size=1, max_size=4))
@example(rows=[
    {"day": date(2024, 6, 1), "store": "Bar", "cents": 450},
    {"day": date(2024, 6, 1), "store": "Bar", "cents": 450},
])
def test_every_generated_row_imports_and_the_batch_undoes(rows, tmp_path):
    _reset_local_user()
    path = _write(tmp_path, _finnish(rows))
    preview = service.preview_bank_import(path)
    assert preview["needs_mapping"] is False
    assert preview["parsed"] == len(rows)
    assert preview["to_import"] == len(rows)
    assert "duplicates" not in preview
    committed = service.commit_bank_import(path, expected_new=len(rows))
    assert committed["committed"] == len(rows)
    assert service.search_transactions(limit=20)["total"] == len(rows)
    undone = service.undo_change(committed["audit_id"])
    assert undone["result"]["removed"] == len(rows)
    assert service.search_transactions()["total"] == 0


@given(store=_STORES)
@settings(max_examples=12, deadline=None, derandomize=True)
def test_an_exact_rule_matches_regardless_of_case(store):
    _reset_local_user()
    service.create_category_rule(
        store.lower(), "exact", category="Groceries", category_type="expense",
    )
    with db.db_conn() as conn:
        found = suggest_category_detail(store.upper(), conn, config.LOCAL_USER_ID, "expense")
    assert found == {"name": "Groceries", "source": "rule"}


@given(label=_STORES)
@settings(max_examples=8, deadline=None, derandomize=True)
def test_setting_a_category_and_undoing_it_restores_other(label):
    _reset_local_user()
    name = "Kind" + label
    created = service.create_category(name, "expense")
    with db.db_conn() as conn:
        other = conn.execute(
            "SELECT id FROM categories WHERE user_id = %s AND name = 'Other' AND type = 'expense'",
            (config.LOCAL_USER_ID,),
        ).fetchone()["id"]
        transaction_id = conn.execute(
            "INSERT INTO transactions (user_id, date, store, category_id, amount, type) "
            "VALUES (%s, '2024-06-01', 'Corner shop', %s, 5, 'expense') RETURNING id",
            (config.LOCAL_USER_ID, other),
        ).fetchone()["id"]
    changed = service.set_transaction_category(transaction_id, category_id=created["id"])
    assert changed["transaction"]["category"] == name
    service.undo_change(changed["audit_id"])
    assert service.search_transactions()["items"][0]["category"] == "Other"
    service.undo_change(created["audit_id"])
    names = {row["name"] for row in service.list_categories(category_type="expense")["categories"]}
    assert name not in names
