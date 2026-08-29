"""Recurring transaction detection.

Pure analytics over the ``transactions`` table — no schema changes. Groups
transactions by normalized merchant + type, infers a cadence from the median
gap between occurrences, scores confidence on gap/amount regularity, predicts
the next due date, and flags a status (active / due_soon / overdue /
price_changed).

See RECURRING_DETECTION_PLAN.md. Phase 2 adds: fuzzy merging of duplicate
merchant variants, separation of transfer/investment series from the expense
total, and filtering of user-dismissed series.
"""
from __future__ import annotations

import difflib
import re
import statistics
from collections import Counter
from datetime import date, datetime, timedelta

# Cadence buckets: (label, ideal_interval_days, low, high) on the median gap.
# Subscription-focused: monthly and slower only. Weekly/biweekly cadences are
# almost always shopping patterns, not bills, so they are intentionally omitted.
CADENCES = [
    ("monthly", 30, 26, 35),
    ("quarterly", 91, 80, 100),
    ("yearly", 365, 330, 400),
]

_DUE_SOON_DAYS = 7
_PRICE_CHANGE_PCT = 0.10
_MIN_CONFIDENCE = 0.5
# Subscription gates (see RECURRING_DETECTION_PLAN.md "subscription-focused"):
_MAX_AMOUNT_COV = 0.40     # reject wildly variable amounts (e.g. restaurants)
_FREQ_SLACK = 1.4          # reject merchants firing far more often than 1×/interval
_STABLE_COV = 0.10         # a series this amount-stable can flag a real price change

# Generic / noise merchants that are recurring but not user-actionable.
_SKIP_STORES = {"", "other", "rent", "missing info", "monthly fee", "korko"}

# Categories that are transfers/investments rather than true consumption.
# These are still detected, but excluded from the recurring EXPENSE summary
# total and surfaced separately so they don't inflate "what I spend per month".
_TRANSFER_CATEGORIES = {"investments", "debt"}

# Fuzzy merchant merging: two normalized merchant names are treated as the same
# service when their SequenceMatcher ratio is at least this high, or when one is
# a substring of the other after stripping common payment-processor prefixes.
# Mirrors the merchant_rules "smart" match (0.72); nudged to 0.84 here because a
# false merge silently combines two distinct subscriptions' costs — over-merging
# is worse than under-merging, so we stay conservative.
_MERGE_RATIO = 0.84

# Payment-processor / gateway prefixes that wrap the real merchant name. Stripped
# before fuzzy comparison so "PAYPAL *GOOGLE YOUTUBE" lines up with "Google
# YouTube". Order-independent; applied repeatedly until none remain.
_PAYMENT_PREFIXES = (
    "paypal *", "paypal*", "paypal ",
    "google *", "google*",
    "chf*", "chf *",
    "klarna*", "klarna *", "klarna ",
    "sumup *", "sumup*", "sq *", "sq*", "izettle *", "izettle*",
    "apl*", "apple.com/bill", "apple.com",
    "amzn mktp", "amazon *", "amazon*",
)


def _normalize_store(store: str) -> str:
    return re.sub(r"\s+", " ", (store or "").strip().lower())


def _merge_key(norm: str) -> str:
    """Aggressively strip payment-gateway noise from a normalized name so that
    variants of the same service line up for fuzzy comparison.

    Removes known processor prefixes, drops a trailing geo/branch suffix (e.g.
    "ouraring, oulu, fi" -> "ouraring"), and collapses non-alphanumerics.
    """
    s = norm
    # Repeatedly peel known payment prefixes (e.g. "paypal *google*...").
    changed = True
    while changed:
        changed = False
        for p in _PAYMENT_PREFIXES:
            if s.startswith(p):
                s = s[len(p):].strip()
                changed = True
    # Drop a trailing ", city, country"-style location suffix.
    s = s.split(",", 1)[0].strip()
    # Collapse to alphanumerics only (kills "*", "-", spaces, "fi" separators).
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def _similar(a: str, b: str) -> bool:
    """True if two merge-keys denote the same merchant.

    Two signals, both conservative:
      * one key is fully contained in the other (after prefix stripping), which
        catches geo/branch suffixes ("ouraring" in "ouraring oulu fi") and
        "tesla" in "teslafi"; or
      * the overall fuzzy ratio clears ``_MERGE_RATIO``.

    A shared-substring/token heuristic was considered and rejected: a common
    chain word ("store", "k-market", "kauppa") would bridge genuinely distinct
    merchants. Under-merging (e.g. one payment-gateway YouTube variant staying
    separate) is the lesser evil — it never corrupts another series' cost.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    # Substring containment (after prefix stripping). Guard against trivially
    # short keys swallowing unrelated merchants.
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 5 and shorter in longer:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= _MERGE_RATIO


def _merge_variants(group_list: list) -> list:
    """Merge groups whose merchants are fuzzy-equivalent into single series.

    Only same-``type`` groups are ever merged (an expense never merges into an
    income). Merging is **transitive** (union-find): if variant A matches B and
    B matches C, all three collapse even when A and C alone look weaker — this
    is what links "YouTubePremium", "Google YouTubePremium" and
    "GOOGLE YOUTUBE SU". Each merged group accumulates the events, categories,
    and store strings of its members; the largest member anchors the cluster.
    """
    # Process largest-first so the dominant variant tends to anchor clusters.
    groups = sorted(group_list, key=lambda x: -len(x["events"]))
    keys = [_merge_key(g["norm"]) for g in groups]
    n = len(groups)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    for i in range(n):
        for j in range(i + 1, n):
            if groups[i]["type"] != groups[j]["type"]:
                continue
            if find(i) == find(j):
                continue
            if _similar(keys[i], keys[j]):
                union(i, j)

    clusters: dict = {}
    for i in range(n):
        root = find(i)
        if root not in clusters:
            # Copy the anchor group so we don't mutate the input.
            anchor = groups[i] if i == root else groups[root]
            clusters[root] = {
                "norm": anchor["norm"],
                "type": anchor["type"],
                "stores": Counter(),
                "events": [],
                "categories": [],
            }
        c = clusters[root]
        c["events"].extend(groups[i]["events"])
        c["categories"].extend(groups[i]["categories"])
        c["stores"].update(groups[i]["stores"])
    return list(clusters.values())


def _load_dismissed(conn, user_id) -> set:
    """Return the set of dismissed signatures for ``user_id`` (empty on error).

    The ``recurring_dismissed`` table always exists in the Postgres schema, so
    this queries directly; a defensive guard still returns an empty set (treat
    as "nothing dismissed") if the table is somehow missing.
    """
    try:
        rows = conn.execute(
            "SELECT signature FROM recurring_dismissed WHERE user_id = %s",
            (user_id,),
        ).fetchall()
    except Exception:
        return set()
    return {r["signature"] for r in rows}


# Cadence → representative interval in days, for costing manual subscriptions.
_MANUAL_INTERVALS = {"monthly": 30.4, "quarterly": 91.0, "yearly": 365.0}


def _load_manual(conn, user_id) -> list:
    """Return user-added subscriptions for ``user_id`` as recurring items.

    Manual subscriptions live in ``manual_subscriptions`` and are shaped to match
    the detected-item dict so the UI renders them in the same table. They carry
    ``is_manual=True`` + ``manual_id`` so the row's remove button deletes the row
    rather than dismissing a detected signature.
    """
    try:
        rows = conn.execute(
            "SELECT id, store, amount, cadence, category, type "
            "FROM manual_subscriptions WHERE user_id = %s",
            (user_id,),
        ).fetchall()
    except Exception:
        return []

    out = []
    for r in rows:
        cadence = r["cadence"] if r["cadence"] in _MANUAL_INTERVALS else "monthly"
        interval = _MANUAL_INTERVALS[cadence]
        amount = float(r["amount"])
        category = r["category"]
        is_transfer = bool(category) and category.lower() in _TRANSFER_CATEGORIES
        out.append({
            "store": r["store"],
            "type": r["type"],
            "category": category,
            "cadence": cadence,
            "interval_days": int(round(interval)),
            "occurrences": None,
            "avg_amount": round(amount, 2),
            "last_amount": round(amount, 2),
            "last_date": None,
            "next_date": None,
            "monthly_cost": round(amount * (30.4 / interval), 2),
            "annual_cost": round(amount * (365.0 / interval), 2),
            "status": "active",
            "confidence": 1.0,
            "signature": None,
            "is_transfer": is_transfer,
            "is_manual": True,
            "manual_id": r["id"],
        })
    return out


def _parse_date(value: str):
    """transactions.date is stored as YYYY-MM-DD."""
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _classify_cadence(median_gap: float):
    """Return (label, ideal_interval_days) for the closest bucket, else (None, None)."""
    for label, ideal, low, high in CADENCES:
        if low <= median_gap <= high:
            return label, ideal
    return None, None


def _cov(values) -> float:
    """Coefficient of variation (stddev / |mean|); 0 for <2 points or zero mean."""
    if len(values) < 2:
        return 0.0
    mean = statistics.fmean(values)
    if mean == 0:
        return 0.0
    return statistics.pstdev(values) / abs(mean)


def signature(store: str, cadence: str) -> str:
    """Stable identifier for a detected series, used for dismiss persistence.

    Normalized merchant + cadence. Kept deliberately coarse (cadence, not exact
    amount/date) so a dismissal survives the next month's new charge.
    """
    return f"{_normalize_store(store)}|{cadence}"


def detect_recurring(conn, user_id, lookback_months: int = 18,
                     min_occurrences: int = 3,
                     today: date | None = None,
                     dismissed: set | None = None) -> dict:
    """Detect recurring transaction series for ``user_id``.

    ``conn`` is an open psycopg connection yielding ``dict_row`` rows.
    ``dismissed`` is an optional set of signatures (see :func:`signature`) to
    exclude; if ``None``, the ``recurring_dismissed`` table is queried for this
    user.

    Returns ``{"summary": {...}, "items": [...]}`` sorted by monthly cost desc.
    Each item carries a ``signature`` and an ``is_transfer`` flag. Transfer /
    investment series are detected but excluded from the summary expense total.
    """
    today = today or date.today()
    cutoff = today - timedelta(days=int(lookback_months * 30.4))

    if dismissed is None:
        dismissed = _load_dismissed(conn, user_id)

    rows = conn.execute(
        """
        SELECT t.store AS store, t.type AS type, t.amount AS amount,
               t.date AS date, c.name AS category
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE t.date >= %s AND t.user_id = %s
        ORDER BY t.date
        """,
        (cutoff.isoformat(), user_id),
    ).fetchall()

    # Group by (normalized store, type). Keep dates + amounts + categories.
    groups: dict = {}
    for r in rows:
        store = r["store"]
        type_ = r["type"]
        amount = r["amount"]
        date_str = r["date"]
        category = r["category"]
        norm = _normalize_store(store)
        if norm in _SKIP_STORES:
            continue
        d = _parse_date(date_str)
        if d is None:
            continue
        g = groups.setdefault(
            (norm, type_),
            {"norm": norm, "stores": Counter(), "type": type_,
             "events": [], "categories": []},
        )
        g["stores"][store] += 1
        g["events"].append((d, abs(float(amount))))
        if category:
            g["categories"].append(category)

    # Fuzzy-merge variants of the same merchant (e.g. the three YouTube store
    # strings) before cadence analysis, so their charges form one series.
    merged_groups = _merge_variants(list(groups.values()))

    items = []
    for g in merged_groups:
        events = sorted(g["events"])  # by date
        if len(events) < min_occurrences:
            continue
        dates = [d for d, _ in events]
        amounts = [a for _, a in events]

        gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
        gaps = [gp for gp in gaps if gp > 0]  # drop same-day duplicates
        if len(gaps) < min_occurrences - 1:
            continue

        median_gap = statistics.median(gaps)
        cadence, ideal = _classify_cadence(median_gap)
        if cadence is None:
            continue

        # Amount-stability gate: subscriptions/bills hold a roughly stable amount;
        # a merchant with wildly varying spend (restaurants, going out) is not one.
        amount_cov = _cov(amounts)
        if amount_cov > _MAX_AMOUNT_COV:
            continue

        # Frequency gate: a once-per-interval series fires about span/interval
        # times. Frequent shopping (groceries) fires far more often — reject it.
        span_days = (dates[-1] - dates[0]).days
        expected = span_days / median_gap + 1
        if len(dates) > expected * _FREQ_SLACK:
            continue

        # Confidence: 70% gap regularity, 30% amount stability.
        gap_score = max(0.0, 1.0 - _cov(gaps))
        amt_score = max(0.0, 1.0 - amount_cov)
        confidence = round(0.7 * gap_score + 0.3 * amt_score, 2)
        if confidence < _MIN_CONFIDENCE:
            continue

        interval = int(round(median_gap))
        avg_amount = round(statistics.fmean(amounts), 2)
        last_amount = round(amounts[-1], 2)
        last_date = dates[-1]
        next_date = last_date + timedelta(days=interval)
        days_to_next = (next_date - today).days

        # Price change only for series that are otherwise amount-stable, compared
        # against the typical (median) of the prior charges — not last-vs-previous.
        baseline = statistics.median(amounts[:-1])
        baseline_stable = _cov(amounts[:-1]) <= _STABLE_COV
        if (baseline_stable and baseline
                and abs(last_amount - baseline) / baseline > _PRICE_CHANGE_PCT):
            status = "price_changed"
        elif 0 <= days_to_next <= _DUE_SOON_DAYS:
            status = "due_soon"
        else:
            status = "active"

        category = (Counter(g["categories"]).most_common(1)[0][0]
                    if g["categories"] else None)

        # Display name: the most frequent original store string in the merged
        # group (so the cleanest/most-common variant is shown).
        display = g["stores"].most_common(1)[0][0]
        sig = signature(display, cadence)
        if sig in dismissed:
            continue

        is_transfer = bool(category) and category.lower() in _TRANSFER_CATEGORIES

        items.append({
            "store": display,
            "type": g["type"],
            "category": category,
            "cadence": cadence,
            "interval_days": interval,
            "occurrences": len(dates),
            "avg_amount": avg_amount,
            "last_amount": last_amount,
            "last_date": last_date.isoformat(),
            "next_date": next_date.isoformat(),
            "monthly_cost": round(avg_amount * (30.4 / interval), 2),
            "annual_cost": round(avg_amount * (365.0 / interval), 2),
            "status": status,
            "confidence": confidence,
            "signature": sig,
            "is_transfer": is_transfer,
            "is_manual": False,
            "manual_id": None,
        })

    # Fold in user-added (manual) subscriptions — ones detection missed.
    items.extend(_load_manual(conn, user_id))

    items.sort(key=lambda x: x["monthly_cost"], reverse=True)

    # Transfers/investments are real recurring movements but not consumption, so
    # they're excluded from the monthly/annual EXPENSE total (the UI surfaces
    # them in a separate subsection via the is_transfer flag).
    expenses = [i for i in items
                if i["type"] == "expense" and not i["is_transfer"]]
    summary = {
        "count": len(items),
        "monthly_total": round(sum(i["monthly_cost"] for i in expenses), 2),
        "annual_total": round(sum(i["annual_cost"] for i in expenses), 2),
    }
    return {"summary": summary, "items": items}


if __name__ == "__main__":  # manual smoke run against the live DB
    import sys

    import db

    with db.db_conn() as conn:
        row = conn.execute("SELECT min(id) AS uid FROM users").fetchone()
        user_id = row["uid"] if row else None
        if user_id is None:
            print("No users in the database — nothing to detect. "
                  "Create a user first.")
            sys.exit(0)
        result = detect_recurring(conn, user_id)

    s = result["summary"]
    print(f"Detected {s['count']} recurring series — "
          f"monthly recurring expense ≈ {s['monthly_total']:.0f} EUR, "
          f"annual ≈ {s['annual_total']:.0f} EUR\n")
    print(f"{'mo cost':>8}  {'cadence':<9} {'n':>3} {'conf':>4}  "
          f"{'status':<13} {'store':<30} next due")
    for i in result["items"][:30]:
        print(f"{i['monthly_cost']:>8.0f}  {i['cadence']:<9} {i['occurrences']:>3} "
              f"{i['confidence']:>4.2f}  {i['status']:<13} {i['store'][:30]:<30} {i['next_date']}")
