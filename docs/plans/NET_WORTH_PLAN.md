# Net Worth Tab — Implementation Plan

A dedicated **Net Worth** tab that tracks net worth over time from **manually
entered** account balances (cash, savings, investments, loans). Fully manual,
no aggregation — fits the local-first / privacy / single-user constraints.

---

## Schema (new tables in `database.py` `init_db()`)

```sql
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL CHECK(type IN ('asset','liability')),
    sort_order  INTEGER NOT NULL DEFAULT 0,
    is_archived INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS account_balances (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    as_of      TEXT NOT NULL,            -- YYYY-MM-DD
    balance    REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(account_id, as_of),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
```

**Net worth at date D** = Σ(latest asset balance with `as_of <= D`)
− Σ(latest liability balance with `as_of <= D`). Balances **carry forward**:
the most recent snapshot per account is used until a newer one exists.

---

## Backend (`app.py` routes)

- `GET  /api/accounts` — list (optionally include archived).
- `POST /api/accounts` — `{name, type, sort_order?}`.
- `PUT/DELETE /api/accounts/<id>` — edit / archive (soft delete preferred so
  history is preserved; hard delete cascades balances).
- `GET  /api/accounts/<id>/balances`, `POST /api/accounts/<id>/balances`
  (`{as_of, balance}`; upsert on `UNIQUE(account_id, as_of)`),
  `DELETE /api/balances/<id>`.
- `GET  /api/networth/history?months=12` — monthly time series of net worth
  (carry-forward), plus total assets, total liabilities, current net worth, and
  delta vs previous period.
- `GET  /api/networth/summary` — current totals + per-account latest balances.

Follow the existing route/`get_db()` pattern (after the bug-fix branch lands,
use the new connection context manager).

---

## Frontend

- **Nav item** in `templates/index.html` sidebar (after `reports`, before
  `settings`): `<button class="nav-item" data-page="networth">` with an icon +
  "Net Worth".
- **Page** `<div class="page" id="page-networth">` containing:
  - Summary cards: **Net Worth**, **Total Assets**, **Total Liabilities**,
    **Change vs last month** (green/red, reusing the dashboard pill style).
  - **Net worth line chart** over time (Chart.js, styled like the existing
    dashboard charts — no gridlines, clean look).
  - **Assets vs Liabilities** stacked bar/area per month (optional, Phase 2).
  - **Accounts table**: add/edit/archive accounts; inline current balance.
  - **"Update balances" modal**: lists all active accounts with number inputs
    pre-filled with the last known balance, `as_of` defaulting to today — the
    fast monthly-update path.
- **`static/js/app.js`**: add `loadNetWorth()` and wire it into the nav click
  dispatch (the `.nav-item[data-page]` handler near the bottom of `app.js`,
  ~line 2993, which toggles `.page.active` — add the `networth` → `loadNetWorth()`
  case). Add `renderNetWorthCards()`, `renderNetWorthChart()`,
  `renderAccountsTable()`. Reuse the global `fmt()` currency formatter.

---

## Implementation status (2026-05-31)

**Phase 1 complete** on branch `feat/net-worth`.

- ✅ Schema: `accounts` + `account_balances` (cascade FK, `UNIQUE(account_id,
  as_of)`) added to `database.py` `init_db()` (idempotent migration; ran on the
  live DB).
- ✅ `networth.py` — `compute_history()` (carry-forward monthly series) and
  `summary()` (current totals, change-vs-last-month, per-account latest balance).
- ✅ Routes in `app.py`: accounts CRUD, balances (POST upserts on `as_of`),
  `DELETE /api/balances/<id>`, `GET /api/networth/history|summary` — all on the
  `db_conn()` helper, with 400 validation on bad type/date/amount.
- ✅ Frontend: **Net Worth** nav item + page (`templates/index.html`), summary
  cards, Chart.js net-worth line, and an inline-editable accounts table with a
  shared "Balances as of" date + "Save balances" (the fast monthly-update path).
  Add/delete account inline. `loadNetWorth()` wired into nav dispatch.
- ✅ `test_networth.py` — 5 unit tests (carry-forward, change-vs-prev, archived
  exclusion, empty DB, latest-balance), all passing. No regression in recurring
  tests (8/8).
- Note: the "Update balances" UI is an **inline editable table**, not a modal —
  the app has no modal pattern and uses inline editing elsewhere (import review).

### Phasing

1. ~~**Phase 1** — schema + accounts CRUD + balance entry + net worth line chart
   + summary cards.~~ ✅ Done.
2. **Phase 2** — assets-vs-liabilities breakdown chart, MoM/YoY deltas, account
   archiving, sort ordering.
3. **Phase 3 (optional)** — include net worth series in the iCloud snapshot
   (`icloud_export.py`) so the iOS reader can show it; bump `SCHEMA_VERSION` and
   `SnapshotLoader.supportedSchema` together.

## Notes / decisions to confirm

- **Carry-forward vs sparse**: carry-forward (recommended) means you only enter
  balances when they change; the chart stays continuous.
- **Backfill**: allow entering historical `as_of` dates so the trend isn't empty
  on day one.
- **Investments** already exists as a transaction category — net worth accounts
  are separate from transactions by design (balances, not flows); no coupling.
