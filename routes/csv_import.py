"""CSV import: parse a statement, stage it for review, confirm it."""

import csv
import io
import re
import difflib
import hashlib
from datetime import date
from dateutil import parser as date_parser
from flask import Blueprint, request, jsonify
import db
from database import get_db, db_conn, backup_db
from core import limiter, current_user_id, bump_data_version
from routes.merchant_rules import _rebuild_merchant_rules

bp = Blueprint("csv_import", __name__)


def parse_date(date_str):
    """Parse a date string into ISO 'YYYY-MM-DD'.

    The three supported export formats are matched explicitly first so we never
    fall into dateutil's ambiguous month/day guessing:
      * YYYY-MM-DD and YYYY/MM/DD (ISO and Finnish bank statement)
      * D.M.YYYY (Nordea Platinum / Finnish dot format)
    Only genuinely unrecognized formats reach the dateutil fallback, which uses
    dayfirst=True because all inputs here are European (day-first) dates.
    """
    if not date_str:
        return None
    s = date_str.strip()

    # YYYY-MM-DD or YYYY/MM/DD — unambiguous (4-digit year leads), parse directly
    m = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$', s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # DD.MM.YYYY or D.M.YYYY — Finnish dot format (day first, 4-digit year)
    m = re.match(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Fallback: dateutil with explicit dayfirst for any other European format
    try:
        return date_parser.parse(s, dayfirst=True).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OverflowError):
        return None


def parse_amount(amount_str):
    """Parse a monetary amount string into a positive float (sign is dropped).

    Handles the separator conventions seen in the supported exports:
      * fi-FI decimal comma with optional space/dot thousands: "1 234,56", "1.234,56"
      * en-US decimal dot with comma thousands: "1,234.56"
      * plain integers/decimals: "1234", "12.50"
    Non-breaking and thin spaces (common fi-FI thousands separators) are stripped
    alongside ASCII spaces so they don't break parsing.
    """
    if not amount_str:
        return None
    # Strip currency symbols and all whitespace variants (ASCII, NBSP, thin space)
    cleaned = re.sub(r"[€$£¥\s]", "", amount_str.strip())

    if "," in cleaned and "." in cleaned:
        # Both present: the right-most separator is the decimal mark.
        if cleaned.rindex(",") > cleaned.rindex("."):
            # fi-FI: "." groups thousands, "," is decimal
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            # en-US: "," groups thousands, "." is decimal
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts) == 2 and len(parts[1]) <= 2:
            # Single comma with ≤2 trailing digits → decimal comma
            cleaned = cleaned.replace(",", ".")
        else:
            # Multiple commas or long trailing group → thousands separators
            cleaned = cleaned.replace(",", "")
    elif cleaned.count(".") > 1:
        # Multiple dots with no comma → "." is a thousands separator (e.g. "1.234.567")
        cleaned = cleaned.replace(".", "")

    try:
        return abs(float(cleaned))
    except ValueError:
        return None


# How much of a store's history must agree before the past is allowed to speak
# for it. Matches the threshold scripts/generate_merchant_rules.py uses when
# deciding whether a store deserves a rule at all.
_HISTORY_CONFIDENCE = 0.70


def suggest_category(store_name, conn, user_id, txn_type=None):
    """Name of the category to suggest for ``store_name``, or None.

    ``txn_type`` is the expense/income the row was read as, from the sign on the
    statement. When given, only categories of that type can be suggested.

    That restriction is the point, not a detail. A suggestion is a bare category
    *name*, and two names exist on both sides — "Other" (the default for a store
    nothing is known about) and "Investments". A merchant rule or a store's
    history is type-blind, so an income row could come back suggested "Other"
    and the review table, resolving that name against a list where the expense
    side sorts first, would adopt the expense "Other" — and with it the expense
    type, silently overturning the sign read off the statement. A refund from a
    grocer landed as spending; a card payment to a store whose rule points at an
    income category landed as earnings.

    ``POST /api/merchant-rules/<id>/apply`` has always refused to re-categorise
    rows of the other type for the same reason. This is that rule, applied where
    the category is first proposed.
    """
    if not store_name:
        return None

    store_lower = store_name.strip().lower()

    type_clause = "AND c.type = %s" if txn_type else ""
    type_args = (txn_type,) if txn_type else ()

    # Check merchant rules first: exact → contains → smart. Scoped to the user.
    rules = conn.execute("""
        SELECT mr.match_type, mr.pattern, c.name as category_name
        FROM merchant_rules mr
        JOIN categories c ON mr.category_id = c.id
        WHERE mr.user_id = %s {type_clause}
        ORDER BY CASE mr.match_type WHEN 'exact' THEN 1 WHEN 'contains' THEN 2 ELSE 3 END
    """.format(type_clause=type_clause), (user_id,) + type_args).fetchall()

    smart_candidates = []
    for rule in rules:
        pattern_lower = rule["pattern"].lower()
        mt = rule["match_type"]
        if mt == "exact" and store_lower == pattern_lower:
            return rule["category_name"]
        elif mt == "contains" and pattern_lower in store_lower:
            return rule["category_name"]
        elif mt == "smart":
            ratio = difflib.SequenceMatcher(None, store_lower, pattern_lower).ratio()
            if ratio >= 0.72:
                smart_candidates.append((ratio, rule["category_name"]))

    if smart_candidates:
        smart_candidates.sort(reverse=True)
        return smart_candidates[0][1]

    # Fall back to past transactions with same store (case-insensitive match;
    # Postgres '=' is case-sensitive, so compare lowered values to preserve the
    # original SQLite LOWER(...) = LOWER(...) behaviour).
    #
    # Held to the same bar as the rule generator, which skips a store whose
    # history does not agree with itself 70% of the time. Without it the two
    # halves of one feature disagreed: a store too ambiguous to earn a rule
    # still got a confident-looking suggestion here, from a bare plurality.
    # Two of four is a coin toss, and the review table draws it exactly like a
    # hand-written rule. No suggestion sends the row to "needs review", which
    # is the truth.
    rows = conn.execute("""
        SELECT c.name, COUNT(*) AS n FROM transactions t
        JOIN categories c ON t.category_id = c.id
        WHERE t.user_id = %s AND LOWER(t.store) = LOWER(%s) {type_clause}
        GROUP BY c.id, c.name
        ORDER BY n DESC
    """.format(type_clause=type_clause), (user_id, store_name.strip()) + type_args).fetchall()
    if not rows:
        return None
    total = sum(r["n"] for r in rows)
    top = rows[0]
    return top["name"] if total and top["n"] / total >= _HISTORY_CONFIDENCE else None


COLUMN_ALIASES = {
    "date": ["date", "datum", "päivä", "pvm", "transaction date", "trans date", "booking date",
             "date of payment", "tapahtumapäivä", "kirjauspäivä"],
    "store": ["store", "merchant", "description", "payee", "kauppa", "saaja", "memo", "name",
              "recipient", "location of purchase", "otsikko", "nimi"],
    "category": ["category", "kategoria", "luokka", "type", "group"],
    "amount": ["amount", "sum", "summa", "määrä", "value", "total", "debit"],
    "message": ["viesti", "message", "note", "memo2"],
    # Payment reference. Only read to tell a card purchase from a transfer when
    # deciding whether the message column may stand in as the store name.
    "reference": ["viitenumero", "reference", "reference number", "arkistointitunnus"],
}


def detect_columns(headers):
    mapping = {}
    normalized = [h.strip().lower() for h in headers]
    for field, aliases in COLUMN_ALIASES.items():
        for i, header in enumerate(normalized):
            if header in aliases:
                mapping[field] = i
                break
    return mapping


def _detect_delimiter(content):
    """Pick the most likely CSV delimiter from the header line.

    Counts ';' vs ',' in the first non-empty line. Ties (or a header with no
    commas) resolve to ';' because two of the three supported export formats
    (Finnish bank statement, Nordea Platinum) are semicolon-delimited.
    """
    first_line = ""
    for line in content.splitlines():
        if line.strip():
            first_line = line
            break
    semis = first_line.count(";")
    commas = first_line.count(",")
    if semis == 0 and commas == 0:
        return ";"
    return ";" if semis >= commas else ","


# Finnair credit-card exports are comma-delimited and carry the purchase-currency
# amount in a generic column, while the EUR amount lives in a fixed column.
# Per CLAUDE.md the EUR amount is column index 8. Detect the format by its
# signature headers so multi-currency rows import with the correct EUR amount.
FINNAIR_EUR_AMOUNT_COL = 8


FINNAIR_SIGNATURE_HEADERS = {"date of payment", "location of purchase"}


def _is_finnair_format(headers):
    normalized = {h.strip().lower() for h in headers}
    return FINNAIR_SIGNATURE_HEADERS.issubset(normalized)


def _decode_csv_bytes(raw):
    """Decode uploaded CSV bytes, trying UTF-8 (BOM-aware) then common fallbacks.

    Raises UnicodeDecodeError only if none of the candidate encodings work.
    """
    last_error = None
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError as e:
            last_error = e
    # latin-1 decodes any byte string, so this is effectively unreachable,
    # but re-raise to be explicit if the candidate list ever changes.
    raise last_error


# Card rows in Nordea exports put the terminal receipt line in Viesti, e.g.
# "EUR          10,76 CORK" or "SEK          15,00 Solna". It starts with a
# 3-letter currency code and an amount, and is never a merchant name.
CARD_DETAIL_RE = re.compile(r"^[A-Z]{3}\s+[\d\s.,]*\d[.,]\d{2}\b")


# Boilerplate a bank writes into Viesti in place of a real message.
VIESTI_BOILERPLATE = {
    "tiedot mobilepay-sovelluksessa",
}


def _viesti_as_store(viesti):
    """Return viesti string as the store name if it looks like a real merchant name."""
    if not viesti or not viesti.strip():
        return None
    v = viesti.strip()
    if len(v) > 50:
        return None
    if v[0].isdigit():          # reference numbers start with digits
        return None
    if re.match(r"^[A-Z]{2}\d{2}", v):  # IBAN pattern
        return None
    if CARD_DETAIL_RE.match(v):  # card receipt line, not a merchant
        return None
    if v.lower() in VIESTI_BOILERPLATE:
        return None
    return v


def format_signature(headers, delimiter):
    """Stable fingerprint of a CSV layout (header names + delimiter).

    Lowercases and trims each header, joins with '|', appends the delimiter, and
    SHA-1s the result. The same bank export (same header row + delimiter) always
    yields the same signature, so a learned column mapping can be looked up by it.
    """
    joined = "|".join(h.strip().lower() for h in headers)
    return hashlib.sha1((joined + "||" + delimiter).encode("utf-8")).hexdigest()


def _read_headers(reader):
    """Read + normalize the header row from a csv.reader (drop trailing empties)."""
    headers = next(reader, None)
    if not headers:
        return None
    while headers and not headers[-1].strip():
        headers = headers[:-1]
    return headers


def _stage_rows(conn, uid, batch_id, reader, col_map, amount_sign="neg_expense"):
    """Parse the remaining rows of ``reader`` and INSERT them into import_staging.

    ``col_map`` maps field name -> column index and may contain: ``date`` and
    ``amount`` (required), and optionally ``store``, ``message``, ``category``.

    ``amount_sign`` controls the expense/income convention:
      * ``'neg_expense'`` (default, the historical behaviour): a leading '-' means
        expense, otherwise income.
      * ``'pos_expense'``: a positive amount means expense, a leading '-' income.

    Returns the number of rows staged.
    """
    staged = 0
    for row in reader:
        # Drop trailing empty cells
        while row and not row[-1].strip():
            row = row[:-1]

        if not row or all(c.strip() == "" for c in row):
            continue

        if col_map["date"] >= len(row):
            continue
        parsed_date = parse_date(row[col_map["date"]])
        if not parsed_date:
            continue

        raw_amount = row[col_map["amount"]].strip() if col_map["amount"] < len(row) else ""
        is_negative = raw_amount.startswith("-")
        if amount_sign == "pos_expense":
            txn_type = "income" if is_negative else "expense"
        else:
            txn_type = "expense" if is_negative else "income"
        amount = parse_amount(raw_amount)
        if amount is None or amount == 0:
            continue

        store = row[col_map["store"]].strip() if "store" in col_map and col_map["store"] is not None and col_map["store"] < len(row) else ""

        # Use viesti as store override when it's a meaningful merchant name.
        # Skip it on rows that carry a payment reference: those are card
        # purchases and direct debits, where the bank writes the receipt line
        # or the city into viesti and the real merchant sits in the name
        # column. Transfers have no reference, and there viesti is the message
        # the sender typed ("Palkka"), which names the transaction better.
        has_reference = (
            "reference" in col_map
            and col_map["reference"] is not None
            and col_map["reference"] < len(row)
            and row[col_map["reference"]].strip() != ""
        )
        if (not has_reference and "message" in col_map
                and col_map["message"] is not None and col_map["message"] < len(row)):
            override = _viesti_as_store(row[col_map["message"]])
            if override:
                store = override

        csv_category = row[col_map["category"]].strip() if "category" in col_map and col_map["category"] is not None and col_map["category"] < len(row) else ""
        # Scoped to the type the sign said this row is: a suggestion must never
        # be what overturns it. See suggest_category.
        suggested = suggest_category(store, conn, uid, txn_type) or csv_category or None

        conn.execute("""
            INSERT INTO import_staging (user_id, date, store, suggested_category, amount, type, import_batch_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (uid, parsed_date, store, suggested, amount, txn_type, batch_id))
        staged += 1
    return staged


def _staging_response(conn, uid, batch_id):
    """Build the standard {batch_id, count, items} response for a staged batch."""
    rows = conn.execute(
        "SELECT * FROM import_staging "
        "WHERE import_batch_id = %s AND user_id = %s ORDER BY date DESC",
        (batch_id, uid),
    ).fetchall()
    return jsonify({
        "batch_id": batch_id,
        "count": len(rows),
        "items": [dict(r) for r in rows],
    })


@bp.route("/api/import/upload", methods=["POST"])
@limiter.limit("30/hour")
def upload_csv():
    uid = current_user_id()
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    filename = file.filename or ""
    if not filename.lower().endswith(".csv"):
        return jsonify({"error": "File must be CSV"}), 400

    backup_db("pre-import")
    conn = get_db()
    batch_id = None
    try:
        # Decode first so an encoding problem fails before we create a batch.
        try:
            content = _decode_csv_bytes(file.read())
        except UnicodeDecodeError:
            return jsonify({
                "error": "Could not decode CSV file. Please save it as UTF-8."
            }), 400

        delimiter = _detect_delimiter(content)
        reader = csv.reader(io.StringIO(content), delimiter=delimiter)

        headers = _read_headers(reader)
        if not headers:
            return jsonify({"error": "Empty CSV file"}), 400

        col_map = detect_columns(headers)

        # Finnair multi-currency rows carry their EUR amount in a fixed column;
        # honor that instead of the generic 'amount' alias (which may be the
        # purchase-currency amount).
        if _is_finnair_format(headers):
            col_map["amount"] = FINNAIR_EUR_AMOUNT_COL

        amount_sign = "neg_expense"

        # If the auto-detector can't find the required columns, fall back to a
        # format this user has previously taught us (keyed by layout signature).
        if "date" not in col_map or "amount" not in col_map:
            signature = format_signature(headers, delimiter)
            learned = conn.execute(
                "SELECT * FROM import_formats WHERE user_id = %s AND signature = %s",
                (uid, signature),
            ).fetchone()
            if learned:
                col_map = {"date": learned["date_col"], "amount": learned["amount_col"]}
                if learned["store_col"] is not None:
                    col_map["store"] = learned["store_col"]
                amount_sign = learned["amount_sign"]
            else:
                # Still unmapped: don't 400 and don't create an orphan batch.
                # Return a mapping preview so the UI can ask the user to map columns.
                sample_rows = []
                for row in reader:
                    while row and not row[-1].strip():
                        row = row[:-1]
                    if not row or all(c.strip() == "" for c in row):
                        continue
                    sample_rows.append(row)
                    if len(sample_rows) >= 5:
                        break
                guess = detect_columns(headers)
                return jsonify({
                    "needs_mapping": True,
                    "signature": signature,
                    "delimiter": delimiter,
                    "headers": headers,
                    "sample_rows": sample_rows,
                    "guess": {
                        "date": guess.get("date"),
                        "amount": guess.get("amount"),
                        "store": guess.get("store"),
                    },
                })

        # We can parse — create the batch now and stage every row.
        cursor = conn.execute(
            "INSERT INTO import_batches (user_id, filename) VALUES (%s, %s) RETURNING id",
            (uid, filename),
        )
        batch_id = cursor.fetchone()["id"]

        _stage_rows(conn, uid, batch_id, reader, col_map, amount_sign)
        conn.commit()

        return _staging_response(conn, uid, batch_id)
    except Exception as e:
        # Roll back the in-flight transaction, then remove any partially-created
        # batch + staging rows so a failed import leaves no half-written state.
        conn.rollback()
        if batch_id is not None:
            try:
                _cleanup_import_batch(conn, batch_id, uid)
                conn.commit()
            except db.DatabaseError:
                conn.rollback()
        status = 400 if isinstance(e, (csv.Error, ValueError)) else 500
        return jsonify({"error": f"Import failed: {e}"}), status
    finally:
        conn.close()


@bp.route("/api/import/batches", methods=["GET"])
def list_import_batches():
    """Past imports, newest first — what came in, when, and from which file.

    ``import_batches`` was written to by three code paths and read by none, so
    an abandoned review vanished with nowhere to resume from and a finished one
    left no record of what it had brought in. This is that reader.

    A pending batch reports its staged rows; a completed one reports the
    transactions it created. An import from before ``import_batch_id`` existed
    reports nothing to undo, because there is no honest way to tell which rows
    were its.
    """
    uid = current_user_id()
    with db_conn() as conn:
        rows = conn.execute("""
            SELECT b.id, b.filename, b.imported_at, b.status,
                   (SELECT COUNT(*) FROM import_staging s
                     WHERE s.import_batch_id = b.id AND s.user_id = b.user_id) AS staged,
                   (SELECT COUNT(*) FROM transactions t
                     WHERE t.import_batch_id = b.id AND t.user_id = b.user_id) AS imported,
                   (SELECT COALESCE(SUM(t.amount), 0) FROM transactions t
                     WHERE t.import_batch_id = b.id AND t.user_id = b.user_id
                       AND t.type = 'expense') AS sum_expense,
                   (SELECT COALESCE(SUM(t.amount), 0) FROM transactions t
                     WHERE t.import_batch_id = b.id AND t.user_id = b.user_id
                       AND t.type = 'income') AS sum_income
            FROM import_batches b
            WHERE b.user_id = %s
            ORDER BY b.imported_at DESC, b.id DESC
            LIMIT 50
        """, (uid,)).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route("/api/import/batch/<int:batch_id>/undo", methods=["POST"])
def undo_import_batch(batch_id):
    """Delete the transactions one confirmed import created.

    An import writes hundreds of rows at once and the app has no other bulk
    undo, so taking one back a row at a time is not a real option. The batch
    record survives, marked ``undone``, because the history should say what
    happened rather than pretend the import never did.
    """
    uid = current_user_id()
    with db_conn() as conn:
        batch = conn.execute(
            "SELECT status FROM import_batches WHERE id = %s AND user_id = %s",
            (batch_id, uid),
        ).fetchone()
        if not batch:
            return jsonify({"error": "No such import"}), 404
        if batch["status"] != "completed":
            return jsonify({"error": "That import was never confirmed"}), 409

        n = conn.execute(
            "SELECT COUNT(*) AS n FROM transactions "
            "WHERE import_batch_id = %s AND user_id = %s",
            (batch_id, uid),
        ).fetchone()["n"]
        if not n:
            # Imported before the batch link existed. Say so rather than
            # reporting a successful undo that removed nothing.
            return jsonify({"error": "This import is too old to undo — its "
                                     "transactions aren't linked to it"}), 409

        conn.execute(
            "DELETE FROM transactions WHERE import_batch_id = %s AND user_id = %s",
            (batch_id, uid),
        )
        conn.execute(
            "UPDATE import_batches SET status = 'undone' WHERE id = %s AND user_id = %s",
            (batch_id, uid),
        )
    bump_data_version()
    return jsonify({"status": "undone", "removed": n})


@bp.route("/api/import/batch/<int:batch_id>", methods=["DELETE"])
def discard_import_batch(batch_id):
    """Throw away an unconfirmed batch and its staged rows.

    Cancelling a review used to reset the screen and nothing else, so the batch
    and its staging rows stayed in the database with no way to see or resume
    them — they simply accumulated. A confirmed batch is history and is left
    alone; only a pending one can be discarded.
    """
    uid = current_user_id()
    with db_conn() as conn:
        batch = conn.execute(
            "SELECT status FROM import_batches WHERE id = %s AND user_id = %s",
            (batch_id, uid),
        ).fetchone()
        if not batch:
            return jsonify({"error": "No such import"}), 404
        if batch["status"] == "completed":
            return jsonify({"error": "That import was already confirmed"}), 409
        _cleanup_import_batch(conn, batch_id, uid)
    return jsonify({"status": "discarded"})


def _cleanup_import_batch(conn, batch_id, user_id):
    """Remove staging rows and the batch record for a failed/empty import."""
    conn.execute(
        "DELETE FROM import_staging WHERE import_batch_id = %s AND user_id = %s",
        (batch_id, user_id),
    )
    conn.execute(
        "DELETE FROM import_batches WHERE id = %s AND user_id = %s",
        (batch_id, user_id),
    )


def _parse_col_index(value, required=False):
    """Coerce a form field to a non-negative int column index.

    Blank/missing → None (only allowed when ``required`` is False). Returns
    ``(index_or_None, error_or_None)``.
    """
    if value is None or str(value).strip() == "":
        if required:
            return None, "missing required column index"
        return None, None
    try:
        idx = int(value)
    except (TypeError, ValueError):
        return None, "column index must be an integer"
    if idx < 0:
        return None, "column index out of range"
    return idx, None


@bp.route("/api/import/upload-mapped", methods=["POST"])
@limiter.limit("30/hour")
def upload_mapped_csv():
    """Import a CSV using a user-supplied column mapping (the "learn" path).

    Accepts the re-uploaded file (multipart, same field name as /upload) plus
    form fields:
      * date_col      (required int)  — column index of the date
      * amount_col    (required int)  — column index of the amount
      * store_col     (optional int / blank) — column index of the merchant
      * amount_sign   ('neg_expense' | 'pos_expense')
      * remember      ('1' | '0')     — persist this mapping for the layout

    The signature is recomputed server-side from the file's headers + delimiter
    (a client-sent signature is never trusted). Returns the same shape as
    /api/import/upload so the UI flows into the existing review table.
    """
    uid = current_user_id()
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    filename = file.filename or ""
    if not filename.lower().endswith(".csv"):
        return jsonify({"error": "File must be CSV"}), 400

    date_col, err = _parse_col_index(request.form.get("date_col"), required=True)
    if err:
        return jsonify({"error": f"date_col: {err}"}), 400
    amount_col, err = _parse_col_index(request.form.get("amount_col"), required=True)
    if err:
        return jsonify({"error": f"amount_col: {err}"}), 400
    store_col, err = _parse_col_index(request.form.get("store_col"), required=False)
    if err:
        return jsonify({"error": f"store_col: {err}"}), 400

    amount_sign = (request.form.get("amount_sign") or "neg_expense").strip()
    if amount_sign not in ("neg_expense", "pos_expense"):
        return jsonify({"error": "amount_sign must be 'neg_expense' or 'pos_expense'"}), 400
    remember = request.form.get("remember") == "1"

    backup_db("pre-import")
    conn = get_db()
    batch_id = None
    try:
        try:
            content = _decode_csv_bytes(file.read())
        except UnicodeDecodeError:
            return jsonify({
                "error": "Could not decode CSV file. Please save it as UTF-8."
            }), 400

        delimiter = _detect_delimiter(content)
        reader = csv.reader(io.StringIO(content), delimiter=delimiter)

        headers = _read_headers(reader)
        if not headers:
            return jsonify({"error": "Empty CSV file"}), 400

        # Validate indices against the actual header width.
        ncols = len(headers)
        for label, idx in (("date_col", date_col), ("amount_col", amount_col)):
            if idx >= ncols:
                return jsonify({"error": f"{label} out of range"}), 400
        if store_col is not None and store_col >= ncols:
            return jsonify({"error": "store_col out of range"}), 400

        signature = format_signature(headers, delimiter)
        col_map = {"date": date_col, "amount": amount_col}
        if store_col is not None:
            col_map["store"] = store_col

        cursor = conn.execute(
            "INSERT INTO import_batches (user_id, filename) VALUES (%s, %s) RETURNING id",
            (uid, filename),
        )
        batch_id = cursor.fetchone()["id"]

        _stage_rows(conn, uid, batch_id, reader, col_map, amount_sign)

        if remember:
            conn.execute("""
                INSERT INTO import_formats
                    (user_id, signature, delimiter, date_col, amount_col, store_col, amount_sign)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, signature) DO UPDATE SET
                    delimiter   = EXCLUDED.delimiter,
                    date_col    = EXCLUDED.date_col,
                    amount_col  = EXCLUDED.amount_col,
                    store_col   = EXCLUDED.store_col,
                    amount_sign = EXCLUDED.amount_sign
            """, (uid, signature, delimiter, date_col, amount_col, store_col, amount_sign))

        conn.commit()
        return _staging_response(conn, uid, batch_id)
    except Exception as e:
        conn.rollback()
        if batch_id is not None:
            try:
                _cleanup_import_batch(conn, batch_id, uid)
                conn.commit()
            except db.DatabaseError:
                conn.rollback()
        status = 400 if isinstance(e, (csv.Error, ValueError)) else 500
        return jsonify({"error": f"Import failed: {e}"}), status
    finally:
        conn.close()


@bp.route("/api/import/formats", methods=["GET"])
def list_import_formats():
    """Return this user's saved CSV-format mappings (for a Settings/Import list)."""
    uid = current_user_id()
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id, signature, delimiter, date_col, amount_col, store_col, "
            "amount_sign, created_at FROM import_formats "
            "WHERE user_id = %s ORDER BY created_at DESC, id DESC",
            (uid,),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route("/api/import/formats/<int:format_id>", methods=["DELETE"])
def delete_import_format(format_id):
    """Delete one saved CSV-format mapping. 404 if it isn't this user's."""
    uid = current_user_id()
    with db_conn() as conn:
        row = conn.execute(
            "DELETE FROM import_formats WHERE id = %s AND user_id = %s RETURNING id",
            (format_id, uid),
        ).fetchone()
    if not row:
        return jsonify({"error": "Format not found"}), 404
    return "", 204


@bp.route("/api/import/staging/<int:batch_id>")
def get_staging(batch_id):
    """The staged rows of one batch, shaped exactly like an upload's response.

    This used to hand back a bare array while the upload path returned
    {batch_id, count, items} — two shapes for the same thing, which went
    unnoticed because nothing read this endpoint. Resuming an unfinished review
    feeds it straight into the review table, so the shapes have to agree.
    """
    uid = current_user_id()
    with db_conn() as conn:
        return _staging_response(conn, uid, batch_id)


@bp.route("/api/import/confirm", methods=["POST"])
def confirm_imports():
    """Confirm staged imports. Handles normal items and splits (staging_id + amount override).

    The whole confirm is atomic: either every staged row is committed together
    or — if any item is malformed or a write fails — nothing is committed.
    """
    uid = current_user_id()
    data = request.json or {}
    items = data.get("items")
    if not isinstance(items, list):
        return jsonify({"error": "Missing or invalid 'items' list"}), 400

    # Read up front: every row written below is stamped with it, which is what
    # makes an import undoable as one thing.
    batch_id = data.get("batch_id")

    try:
        with db_conn() as conn:
            confirmed_staging_ids = set()

            for item in items:
                # splits pass staging_id; normal items pass id
                staging_id = item.get("staging_id") or item.get("id")
                if staging_id is None:
                    raise ValueError("Item is missing a staging id")
                staging = conn.execute(
                    "SELECT * FROM import_staging WHERE id = %s AND user_id = %s",
                    (staging_id, uid),
                ).fetchone()
                if not staging:
                    continue

                amount = item.get("amount", staging["amount"])
                store  = item.get("store",  staging["store"])
                # Normalize whatever the client sent (ISO or D.M.YYYY) to ISO;
                # never let a raw display string reach the transactions table.
                date = parse_date(str(item.get("date", staging["date"])))
                if not date:
                    raise ValueError(f"Unrecognized date on item {staging_id}")

                # The target category must belong to this user.
                cat = conn.execute(
                    "SELECT 1 FROM categories WHERE id = %s AND user_id = %s",
                    (item["category_id"], uid),
                ).fetchone()
                if not cat:
                    raise ValueError("Invalid category for import item")

                conn.execute("""
                    INSERT INTO transactions
                        (user_id, date, store, category_id, amount, type, import_batch_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (uid, date, store, item["category_id"], amount,
                      item.get("type") or staging["type"] or "expense", batch_id))

                if staging_id not in confirmed_staging_ids:
                    conn.execute("""
                        UPDATE import_staging SET confirmed = 1, final_category_id = %s
                        WHERE id = %s AND user_id = %s
                    """, (item["category_id"], staging_id, uid))
                    confirmed_staging_ids.add(staging_id)

            if batch_id:
                conn.execute(
                    "UPDATE import_batches SET status = 'completed' "
                    "WHERE id = %s AND user_id = %s",
                    (batch_id, uid),
                )
    except (KeyError, ValueError, TypeError) as e:
        # Malformed item payload — db_conn() already rolled back.
        return jsonify({"error": f"Invalid import item: {e}"}), 400
    except db.DatabaseError as e:
        return jsonify({"error": f"Database error during import: {e}"}), 500

    bump_data_version()
    # Auto-retrain merchant rules so the corrections made during this review
    # feed the next import's suggestions. Runs after the confirm committed, in
    # its own transaction — a training failure must never undo the import.
    retrained = None
    try:
        with db_conn() as conn:
            retrained, _ = _rebuild_merchant_rules(conn, uid)
    except db.DatabaseError:
        pass

    return jsonify({"status": "ok", "rules_retrained": retrained})


@bp.route("/api/import/staging/<int:item_id>", methods=["DELETE"])
def delete_staging_item(item_id):
    uid = current_user_id()
    with db_conn() as conn:
        conn.execute(
            "DELETE FROM import_staging WHERE id = %s AND user_id = %s",
            (item_id, uid),
        )
    return "", 204
