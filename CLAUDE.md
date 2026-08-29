# Expense Tracker App ("Balance.") — Reference

## Stack

| Layer | Tech |
|-------|------|
| Backend | Python, Flask |
| Database | **SQLite (desktop/local)** or Postgres (hosted) — same query layer, see "Runtime modes" |
| Frontend | Vanilla HTML/CSS/JS, Apple-style design |
| Desktop shell | pywebview (native window on port 5050, `PORT` env-overridable) |
| Charts | Chart.js |
| iOS reader | SwiftUI (iOS 26+, Liquid Glass), Swift Charts |

**Entry points:**
- `main.py` — desktop app launcher (pywebview); on launch creates the schema,
  backs up the DB, and seeds the single local user.
- `app.py` — Flask server with all API routes
- `config.py` — runtime config; `APP_MODE` (`desktop`/`hosted`) drives `USE_SQLITE`
- `db.py` — **database dispatcher**: re-exports the active engine's public surface
- `db_sqlite.py` — local SQLite engine (translates `%s`→`?`, `now()`→`datetime('now')`, dict rows)
- `db_postgres.py` — hosted psycopg connection-pool engine
- `database.py` — schema DDL (one per backend) + seeding + backups
- `scripts/migrate_to_local_sqlite.py` — one-time import of an old single-user DB into the local schema
- `recurring.py` — recurring/subscription detection over transaction history
- `networth.py` — net worth tracking (carry-forward over manual account balances; grouped + holdings).
  Totals come from balances alone: an account you leave out of an update keeps its
  last value, and closing one writes a zero at the closing date rather than hiding
  it, so past months stay true. Never filter `is_archived` in the total queries.
- `investment_import.py` — Nordnet CSV / Nordea xlsx portfolio parsers (→ holdings + Net Worth)

**Single-user local app:** there is no login/OAuth/admin. `current_user_id()`
returns the fixed `config.LOCAL_USER_ID` (one local user, seeded at startup); the
`users` table + `user_id` columns are kept only as an internal anchor so the
query layer is unchanged. iCloud export and the multi-user/auth/admin surface
were removed for the local build (the `ios/` reader and `auth.py`/`mailer.py`
are gone).

---

## Runtime modes (desktop SQLite vs hosted Postgres)

`config.APP_MODE` (env, default `desktop`) selects everything:

- **Desktop / local (default, `USE_SQLITE=True`)** — a single-user macOS app.
  No Postgres, no Supabase, no Google OAuth, no network DB. The DB is a local
  SQLite file at **`~/Library/Application Support/Balance/expenses.db`**
  (override with `SQLITE_PATH`). `require_login` auto-attaches a fixed local user
  (`config.LOCAL_USER_ID = 1`, approved admin) to the session, so the OAuth/access
  gates are satisfied without any login. Run with `python3 main.py`; package with
  `./scripts/build_app.sh` → `Balance.app` / `Balance.dmg`.
- **Hosted (`APP_MODE=hosted`)** — the multi-user cloud build: Postgres via
  `DATABASE_URL`, Google OAuth, access-request approval, gunicorn. Unchanged.

The app is written once against a psycopg-style API (`%s` placeholders, dict
rows, `RETURNING`, `ON CONFLICT`). `db_sqlite` makes that run on SQLite by
translating SQL on the way to the driver and returning dict rows, so route code
is identical on both backends. Backend-specific bits are funnelled through `db`:
`db.IntegrityError`, `db.DatabaseError`, `db.Json(...)`, `db.load_json(...)`.

> History: this codebase began as a single-user SQLite desktop app, was ported
> wholesale to multi-user Postgres for a Supabase/Render deployment, then brought
> back to local SQLite (this mode) to drop the hosting/usage costs while keeping
> all the newer features. The investment **holdings** feature lived on a separate
> branch and is NOT in this lineage.

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
- **Per-user isolation:** every bank query is scoped by `current_user_id()`; `account_uid` must
  belong to the session; `bank_sessions` cascade-deletes with the user.
- **Consent lifetime:** PSD2 caps consent at ~90 days; expired → 401, UI prompts reconnect.

**Current live state (as of 2026-06):**
- Enable Banking app is **PRODUCTION**, **Restricted Mode** (account-linking / whitelisted
  own accounts). This means **only the owner's whitelisted accounts return data** — effectively
  single-user. Opening up to other users connecting their *own* banks requires full production
  activation (commercial agreement with Enable Banking).
- Default ASPSP `Nordea` / country `FI` (`ENABLE_BANKING_ASPSP`, `ENABLE_BANKING_COUNTRY`).
- Render env vars required: `ENABLE_BANKING_APP_ID`, `ENABLE_BANKING_PRIVATE_KEY` (base64 PEM).
- `bank_sessions` table must exist in Supabase (`init_db()` / the Step-1 DDL creates it).
- ⚠️ Error mapping is currently coarse: a 403 at **connect** (app JWT rejected) is reported as
  "consent expired", which is misleading at that stage. Candidate cleanup.
- This feature is **hosted-only** (`main` lineage). The desktop `dev` branch has a separate,
  local variant (reads `~/.bank-mcp/config.json` + SQLite).

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
  renderer (`renderBreakdownBars`), over the same period. The expense card
  resolves the period; `loadIncomeBreakdown()` then pins income to it, so the
  two can never describe different months. Category lookup matches on **name
  and type** — "Other" and "Investments" exist on both sides.
  - **Clicking a bar** opens a drill-down modal: all transactions for that category in the same period, sorted largest → smallest
- **Monthly Summary** — clicking any row (monthly or yearly view) opens every
  transaction in that month, income and expense, newest first. The note button
  stops propagation so it still opens only the note.
- **Floating period pill** — once the page title scrolls out of view, the
  horizon buttons and period picker are **moved** (never copied) from
  `#dash-header-controls` into `#dash-float-pill`, so there is only ever one
  period dropdown in the DOM. See `initDashboardFloatPill()`.
- Annual report view (`/api/reports/annual`)
- Period filter: YTD, last 3/6/12 months, all time, or custom month picker

> The Cash Flow Calendar was removed — the Spending Heatmap covers the same
> ground over a whole year. Its endpoint `/api/dashboard/daily-totals` is still
> served but now has no caller.

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
- Apple-style: white bg, `#f5f5f7` secondary, `#1d1d1f` text, `#0071e3` accent
- 8px spacing grid, rounded corners, subtle shadows
- Sidebar navigation, smooth transitions
- `fmt(amount)` — global currency formatter (fi-FI locale, EUR, **0 decimal places**)

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
GET        /api/dashboard/daily-totals          (unused since the calendar went)

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
