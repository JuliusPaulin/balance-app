# Expense Tracker App ("Balance.") — Reference

## Stack

| Layer | Tech |
|-------|------|
| Backend | Python, Flask |
| Database | SQLite — one local file |
| Frontend | Vanilla HTML/CSS/JS, Apple-style design |
| Desktop shell | pywebview (native window on port 5050, `PORT` env-overridable) |
| Charts | Chart.js |

**Entry points:**
- `main.py` — desktop app launcher (pywebview); on launch creates the schema,
  backs up the DB, and seeds the local user.
- `app.py` — the wiring: registers the blueprints and runs the server. ~30 lines.
- `core.py` — the Flask `app` object and the request plumbing every route shares:
  the rate limiter, CSRF, `current_user_id()`, the security headers, the
  before-request guards, and the recurring-detection cache.
- `routes/` — one module per area, each owning a blueprint (see below)
- `config.py` — runtime config (all optional; safe defaults throughout)
- `db.py` — the single import point for the database; re-exports `db_sqlite`
- `db_sqlite.py` — the SQLite engine (translates `%s`→`?`, `now()`→`datetime('now')`, dict rows)
- `database.py` — schema DDL + seeding + backups
- `scripts/migrate_to_local_sqlite.py` — one-time import of an old DB into this schema
- `recurring.py` — recurring/subscription detection over transaction history
- `networth.py` — net worth tracking (carry-forward over manual account balances; grouped + holdings).
  Totals come from balances alone: an account you leave out of an update keeps its
  last value, and closing one writes a zero at the closing date rather than hiding
  it, so past months stay true. Never filter `is_archived` in the total queries.
- `investment_import.py` — Nordnet CSV / Nordea xlsx portfolio parsers (→ holdings + Net Worth)

**The `routes/` package.** `app.py` held every route until it reached 3,000
lines; the routes now sit one area per module, and `routes/__init__.py` lists
the blueprints `app.py` registers — the only place a new area is added.

| Module | Covers |
|--------|--------|
| `system.py` | the page itself, `/api/me`, health checks, backups, quit |
| `categories.py` | spending categories |
| `merchant_rules.py` | the store-name patterns that auto-assign a category |
| `notes.py` | per-month notes |
| `transactions.py` | the filtered list, and create / update / delete |
| `dashboard.py` | Dashboard, Reports and Trends |
| `subscriptions.py` | recurring charges: detected, dismissed, hand-added |
| `net_worth.py` | accounts, balances, net worth, holdings, investment import |
| `csv_import.py` | parse a statement, stage it, confirm it |
| `bank_import.py` | Open Banking consent, fetch, disconnect |

They lean on each other in **one direction only**, so nothing imports in a
circle: everything imports `core`; `csv_import` takes the rule rebuilder from
`merchant_rules`; `bank_import` takes the staging helpers from `csv_import`.
Blueprint endpoint names are now prefixed (`categories.get_categories`), which
costs nothing here because the app has never used `url_for`.

---

## One user, one file, no server

There is no login, no OAuth, no admin, no network database. `current_user_id()`
returns the fixed `config.LOCAL_USER_ID`. The `users` table and the `user_id`
columns on every table survive from an earlier multi-user port and are kept
**purely as an internal anchor**, so the user-scoped queries throughout the app
run unchanged — do not try to remove them without rewriting every query and
migrating existing databases.

The database is a single SQLite file at
**`~/Library/Application Support/Balance/expenses.db`** (override with
`SQLITE_PATH`). Run with `python3 main.py`; package with
`./scripts/build_app.sh` → `Balance.app` / `Balance.dmg`.

The SQL throughout the app is psycopg-flavoured — `%s` placeholders, `RETURNING`,
`ON CONFLICT`, dict rows. `db_sqlite` translates that on the way to the driver,
which is why route code reads the way it does. Driver-specific bits funnel
through `db`: `db.IntegrityError`, `db.DatabaseError`, `db.Json(...)`,
`db.load_json(...)` — never import `sqlite3` directly in route code.

> History: this began as a single-user SQLite desktop app, was ported wholesale
> to multi-user Postgres for a Supabase/Render deployment, then brought back to
> local SQLite to drop the hosting costs. The hosted build — Postgres, Google
> OAuth, access approval, gunicorn, the SMTP notifier — has now been removed
> outright. The investment **holdings** feature lived on a separate branch and
> is NOT in this lineage.

---

## Tests

`python3 -m pytest tests/` — 74 tests, all green.

`conftest.py` points `SQLITE_PATH` at a throwaway file **at import time**, before
pytest collects any test module. This matters: test modules `import config` /
`import db` at the top, and both read their settings once on first import, so
setting the path in a fixture would be too late and the suite would write into
the real database. There is an assert guarding it.

Every table has `user_id` with `ON DELETE CASCADE`, so each test resets by
deleting the one user row and re-seeding.

---

## Database Schema

| Table | Key fields |
|-------|-----------|
| `transactions` | id, date, store, category_id (FK), amount, type (expense/income) |
| `categories` | id, name, type, is_default |
| `merchant_rules` | id, pattern, category_id (FK), match_type (exact/contains/smart) |
| `import_staging` | id, date, store, suggested_category, amount, type, confirmed, final_category_id, import_batch_id |
| `import_batches` | id, filename, imported_at, status (pending/completed) |
| `month_notes` | id, month (YYYY-MM), note |
| `accounts` | id, name, type (asset/liability), sort_order, is_archived |
| `account_balances` | id, account_id (FK, cascade), as_of (YYYY-MM-DD), balance; UNIQUE(account_id, as_of) |
| `recurring_dismissed` | id, signature (UNIQUE, = normalized merchant + cadence), dismissed_at |
| `bank_sessions` | id, user_id (FK, cascade), session_id, aspsp_name, aspsp_country, valid_until, accounts (JSONB), created_at; UNIQUE(user_id) |

---

## Implemented Features

### Transactions
- CRUD: create, read, update, delete
- Filters: by month, category, type, store (search), date range, amount range
- `GET /api/transactions` query params: `month`, `months` (comma-separated), `category_ids`, `type`, `q`, `date_from`, `date_to`, `amount_min`, `amount_max`, `sort`, `dir`, `page`, `per_page`

### Categories
- CRUD with reassignment of transactions on delete
- Predefined expense categories (28): Car charging, Car maintenance, Car parking, Car payment, Clothing, Condo fees, Debt, Dog, Electronics, Entertainment, Exercise, Gas, Gifts, Going out, Groceries, Home maintenance, Insurance, Investments, Lunch, Medical, Other, Public transportation, Rent, Restaurant, Telecom, Travel, Utilities, Work
- Predefined income categories (6): Job, Side project, Kela, Expense reimbursement, Other, Investments

### Merchant Rules (Categories tab)
- Rules auto-assign a category to a transaction based on store name
- Match types: `exact` (case-insensitive), `contains` (substring), `smart` (fuzzy via difflib ≥ 0.72)
- Fallback: if no rule matches, check most frequent historical category for that store
- CRUD via UI and API (`/api/merchant-rules`)
- **567 rules auto-generated** from historical data via `scripts/generate_merchant_rules.py`

### CSV Import
Supports three CSV formats:

| Format | Delimiter | Date format | Key columns |
|--------|-----------|-------------|-------------|
| Finnair credit card | `,` | `YYYY-MM-DD` | Date of payment, Location of purchase, Amount (col 8 = EUR) |
| Finnish bank statement (EtuTili) | `;` | `YYYY/MM/DD` | Kirjauspäivä, Nimi, Määrä; Viesti overrides the store only when the row has no Viitenumero |
| Nordea Platinum credit card | `;` | `DD.M.YYYY` (quoted) | Tapahtumapäivä, Otsikko, Määrä |

**General behaviour:**
- Delimiter auto-detected (`;` vs `,`)
- Amount sign determines type: negative → expense, positive → income
- `Viesti` (message) may stand in as the store name, but only on rows with no
  payment reference (`Viitenumero`). Rows that carry one are card purchases and
  direct debits, where Nordea writes the receipt line or the city into `Viesti`
  and the real merchant sits in `Nimi`. The override also rejects strings over
  50 chars, IBANs, reference numbers and card receipt lines (`EUR   10,76 CORK`).
- Date parsing: explicit regex for `YYYY/MM/DD`, `YYYY-MM-DD`, `DD.M.YYYY` before dateutil fallback — avoids dayfirst ambiguity
- Category auto-suggested via merchant rules then historical fallback; defaults to "Other" when unknown
- Staged in `import_staging` for user review before committing
- Review table: all fields (date, store, amount, category, type) are editable inline before confirming
- **÷2 Split costs** button halves all amounts in one click (shared expense use case)
- Bulk category assignment, per-row delete, transaction split

### Open Banking Import (Enable Banking)
Pulls real bank transactions into the **same** staging → review → confirm pipeline as CSV.
Module: `enable_banking.py`.

- **Flow:** Import tab → "Import from bank" → **Connect** (PSD2 consent redirect) → pick
  account + date range → **Fetch** → standard review table (auto-categorised, editable) → **Confirm All**.
- **Auth:** app authenticates to Enable Banking with an RS256 JWT (`kid = ENABLE_BANKING_APP_ID`)
  signed by a private key supplied **base64-encoded** in `ENABLE_BANKING_PRIVATE_KEY` (env var, never a file).
- **Consent flow:** `GET /connect` mints a CSRF `state` (Flask session) and 302s to the bank;
  `GET /callback` verifies `state`, exchanges the code via `POST /sessions`, and upserts the
  user's `bank_sessions` row (one per user, replace-on-reconnect).
- **Fetch:** `get_transactions()` paginates via `continuation_key`, keeps `status == "BOOK"`,
  maps DBIT→expense/creditor, CRDT→income/debtor (fallbacks: remittance info → "Unknown"),
  stages into `import_staging` with `suggest_category`. Response shape is identical to `/api/import/upload`.
- **Scoping:** every bank query goes through `current_user_id()` and `account_uid` must
  belong to the stored session; `bank_sessions` cascade-deletes with the user row.
- **Consent lifetime:** PSD2 caps consent at ~90 days; expired → 401, UI prompts reconnect.

**Current live state (as of 2026-06):**
- Enable Banking app is **PRODUCTION**, **Restricted Mode** (account-linking / whitelisted
  own accounts): only the owner's whitelisted accounts return data. Which suits an app
  with one user.
- Default ASPSP `Nordea` / country `FI` (`ENABLE_BANKING_ASPSP`, `ENABLE_BANKING_COUNTRY`).
- Needs `ENABLE_BANKING_APP_ID` and `ENABLE_BANKING_PRIVATE_KEY` (base64 PEM) in the
  environment; without both, `enable_banking_configured()` is False and the UI hides the card.
- The bank returns the browser to `BANK_REDIRECT_BASE` + `/api/import/bank/callback`
  (default `http://localhost:5050`), which must be registered in the EB app config.

**What a 401/403 means depends on the stage**, so every call through `_get` / `_post`
states which stage it is in (`session_scoped`, a required keyword — a new call site
has to decide):

| Stage | Call | 401/403 → | The user sees |
|-------|------|-----------|---------------|
| Before consent | `/auth`, `/sessions` | `BankAuthError` | "This app's bank credentials were refused" — only the owner can fix it |
| On a consent | `/accounts/…/transactions` | `SessionExpired` | "Reconnect your bank" |

Getting this wrong is what sent people round a reconnect loop that could never
succeed. The bank's own response body rides along in both messages.

`/connect` and `/callback` are full-page browser navigations, so they never return
JSON: `_bank_failed()` logs the detail and bounces to `/#import?bank=<reason>`
(`connected` · `cancelled` · `error` · `auth_error` · `not_configured`), which
`handleBankReturn()` turns into a sentence. `/fetch` is an XHR and keeps returning
JSON — `session_expired` (401) and `bank_auth` (502) are separate codes.

### Dashboard
Card order: Monthly Overview → Expenses by Category → Income by Category →
Expense Trends → Spending Heatmap → Monthly Summary.

- Monthly expense vs income bar chart
  - No grid lines or y-axis labels; clean look
  - Y-axis max = data max × 1.15 (15% headroom for labels)
  - White value label rendered inside each bar near top (hidden if bar too short)
  - Net diff badge (green/red pill) floats above tallest bar in each group; clamped to chart area top, never overlaps bar
- Expense/income over time line chart
- Top 5 expense categories trend (last N months)
- **Expenses by Category** and **Income by Category** — the same bars from one
  renderer (`renderBreakdownBars`), over the same period. `breakdownPeriodMonths()`
  is the single answer to "which months are we showing": the explicit month
  picks if there are any, otherwise **every month the horizon covers**. Both
  cards ask it, so they cannot drift from each other or from the rest of the
  page. They once ignored the horizon and always drew the latest single month,
  which put a 3 300 € breakdown beside a 95 616 € total on the same screen —
  the reason `loadDashboard()` now waits for the monthly rows before asking for
  the breakdown. Category lookup matches on **name and type** — "Other" and
  "Investments" exist on both sides.
  - **Clicking a bar** opens a drill-down modal: all transactions for that category in the same period, sorted largest → smallest
- **Monthly Summary** — clicking any row (monthly or yearly view) opens every
  transaction in that month, income and expense, newest first. The note button
  stops propagation so it still opens only the note.
- **Floating period pill** — once the page title scrolls out of view, the
  horizon buttons and period picker are **moved** (never copied) from
  `#dash-header-controls` into `#dash-float-pill`, so there is only ever one
  period dropdown in the DOM. See `initDashboardFloatPill()`.
- Annual report view (`/api/reports/annual`). Every year-on-year figure is held
  to the months the chosen year actually has: `compare_months` limits the
  previous year to the same calendar months, and the labels name the range
  ("Income vs 2025 (Jan–Jul)"). Measuring a part-finished year against a full
  one reported a 28% collapse in income that was really a 12% rise.
- Period filter: YTD, last 3/6/12 months, all time, or custom month picker

> The Cash Flow Calendar was removed — the Spending Heatmap covers the same
> ground over a whole year, so it took over.

### Trends

One category (or several) charted over a range. **The page charts income as
well as spending, so nothing on it may assume spending.** `data.category.type`
comes back from `/api/trends/category` already resolved for a multi-category
pick ("income" only when every one of them is), and three things read it: the
stat card ("Total Earned" vs "Total Spent"), the month chart's title, and the
colours. Up is red on an expense and green on an income — a rising salary
painted red reads as a warning about earning more.

### Subscriptions (recurring detection)

`recurring.py` groups transactions by merchant, infers a cadence from the median
gap, and flags each series:

| Status | Means |
|--------|-------|
| `active` | charging on schedule |
| `due_soon` | next charge within 7 days |
| `price_changed` | an amount-stable series whose latest charge moved >10% |
| `overdue` | one or two expected charges never came — late, probably still live |
| `stopped` | **three or more** missed (`_STOPPED_MISSED_CYCLES`) — the service ended |

The overdue/stopped split exists because without it every long-dead series
shouted "Overdue" beside the genuinely late ones and the column said nothing: on
real data ten rows out of ten were red. Stopped series stay in the table but are
kept **out of `monthly_total` / `annual_total`** — a service that ended is not
part of what you pay each month — so the header reads "737 €/mo · 5 active",
not 855 € across 10. Transfers and investments are excluded from those totals
too, for a different reason (they are movements, not consumption).

### Month Notes
- Per-month text notes stored in `month_notes`
- Accessible via `/api/notes/<YYYY-MM>`

---

## Merchant Rule Auto-Generation

Script: `scripts/generate_merchant_rules.py`

**Algorithm:**
1. Read all transactions, compute dominant category per store (highest count wins)
2. Apply 70% confidence threshold — skip ambiguous stores
3. **Contains-rule grouping:** if store A is a case-insensitive substring of store B and both share the same dominant category, create one `contains` rule for A covering all variants
4. Remaining stores get `exact` rules
5. Skip generic noise stores: `Other`, `Rent`, `Missing info`, `Monthly fee`, `Korko`

Re-run anytime with `python3 scripts/generate_merchant_rules.py` — clears and rebuilds all rules.

---

## Design System
- Light: `#ffffff` bg, `#f6f7f9` grouped, `#0d1320` text, **`#00a06b` green accent**
- Dark: the same roles re-pointed, accent `#00e599`. Both themes are defined in
  full — never give a colour its only value inside the dark block.
- The accent has been green since the redesign. `rgba(0, 122, 255, …)` anywhere
  in the CSS is a leftover from the old blue and is a bug, not a choice.
- 8px spacing grid, rounded corners, subtle shadows
- Sidebar navigation, smooth transitions
- `fmt(amount)` — global currency formatter (fi-FI locale, EUR, **0 decimal places**)

**Every range selector is styled by one rule.** `.horizon-btn`,
`.trends-period-btn`, `.trends-toptx-btn` and `.nw-period-btn` share the
`.active` styling, so the chosen range is marked the same way on every page. A
new selector that does not join that list toggles a class nothing paints, and
no button ever looks selected.

**A filter that cannot be applied has to say so.** `readDateFilter()` marks a
date box `.input-invalid` when it holds text the parser cannot read: the filter
is dropped from the query rather than refused, so without the mark the list
quietly shows everything under a date the user typed. The Filters button
carries the same duty in reverse — it shows how many drawer filters are
narrowing the list, because those are the ones you cannot see when it is shut.

---

## API Surface

```
GET/POST   /api/categories
PUT/DELETE /api/categories/<id>

GET/POST   /api/merchant-rules
PUT/DELETE /api/merchant-rules/<id>

GET/POST   /api/transactions          ?month, months, category_ids, type, q,
PUT/DELETE /api/transactions/<id>      date_from, date_to, amount_min, amount_max,
                                       sort, dir, page, per_page

GET        /api/dashboard/monthly-summary
GET        /api/dashboard/top-expenses
GET        /api/dashboard/category-trends
GET        /api/dashboard/category-breakdown   ?month, months, year, type
                                               (type = expense | income; default expense)
GET        /api/dashboard/heatmap          ?year

GET        /api/reports/annual

GET        /api/recurring               ?lookback_months, min_occurrences
POST       /api/recurring/dismiss               (body: {signature})  hide a series
DELETE     /api/recurring/dismiss/<signature>                       un-hide a series

GET/POST   /api/accounts
PUT/DELETE /api/accounts/<id>                       (DELETE cascades balances)
POST       /api/accounts/<id>/close                 (body: {as_of}) zero + archive
POST       /api/accounts/<id>/reopen
GET/POST   /api/accounts/<id>/balances              (POST upserts on as_of)
DELETE     /api/balances/<id>
GET        /api/networth/history        ?months
GET        /api/networth/summary
GET        /api/networth/holdings       ?account_id[&as_of]
DELETE     /api/networth/holdings/<id>              recomputes that snapshot's total

POST       /api/import/upload
GET        /api/import/staging/<batch_id>
POST       /api/import/confirm
DELETE     /api/import/staging/<item_id>

GET        /api/import/bank/status                 connection state + cached accounts
GET        /api/import/bank/connect                302 → bank consent (mints CSRF state)
GET        /api/import/bank/callback               consent return; verifies state, upserts session
POST       /api/import/bank/fetch                  (body: {account_uid, date_from, date_to}) → stages txns
POST       /api/import/bank/disconnect             drops the user's bank session

GET/PUT    /api/notes/<YYYY-MM>
GET        /api/notes

POST       /api/quit
```
