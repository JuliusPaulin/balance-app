"""Accounts, their balances over time, net worth, holdings and the investment import."""

import re
from flask import Blueprint, request, jsonify
from data.schema import db_conn, backup_db
from services import networth
from services import investment_import
from core import current_user_id

bp = Blueprint("net_worth", __name__)


@bp.route("/api/accounts")
def get_accounts():
    uid = current_user_id()
    include_archived = request.args.get("include_archived")
    with db_conn() as conn:
        if include_archived:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE user_id = %s "
                "ORDER BY is_archived, type, sort_order, name",
                (uid,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE user_id = %s AND is_archived = 0 "
                "ORDER BY type, sort_order, name",
                (uid,),
            ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route("/api/accounts", methods=["POST"])
def create_account():
    uid = current_user_id()
    data = request.json or {}
    name = (data.get("name") or "").strip()
    acc_type = data.get("type")
    if not name or acc_type not in ("asset", "liability"):
        return jsonify({"error": "name and type ('asset'|'liability') are required"}), 400
    with db_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO accounts (user_id, name, type, sort_order) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (uid, name, acc_type, int(data.get("sort_order", 0))),
        )
        new_id = cursor.fetchone()["id"]
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id = %s AND user_id = %s",
            (new_id, uid),
        ).fetchone()
    return jsonify(dict(acc)), 201


@bp.route("/api/accounts/<int:account_id>", methods=["PUT"])
def update_account(account_id):
    uid = current_user_id()
    data = request.json or {}
    fields, params = [], []
    if "name" in data:
        fields.append("name = %s"); params.append((data.get("name") or "").strip())
    if "type" in data:
        if data["type"] not in ("asset", "liability"):
            return jsonify({"error": "invalid type"}), 400
        fields.append("type = %s"); params.append(data["type"])
    if "sort_order" in data:
        fields.append("sort_order = %s"); params.append(int(data["sort_order"]))
    if "is_archived" in data:
        fields.append("is_archived = %s"); params.append(1 if data["is_archived"] else 0)
    if not fields:
        return jsonify({"error": "no fields to update"}), 400
    params.extend([account_id, uid])
    with db_conn() as conn:
        conn.execute(
            f"UPDATE accounts SET {', '.join(fields)} WHERE id = %s AND user_id = %s",
            params,
        )
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, uid),
        ).fetchone()
    if not acc:
        return jsonify({"error": "account not found"}), 404
    return jsonify(dict(acc))


@bp.route("/api/accounts/<int:account_id>/close", methods=["POST"])
def close_account(account_id):
    """Close an account you sold or paid off, keeping its history.

    Writes a zero balance at ``as_of`` and marks the account closed. Carry-forward
    then leaves it out of every total from that date on, while earlier months
    still count what you held. Deleting the account instead would erase the past.
    """
    uid = current_user_id()
    data = request.json or {}
    as_of = (data.get("as_of") or "").strip()
    if not _DATE_RE.match(as_of):
        return jsonify({"error": "as_of must be YYYY-MM-DD"}), 400
    with db_conn() as conn:
        if not conn.execute(
            "SELECT 1 FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, uid),
        ).fetchone():
            return jsonify({"error": "account not found"}), 404
        conn.execute(
            """
            INSERT INTO account_balances (user_id, account_id, as_of, balance)
            VALUES (%s, %s, %s, 0)
            ON CONFLICT (account_id, as_of) DO UPDATE SET balance = excluded.balance
            """,
            (uid, account_id, as_of),
        )
        conn.execute(
            "UPDATE accounts SET is_archived = 1 WHERE id = %s AND user_id = %s",
            (account_id, uid),
        )
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, uid),
        ).fetchone()
    return jsonify(dict(acc))


@bp.route("/api/accounts/<int:account_id>/reopen", methods=["POST"])
def reopen_account(account_id):
    """Undo a close: the account is listed again. Its zero balance stays, so the
    total only moves once a new balance is entered."""
    uid = current_user_id()
    with db_conn() as conn:
        cursor = conn.execute(
            "UPDATE accounts SET is_archived = 0 WHERE id = %s AND user_id = %s",
            (account_id, uid),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "account not found"}), 404
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, uid),
        ).fetchone()
    return jsonify(dict(acc))


@bp.route("/api/accounts/<int:account_id>", methods=["DELETE"])
def delete_account(account_id):
    uid = current_user_id()
    # account_balances rows cascade-delete (FK ON DELETE CASCADE).
    with db_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, uid),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "account not found"}), 404
    return "", 204


@bp.route("/api/accounts/<int:account_id>/balances")
def get_balances(account_id):
    uid = current_user_id()
    with db_conn() as conn:
        # Confirm the account belongs to this user before returning balances.
        if not conn.execute(
            "SELECT 1 FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, uid),
        ).fetchone():
            return jsonify({"error": "account not found"}), 404
        rows = conn.execute(
            "SELECT * FROM account_balances "
            "WHERE account_id = %s AND user_id = %s ORDER BY as_of",
            (account_id, uid),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route("/api/accounts/<int:account_id>/balances", methods=["POST"])
def set_balance(account_id):
    uid = current_user_id()
    data = request.json or {}
    as_of = (data.get("as_of") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", as_of):
        return jsonify({"error": "as_of must be YYYY-MM-DD"}), 400
    try:
        balance = float(data["balance"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "balance must be a number"}), 400
    with db_conn() as conn:
        if not conn.execute(
            "SELECT 1 FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, uid),
        ).fetchone():
            return jsonify({"error": "account not found"}), 404
        conn.execute(
            """
            INSERT INTO account_balances (user_id, account_id, as_of, balance)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (account_id, as_of) DO UPDATE SET balance = excluded.balance
            """,
            (uid, account_id, as_of, balance),
        )
        row = conn.execute(
            "SELECT * FROM account_balances "
            "WHERE account_id = %s AND as_of = %s AND user_id = %s",
            (account_id, as_of, uid),
        ).fetchone()
    return jsonify(dict(row)), 201


@bp.route("/api/balances/<int:balance_id>", methods=["DELETE"])
def delete_balance(balance_id):
    uid = current_user_id()
    with db_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM account_balances WHERE id = %s AND user_id = %s",
            (balance_id, uid),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "balance not found"}), 404
    return "", 204


@bp.route("/api/networth/history")
def networth_history():
    uid = current_user_id()
    months = request.args.get("months", default=12, type=int) or 12
    with db_conn() as conn:
        series = networth.compute_history(conn, uid, months=months)
    return jsonify({"series": series})


@bp.route("/api/networth/summary")
def networth_summary():
    uid = current_user_id()
    with db_conn() as conn:
        result = networth.summary(conn, uid)
    return jsonify(result)


# ── Investment holdings + import (CSV / xlsx → Net Worth) ──────────────

@bp.route("/api/networth/holdings")
def networth_holdings():
    """Latest (or a specific as_of) holdings for one account, for the drill-down."""
    uid = current_user_id()
    account_id = request.args.get("account_id", type=int)
    if not account_id:
        return jsonify({"error": "account_id is required"}), 400
    as_of = (request.args.get("as_of") or "").strip()
    with db_conn() as conn:
        # The account must belong to this user before exposing its holdings.
        owns = conn.execute(
            "SELECT 1 FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, uid),
        ).fetchone()
        if not owns:
            return jsonify({"error": "account not found"}), 404
        if not as_of:
            row = conn.execute(
                "SELECT MAX(as_of) AS m FROM holdings WHERE account_id = %s",
                (account_id,),
            ).fetchone()
            as_of = row["m"] if row else None
        if not as_of:
            return jsonify({"account_id": account_id, "as_of": None, "holdings": []})
        rows = conn.execute(
            """
            SELECT id, name, isin, units, value_eur, return_pct, return_eur, currency
            FROM holdings
            WHERE account_id = %s AND as_of = %s
            ORDER BY value_eur DESC, name
            """,
            (account_id, as_of),
        ).fetchall()
    return jsonify({
        "account_id": account_id,
        "as_of": as_of,
        "holdings": [dict(r) for r in rows],
    })


@bp.route("/api/networth/holdings/<int:holding_id>", methods=["DELETE"])
def delete_holding(holding_id):
    """Remove one holding from a snapshot — the "I sold this" action.

    The account total for that snapshot date was written as the sum of its
    holdings, so it is recomputed here. Earlier snapshots keep the holding, so
    the net-worth history stays true to what was held at the time.
    """
    uid = current_user_id()
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT h.account_id, h.as_of
            FROM holdings h
            JOIN accounts a ON a.id = h.account_id
            WHERE h.id = %s AND a.user_id = %s
            """,
            (holding_id, uid),
        ).fetchone()
        if not row:
            return jsonify({"error": "holding not found"}), 404
        account_id, as_of = row["account_id"], row["as_of"]
        conn.execute("DELETE FROM holdings WHERE id = %s", (holding_id,))
        agg = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(value_eur), 0) AS t "
            "FROM holdings WHERE account_id = %s AND as_of = %s",
            (account_id, as_of),
        ).fetchone()
        total = round(agg["t"], 2)
        # Only touch a balance that already exists for this date — never invent
        # one, or deleting a holding from an old snapshot would add a data point.
        if conn.execute(
            "SELECT 1 FROM account_balances WHERE account_id = %s AND as_of = %s",
            (account_id, as_of),
        ).fetchone():
            conn.execute(
                "UPDATE account_balances SET balance = %s "
                "WHERE account_id = %s AND as_of = %s",
                (total, account_id, as_of),
            )
    return jsonify({
        "account_id": account_id,
        "as_of": as_of,
        "holdings_left": agg["c"],
        "total": total,
    })


def _match_existing_account(conn, uid, acct):
    """Suggest an existing accounts row a parsed account could map onto (user-scoped).

    Order: exact external_id, then (cash only) IBAN parsed from the label, then a
    case-insensitive name match. Returns (account_id, by) or (None, None). Never
    auto-merges — only a dedupe *suggestion* for the review UI.
    """
    row = conn.execute(
        "SELECT id FROM accounts WHERE external_id = %s AND user_id = %s",
        (acct.external_id, uid),
    ).fetchone()
    if row:
        return row["id"], "external_id"

    if acct.kind == "cash":
        m = re.search(r"\b([A-Z]{2}\d{2}[\d ]{6,})", acct.label or "")
        if m:
            iban = re.sub(r"\s", "", m.group(1))
            row = conn.execute(
                "SELECT id FROM accounts WHERE external_id LIKE %s AND user_id = %s",
                (f"%{iban}%", uid),
            ).fetchone()
            if row:
                return row["id"], "iban"

    label = (acct.label or "").strip()
    if label:
        lead = label.split()[0] if label.split() else label
        row = conn.execute(
            "SELECT id FROM accounts WHERE user_id = %s AND "
            "(LOWER(name) = LOWER(%s) OR LOWER(name) = LOWER(%s)) LIMIT 1",
            (uid, label, lead),
        ).fetchone()
        if row:
            return row["id"], "name"
    return None, None


def _account_to_dict(acct, match_id=None, match_by=None):
    return {
        "broker": acct.broker,
        "label": acct.label,
        "external_id": acct.external_id,
        "kind": acct.kind,
        "total_eur": acct.total_eur,
        "holdings": [
            {
                "name": h.name,
                "units": h.units,
                "value_eur": h.value_eur,
                "return_pct": h.return_pct,
                "return_eur": h.return_eur,
                "isin": h.isin,
                "currency": h.currency,
            }
            for h in acct.holdings
        ],
        "match": {"existing_account_id": match_id, "by": match_by},
    }


@bp.route("/api/networth/import-investments/preview", methods=["POST"])
def import_investments_preview():
    """Parse 1+ uploaded broker exports and return the hierarchy + dedupe hints.

    NO DB writes. Each file is parsed independently; an unreadable/unknown file
    yields a 400 naming the file and why.
    """
    uid = current_user_id()
    files = request.files.getlist("files") or request.files.getlist("file")
    if not files:
        return jsonify({"error": "No files provided"}), 400

    out_files = []
    with db_conn() as conn:
        for f in files:
            filename = f.filename or "(unnamed)"
            try:
                parsed = investment_import.detect_and_parse(filename, f.read())
            except investment_import.ImportError_ as e:
                return jsonify({"error": f"Could not read {filename}: {e}",
                                "filename": filename}), 400
            except Exception as e:
                return jsonify({"error": f"Could not read {filename}: {e}",
                                "filename": filename}), 400

            accounts = []
            for acct in parsed.accounts:
                match_id, match_by = _match_existing_account(conn, uid, acct)
                accounts.append(_account_to_dict(acct, match_id, match_by))

            out_files.append({
                "filename": filename,
                "source": parsed.source,
                "as_of": parsed.as_of,
                "warnings": parsed.warnings,
                "accounts": accounts,
            })

    return jsonify({"files": out_files})


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@bp.route("/api/networth/import-investments/confirm", methods=["POST"])
def import_investments_confirm():
    """Write the user-reviewed selection: upsert accounts, holdings, balances.

    For each included account, resolve / create the target accounts row (scoped to
    the local user), replace/union its holdings for (account_id, as_of), and upsert
    the account total into account_balances. Idempotent on (account_id, as_of).
    """
    uid = current_user_id()
    data = request.json or {}
    incoming = data.get("accounts") or []
    selected = [a for a in incoming if a.get("include", True)]
    if not selected:
        return jsonify({"error": "No accounts selected to import"}), 400

    for a in selected:
        as_of = (a.get("as_of") or "").strip()
        if not _DATE_RE.match(as_of):
            label = a.get("name") or a.get("external_id") or "(account)"
            return jsonify({"error": f"{label}: as_of must be YYYY-MM-DD"}), 400

    # Merge selected accounts that resolve to the same target + as_of so multiple
    # files for one account UNION their holdings instead of overwriting.
    _merged, _order = {}, []
    for a in selected:
        key = (
            a.get("target_account_id") or (a.get("external_id") or "").strip() or id(a),
            (a.get("as_of") or "").strip(),
        )
        if key not in _merged:
            m = dict(a)
            m["holdings"] = list(a.get("holdings") or [])
            _merged[key] = m
            _order.append(key)
        elif a.get("kind") != "cash":
            _merged[key]["holdings"].extend(a.get("holdings") or [])
            _merged[key]["group_name"] = _merged[key].get("group_name") or a.get("group_name")
    selected = [_merged[k] for k in _order]

    backup_db("invest-import")

    results = []
    as_of_seen = None
    with db_conn() as conn:
        for a in selected:
            as_of = a["as_of"].strip()
            as_of_seen = as_of
            external_id = (a.get("external_id") or "").strip() or None
            name = (a.get("name") or external_id or "Investment").strip()
            group_name = (a.get("group_name") or "").strip() or None
            kind = a.get("kind") or "investment"
            acc_type = a.get("type") or "asset"
            if acc_type not in ("asset", "liability"):
                acc_type = "asset"
            target_id = a.get("target_account_id")
            holdings = a.get("holdings") or []

            # ── Resolve the target accounts row (user-scoped) ────────────
            if target_id:
                row = conn.execute(
                    "SELECT id FROM accounts WHERE id = %s AND user_id = %s",
                    (target_id, uid),
                ).fetchone()
                if not row:
                    return jsonify({"error": f"target_account_id {target_id} not found"}), 400
                account_id = row["id"]
                conn.execute(
                    "UPDATE accounts SET external_id = COALESCE(%s, external_id), "
                    "group_name = COALESCE(%s, group_name) WHERE id = %s AND user_id = %s",
                    (external_id, group_name, account_id, uid),
                )
                matched = "adopted"
            else:
                row = (
                    conn.execute(
                        "SELECT id FROM accounts WHERE external_id = %s AND user_id = %s",
                        (external_id, uid),
                    ).fetchone()
                    if external_id
                    else None
                )
                if row:
                    account_id = row["id"]
                    conn.execute(
                        "UPDATE accounts SET group_name = COALESCE(%s, group_name) "
                        "WHERE id = %s AND user_id = %s",
                        (group_name, account_id, uid),
                    )
                    matched = "existing"
                else:
                    cursor = conn.execute(
                        "INSERT INTO accounts (user_id, name, type, external_id, group_name, is_archived) "
                        "VALUES (%s, %s, %s, %s, %s, 0) RETURNING id",
                        (uid, name, acc_type, external_id, group_name),
                    )
                    account_id = cursor.fetchone()["id"]
                    matched = "created"

            # ── Holdings + total ─────────────────────────────────────────
            if kind == "cash":
                try:
                    total = round(float(a.get("total_eur", 0) or 0), 2)
                except (TypeError, ValueError):
                    total = 0.0
                conn.execute(
                    "DELETE FROM holdings WHERE account_id = %s AND as_of = %s",
                    (account_id, as_of),
                )
                holdings_count = 0
            else:
                seen_names = set()
                for h in holdings:
                    hname = (h.get("name") or "").strip()
                    if not hname or hname in seen_names:
                        continue
                    try:
                        value_eur = float(h.get("value_eur", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    seen_names.add(hname)

                    def _num(v):
                        try:
                            return float(v) if v is not None and v != "" else None
                        except (TypeError, ValueError):
                            return None

                    conn.execute(
                        """
                        INSERT INTO holdings
                          (account_id, as_of, name, isin, units, value_eur,
                           return_pct, return_eur, currency)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT(account_id, as_of, name) DO UPDATE SET
                          isin = excluded.isin, units = excluded.units,
                          value_eur = excluded.value_eur, return_pct = excluded.return_pct,
                          return_eur = excluded.return_eur, currency = excluded.currency
                        """,
                        (
                            account_id, as_of, hname,
                            (h.get("isin") or None),
                            _num(h.get("units")),
                            value_eur,
                            _num(h.get("return_pct")),
                            _num(h.get("return_eur")),
                            (h.get("currency") or None),
                        ),
                    )
                agg = conn.execute(
                    "SELECT COUNT(*) AS c, COALESCE(SUM(value_eur), 0) AS t "
                    "FROM holdings WHERE account_id = %s AND as_of = %s",
                    (account_id, as_of),
                ).fetchone()
                holdings_count = agg["c"]
                total = round(agg["t"], 2)

            # ── Account total → account_balances (carry-forward source) ──
            conn.execute(
                """
                INSERT INTO account_balances (user_id, account_id, as_of, balance)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(account_id, as_of) DO UPDATE SET balance = excluded.balance
                """,
                (uid, account_id, as_of, total),
            )

            acc_row = conn.execute(
                "SELECT name, group_name FROM accounts WHERE id = %s", (account_id,)
            ).fetchone()
            results.append({
                "id": account_id,
                "name": acc_row["name"],
                "group_name": acc_row["group_name"],
                "kind": kind,
                "total": total,
                "holdings_count": holdings_count,
                "matched": matched,
            })

    return jsonify({
        "updated": len(results),
        "as_of": as_of_seen,
        "accounts": results,
    })
