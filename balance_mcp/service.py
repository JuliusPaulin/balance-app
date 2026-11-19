"""The tools, as plain functions over the same database the app uses.

Nothing in this module imports the MCP SDK, opens a socket, or fetches a URL.
Import parsing, category suggestion, and net worth all come from the app.
A bank import here does not retrain merchant rules: the in-app Confirm button
does, and that deletes hand-written rules first.
"""

from __future__ import annotations

import csv
import io
import os
from contextlib import contextmanager
from datetime import date, datetime

import config
from balance_mcp.errors import BalanceMcpError
from data import db
from data.schema import backup_db
from routes.csv_import import (
    FINNAIR_EUR_AMOUNT_COL,
    _decode_csv_bytes,
    _default_currency,
    _detect_delimiter,
    _is_finnair_format,
    _read_headers,
    _revolut_columns,
    _stage_rows,
    detect_columns,
    format_signature,
    suggest_category_detail,
)
from routes.merchant_rules import _matches_rule
from routes.transactions import _base_from, _filter_clauses
from services.exchange_rates import ExchangeRateError, currency_code
from services.networth import compute_history, summary

_MAX_CSV_BYTES = 16 * 1024 * 1024
_SAMPLE_NEW = 8
_SAMPLE_RULE = 20

_READ_TX = """
    SELECT t.id, t.date, t.store, t.amount, t.type, t.category_id, t.import_batch_id,
           c.name AS category_name
    FROM transactions t
    JOIN categories c ON c.id = t.category_id
"""


class _Args:
    """The ``.get(key, type=)`` shape ``_filter_clauses`` already speaks."""

    def __init__(self, data):
        self._d = data

    def get(self, key, default=None, type=None):
        if key not in self._d or self._d[key] in (None, ""):
            return default
        value = self._d[key]
        if type is None:
            return value
        try:
            return type(value)
        except (TypeError, ValueError):
            return default


def _uid() -> int:
    return config.LOCAL_USER_ID


def _money(value) -> float:
    return round(float(value), 2)


def _cents(value) -> int:
    return int(round(float(value) * 100))


def _int(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BalanceMcpError(f"{label} must be an integer.")
    return value


def _bounded(value, default: int, lo: int, hi: int, label: str) -> int:
    if value is None:
        return default
    number = _int(value, label)
    if number < lo or number > hi:
        raise BalanceMcpError(f"{label} must be from {lo} to {hi}.")
    return number


def _type(value, *, required: bool) -> str | None:
    if value is None or str(value).strip() == "":
        if required:
            raise BalanceMcpError("category_type must be expense or income.")
        return None
    text = str(value).strip()
    if text not in ("expense", "income"):
        raise BalanceMcpError("category_type must be expense or income.")
    return text


def _day(value, label: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise BalanceMcpError(f"{label} must be YYYY-MM-DD.") from None
    if parsed.strftime("%Y-%m-%d") != text:
        raise BalanceMcpError(f"{label} must be YYYY-MM-DD.")
    return text


def _month(value, label: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m")
    except ValueError:
        raise BalanceMcpError(f"{label} must be YYYY-MM.") from None
    if parsed.strftime("%Y-%m") != text:
        raise BalanceMcpError(f"{label} must be YYYY-MM.")
    return text


def _year(value) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if len(text) != 4 or not text.isdigit():
        raise BalanceMcpError("year must be a four-digit year.")
    return text


def _name(value, label: str, limit: int = 80) -> str:
    text = str(value or "").strip()
    if not text:
        raise BalanceMcpError(f"{label} is empty.")
    if len(text) > limit:
        raise BalanceMcpError(f"{label} is longer than {limit} characters.")
    return text


def _chunks(items, size: int):
    items = list(items)
    for start in range(0, len(items), size):
        yield items[start:start + size]


@contextmanager
def _write_conn():
    """One immediate transaction. A lock that outlasts the busy timeout is an error."""
    try:
        with db.db_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
    except db.DatabaseError as exc:
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            raise BalanceMcpError(
                "The database is busy because Balance is writing to it. "
                "Nothing was saved. Try again."
            ) from exc
        raise


def _record(conn, action: str, summary: str, undo_kind: str, payload: dict) -> int:
    summary = summary[:500]
    cursor = conn.execute(
        "INSERT INTO mcp_audit (user_id, action, summary, undo_kind, undo_payload) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (_uid(), action, summary, undo_kind, db.Json(payload)),
    )
    return cursor.fetchone()["id"]


def _tx(row) -> dict:
    return {
        "id": row["id"],
        "date": row["date"],
        "store": row["store"],
        "amount": _money(row["amount"]),
        "type": row["type"],
        "category_id": row["category_id"],
        "category": row["category_name"],
        "import_batch_id": row["import_batch_id"],
    }


def _category(conn, *, category_id=None, name=None, category_type=None, required=True):
    category_type = _type(category_type, required=False)
    uid = _uid()
    if category_id is not None:
        _int(category_id, "category_id")
        row = conn.execute(
            "SELECT id, name, type FROM categories WHERE id = %s AND user_id = %s",
            (category_id, uid),
        ).fetchone()
        if not row:
            raise BalanceMcpError("No category with that id.")
        if category_type and row["type"] != category_type:
            raise BalanceMcpError(f"That category is {row['type']}, not {category_type}.")
        return row
    if name is None or str(name).strip() == "":
        if required:
            raise BalanceMcpError("Pass category_id or category.")
        return None
    text = str(name).strip()
    sql = ("SELECT id, name, type FROM categories "
           "WHERE user_id = %s AND LOWER(name) = LOWER(%s)")
    params: list = [uid, text]
    if category_type:
        sql += " AND type = %s"
        params.append(category_type)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        kind = f"{category_type} " if category_type else ""
        raise BalanceMcpError(f"No {kind}category named {text}.")
    if len(rows) > 1:
        raise BalanceMcpError(
            f"{rows[0]['name']} exists as both expense and income. Pass category_type."
        )
    return rows[0]


def _transaction(conn, transaction_id: int):
    _int(transaction_id, "transaction_id")
    row = conn.execute(
        _READ_TX + " WHERE t.id = %s AND t.user_id = %s",
        (transaction_id, _uid()),
    ).fetchone()
    if not row:
        raise BalanceMcpError("No transaction with that id.")
    return row


def _account(row) -> dict:
    balance = row["latest_balance"]
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "archived": bool(row["is_archived"]),
        "group": row["group_name"],
        "holdings": row["holdings_count"] or 0,
        "balance": None if balance is None else _money(balance),
        "as_of": row["latest_as_of"],
    }


# ── Reads ────────────────────────────────────────────────────────────────


def list_accounts(account_id: int | None = None) -> dict:
    uid = _uid()
    with db.db_conn() as conn:
        rows = summary(conn, uid)["accounts"]
        if account_id is not None:
            _int(account_id, "account_id")
            rows = [row for row in rows if row["id"] == account_id]
            if not rows:
                raise BalanceMcpError("No account with that id.")
            history = conn.execute(
                "SELECT as_of, balance FROM account_balances "
                "WHERE account_id = %s AND user_id = %s ORDER BY as_of",
                (account_id, uid),
            ).fetchall()
            account = _account(rows[0])
            account["balances"] = [
                {"as_of": item["as_of"], "balance": _money(item["balance"])}
                for item in history
            ]
            return {"accounts": [account]}
        return {"accounts": [_account(row) for row in rows]}


def net_worth(months: int = 12) -> dict:
    months = _bounded(months, 12, 1, 120, "months")
    with db.db_conn() as conn:
        current = summary(conn, _uid())
        history = compute_history(conn, _uid(), months=months)
    return {
        "as_of": date.today().isoformat(),
        "assets": _money(current["assets"]),
        "liabilities": _money(current["liabilities"]),
        "net_worth": _money(current["net_worth"]),
        "change_vs_prev_month": _money(current["change_vs_prev"]),
        "accounts": [_account(row) for row in current["accounts"]],
        "history": history,
    }


def search_transactions(
    date_from: str | None = None,
    date_to: str | None = None,
    month: str | None = None,
    category: str | None = None,
    category_id: int | None = None,
    category_type: str | None = None,
    merchant: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    txn_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    limit = _bounded(limit, 50, 1, 200, "limit")
    offset = _bounded(offset, 0, 0, 100000, "offset")
    txn_type = _type(txn_type, required=False)
    date_from = _day(date_from, "date_from")
    date_to = _day(date_to, "date_to")
    month = _month(month, "month")
    if date_from and date_to and date_from > date_to:
        raise BalanceMcpError("date_from is after date_to.")
    with db.db_conn() as conn:
        cat_ids = None
        if category_id is not None or (category and str(category).strip()):
            chosen = _category(
                conn, category_id=category_id, name=category, category_type=category_type,
            )
            cat_ids = str(chosen["id"])
        elif category_type:
            _type(category_type, required=True)
        args = _Args({
            "type": txn_type,
            "month": month,
            "date_from": date_from,
            "date_to": date_to,
            "category_ids": cat_ids,
            "merchant": (merchant or "").strip(),
            "amount_min": amount_min,
            "amount_max": amount_max,
        })
        conditions, params = _filter_clauses(args, _uid())
        base = _base_from(conditions)
        totals = conn.execute(
            f"""SELECT COUNT(*) AS n,
                       COALESCE(SUM(CASE WHEN t.type = 'expense' THEN t.amount END), 0) AS sum_expense,
                       COALESCE(SUM(CASE WHEN t.type = 'income' THEN t.amount END), 0) AS sum_income
                {base}""",
            params,
        ).fetchone()
        rows = conn.execute(
            "SELECT t.id, t.date, t.store, t.amount, t.type, t.category_id, "
            "t.import_batch_id, c.name AS category_name "
            f"{base} ORDER BY t.date DESC, t.id DESC LIMIT %s OFFSET %s",
            params + [limit, offset],
        ).fetchall()
    return {
        "total": totals["n"],
        "offset": offset,
        "limit": limit,
        "sum_expense": _money(totals["sum_expense"]),
        "sum_income": _money(totals["sum_income"]),
        "items": [_tx(row) for row in rows],
    }


def spending_by_category(
    txn_type: str = "expense",
    date_from: str | None = None,
    date_to: str | None = None,
    month: str | None = None,
    year: str | None = None,
) -> dict:
    txn_type = _type(txn_type, required=True)
    date_from = _day(date_from, "date_from")
    date_to = _day(date_to, "date_to")
    month = _month(month, "month")
    year = _year(year)
    if (date_from or date_to) and (month or year):
        raise BalanceMcpError("Pass a month, a year, or a date range, not more than one.")
    if month and year:
        raise BalanceMcpError("Pass a month or a year, not both.")
    if date_from and date_to and date_from > date_to:
        raise BalanceMcpError("date_from is after date_to.")
    uid = _uid()
    with db.db_conn() as conn:
        if not (date_from or date_to or month or year):
            latest = conn.execute(
                "SELECT substr(date, 1, 7) AS month FROM transactions "
                "WHERE user_id = %s AND type = %s ORDER BY date DESC LIMIT 1",
                (uid, txn_type),
            ).fetchone()
            month = latest["month"] if latest else None
            if month is None:
                return {"type": txn_type, "month": None, "total": 0, "items": []}
        conditions = ["t.user_id = %s", "t.type = %s"]
        params: list = [uid, txn_type]
        period: dict = {"type": txn_type}
        if month:
            conditions.append("substr(t.date, 1, 7) = %s")
            params.append(month)
            period["month"] = month
        elif year:
            conditions.append("substr(t.date, 1, 4) = %s")
            params.append(year)
            period["year"] = year
        else:
            if date_from:
                conditions.append("t.date >= %s")
                params.append(date_from)
            if date_to:
                conditions.append("t.date <= %s")
                params.append(date_to)
            period["date_from"] = date_from
            period["date_to"] = date_to
        rows = conn.execute(
            "SELECT c.id AS category_id, c.name AS category, "
            "SUM(t.amount) AS total, COUNT(*) AS count "
            "FROM transactions t JOIN categories c ON c.id = t.category_id "
            f"WHERE {' AND '.join(conditions)} "
            "GROUP BY c.id, c.name ORDER BY total DESC, c.name",
            params,
        ).fetchall()
    items = [
        {
            "category_id": row["category_id"],
            "category": row["category"],
            "total": _money(row["total"]),
            "count": row["count"],
        }
        for row in rows
    ]
    period["total"] = _money(sum(_cents(row["total"]) for row in rows) / 100) if rows else 0
    period["items"] = items
    return period


def cash_flow(date_from: str | None = None, date_to: str | None = None) -> dict:
    date_from = _day(date_from, "date_from")
    date_to = _day(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise BalanceMcpError("date_from is after date_to.")
    conditions = ["user_id = %s"]
    params: list = [_uid()]
    if date_from:
        conditions.append("date >= %s")
        params.append(date_from)
    if date_to:
        conditions.append("date <= %s")
        params.append(date_to)
    where = " AND ".join(conditions)
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT substr(date, 1, 7) AS month, "
            "COALESCE(SUM(CASE WHEN type = 'income' THEN amount END), 0) AS income, "
            "COALESCE(SUM(CASE WHEN type = 'expense' THEN amount END), 0) AS expense "
            f"FROM transactions WHERE {where} GROUP BY substr(date, 1, 7) ORDER BY month",
            params,
        ).fetchall()
        invested = conn.execute(
            "SELECT substr(t.date, 1, 7) AS month, SUM(t.amount) AS total "
            "FROM transactions t JOIN categories c ON c.id = t.category_id "
            "WHERE t.user_id = %s AND t.type = 'expense' AND c.name = 'Investments' "
            + (" AND t.date >= %s" if date_from else "")
            + (" AND t.date <= %s" if date_to else "")
            + " GROUP BY substr(t.date, 1, 7)",
            [_uid()] + ([date_from] if date_from else []) + ([date_to] if date_to else []),
        ).fetchall()
    by_month = {row["month"]: _cents(row["total"]) for row in invested}
    months = []
    income_cents = expense_cents = investment_cents = 0
    for row in rows:
        inc = _cents(row["income"])
        exp = _cents(row["expense"])
        inv = by_month.get(row["month"], 0)
        income_cents += inc
        expense_cents += exp
        investment_cents += inv
        months.append({
            "month": row["month"],
            "income": inc / 100,
            "expense": exp / 100,
            "investments": inv / 100,
            "net": (inc - exp) / 100,
        })
    return {
        "date_from": date_from,
        "date_to": date_to,
        "income": income_cents / 100,
        "expense": expense_cents / 100,
        "investments": investment_cents / 100,
        "net": (income_cents - expense_cents) / 100,
        "months": months,
    }


def list_categories(category_type: str | None = None) -> dict:
    category_type = _type(category_type, required=False)
    with db.db_conn() as conn:
        if category_type:
            rows = conn.execute(
                "SELECT c.id, c.name, c.type, c.is_default, "
                "COUNT(t.id) AS tx_count, MAX(t.date) AS last_used "
                "FROM categories c "
                "LEFT JOIN transactions t ON t.category_id = c.id AND t.user_id = c.user_id "
                "WHERE c.user_id = %s AND c.type = %s "
                "GROUP BY c.id ORDER BY c.name",
                (_uid(), category_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT c.id, c.name, c.type, c.is_default, "
                "COUNT(t.id) AS tx_count, MAX(t.date) AS last_used "
                "FROM categories c "
                "LEFT JOIN transactions t ON t.category_id = c.id AND t.user_id = c.user_id "
                "WHERE c.user_id = %s GROUP BY c.id ORDER BY c.type, c.name",
                (_uid(),),
            ).fetchall()
    return {
        "categories": [
            {
                "id": row["id"],
                "name": row["name"],
                "type": row["type"],
                "is_default": bool(row["is_default"]),
                "tx_count": row["tx_count"],
                "last_used": row["last_used"],
            }
            for row in rows
        ]
    }


def list_category_rules() -> dict:
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT mr.id, mr.pattern, mr.match_type, mr.category_id, "
            "c.name AS category, c.type AS category_type "
            "FROM merchant_rules mr JOIN categories c ON c.id = mr.category_id "
            "WHERE mr.user_id = %s ORDER BY mr.pattern, mr.id",
            (_uid(),),
        ).fetchall()
    return {"rules": [dict(row) for row in rows]}


def list_audit_log(limit: int = 20) -> dict:
    limit = _bounded(limit, 20, 1, 100, "limit")
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, action, summary, undone, undone_at "
            "FROM mcp_audit WHERE user_id = %s "
            "ORDER BY id DESC LIMIT %s",
            (_uid(), limit),
        ).fetchall()
    return {
        "entries": [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "action": row["action"],
                "summary": row["summary"],
                "undone": bool(row["undone"]),
                "undone_at": row["undone_at"],
            }
            for row in rows
        ]
    }


# ── Bank CSV import ──────────────────────────────────────────────────────


def _csv_path(path: str) -> str:
    text = str(path or "").strip()
    if not text:
        raise BalanceMcpError("Pass the path of a CSV file on this Mac.")
    if "://" in text:
        raise BalanceMcpError("Pass a local file path, not a URL.")
    resolved = os.path.realpath(os.path.expanduser(text))
    database = os.path.realpath(config.SQLITE_PATH)
    if resolved in {database, database + "-wal", database + "-shm"}:
        raise BalanceMcpError("That file is the Balance database, not a bank export.")
    if not os.path.isfile(resolved):
        raise BalanceMcpError("That path is not a file.")
    if not resolved.lower().endswith(".csv"):
        raise BalanceMcpError("Bank exports imported here are CSV files.")
    size = os.path.getsize(resolved)
    if size == 0:
        raise BalanceMcpError("That file is empty.")
    if size > _MAX_CSV_BYTES:
        raise BalanceMcpError("That file is larger than 16 MB.")
    return resolved


def _read_csv(path: str) -> tuple[str, str]:
    resolved = _csv_path(path)
    with open(resolved, "rb") as handle:
        raw = handle.read()
    try:
        content = _decode_csv_bytes(raw)
    except UnicodeDecodeError:
        raise BalanceMcpError("Could not decode that CSV. Save it as UTF-8.") from None
    return resolved, content


def _mapping(date_col, amount_col, store_col, amount_sign):
    if date_col is None and amount_col is None and store_col is None:
        if amount_sign not in (None, "", "neg_expense"):
            raise BalanceMcpError("date_col and amount_col are both required when mapping columns.")
        return None
    if date_col is None or amount_col is None:
        raise BalanceMcpError("date_col and amount_col are both required when mapping columns.")
    for label, index in (("date_col", date_col), ("amount_col", amount_col), ("store_col", store_col)):
        if index is None:
            continue
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise BalanceMcpError(f"{label} must be a column index starting at 0.")
    sign = amount_sign or "neg_expense"
    if sign not in ("neg_expense", "pos_expense"):
        raise BalanceMcpError("amount_sign must be neg_expense or pos_expense.")
    return {
        "date_col": date_col,
        "amount_col": amount_col,
        "store_col": store_col,
        "amount_sign": sign,
    }


def _raw_samples(content: str, delimiter: str, limit: int = 5) -> list:
    reader = csv.reader(io.StringIO(content), delimiter=delimiter)
    _read_headers(reader)
    rows = []
    for row in reader:
        while row and not row[-1].strip():
            row = row[:-1]
        if not row or all(cell.strip() == "" for cell in row):
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _resolve_layout(conn, content: str, mapping) -> dict:
    """The same column decision as ``POST /api/import/upload``.

    A recognised file wins over a caller-supplied mapping. A saved mapping is
    consulted only when detection fails, and a manual mapping only then too.
    """
    delimiter = _detect_delimiter(content)
    reader = csv.reader(io.StringIO(content), delimiter=delimiter)
    headers = _read_headers(reader)
    if not headers:
        raise BalanceMcpError("That CSV has no header row.")
    signature = format_signature(headers, delimiter)
    detected = detect_columns(headers)
    default_currency = currency_code(_default_currency(conn, _uid()))
    col_map = dict(detected)
    amount_sign = "neg_expense"
    revolut = _revolut_columns(headers)
    if revolut:
        col_map = revolut
    if _is_finnair_format(headers):
        col_map["amount"] = FINNAIR_EUR_AMOUNT_COL
        col_map.pop("currency", None)
        default_currency = "EUR"
    how = None
    if "date" in col_map and "amount" in col_map:
        how = "automatic"
    elif mapping:
        width = len(headers)
        for label, index in (
            ("date_col", mapping["date_col"]),
            ("amount_col", mapping["amount_col"]),
            ("store_col", mapping["store_col"]),
        ):
            if index is not None and index >= width:
                raise BalanceMcpError(f"{label} is past the last column.")
        col_map = {"date": mapping["date_col"], "amount": mapping["amount_col"]}
        if mapping["store_col"] is not None:
            col_map["store"] = mapping["store_col"]
        if detected.get("currency") is not None:
            col_map["currency"] = detected["currency"]
        amount_sign = mapping["amount_sign"]
        how = "manual"
    else:
        learned = conn.execute(
            "SELECT * FROM import_formats WHERE user_id = %s AND signature = %s",
            (_uid(), signature),
        ).fetchone()
        if learned:
            col_map = {"date": learned["date_col"], "amount": learned["amount_col"]}
            if detected.get("currency") is not None:
                col_map["currency"] = detected["currency"]
            if learned["store_col"] is not None:
                col_map["store"] = learned["store_col"]
            amount_sign = learned["amount_sign"]
            how = "saved"
    base = {
        "delimiter": delimiter,
        "headers": headers,
        "signature": signature,
        "mapping_ignored": bool(mapping) and how == "automatic",
    }
    if how is None:
        guess = detect_columns(headers)
        base.update({
            "needs_mapping": True,
            "sample_rows": _raw_samples(content, delimiter),
            "guess": {
                "date": guess.get("date"),
                "amount": guess.get("amount"),
                "store": guess.get("store"),
            },
        })
        return base
    base.update({
        "needs_mapping": False,
        "col_map": col_map,
        "amount_sign": amount_sign,
        "default_currency": default_currency,
        "how": how,
    })
    return base


def _category_for_staged(conn, suggested: str | None, txn_type: str):
    if suggested:
        row = conn.execute(
            "SELECT id, name FROM categories WHERE user_id = %s AND name = %s AND type = %s",
            (_uid(), suggested, txn_type),
        ).fetchone()
        if row:
            return row["id"], row["name"], True
    other = conn.execute(
        "SELECT id, name FROM categories WHERE user_id = %s AND name = 'Other' AND type = %s",
        (_uid(), txn_type),
    ).fetchone()
    if not other:
        raise BalanceMcpError(f"No {txn_type} category named Other to file an unmatched row under.")
    return other["id"], other["name"], False


def _stage(conn, filename: str, content: str, layout: dict) -> int:
    reader = csv.reader(io.StringIO(content), delimiter=layout["delimiter"])
    _read_headers(reader)
    cursor = conn.execute(
        "INSERT INTO import_batches (user_id, filename) VALUES (%s, %s) RETURNING id",
        (_uid(), filename[:180]),
    )
    batch_id = cursor.fetchone()["id"]
    try:
        _stage_rows(
            conn, _uid(), batch_id, reader, layout["col_map"], layout["amount_sign"],
            layout["default_currency"], offline=True,
        )
    except ExchangeRateError as exc:
        raise BalanceMcpError(
            "This file has amounts that are not in EUR, and there is no rate "
            "saved for them. Nothing was imported. The MCP server does not fetch "
            "rates. Import the file in Balance, or export it in EUR."
        ) from exc
    except (csv.Error, ValueError) as exc:
        raise BalanceMcpError(f"Could not read that file: {exc}") from exc
    return batch_id


def _classify(conn, batch_id: int) -> dict:
    staged = conn.execute(
        "SELECT id, date, store, amount, type, suggested_category, import_fx "
        "FROM import_staging WHERE import_batch_id = %s AND user_id = %s ORDER BY id",
        (batch_id, _uid()),
    ).fetchall()
    rows = []
    for raw in staged:
        category_id, category, suggested = _category_for_staged(
            conn, raw["suggested_category"], raw["type"],
        )
        rows.append({
            "staging_id": raw["id"],
            "date": raw["date"],
            "store": raw["store"],
            "amount": raw["amount"],
            "type": raw["type"],
            "import_fx": raw["import_fx"],
            "category_id": category_id,
            "category": category,
            "suggested": suggested,
        })
    chosen = rows
    expense, income = _sums(chosen)
    batch = conn.execute(
        "SELECT import_summary FROM import_batches WHERE id = %s AND user_id = %s",
        (batch_id, _uid()),
    ).fetchone()
    return {
        "parsed": len(rows),
        "to_import": len(chosen),
        "sum_expense": expense,
        "sum_income": income,
        "samples": _public_import_rows(chosen, _SAMPLE_NEW),
        "parser": db.load_json(batch["import_summary"]) if batch and batch["import_summary"] else None,
        "_chosen": chosen,
    }


def _sums(rows) -> tuple[float, float]:
    expense = sum(_cents(row["amount"]) for row in rows if row["type"] == "expense")
    income = sum(_cents(row["amount"]) for row in rows if row["type"] == "income")
    return expense / 100, income / 100


def _public_import_rows(rows, limit: int) -> list:
    return [
        {
            "date": row["date"],
            "store": row["store"],
            "amount": _money(row["amount"]),
            "type": row["type"],
            "category_id": row["category_id"],
            "category": row["category"],
            "suggested": row["suggested"],
        }
        for row in rows[:limit]
    ]


def _preview_payload(filename: str, layout: dict, classified: dict | None) -> dict:
    if layout["needs_mapping"]:
        return {
            "dry_run": True,
            "needs_mapping": True,
            "filename": filename,
            "delimiter": layout["delimiter"],
            "headers": layout["headers"],
            "signature": layout["signature"],
            "sample_rows": layout["sample_rows"],
            "guess": layout["guess"],
        }
    payload = {
        "dry_run": True,
        "needs_mapping": False,
        "filename": filename,
        "mapping": layout["how"],
        "mapping_ignored": layout["mapping_ignored"],
    }
    payload.update({key: classified[key] for key in (
        "parsed", "to_import", "sum_expense", "sum_income", "samples", "parser",
    )})
    return payload


def preview_bank_import(
    path: str,
    date_col: int | None = None,
    amount_col: int | None = None,
    store_col: int | None = None,
    amount_sign: str = "neg_expense",
) -> dict:
    resolved, content = _read_csv(path)
    mapping = _mapping(date_col, amount_col, store_col, amount_sign)
    filename = os.path.basename(resolved)
    conn = db.get_db()
    try:
        layout = _resolve_layout(conn, content, mapping)
        if layout["needs_mapping"]:
            return _preview_payload(filename, layout, None)
        batch_id = _stage(conn, filename, content, layout)
        classified = _classify(conn, batch_id)
        return _preview_payload(filename, layout, classified)
    finally:
        conn.rollback()
        conn.close()


def _remember_format(conn, layout: dict, mapping) -> dict:
    existing = conn.execute(
        "SELECT * FROM import_formats WHERE user_id = %s AND signature = %s",
        (_uid(), layout["signature"]),
    ).fetchone()
    conn.execute(
        "INSERT INTO import_formats "
        "(user_id, signature, delimiter, date_col, amount_col, store_col, amount_sign) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (user_id, signature) DO UPDATE SET "
        "delimiter = excluded.delimiter, date_col = excluded.date_col, "
        "amount_col = excluded.amount_col, store_col = excluded.store_col, "
        "amount_sign = excluded.amount_sign",
        (
            _uid(), layout["signature"], layout["delimiter"], mapping["date_col"],
            mapping["amount_col"], mapping["store_col"], mapping["amount_sign"],
        ),
    )
    saved = conn.execute(
        "SELECT * FROM import_formats WHERE user_id = %s AND signature = %s",
        (_uid(), layout["signature"]),
    ).fetchone()

    def columns(row):
        return {
            "delimiter": row["delimiter"],
            "date_col": row["date_col"],
            "amount_col": row["amount_col"],
            "store_col": row["store_col"],
            "amount_sign": row["amount_sign"],
        }

    return {
        "id": saved["id"],
        "created": existing is None,
        "previous": None if existing is None else columns(existing),
        "written": columns(saved),
    }


def commit_bank_import(
    path: str,
    expected_new: int,
    date_col: int | None = None,
    amount_col: int | None = None,
    store_col: int | None = None,
    amount_sign: str = "neg_expense",
    remember_format: bool = False,
) -> dict:
    expected_new = _int(expected_new, "expected_new")
    if expected_new < 0:
        raise BalanceMcpError("expected_new cannot be negative.")
    resolved, content = _read_csv(path)
    mapping = _mapping(date_col, amount_col, store_col, amount_sign)
    filename = os.path.basename(resolved)
    preview = preview_bank_import(
        resolved, date_col, amount_col, store_col, amount_sign,
    )
    if preview.get("needs_mapping"):
        raise BalanceMcpError(
            "This file needs a column mapping. Preview it, then pass date_col and amount_col."
        )
    if preview["to_import"] != expected_new:
        raise BalanceMcpError(
            f"This file would import {preview['to_import']} transactions, "
            f"not {expected_new}. "
            "Preview again and pass that to_import count."
        )
    if expected_new == 0:
        return {
            "dry_run": False,
            "committed": 0,
            "batch_id": None,
            "audit_id": None,
            "filename": filename,
            "sum_expense": 0,
            "sum_income": 0,
            "to_import": 0,
            "format_saved": False,
        }
    backup_db("pre-import")
    with _write_conn() as conn:
        layout = _resolve_layout(conn, content, mapping)
        if layout["needs_mapping"]:
            raise BalanceMcpError(
                "This file needs a column mapping. Preview it, then pass date_col and amount_col."
            )
        batch_id = _stage(conn, filename, content, layout)
        classified = _classify(conn, batch_id)
        if classified["to_import"] != expected_new:
            raise BalanceMcpError(
                "The file or the database changed since the preview. Nothing was imported. Preview again."
            )
        ids = []
        for row in classified["_chosen"]:
            cursor = conn.execute(
                "INSERT INTO transactions "
                "(user_id, date, store, category_id, amount, type, import_batch_id, import_fx) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    _uid(), row["date"], row["store"], row["category_id"], row["amount"],
                    row["type"], batch_id, row["import_fx"],
                ),
            )
            ids.append(cursor.fetchone()["id"])
            conn.execute(
                "UPDATE import_staging SET confirmed = 1, final_category_id = %s "
                "WHERE id = %s AND user_id = %s",
                (row["category_id"], row["staging_id"], _uid()),
            )
        conn.execute(
            "UPDATE import_batches SET status = 'completed' WHERE id = %s AND user_id = %s",
            (batch_id, _uid()),
        )
        format_info = None
        if remember_format and layout["how"] == "manual" and mapping:
            format_info = _remember_format(conn, layout, mapping)
        summary_text = f"Imported {len(ids)} transactions from {filename}."
        audit_id = _record(conn, "import", summary_text, "import_batch", {
            "batch_id": batch_id,
            "transaction_ids": ids,
            "format": format_info,
        })
    return {
        "dry_run": False,
        "committed": len(ids),
        "batch_id": batch_id,
        "audit_id": audit_id,
        "filename": filename,
        "sum_expense": classified["sum_expense"],
        "sum_income": classified["sum_income"],
        "to_import": classified["to_import"],
        "format_saved": format_info is not None,
    }


# ── Categories, rules, suggestions ───────────────────────────────────────


def set_transaction_category(
    transaction_id: int,
    category_id: int | None = None,
    category: str | None = None,
    category_type: str | None = None,
) -> dict:
    with _write_conn() as conn:
        row = _transaction(conn, transaction_id)
        chosen = _category(
            conn, category_id=category_id, name=category, category_type=category_type,
        )
        if chosen["type"] != row["type"]:
            raise BalanceMcpError(
                f"That category is {chosen['type']} and this transaction is {row['type']}."
            )
        if chosen["id"] == row["category_id"]:
            return {"changed": False, "audit_id": None, "transaction": _tx(row)}
        conn.execute(
            "UPDATE transactions SET category_id = %s, updated_at = now() "
            "WHERE id = %s AND user_id = %s",
            (chosen["id"], row["id"], _uid()),
        )
        audit_id = _record(
            conn, "set_category",
            f"Set transaction {row['id']} ({row['store']}, {row['date']}) "
            f"to {chosen['name']}. It was {row['category_name']}.",
            "restore_categories",
            {"changes": [{"id": row["id"], "category_id": row["category_id"]}]},
        )
        updated = _transaction(conn, row["id"])
    return {"changed": True, "audit_id": audit_id, "transaction": _tx(updated)}


def create_category(name: str, category_type: str) -> dict:
    name = _name(name, "name")
    category_type = _type(category_type, required=True)
    with _write_conn() as conn:
        clash = conn.execute(
            "SELECT name FROM categories WHERE user_id = %s AND type = %s AND LOWER(name) = LOWER(%s)",
            (_uid(), category_type, name),
        ).fetchone()
        if clash:
            raise BalanceMcpError(
                f"A {category_type} category named {clash['name']} already exists."
            )
        cursor = conn.execute(
            "INSERT INTO categories (user_id, name, type, is_default) "
            "VALUES (%s, %s, %s, 0) RETURNING id",
            (_uid(), name, category_type),
        )
        category_id = cursor.fetchone()["id"]
        audit_id = _record(
            conn, "create_category",
            f"Created {category_type} category {name}.",
            "delete_category",
            {"category_id": category_id},
        )
    return {
        "id": category_id,
        "name": name,
        "type": category_type,
        "audit_id": audit_id,
    }


def rename_category(category_id: int, name: str) -> dict:
    name = _name(name, "name")
    _int(category_id, "category_id")
    with _write_conn() as conn:
        row = _category(conn, category_id=category_id)
        if row["name"] == name:
            return {"changed": False, "audit_id": None, "id": row["id"], "name": row["name"], "type": row["type"]}
        clash = conn.execute(
            "SELECT id, name FROM categories WHERE user_id = %s AND type = %s "
            "AND LOWER(name) = LOWER(%s) AND id != %s",
            (_uid(), row["type"], name, row["id"]),
        ).fetchone()
        if clash:
            raise BalanceMcpError(
                f"A {row['type']} category named {clash['name']} already exists."
            )
        conn.execute(
            "UPDATE categories SET name = %s, updated_at = now() WHERE id = %s AND user_id = %s",
            (name, row["id"], _uid()),
        )
        audit_id = _record(
            conn, "rename_category",
            f"Renamed {row['type']} category {row['name']} to {name}.",
            "restore_category_name",
            {"category_id": row["id"], "name": row["name"], "new_name": name},
        )
    return {"changed": True, "audit_id": audit_id, "id": category_id, "name": name, "type": row["type"]}


def _check_pattern(pattern: str, match_type: str) -> str:
    if match_type not in ("exact", "contains", "smart"):
        raise BalanceMcpError("match_type must be exact, contains, or smart.")
    text = pattern.strip()
    if not text:
        raise BalanceMcpError("The pattern is empty.")
    if len(text) > 200:
        raise BalanceMcpError("The pattern is longer than 200 characters.")
    if match_type in ("contains", "smart") and len(text) < 3:
        raise BalanceMcpError(
            "contains and smart patterns must be at least 3 characters. "
            "A shorter one would match far too much."
        )
    return text


def create_category_rule(
    pattern: str,
    match_type: str = "exact",
    category_id: int | None = None,
    category: str | None = None,
    category_type: str | None = None,
) -> dict:
    pattern = _check_pattern(pattern, match_type)
    with _write_conn() as conn:
        chosen = _category(
            conn, category_id=category_id, name=category, category_type=category_type,
        )
        clash = conn.execute(
            "SELECT mr.id, c.name AS category_name FROM merchant_rules mr "
            "JOIN categories c ON c.id = mr.category_id "
            "WHERE mr.user_id = %s AND mr.match_type = %s AND LOWER(mr.pattern) = LOWER(%s)",
            (_uid(), match_type, pattern),
        ).fetchone()
        if clash:
            raise BalanceMcpError(
                f"A {match_type} rule for {pattern} already exists "
                f"(id {clash['id']}, {clash['category_name']})."
            )
        cursor = conn.execute(
            "INSERT INTO merchant_rules (user_id, pattern, category_id, match_type) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (_uid(), pattern, chosen["id"], match_type),
        )
        rule_id = cursor.fetchone()["id"]
        audit_id = _record(
            conn, "create_rule",
            f"Created {match_type} rule {pattern} → {chosen['name']}.",
            "delete_rule",
            {"rule_id": rule_id},
        )
    return {
        "id": rule_id,
        "pattern": pattern,
        "match_type": match_type,
        "category_id": chosen["id"],
        "category": chosen["name"],
        "category_type": chosen["type"],
        "audit_id": audit_id,
    }


def _rule_spec(conn, rule_id, pattern, match_type, category_id, category, category_type):
    supplied_pattern = pattern is not None and str(pattern).strip() != ""
    supplied_category = category_id is not None or (category is not None and str(category).strip() != "")
    if rule_id is not None and (supplied_pattern or supplied_category):
        raise BalanceMcpError("Pass rule_id, or a pattern and a category, not both.")
    if rule_id is not None:
        _int(rule_id, "rule_id")
        row = conn.execute(
            "SELECT mr.pattern, mr.match_type, mr.category_id, "
            "c.name AS category_name, c.type AS category_type "
            "FROM merchant_rules mr JOIN categories c ON c.id = mr.category_id "
            "WHERE mr.id = %s AND mr.user_id = %s",
            (rule_id, _uid()),
        ).fetchone()
        if not row:
            raise BalanceMcpError("No rule with that id.")
        return row
    if not supplied_pattern:
        raise BalanceMcpError("Pass rule_id, or a pattern and a category.")
    text = _check_pattern(str(pattern), match_type or "exact")
    chosen = _category(
        conn, category_id=category_id, name=category, category_type=category_type,
    )
    return {
        "pattern": text,
        "match_type": match_type or "exact",
        "category_id": chosen["id"],
        "category_name": chosen["name"],
        "category_type": chosen["type"],
    }


def _recategorise_plan(conn, rule) -> dict:
    rows = conn.execute(
        "SELECT t.id, t.date, t.store, t.amount, t.type, t.category_id, "
        "c.name AS category_name "
        "FROM transactions t JOIN categories c ON c.id = t.category_id "
        "WHERE t.user_id = %s AND t.store != ''",
        (_uid(),),
    ).fetchall()
    matched = 0
    skipped_other_type = 0
    would = []
    for row in rows:
        if not _matches_rule(row["store"], rule["pattern"], rule["match_type"]):
            continue
        if rule["category_type"] != row["type"]:
            skipped_other_type += 1
            continue
        matched += 1
        if row["category_id"] != rule["category_id"]:
            would.append(row)
    return {
        "pattern": rule["pattern"],
        "match_type": rule["match_type"],
        "category_id": rule["category_id"],
        "category": rule["category_name"],
        "category_type": rule["category_type"],
        "matches": matched,
        "would_update": len(would),
        "skipped_other_type": skipped_other_type,
        "samples": [
            {
                "id": row["id"],
                "date": row["date"],
                "store": row["store"],
                "amount": _money(row["amount"]),
                "type": row["type"],
                "from_category": row["category_name"],
                "to_category": rule["category_name"],
            }
            for row in would[:_SAMPLE_RULE]
        ],
        "_would": would,
    }


def preview_recategorise(
    rule_id: int | None = None,
    pattern: str | None = None,
    match_type: str = "exact",
    category_id: int | None = None,
    category: str | None = None,
    category_type: str | None = None,
) -> dict:
    with db.db_conn() as conn:
        rule = _rule_spec(
            conn, rule_id, pattern, match_type, category_id, category, category_type,
        )
        plan = _recategorise_plan(conn, rule)
    plan.pop("_would", None)
    plan["dry_run"] = True
    return plan


def apply_recategorise(
    expected_updates: int,
    rule_id: int | None = None,
    pattern: str | None = None,
    match_type: str = "exact",
    category_id: int | None = None,
    category: str | None = None,
    category_type: str | None = None,
) -> dict:
    expected_updates = _int(expected_updates, "expected_updates")
    if expected_updates < 0:
        raise BalanceMcpError("expected_updates cannot be negative.")
    with _write_conn() as conn:
        rule = _rule_spec(
            conn, rule_id, pattern, match_type, category_id, category, category_type,
        )
        plan = _recategorise_plan(conn, rule)
        if plan["would_update"] != expected_updates:
            raise BalanceMcpError(
                f"That would change {plan['would_update']} transactions, not {expected_updates}. "
                "Preview again and pass that would_update count."
            )
        if expected_updates == 0:
            return {
                "updated": 0,
                "audit_id": None,
                "category": rule["category_name"],
                "pattern": rule["pattern"],
            }
        for row in plan["_would"]:
            conn.execute(
                "UPDATE transactions SET category_id = %s, updated_at = now() "
                "WHERE id = %s AND user_id = %s",
                (rule["category_id"], row["id"], _uid()),
            )
        audit_id = _record(
            conn, "recategorise",
            f"Moved {expected_updates} transactions to {rule['category_name']} "
            f"({rule['match_type']} {rule['pattern']!r}).",
            "restore_categories",
            {"changes": [{"id": row["id"], "category_id": row["category_id"]} for row in plan["_would"]]},
        )
    return {
        "updated": expected_updates,
        "audit_id": audit_id,
        "category": rule["category_name"],
        "pattern": rule["pattern"],
    }


def suggest_categories(limit: int = 100) -> dict:
    limit = _bounded(limit, 100, 1, 500, "limit")
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT t.id, t.date, t.store, t.amount, t.type, c.name AS category_name "
            "FROM transactions t JOIN categories c ON c.id = t.category_id "
            "WHERE t.user_id = %s AND c.name = 'Other' AND t.store != '' "
            "ORDER BY t.date DESC, t.id DESC",
            (_uid(),),
        ).fetchall()
        suggestions = []
        for row in rows:
            detail = suggest_category_detail(row["store"], conn, _uid(), row["type"])
            if not detail or detail["name"] == "Other":
                continue
            chosen = conn.execute(
                "SELECT id FROM categories WHERE user_id = %s AND name = %s AND type = %s",
                (_uid(), detail["name"], row["type"]),
            ).fetchone()
            if not chosen:
                continue
            suggestions.append({
                "transaction_id": row["id"],
                "date": row["date"],
                "store": row["store"],
                "amount": _money(row["amount"]),
                "type": row["type"],
                "category_id": chosen["id"],
                "category": detail["name"],
                "source": detail["source"],
            })
    truncated = len(suggestions) > limit
    return {
        "count": min(len(suggestions), limit),
        "truncated": truncated,
        "suggestions": suggestions[:limit],
    }


def apply_category_suggestions(transaction_ids: list[int]) -> dict:
    if not isinstance(transaction_ids, list) or not transaction_ids:
        raise BalanceMcpError("Pass the transaction ids from suggest_categories.")
    if len(transaction_ids) > 500:
        raise BalanceMcpError("Apply at most 500 suggestions at a time.")
    unique = []
    seen = set()
    for value in transaction_ids:
        number = _int(value, "transaction ids")
        if number not in seen:
            seen.add(number)
            unique.append(number)
    with _write_conn() as conn:
        changes = []
        skipped = []
        applied = []
        for transaction_id in unique:
            row = conn.execute(
                _READ_TX + " WHERE t.id = %s AND t.user_id = %s",
                (transaction_id, _uid()),
            ).fetchone()
            if not row:
                skipped.append({"id": transaction_id, "reason": "not found"})
                continue
            if row["category_name"] != "Other":
                skipped.append({"id": transaction_id, "reason": "not in Other"})
                continue
            detail = suggest_category_detail(row["store"], conn, _uid(), row["type"])
            if not detail or detail["name"] == "Other":
                skipped.append({"id": transaction_id, "reason": "no suggestion"})
                continue
            chosen = conn.execute(
                "SELECT id, name FROM categories WHERE user_id = %s AND name = %s AND type = %s",
                (_uid(), detail["name"], row["type"]),
            ).fetchone()
            if not chosen:
                skipped.append({"id": transaction_id, "reason": "suggestion has no category"})
                continue
            conn.execute(
                "UPDATE transactions SET category_id = %s, updated_at = now() "
                "WHERE id = %s AND user_id = %s",
                (chosen["id"], row["id"], _uid()),
            )
            changes.append({"id": row["id"], "category_id": row["category_id"]})
            applied.append({
                "id": row["id"],
                "store": row["store"],
                "category": chosen["name"],
                "source": detail["source"],
            })
        audit_id = None
        if changes:
            audit_id = _record(
                conn, "apply_suggestions",
                f"Applied {len(changes)} category suggestions.",
                "restore_categories",
                {"changes": changes},
            )
    return {"applied": len(applied), "audit_id": audit_id, "transactions": applied, "skipped": skipped}


# ── Undo ─────────────────────────────────────────────────────────────────


def _restore_categories(conn, changes) -> dict:
    restored = 0
    missing = 0
    for change in changes:
        txn = conn.execute(
            "SELECT id, type FROM transactions WHERE id = %s AND user_id = %s",
            (change["id"], _uid()),
        ).fetchone()
        if not txn:
            missing += 1
            continue
        category = conn.execute(
            "SELECT id, type FROM categories WHERE id = %s AND user_id = %s",
            (change["category_id"], _uid()),
        ).fetchone()
        if not category:
            raise BalanceMcpError(
                "A category this change would restore has been deleted. Nothing was undone."
            )
        if category["type"] != txn["type"]:
            raise BalanceMcpError(
                "A category this change would restore is the wrong type. Nothing was undone."
            )
        conn.execute(
            "UPDATE transactions SET category_id = %s, updated_at = now() "
            "WHERE id = %s AND user_id = %s",
            (change["category_id"], change["id"], _uid()),
        )
        restored += 1
    return {"restored": restored, "missing_transactions": missing}


def _undo_import(conn, payload) -> dict:
    removed = 0
    batch_id = payload["batch_id"]
    for chunk in _chunks(payload.get("transaction_ids") or [], 400):
        marks = ",".join(["%s"] * len(chunk))
        cursor = conn.execute(
            f"DELETE FROM transactions WHERE user_id = %s AND import_batch_id = %s "
            f"AND id IN ({marks})",
            [_uid(), batch_id, *chunk],
        )
        removed += cursor.rowcount
    conn.execute(
        "UPDATE import_batches SET status = 'undone' WHERE id = %s AND user_id = %s",
        (batch_id, _uid()),
    )
    return {"removed": removed, "batch_id": batch_id, "format": _undo_format(conn, payload.get("format"))}


def _undo_format(conn, info) -> str:
    if not info:
        return "no format was saved"
    row = conn.execute(
        "SELECT * FROM import_formats WHERE id = %s AND user_id = %s",
        (info["id"], _uid()),
    ).fetchone()
    if row is None:
        return "the saved format was already removed"

    def columns(item):
        return {
            "delimiter": item["delimiter"],
            "date_col": item["date_col"],
            "amount_col": item["amount_col"],
            "store_col": item["store_col"],
            "amount_sign": item["amount_sign"],
        }

    if columns(row) != info["written"]:
        return "the saved format was changed later and was left as it is"
    if info["created"]:
        conn.execute(
            "DELETE FROM import_formats WHERE id = %s AND user_id = %s",
            (info["id"], _uid()),
        )
        return "removed the format this import saved"
    previous = info["previous"]
    conn.execute(
        "UPDATE import_formats SET delimiter = %s, date_col = %s, amount_col = %s, "
        "store_col = %s, amount_sign = %s WHERE id = %s AND user_id = %s",
        (
            previous["delimiter"], previous["date_col"], previous["amount_col"],
            previous["store_col"], previous["amount_sign"], info["id"], _uid(),
        ),
    )
    return "restored the previous column mapping"


def _undo_delete_category(conn, payload) -> dict:
    category_id = payload["category_id"]
    row = conn.execute(
        "SELECT id, name FROM categories WHERE id = %s AND user_id = %s",
        (category_id, _uid()),
    ).fetchone()
    if not row:
        return {"removed": False, "note": "already gone"}
    used = 0
    for table, column in (
        ("transactions", "category_id"),
        ("import_staging", "final_category_id"),
        ("merchant_rules", "category_id"),
    ):
        used += conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = %s AND user_id = %s",
            (category_id, _uid()),
        ).fetchone()["n"]
    if used:
        raise BalanceMcpError(
            "That category is in use, so creating it cannot be undone this way. Nothing was changed."
        )
    conn.execute(
        "DELETE FROM categories WHERE id = %s AND user_id = %s",
        (category_id, _uid()),
    )
    return {"removed": True, "name": row["name"]}


def _undo_rename(conn, payload) -> dict:
    row = conn.execute(
        "SELECT id, name, type FROM categories WHERE id = %s AND user_id = %s",
        (payload["category_id"], _uid()),
    ).fetchone()
    if not row:
        raise BalanceMcpError("That category has been deleted. Nothing was undone.")
    if row["name"] != payload["new_name"]:
        raise BalanceMcpError("That category was renamed again since. Nothing was undone.")
    clash = conn.execute(
        "SELECT id FROM categories WHERE user_id = %s AND type = %s AND name = %s AND id != %s",
        (_uid(), row["type"], payload["name"], row["id"]),
    ).fetchone()
    if clash:
        raise BalanceMcpError(
            "The previous name is now used by another category. Nothing was undone."
        )
    conn.execute(
        "UPDATE categories SET name = %s, updated_at = now() WHERE id = %s AND user_id = %s",
        (payload["name"], row["id"], _uid()),
    )
    return {"id": row["id"], "name": payload["name"]}


def _undo_delete_rule(conn, payload) -> dict:
    cursor = conn.execute(
        "DELETE FROM merchant_rules WHERE id = %s AND user_id = %s",
        (payload["rule_id"], _uid()),
    )
    return {"removed": cursor.rowcount > 0, "rule_id": payload["rule_id"]}


def undo_change(audit_id: int) -> dict:
    _int(audit_id, "audit_id")
    with _write_conn() as conn:
        row = conn.execute(
            "SELECT * FROM mcp_audit WHERE id = %s AND user_id = %s",
            (audit_id, _uid()),
        ).fetchone()
        if not row:
            raise BalanceMcpError("No audit entry with that id.")
        if row["undo_kind"] == "none":
            raise BalanceMcpError("That entry records an undo and cannot itself be undone.")
        if row["undone"]:
            raise BalanceMcpError("That change was already undone.")
        payload = db.load_json(row["undo_payload"]) or {}
        kind = row["undo_kind"]
        if kind == "import_batch":
            result = _undo_import(conn, payload)
        elif kind == "restore_categories":
            result = _restore_categories(conn, payload.get("changes") or [])
        elif kind == "delete_category":
            result = _undo_delete_category(conn, payload)
        elif kind == "restore_category_name":
            result = _undo_rename(conn, payload)
        elif kind == "delete_rule":
            result = _undo_delete_rule(conn, payload)
        else:
            raise BalanceMcpError("That audit entry has no undo. Nothing was changed.")
        conn.execute(
            "UPDATE mcp_audit SET undone = 1, undone_at = datetime('now') "
            "WHERE id = %s AND user_id = %s",
            (audit_id, _uid()),
        )
        undo_audit_id = _record(
            conn, "undo",
            f"Undid {row['action']}: {row['summary']}",
            "none",
            {"audit_id": audit_id},
        )
    return {
        "audit_id": audit_id,
        "undo_audit_id": undo_audit_id,
        "action": row["action"],
        "undone": True,
        "result": result,
    }
