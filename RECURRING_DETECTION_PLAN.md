# Recurring Transaction Detection — Implementation Plan

Detect subscriptions, bills, and other recurring charges/income from existing
transaction history and surface them (cadence, next due date, monthly &
annualized cost, missed/changed alerts). Pure analytics over the existing
`transactions` table — **no schema change required for detection** (an optional
table is added only in Phase 2 to persist user dismissals).

Fits constraints: local-first, single-user, no cloud, no bank aggregation.

---

## Detection algorithm

`detect_recurring(conn, lookback_months=18, min_occurrences=3)` — proposed new
module `recurring.py` (keeps `app.py` from growing further), imported by `app.py`.

1. **Group** transactions by normalized store name (lowercase, trimmed,
   collapse whitespace — reuse the normalization already used by merchant rules
   in `suggest_category`). Keep `type` (expense/income) in the key so salary is
   detected too.
2. **Filter** to groups with `>= min_occurrences` occurrences within the lookback
   window. This naturally excludes one-off purchases.
3. **Cadence** — sort each group's dates, compute the day-gaps between
   consecutive transactions, take the **median gap**, and bucket it:
   - weekly ≈ 7 (±2), biweekly ≈ 14 (±3), monthly ≈ 30 (26–35),
     quarterly ≈ 91 (±10), yearly ≈ 365 (±20).
   - For "monthly", also compare day-of-month stability (e.g. always the 1st).
4. **Confidence** = function of (a) gap regularity — low coefficient of variation
   of gaps → high; and (b) amount stability — low CoV of amounts → high. Expose
   as a 0–1 score; show only groups above a floor (e.g. 0.5).
5. **Amount** — track `avg_amount`, `last_amount`, and an amount band so
   variable bills (utilities) still match. Flag **price change** when
   `last_amount` differs from the prior amount by > a threshold (e.g. 10%).
6. **Next due** = `last_date + median_interval`. **Status**:
   - `active` — on schedule,
   - `due_soon` — next due within 7 days,
   - `overdue` / `missed` — next due passed by more than the tolerance with no
     new transaction (candidate "forgotten subscription" or cancelled service),
   - `price_changed` — latest amount jumped.
7. **Normalize cost** to `monthly_cost` (interval-scaled) and `annual_cost`.

**Edge cases:** variable-amount utilities (amount band tolerance), irregular
merchants (filtered by confidence floor), income/salary (kept via `type`),
day-of-month drift around weekends (tolerance in the monthly bucket).

---

## Backend

- New `recurring.py` with `detect_recurring(...)` returning a list of dicts:
  `{store, type, category, cadence, interval_days, occurrences, avg_amount,
    last_amount, last_date, next_date, monthly_cost, annual_cost, status,
    confidence}`.
- New route in `app.py`: `GET /api/recurring?lookback_months=&min_occurrences=`
  → JSON `{summary: {monthly_total, annual_total, count}, items: [...]}`.
  Follow the existing `conn = get_db()` route pattern (after the bug-fix branch
  lands, use the new connection context manager).
- **Phase 2 (optional persistence):** table
  `recurring_dismissed (id, signature TEXT UNIQUE, dismissed_at TEXT)` where
  `signature` = normalized store + cadence; `POST /api/recurring/dismiss` and a
  filter in the detector to hide dismissed signatures.

---

## Frontend

**Decided (2026-05-31): Card/section inside the existing Trends page**
(`#page-trends`, `loadTrends()` in `static/js/app.js`) — a "Recurring &
subscriptions" card. No new nav item. Can be promoted to a dedicated tab later
if the feature grows (alerts, dismiss, etc.).

Render:
- Summary header: **total monthly recurring**, total annualized, item count.
- Table sorted by `monthly_cost` desc: store, cadence pill, last/next date,
  amount, monthly cost, status badge (`due soon` / `overdue` / `price ↑`).
- Reuse existing `fmt()` currency formatter and the Apple-style card/table CSS.

---

## Phasing

1. **Phase 1** — `recurring.py` detector + `GET /api/recurring` + Trends card
   (read-only). Ship this first; it's self-contained and additive.
2. **Phase 2** — dismiss/confirm persistence, price-increase + overdue alerts.
3. **Phase 3 (optional)** — include recurring summary in the iCloud snapshot
   (`icloud_export.py`) for the iOS reader; requires bumping `SCHEMA_VERSION`
   and `SnapshotLoader.supportedSchema` together.

## Testing

- Unit-test `detect_recurring` against synthetic series: clean monthly, jittery
  monthly (±3 days), variable utility amounts, a cancelled (overdue) sub, a
  price-increased sub, and a non-recurring noise merchant.
- Verify it runs in well under a second on the full `expenses.db`.

---

## Implementation status (2026-05-31)

- ✅ **`recurring.py`** — `detect_recurring(conn, lookback_months, min_occurrences,
  today)` implemented. Returns `{summary, items}`. Tuned **subscription-focused**
  per user decision:
  - Cadence restricted to **monthly / quarterly / yearly** (weekly & biweekly
    dropped — those are shopping patterns, not bills).
  - **Amount-stability gate** (`_MAX_AMOUNT_COV`) rejects wildly variable spend.
  - **Frequency gate** (`_FREQ_SLACK`) rejects merchants firing far more often
    than once per interval — this is what excludes frequent groceries.
  - `price_changed` now compares last charge vs the **median of prior charges**,
    and only for otherwise amount-stable series (no more over-firing).
- ✅ **`test_recurring.py`** — 8 synthetic-data unit tests, all passing.
- ✅ Validated on live `expenses.db` (2,502 txns): 31 noisy series → 17 clean
  subscriptions/bills after tuning.
- ✅ **`GET /api/recurring` route** — added in `app.py`, built on the bug-fix
  agent's new `db_conn()` context manager. Accepts `lookback_months` &
  `min_occurrences` query args. Returns 200 with `{summary, items}`.
- ✅ **Trends-page card** — "Recurring & Subscriptions" card added to
  `templates/index.html` (always visible, outside the category-gated content),
  with `loadRecurring()` / `renderRecurring()` in `static/js/app.js` (hooked into
  `loadTrends()`), badge styles in `static/css/style.css`, and an `escapeHtml()`
  helper (was missing from the codebase). Summary shows €/mo, €/yr, count.
  Status badges: active / due soon / overdue / price ↑.

**Phase 1 complete.** Verified: `import app` OK, `/api/recurring` → 200 (17
series on live data), `GET /` renders the card, `node --check app.js` clean,
8/8 unit tests pass.

## Phase 2 status (2026-05-31)

- ✅ **Duplicate-merchant merging** — `recurring.py` now fuzzy-merges merchant
  variants before cadence analysis. After exact-normalized grouping, a
  union-find pass (`_merge_variants`) collapses groups whose *merge-keys* are
  equivalent. The merge-key (`_merge_key`) strips known payment-gateway
  prefixes (`paypal *`, `google *`, `chf*`, `klarna*`, …), drops a trailing
  geo/branch suffix (", oulu, fi"), and reduces to alphanumerics. Two keys are
  "the same merchant" (`_similar`) when one is contained in the other
  (len ≥ 5) **or** their `difflib` ratio ≥ **0.84** (above the merchant-rule
  0.72 — a false merge silently sums two subs' costs, so we stay conservative).
  Merging is transitive and only within the same `type`. Display name = the
  most frequent original store string. Verified non-over-merging on adversarial
  pairs (Spotify/Netflix, K-Market branches, Apple/Google Store, Verkko/Ruoka).
- ✅ **Transfers / investments separated** — items in categories
  `{Investments, Debt}` get `is_transfer: true` and are **excluded from**
  `summary.monthly_total` / `annual_total` (they're transfers, not consumption).
  Still detected and returned; the Trends card lists them under a separate
  "Transfers & investments (excluded from totals)" subsection.
- ✅ **Dismiss / hide persistence** — `recurring_dismissed (id, signature UNIQUE,
  dismissed_at)` added to `init_db()` (idempotent). `signature` = normalized
  merchant + cadence (`signature()` helper). `detect_recurring` accepts a
  `dismissed` set (defaults to querying the table) and excludes those series;
  each item carries its `signature`. Routes `POST /api/recurring/dismiss` and
  `DELETE /api/recurring/dismiss/<signature>` (on `db_conn()`). The Trends card
  shows a ✕ "Hide" control per row → POST + reload.
- ✅ **Tests** — `test_recurring.py` extended to **14** (merged YouTube/Oura
  variants, no over-merge of distinct merchants, Investments + Debt excluded
  from the total, dismissed-signature filtering). All pass.
- ✅ Verified: `import app` OK, `node --check app.js` clean, and the
  `/api/recurring` + dismiss/undismiss routes exercised via `app.test_client()`.

**Phase 2 complete.**

### Known limitation
- Heavily-truncated gateway strings (e.g. "PAYPAL *GOOGLE YOUTUBE SU") may still
  stay separate from cleaner variants of the same service — deliberate, since
  the alternative (a looser threshold / shared-token rule) over-merges distinct
  chains. Under-merging never corrupts another series' cost.
