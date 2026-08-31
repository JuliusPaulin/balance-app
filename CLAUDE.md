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
- `ai_tools.py` — the seven read-only tools the chat assistant may call, each a
  wrapper over an endpoint the app already serves
- `ai_chat.py` — the agent loop: system prompt and the tool-call cycle
- `ai_backends.py` — where the model runs. llama.cpp bundled in the app (the
  default), Ollama (for development), and Anthropic (the control), behind one
  interface
- `model_runtime.py` — the model that ships with the app: where the runtime and
  the weights are, fetching the weights once, running the server, and which of
  the GPU and the CPU it ended up on
- `patches/metal-readback.patch` — the one change carried on top of llama.cpp,
  without which every Mac on macOS 13 or older loses the GPU.
  `scripts/fetch_runtime.sh` applies it and compiles the result
- `scripts/ask.py` — ask the assistant from the terminal; prints which tools it
  called, which is how a prompt problem is told apart from a model problem

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
| `chat.py` | the AI assistant: availability, and one answered turn |

They lean on each other in **one direction only**, so nothing imports in a
circle: everything imports `core`; `csv_import` takes the rule rebuilder from
`merchant_rules`; `bank_import` takes the staging helpers from `csv_import`.
Blueprint endpoint names are now prefixed (`categories.get_categories`), which
costs nothing here because the app has never used `url_for`.

**The `static/js/` files.** `app.js` held the whole frontend until it reached
5,300 lines — the same size problem `app.py` had, one layer up. It is now one
area per file, loaded in order by `index.html`.

| File | Covers |
|------|--------|
| `core.js` | the plumbing every area shares: `api()`, the CSRF fetch wrapper, the loading overlay, `toast()`, `confirmDialog()`, navigation, theme, chart theming, the category palette, `fmt()`, card fullscreen |
| `categories.js` | the Categories tab |
| `merchant-rules.js` | the store-name patterns |
| `transactions.js` | the list, the filter rail, the facet counts |
| `import.js` | CSV + bank import, the review ledger, the category picker, bulk select, split, import history |
| `period.js` | the horizon buttons, the month picker, the floating pill |
| `dashboard.js` | the Dashboard cards, the breakdowns and their baselines, the summary table, the heatmap |
| `reports.js` | the annual report |
| `net-worth.js` | accounts, balances, holdings, the investment import |
| `trends.js` | the Trends page and its drilldowns |
| `subscriptions.js` | recurring charges |
| `settings.js` | quit, retrain the rules, the guide |
| `chat.js` | Balance AI: the panel, the event stream, the working under each answer |
| `main.js` | app state and start-up — loaded last, `init()` is the final line |

**They are plain scripts sharing one global scope, not ES modules, and that is
deliberate:** `index.html` wires ~95 inline `onclick=` handlers straight to these
function names, so every one of them has to stay global. **Load order in
`index.html` is part of the contract** — `core.js` first, `main.js` last, and the
handful of listeners registered at load time (the CSRF wrapper, the nav buttons,
the drop zone, two outside-click closers) run in the order the tags appear.
Nothing else executes at load, which is why the split could move whole sections
without touching a line inside them.

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

`python3 -m pytest tests/` — 368 tests, all green.

`conftest.py` points `SQLITE_PATH` at a throwaway file **at import time**, before
pytest collects any test module. This matters: test modules `import config` /
`import db` at the top, and both read their settings once on first import, so
setting the path in a fixture would be too late and the suite would write into
the real database. There is an assert guarding it.

Every table has `user_id` with `ON DELETE CASCADE`, so each test resets by
deleting the one user row and re-seeding.

The route tests drive the real app over HTTP through the `client` fixture, and
`tests/helpers.py` holds the two things they all need: `cat_id()` looks a
seeded category up by **name and type** (creating a second "Groceries" would
test a category the app never uses), and `add_tx()` posts a transaction. It also
builds the two broker exports — `nordnet_csv_bytes()` writes a real one (UTF-16,
TAB, decimal comma) and `nordea_xlsx_bytes()` an xlsx with the export timestamp
in row 0 — because the parser tests and the import route tests need the same
files.

**The broker parsers are tested against the shape of the real exports**
(`test_investment_import.py`), because nobody here owns that format: a renamed
column or a switched decimal separator raises nothing, it just parses to the
wrong number and lands in the net-worth total as fact. `test_holdings.py` then
covers the route from an upload to that total — that a snapshot is
`(account_id, as_of)` and re-importing the same day lands on it rather than
doubling the portfolio, and that the account's `account_balances` row always
equals the sum of its holdings. A holdings write that doesn't move the balance
shows the right drill-down under the wrong total.

---

## Database Schema

| Table | Key fields |
|-------|-----------|
| `transactions` | id, date, store, category_id (FK), amount, type (expense/income), import_batch_id (FK, nullable — the import that created it, NULL if typed by hand or imported before the column) |
| `categories` | id, name, type, is_default |
| `merchant_rules` | id, pattern, category_id (FK), match_type (exact/contains/smart) |
| `import_staging` | id, date, store, suggested_category, amount, type, confirmed, final_category_id, import_batch_id |
| `import_batches` | id, filename, imported_at, status (pending/completed/undone) |
| `import_formats` | id, signature (SHA-1 of the header names + delimiter), delimiter, date_col, amount_col, store_col (nullable), amount_sign (neg_expense/pos_expense); UNIQUE(user_id, signature) |
| `month_notes` | id, month (YYYY-MM), note |
| `accounts` | id, name, type (asset/liability), sort_order, is_archived, external_id (the broker key an import matches on), group_name |
| `holdings` | id, account_id (FK, cascade), as_of, name, isin, units, value_eur, return_pct, return_eur, currency; UNIQUE(account_id, as_of, name). No `user_id` — it is reachable only through `accounts`, which has one |
| `account_balances` | id, account_id (FK, cascade), as_of (YYYY-MM-DD), balance; UNIQUE(account_id, as_of) |
| `recurring_dismissed` | id, signature (UNIQUE, = normalized merchant + cadence), dismissed_at |
| `manual_subscriptions` | id, store, amount, cadence (monthly/quarterly/yearly), category, type |
| `bank_sessions` | id, user_id (FK, cascade), session_id, aspsp_name, aspsp_country, valid_until, accounts (JSONB), created_at; UNIQUE(user_id) |

---

## Implemented Features

### Transactions
- CRUD: create, read, update, delete
- Filters: by month, category, type, store (search), date range, amount range
- `GET /api/transactions` query params: `month`, `months` (comma-separated), `category_ids`, `type`, `q`, `date_from`, `date_to`, `amount_min`, `amount_max`, `sort`, `dir`, `page`, `per_page`

**The filters live in a rail beside the table, not a drawer above it.** The
drawer pushed the list off screen at the moment you were filtering it, hid
what it was doing once shut, and held 34 category chips in a six-row wall with
no search, no counts and no order you would guess. The rail is always there,
so nothing opens, nothing closes, and the table never moves.

- **Every value carries the count it would give** — `GET
  /api/transactions/facets`, which takes the same query params as the list.
  That is what makes the rail worth the width: "Groceries 793" is a read on
  the spending as well as a control, and a dead end can say so.
- **A facet ignores its own filter and honours every other.** Picking
  Groceries must not collapse the category counts to Groceries alone — you
  still need to see what adding Lunch would give — but the *type* counts do
  narrow, because that filter is not theirs. `_filter_clauses(args, uid,
  omit=…)` in `routes/transactions.py` is the single WHERE builder for both
  the list and the counts, so the counts can never describe a different
  filter than the table under them. `omit="period"` covers `month`, `months`
  and the date range together — the rail shows them as one section and each
  undoes the others.
- **A selected value never disappears.** The facet query groups over rows
  that exist, so a category the other filters have taken to zero is simply
  absent from the result — and the rail would then show no sign of a filter
  it is applying. `withSelected()` puts it back at 0, and `collapseKeeping()`
  makes sure the "Show N more" cut never hides it either.
- Categories come back **most-used first**. Alphabetical order buried the five
  categories you live in under the twenty-nine you touch twice a year.
- **Sorting belongs to the table headers.** The old "Sort by" and "Direction"
  dropdowns duplicated headers that already sorted on click and already drew
  the direction arrow; they are gone and the state lives in `txSort`.
- Applied filters also render as **removable tokens above the table**, because
  at ≤1024px the rail stacks below the list and starts shut — and a filter you
  cannot see is the fault the whole page exists to fix.
- Mockups for the four directions considered: `docs/mockups/tx-filters/`.

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
- **A rule says what it would do before you save it.** `/api/merchant-rules/preview`
  runs the candidate pattern over the whole history and returns the match count,
  how many distinct stores it hits, and the first rows — a `contains` rule on a
  four-letter pattern can quietly own half the table, and the modal shows that
  while you type.
- `POST /api/merchant-rules/<id>/apply` re-categorises the history a saved rule
  matches. It **skips rows of the other type**: a rule pointing at an expense
  category must not reassign income, which would move a salary under "Groceries".
- `/api/merchant-rules/stats` gives each rule a hit count and its last match —
  the basis for spotting rules that no longer fire.
- `POST /api/merchant-rules/train` rebuilds every rule from history, running the
  same algorithm as the script below (`_rebuild_merchant_rules()` in
  `routes/merchant_rules.py` is the shared implementation, and confirming an
  import auto-retrains through it). It **clears the user's rules first**, so a
  hand-written rule does not survive a training run.

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
- Category auto-suggested via merchant rules, then a historical fallback;
  defaults to "Other" when unknown. **Both halves use the same 70% bar.** The
  fallback used to take a bare plurality, so a store the rule generator had
  refused a rule for (too ambiguous) still drew a confident-looking suggestion:
  a 2-of-4 history put an electronics purchase under "Dog". Below the bar it
  now suggests nothing and the row says "needs review", which is the truth.
- Staged in `import_staging` for user review before committing
- Review table: all fields (date, store, amount, category, type) are editable
  inline before confirming. The amount box is `type="text"` on purpose —
  `type="number"` swallowed a comma before any of our code saw it, and a comma
  is what a Finnish hand types into an app that prints "16,05" and imports
  CSVs written "-25,00". `parseAmountInput()` takes either separator and says
  so when it cannot, the way the date box always has. It also strips the
  non-breaking space `fmt()` groups thousands with, so "2 500,55" — the app's own
  output, copied or retyped — reads back rather than being refused.
- **÷2 Split costs** halves the **expense** rows — your share of a statement
  you split with someone. It used to halve the salary too, and compound on a
  second click with nothing on screen to say the first had landed. It is a
  toggle now, and undo restores the exact amounts rather than doubling a
  rounded half.
- **Cancel discards the batch** (`DELETE /api/import/batch/<id>`). A *confirmed*
  batch is the record of what was imported and is refused (409); only a pending
  one can be discarded.

### CSV format learning (unknown layouts)

A CSV whose columns the alias table cannot place used to be a **400 and a dead
end** — the app knew the file was a bank export and still refused it. Now the
user maps the columns once and the app remembers the layout.

- `POST /api/import/upload` returns **200 with `{needs_mapping: true, signature,
  delimiter, headers, sample_rows, guess}`** instead of an error, and creates no
  batch. There is nothing to clean up if the user walks away.
- The UI opens a mapping step: the first five rows under their header names,
  a dropdown per field (date and amount required, merchant optional), an
  **amount-sign** toggle (`neg_expense` — the default — or `pos_expense`, for
  banks that write expenses positive), and "Remember this format", on by default.
- `POST /api/import/upload-mapped` re-posts the file with the chosen indices and
  stages the rows, returning **the same shape as `/api/import/upload`** so the
  review table takes it unchanged.
- **The signature is recomputed server-side**, never taken from the client:
  `format_signature()` is SHA-1 over the lowercased, trimmed header names joined
  with `|`, plus the delimiter. The same export always fingerprints the same, so
  the next file of that layout auto-maps and the user never sees the step again.
- A saved mapping is only consulted **when auto-detection fails**, so teaching
  the app one layout can never change how a recognized one imports.

`GET /api/import/formats` and `DELETE /api/import/formats/<id>` are the reader
and the forget button for saved mappings — both implemented, **neither called by
the UI**, so a bad mapping currently cannot be removed from inside the app. That
is the same gap `import_batches` had before the Recent imports card.

### Import history

`import_batches` was written by three code paths and read by none, so an
abandoned review vanished with nowhere to resume from and a finished one left
no record. `GET /api/import/batches` is the reader, and the **Recent imports**
card on the Import page shows it. Four states:

| State | Shows | Offers |
|-------|-------|--------|
| `pending` | rows still waiting | **Resume** (reopens the review) · **Discard** |
| `completed`, linked | transactions and sums | **Undo** |
| `completed`, unlinked | "too old to undo" | nothing |
| `undone` | "its transactions were removed" | nothing |

**Undo** (`POST /api/import/batch/<id>/undo`) deletes the transactions one
import created and marks the batch `undone` — the record stays, because the
history should say what happened rather than pretend the import never did. An
import writes hundreds of rows at once and the app has no other bulk undo.

It works because `transactions.import_batch_id` stamps each row with the import
that made it. Rows from before that column stay NULL and report **"too old to
undo"**: there is no honest way to work out after the fact which rows were
whose, and guessing would put an undo button on transactions it might not own.

`GET /api/import/staging/<batch_id>` returns the same `{batch_id, count, items}`
shape as an upload, because Resume feeds it straight into the review table. It
used to return a bare array — two shapes for one resource, unnoticed because
nothing read it.
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

- Monthly expense vs income bar chart, over whatever the period controls cover
  - A single month (one explicit month picked) is two bars, pinned to
    `barThickness: 72`. `maxBarThickness` alone is not enough there — with one
    category Chart.js measures the slot from the gap between neighbours, has
    none to measure, and settles on a sliver on the first paint after load.
  - No grid lines or y-axis labels; clean look
  - Y-axis max = data max × 1.15 (15% headroom for labels)
  - White value label rendered inside each bar near top (hidden if bar too short)
  - Net diff badge (green/red pill) floats above tallest bar in each group; clamped to chart area top, never overlaps bar
- Expense/income over time line chart
- Top 5 expense categories trend (last N months)
- **Expenses by Category** and **Income by Category** — the same bars from one
  renderer (`renderBreakdownBars`), over the same period. Both open on the
  **latest month alone** and ignore the period controls at the top of the page
  while they do; a **Latest month / Period** toggle on each card switches them
  back to following those controls. One `breakdownScope` for the pair, so either
  set of buttons moves both — they answer the same question about the same
  months, and a pair that could disagree is the bug the next paragraph is about.
  `breakdownPeriodMonths()` is the single answer to "which months are we
  showing": on `latest`, the latest month there is data for (not the latest
  month the period covers); otherwise the explicit month picks if there are
  any, else **every month the horizon covers**. Both
  cards ask it, so they cannot drift from each other or from the rest of the
  page. They once ignored the horizon and always drew the latest single month,
  which put a 3 300 € breakdown beside a 95 616 € total on the same screen —
  the reason `loadDashboard()` now waits for the monthly rows before asking for
  the breakdown. Category lookup matches on **name and type** — "Other" and
  "Investments" exist on both sides.
  - **Each bar carries its own baseline.** A tick on the track marks that
    category's **median month** over the six months *before* the one on
    screen, and the right-hand column says how far this month landed from it.
    A number alone ("Groceries 612 €") is a fact waiting for a comparison;
    the app is the only thing in the room that knows whether that is a lot.
    - The judged month stays **out of its own baseline**, so "usual" means
      what it says. A **median**, not a mean — one holiday should not get to
      redefine normal.
    - A month where a category saw nothing counts as **0**, not as missing.
      Something you buy twice a year is unusual every time, and dropping the
      empty months would report it as routine.
    - **Only a single month gets a baseline.** A twelve-month sum has no
      monthly normal to stand beside, so `median` comes back `null` over a
      period and the bars draw exactly as they did before. The "Latest month"
      scope asks as `?months=<one month>`, so that path is covered too.
    - Under **three months of history** there is no honest baseline and the
      endpoint sends none — better than calling your second recorded month a
      300% overspend.
    - `fixed` marks a category that does not move *and* has not moved this
      month (rent, insurance). Saying "0% off normal" beside rent every month
      teaches the eye to skip the column carrying the news. A flat category
      that **jumps** loses the flag and reports the jump — that is the loudest
      thing on the card.
    - Two bands in `dashboard.js`, both wide on purpose: `QUIET_BAND` (25%) is
      ordinary movement and reads "as usual"; only past `OUTLIER_BAND` (50%)
      does the **bar itself** turn red. At 10% most of a real card went red
      and the colour stopped meaning anything. Past 4× the median a
      percentage is unreadable — "38× usual" beats "+3726%".
    - Polarity follows Trends: over is **red on spending, green on income**.
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

**Detection misses things**, so a series can be added by hand: `POST
/api/subscriptions` writes to `manual_subscriptions` (store, amount, cadence of
monthly/quarterly/yearly, optional category, expense or income) and the next
`/api/recurring` folds it in beside the detected ones; `DELETE
/api/subscriptions/<id>` removes it. A hand-added row is a claim about the
future rather than a reading of the past, which is why it lives in its own
table and not as a fake transaction.

### Net Worth & investment holdings

Accounts are kept by hand (`/api/accounts` + `/api/accounts/<id>/balances`), and
`networth.py` carries the last balance of each forward — the rules that keeps
true are in the entry-points table above. Two things sit on top of it:

- **Holdings** are the per-product rows behind an investment account, snapshotted
  by date. `GET /api/networth/holdings?account_id[&as_of]` is the drill-down
  (latest snapshot by default, largest position first).
- **`DELETE /api/networth/holdings/<id>`** is the "I sold this" action. The
  account's total for that date was written as the sum of its holdings, so it is
  **recomputed and written back** — otherwise the drill-down and the total
  disagree. Only a balance row that already exists is updated: deleting from an
  old snapshot must not invent a data point in the net-worth history. Earlier
  snapshots keep the holding, because they are what was held at the time.

**Investment import** (`investment_import.py`) parses Nordnet CSV and Nordea
`Omistukset.xlsx` exports into broker → account → holding.
`POST /api/networth/import-investments/preview` parses and **writes nothing**,
returning the hierarchy plus a dedupe hint per account (`external_id`, then IBAN
for cash accounts, then name) — a *suggestion* for the review screen, never an
automatic merge. `POST .../confirm` writes what the user reviewed.

- **A snapshot is `(account_id, as_of)`.** Re-importing the same day lands on the
  same snapshot rather than doubling the portfolio, and several files for one
  account (stocks and funds are separate exports) **union** their holdings
  instead of overwriting.
- The account total goes into `account_balances` as the sum of its holdings,
  which is what the history reads. Cash accounts carry the figure directly and
  hold nothing.
- `as_of` must be `YYYY-MM-DD` or the import is refused — the date keys
  everything, and a bad one makes a snapshot nothing can find again.
- The parsers are held to the real export shapes in `tests/test_investment_import.py`;
  a missing "Arvo EUR" column or an unrecognized Holdings layout is **refused**
  rather than imported as a portfolio of zeroes.

### AI Chat Assistant

A chat panel that answers questions about your own figures, **running on a
local model by default** — Ollama on this machine, nothing leaving the disk.
The app has always been one SQLite file on your own Mac, and a panel that
posted a transaction history to somebody's API would be the first thing it ever
did that contradicts that.

**There is nothing to set up.** The app carries llama.cpp's own server in its
bundle and downloads one model file the first time you open the panel: Qwen 3.5
4B at 4-bit, 2.7 GB, Apache 2.0. Ollama is still a backend and still the quicker
way to work on this — swapping models is one `ollama pull` rather than a
rebuild:

```
AI_BACKEND=local ollama pull qwen3.5:4b
python3 scripts/ask.py "what did I spend on groceries last month?"
```

**Why llama.cpp and not the Ollama binary.** Both are MIT and both would fit in
a bundle; Ollama is even what all of this was proved against. But `llama-server`
is 14 MB against Ollama's 65, it will not collide with an Ollama the user is
already running, and — the deciding reason — it takes a plain model file. Owning
the file is what "download it on first launch" needs; driving `ollama pull` or
writing into its blob store would be worse.

Four things were checked before any of it was written, because all four had to
hold or the approach was dead: the tool calls come back in the same shape, the
prompt prefix is cached between the two calls of a turn, the answer streams, and
the model can be told not to think. That last one is the trap. Qwen reasons by
default here, llama.cpp's `--reasoning off` only decides where the thoughts are
*put*, and `enable_thinking: false` through the chat template is the actual
switch — the difference between a first word at 0.14s and one at 14.4s, and
invisible until someone wondered why the shipped build felt slower than Ollama.

**Ollama's weights are not portable.** The obvious shortcut — point llama.cpp at
the GGUF Ollama already downloaded — fails: `qwen35.rope.dimension_sections has
wrong array length; expected 4, got 3`. Ollama patches llama.cpp, so its blobs
load in its build and not upstream's. The weights come from Hugging Face, and
the runtime is pinned to a build (`scripts/fetch_runtime.sh`) for the same
reason — what this app depends on is not llama.cpp in general but those four
behaviours of it.

**Six kinds of "not ready", and only one of them is the user's to act on.**
Ollama had two and both wanted a command typed into a terminal. Owning the
weights changes the shape of the question, so `/api/chat/status` now carries a
`state`, and the panel switches on it:

| `state` | The panel shows | Asks for |
|---------|-----------------|----------|
| `ready` | the transcript | — |
| `needs_download` | what it is and how big | a click |
| `downloading` | a bar, and that it resumes | patience |
| `download_failed` | what stopped it, nothing lost | a click |
| `starting` | that it is loading | nothing |
| `start_failed` | what the server said, and an offer to fetch the model again | a click |
| `unsupported_os` | that this Mac is below macOS 13.3 | nothing |
| `no_runtime` | that this build has no model in it | nothing |

The download streams to a `.part` file and resumes, because 2.7 GB is far too
much to fetch again because a laptop lid closed. **It also checks that it
finished**: a stream can end early without raising anything, and `model_present`
used to ask only whether the file existed and was non-empty — so half a model
passed as a whole one, `llama-server` could not load it, and the panel said
"starting up" until somebody gave up. The size must match what was promised, and
the file has to begin `GGUF`.

**The GPU crash, and the four explanations of it.** A 16 GB M2 Air stopped at
`ggml-metal-context.m:361: GGML_ASSERT(buf_dst) failed`. That line wraps a
pointer with `newBufferWithBytesNoCopy:`, which returns nil — not an error —
when the address is not page-aligned. Three readings of it were wrong and shaped
six releases: that the Mac had run out of memory (it had 10 GB of GPU free),
that llama.cpp would put back what would not fit (it does not, it dies), and
that the memory-mapped model handed out the unaligned pointer (it does not).

The pointer is llama.cpp's **logits buffer** — the destination it reads results
back *into*, allocated by the CPU backend and aligned to 32 bytes. Nothing
started from here reaches it, which is why every flag was tried and every one
aborted: `-ngl 1` as surely as `-ngl 999`, every context size, every load mode,
with and without the app's library path. What separates the machines is the OS.
macOS 14 and later take the pointer; **12 and 13 refuse it**, so every Balance
user on Ventura or older had a CPU-only assistant and no way to tell.

**So the fix is in the build, not in how the server is started.**
`patches/metal-readback.patch` guards that one call: when Metal refuses the
pointer, drain the queue and copy the result out of the source buffer by hand.
Buffers there are `MTLResourceStorageModeShared` and Apple silicon has one
memory pool, so the bytes are already somewhere the CPU can read. Measured over
a 400-token answer the slow path costs about **1%** — 32.2 tokens a second
against 32.2 — which is the difference between this and the fallback it replaces:
the CPU runs at **23 against 310**, where a simple question takes over two
minutes and a month's analysis cannot finish inside the request timeout at all.

macOS 14 and later never reach the guarded branch, so nothing changes for a
current Mac. That is also what made it untestable, so
`GGML_METAL_FORCE_READBACK=1` takes the slow path anywhere — six releases
shipped a theory about this crash that could not be run on any machine to hand,
and the fix should not be another one.

**Which is why `scripts/fetch_runtime.sh` compiles llama.cpp instead of
downloading it.** There is no patched release to fetch: the bug is unfixed
upstream ([#16266](https://github.com/ggml-org/llama.cpp/issues/16266), reported
Sept 2025, closed by a stale bot with nothing done, and the one attempted fix
withdrawn because the maintainer has no machine old enough to reproduce on). So
the release workflow carries a compiler now — 95 seconds cold on eight cores,
about four minutes on a GitHub runner's three — and `vendor/llama/BUILD.txt`
says `b10715+metal-readback` rather than `b10715`.
Pinning to a build from before the bug is not an escape: it landed in Sept 2025
and Qwen 3.5 support landed in Feb 2026, so no build exists that has one without
the other.

**A patch that goes missing has to fail loudly**, because nothing about losing
it looks wrong — the server starts, the assistant answers, and every Mac below
macOS 14 is quietly back on its processor. `model_runtime.runtime_patched()`
reads the marker, a test asserts it, and the release workflow checks it inside
the finished bundle. The runtime is built *before* the tests run for that
reason.

The CPU stays as the last resort, for the machine nobody has tested on yet, and
`/api/chat/status` now says which of the two is running: `accelerator` is `gpu`
or `cpu`, and on `cpu` the panel says so and warns that a long analysis will not
finish. `ready` used to mean both, so a Mac twelve times slower than it should
be looked exactly like one that was fine.

**Only the GPU is negotiable, and the context was too small anyway.** Fixing
the GPU on Sofie's Mac left the assistant still answering nothing after ninety
seconds, and that was a second fault wearing the same face. Measured against the
real database, **every one of the four questions the panel suggests on its own
empty screen** overflowed the 8 192-token window:

| Question | Tokens in |
|---|---|
| what did I spend on groceries last month? | 8 612 |
| what are my subscriptions costing me? | 9 261 |
| make a trend analysis of my latest month | 10 392 |
| how did last month compare to my usual? | 10 406 |

These are single questions, not long conversations — a turn carries the system
prompt, the tool schemas and one tool result. Over the line llama.cpp logs
`Context size has been exceeded`, the decode fails, and the panel gets an empty
reply, which reads as a hang. `MIN_CTX` is now **16 384**: about 57% headroom
over the worst of them, and roughly 0.7 GB of KV cache — 3.5 GB resident against
the model's 2.7 GB, which a 16 GB Mac has. It is asserted across every fallback
level, and a test holds it above the measured worst turn.

An early step-down once halved the context to 4 096 to save memory. The server
started, took a long time and answered nothing at all, which is worse than the
crash it replaced, because a crash says something.

**And the server the last session left behind is not adopted blindly.** The
model server is a child process, stopped only when the window closes cleanly —
force-quit Balance and it stays resident with the port still answering.
`ensure_running()` opened with "is something already answering?", and a leftover
holding the right model file answered yes, so the new launch called itself ready
and never started its own. Harmless when that leftover is configured the way
this version wants; ruinous when it is not. A server that fell back to the CPU
in one session outlived every update meant to fix it, and the only cure was a
reboot nobody would think to try — one Mac was updated three times against a
process older than the first of them. A leftover is now matched against the
flags this version would use: kept when they agree, replaced when they do not,
and either way named in the log.

**Balance runs on more Macs than the assistant does.** The app needs Apple
silicon, which is macOS 11 and up; the bundled llama.cpp reports `minos 13.3`.
That leaves a range of machines where everything else works and the model server
cannot start at all — dyld refuses the binary and the process dies with a
backtrace. `macos_too_old()` checks first, so such a Mac is told plainly instead
of downloading 2.7 GB it can never use.

**And a start that keeps failing says so.** Every poll nudged another attempt,
each one failed, and the screen never changed. After two tries the state becomes
`start_failed` and carries the telling line out of the server's output — not the
last one. A crashing process ends in a backtrace, and a user was shown "It said:
9 dyld 0x00000001ab9c7f28 start + 2236", which no one can act on and which sat
directly beneath the sentence that explained it. "Starting up" for ever is the least useful thing a
screen can say. `licences/` travels in the
bundle and is copied beside the weights when they land — Apache 2.0 permits the
redistribution and asks that its terms and the attribution go with it, and the
first version of this fetched them from the model's own repository, which
declares the licence in its metadata and ships no licence file. It 404'd, and
the obligation quietly went unmet.

`GET /api/chat/status` live-probes Ollama and reports what is actually
installed, because "not configured" is a useless thing to tell someone whose
server is running and who simply typed the model name differently.

**Why 4b and not 9b.** Both were put through the same eleven questions against
the real database. They scored the same — 4b read the seasons better, 9b was
tidier about quoting — while 4b is half the size (3.4 GB against 6.6 GB) and
about twice as fast, which is the difference between needing 16 GB and running
on 8 GB. There was no accuracy to trade away, so it is the default. Set
`OLLAMA_MODEL=qwen3.5:9b` to go back.

**2b is the floor, and it is below it.** Under the same questions it answered
about half, and the two it got wrong it got wrong confidently: asked for the
largest purchase in three months it reported June's entire expense total,
3 523 €, as a single charge for car charging — the real figure was 21 €. Asked
what it spent last month it passed `period=last_month` and `month=2026-08`
together and answered for the wrong month, which is the calendar arithmetic the
tools exist to take away from it. It also tried to call a `delete_transaction`
tool that does not exist. Nothing was written, because the dispatcher only
knows seven functions and refuses everything else — but the model reaching for it
is the argument for keeping the assistant read-only, not a reason to relax it.

Note that a `month` argument still overrides a `period` when both are passed, on
the grounds that a named month is the more specific instruction. That rule was
written for a user naming a month; 2b was the first model confused enough to
name a contradictory one, and 4b and 9b never do. It is left as it is.

**The assistant does not query the database and does not do arithmetic.** It
picks one of seven read-only tools in `ai_tools.py`, each of which dispatches one
of the app's own GET endpoints in-process — so a number it reports is a number
the Dashboard would draw, computed by the same SQL through the same
`_filter_clauses`. Three rules make that hold, and each exists because the
obvious alternative fails in an app about money:

| Rule | Why |
|------|-----|
| No SQL from the model | A plausible query with the join or the sign wrong reports a false number, confidently. Picking one of seven functions is a task a small local model can also do. |
| No arithmetic from the model | Every amount comes back twice — a raw float and a preformatted `_eur` string. The model quotes the string; a rounding it never performs is one it cannot get wrong. |
| No calendar arithmetic from the model | "Last month" and "since summer" are where small models actually fail. Tools take a `period` name from a fixed list and `resolve_period()` turns it into explicit months, in Python, where it is tested. Month **names** go the same way: `months_by_name` in the context resolves "June" to `2026-06`, because asked what it spent in June the model tried `last_3_months`, then `last_6_months`, then `this_year`, and reported a three-month total as one month's. |

**A tool has to hand back every total it will be asked for**, or the model works
it out anyway. Asked what it earned last year, it added up twelve monthly income
figures itself and answered 36 135 € against a real 36 840 €: confident, and
705 € out. Rule two only holds while there is a string to quote, so
`monthly_summary` carries the period's own totals, `annual_report` carries the
year-on-year change with its direction already decided, and
`search_transactions` carries the sum of everything it matched.

**The same goes for every comparison.** Quoting a figure is a small model's
strength; comparing two is not. Given a month beside its usual month it read
both correctly and then filed Medical at 74 € against a usual 9 € under "saving
money" — about half the list came back inverted. So `category_breakdown` states
`direction` and `vs_usual` outright, and `reads_as` says whether the gap is
worth mentioning at all, using the Dashboard's own quiet band so the panel and
the bars never disagree.

**And a flag is not a boundary.** `list_subscriptions` marked the salary
`counts_toward_total: false`; asked for the three biggest subscriptions the
model sorted every row by cost and led with it, labelled "(income)" and still
wrong. Income, transfers and stopped series now sit in a separate
`also_recurring` list, each saying why. Two lists cannot be sorted across.

**Nothing may fail quietly into a number.** Two things guard that. A dispatch
that does not return 200 raises `ToolDispatchError` rather than returning
`None` — the tool bodies' `or {}` used to turn that into an empty result, which
the assistant read aloud as "0 €". And an invented period name is refused with
the valid list rather than resolved to the current month: asked whether July
beat June, the model made up `last_2_months`, was handed August without a word,
and gave up on a question the data answers.

**Making the model comfortable is not the same as making it right.** It kept
asking for a `last_2_months` period, so that period was added — and the answers
got worse. "The last two months" is July and August; it was asking in order to
compare June with July. Given a window that sounded right and did not contain
June, it reported June at 3 598 € against a real 3 523 €. The period came out
again. `monthly_summary` takes an explicit `months` list instead, because the
question it could not answer was never a missing period: every period is a
window ending today, and "June against July" is not one. A tool that cannot say
the thing has to be taught to say it, not given a near-miss.

**The tools read the app's routes, so they have to be on the app.** `core` holds
the bare Flask object and `routes.register(app)` attaches the blueprints;
`app.py` calls it on start-up and `ai_tools._call_api` calls it before it
dispatches. Without that second call every tool 404s in any process that does
not import `app.py` — which is `scripts/ask.py`, the harness the model is judged
with. The suite never saw it, because its `client` fixture imports `app`; the
regression test runs in a subprocess for that reason.

**`analyse_month` is the odd one out**, and deliberately. The other six answer a
question; this one is handed a month and returns the whole of it — totals
against the month before, every category against both last month and its own
usual month, the largest charges, what moved most, and what the subscriptions
came to. "Tell me what stands out" is not a question with a lookup behind it,
and a small model cannot do the reading if it has to decide what to fetch four
times first. So the fetching is decided in Python and the model does the part it
is good at: noticing which of it is worth saying.

It is the longest result any tool returns, so it drops the raw floats the rules
forbid the model to do arithmetic on anyway, and caps both lists.

**Two comparisons, each with its direction already decided.** A category can be
above its usual month and below last month at once, and given only the figures
the model got it wrong: Medical at 74 € against 335 € in July came back as
"spiked to 74 €, up from 335 €". Every row now carries `vs_last_month_direction`
and `vs_usual_direction`, named so that neither can be attached to the wrong
comparison. It still muddles that particular category about one run in three —
the numbers are right and the connective prose is not, which is where a 4B model
sits.

Read-only on purpose — the app has no undo for a hand-edited row.

The loop in `ai_chat.py` is hand-written and knows nothing about which model
answered: it asks a backend for a turn, runs the tools that turn requested, and
hands the results back. `ai_backends.py` holds the two implementations and all
the wire-format translation, so a third (llama.cpp embedded in the app, once
this is proven) is a change in one file. `MAX_TOOL_ROUNDS` caps the cycle at
four — small models circle more readily than large ones — and the final turn
withdraws the tools so a stuck conversation ends in a sentence rather than a
fifth identical lookup.

**Balance AI** is what the panel is called. Which model answers is a setting,
not a brand, so the model name appears in exactly one place: the set-up card,
where it is the thing you have to type.

**The panel.** A round button at the bottom right, "Balance AI" in the sidebar,
or ⌘K, slides a column in from the right.
A column and not a modal: the assistant answers about the figures on the page,
so covering them would defeat it. Below 1024px there is no beside and it takes
the width.

Three things in it are load-bearing rather than decoration:

- **Every answer shows what it read.** `chat()` returns the tool trace and the
  panel renders it under the reply — "1 lookup · Read your transactions ·
  categories: Groceries · period: last_month". That is the whole claim of the
  feature made checkable. A tool that failed is shown as one, because the model
  was handed the error and may have answered around it.
- **An answer with a figure and no lookup behind it is called out**, in orange,
  under the reply. Not every toolless answer — "I can't delete anything" reads
  nothing and needs no warning — so the flag follows the digits.
- **The months read are on the summary line**, not folded away: "Jul 2026 · 1
  lookup". Asked what he spent on groceries last month the assistant answered
  "421 €" with no month on it, beside a Dashboard reading 338 € for August —
  a right figure about July that looked like a wrong one about now. The trace
  carries the period each tool resolved, so the answer's meaning does not
  depend on the model remembering to say it. It is asked to as well.
- **Errors land in the transcript, not in a toast.** `api()` throws and the
  global handler would float the message over a panel still showing the
  question hanging unanswered, so `sendChat()` catches its own.

`/api/chat` is exempt from the app-wide loading overlay
(`SELF_TIMED_PATHS` in `app.js`). A local model takes ten or twenty seconds and
the panel says so itself; blurring out the figures being asked about is the one
thing that must not happen while it thinks.

**The wait is watchable.** `POST /api/chat/stream` sends the same turn as
server-sent events as it happens: the lookup named before it runs, the months it
read once it has, then the answer as it is written. `ai_chat.chat()` takes an
`on_event` callback and is otherwise unchanged, so the plain JSON endpoint and
every test still go through the same loop. The route runs that loop on a worker
thread with a queue between them, because a callback pushes and a response has
to yield; nothing in the loop needs the request context, since
`current_user_id()` is a constant here and the tools open their own.

It sits beside `api()` rather than going through it — `api()` expects one
complete body — and falls back to reading the whole stream at once where
`res.body` cannot be read incrementally, which is a live question inside
whatever WebKit the Mac happens to have.

What this actually bought is worth being precise about. **Most of the wait is
prefill, not writing**: about three seconds for the model to choose a tool, then
three or four more re-reading the result before the first word appears, and the
answer itself lands in a fraction of a second. So streaming the tokens matters
less than showing the lookup — "Read the category breakdown · Jul 2026" appears
about three seconds in and stands there while the rest is written, which is the
answer's provenance arriving before the answer. Trimming the tool results to
speed the prefill up was measured and left alone: it saves about 0.4s of 8 and
costs the model sight of every category below the top dozen.

**The cloud backend is a control, not a destination.** `AI_BACKEND=anthropic`
runs the same loop over the same tools with a frontier model, which is the only
way to tell a bad answer caused by the model apart from one caused by the
prompt or the tools.

**Speed.** A question took about fifteen seconds; it now takes five. Almost all
of that was Ollama dropping the model after its own five-minute idle default and
reading 3.4 GB back off disk — and someone dipping into a side panel while they
read their spending is exactly the person who pauses longer than five minutes.
`OLLAMA_KEEP_ALIVE` (30m) keeps it resident. What is left is two model calls per
answer: one to choose the tool, one to write the sentence once the result is
back, at roughly three and four seconds. Cutting further means streaming, not
tuning.

Local-model specifics that are load-bearing: `temperature` 0.1 (this is
routing, and creativity here shows up as invented category names); `think` off
by default with a one-time retry for models that reject the field; and
`<think>` tags stripped from any reply, because a model narrating itself is not
an answer to show anyone.

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
  in the CSS is a leftover from the old blue and is a bug, not a choice. There
  are none left: a tinted surface takes `--accent-soft`, `--accent-softer` or
  `--accent-ring`, all three defined in both themes. Writing the accent's own
  rgba into a rule is what let five blue tints sit under green text for a year.
- 8px spacing grid, rounded corners, subtle shadows
- Sidebar navigation, smooth transitions
- `fmt(amount)` — global currency formatter (fi-FI locale, EUR, **0 decimal places**)

**Every range selector is styled by one rule.** `.horizon-btn`,
`.breakdown-scope-btn`, `.trends-period-btn`, `.trends-toptx-btn` and
`.nw-period-btn` share the
`.active` styling, so the chosen range is marked the same way on every page. A
new selector that does not join that list toggles a class nothing paints, and
no button ever looks selected.

**One segmented control.** `.seg` / `.seg-btn` styles every two-or-three-way
pick — the theme switch in Settings and the Expense/Income toggle in the
transaction modal — and `.theme-seg` / `.theme-seg-btn` are the same rule under
their original name. A second copy under a third name is how two controls doing
the same job start looking like different controls.

**The category picker reads down its columns.** `.cat-pop-grid` is CSS
multi-column, not a 3-across grid: a grid filled left-to-right put "Car
charging, Car maintenance, Car parking" across the top and broke the alphabet at
every row end, so finding a name you already knew the position of meant scanning
the whole block. Multi-column fills column 1 top to bottom before starting
column 2 — the order the list is sorted in, and the DOM order, so ↑/↓ still
steps the way it looks. The panel sits at `z-index: 1100`, above
`.modal-overlay`, because it opens from the import review (page content) *and*
from the transaction modal (not).

**The one row you type by hand is entered the way the four hundred you import
are.** The Add/Edit Transaction modal was the last place still asking with
native `<select>`s and a `type="number"` amount. It uses the same category
picker, the same `parseAmountInput()`, and the same Expense/Income segment as
the import review. Type and category are one decision and are held as one
(`txModal`): picking a category adopts its type, and flipping the type keeps the
category only when the other side has one by that name.

**A failed request has to say so.** `api()` throws an `ApiError` on any
non-2xx, carrying the server's own `error` string. That is what stops a caller
mid-flight: it used to hand the error body back as data, so `saveTransaction()`
closed the modal over what the user had typed and toasted "Transaction added"
for a row the server had refused. One `unhandledrejection` listener turns the
throw into a toast, so a call site only needs its own `catch` when it wants
something other than a message — `loadBankStatus()` catches to keep its card
hidden. The import, bank and investment paths use raw `fetch` and check
`res.ok` themselves; leave them be.

**The app asks its own questions.** `confirmDialog({title, body, confirmLabel,
danger})` returns a promise of true or false, and every "are you sure?" goes
through it. The browser's `confirm()` inside a pywebview window is a system
alert box — another app's typeface, no way to mark the destructive answer, and
paragraphs faked with `\n\n`.

**A modal closes the modal that is open.** The per-open modals used to close
themselves with `document.querySelector(".modal-overlay").remove()`, which takes
the FIRST overlay in the document — and `#invest-overlay` is declared in
`index.html`, so it sits ahead of anything appended to the body and is a
`.modal-overlay` whether or not it is showing. Every such save deleted that
hidden overlay and left the modal the user was looking at on screen, under a
toast saying the save had landed: the "it does not close, you have to click
outside" bug. It also took the investment-import overlay out of the DOM for
good. `closeTopOverlay()` in `core.js` takes the last **visible** overlay, the
same rule `closeTopModal()` uses for Escape, and calls its `data-close` closer
when it has one.

**Escape closes the top modal.** Only `confirmDialog()` used to take the key, so
a drilldown opened by clicking a bar could be dismissed only by aiming at the
backdrop — and a pywebview window has no browser chrome to fall back on.
`closeTopModal()` in `core.js` takes the last open overlay in the DOM, which is
the one on top. Modals built per open are removed; an overlay declared in
`index.html` is reused rather than rebuilt, so it names its own closer in
**`data-close`** (`#invest-overlay`, and the guide, which is not a
`.modal-overlay` at all). One press closes one thing: the handler stands down
when the key was already handled (`defaultPrevented` — `confirmDialog()` and the
import category picker) or when something sits above the modal (`fs-active`,
`nav-open`).

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
GET        /api/merchant-rules/preview   ?pattern, match_type, limit
                                         what a candidate rule would match
POST       /api/merchant-rules/<id>/apply  re-categorise the history it matches
GET        /api/merchant-rules/stats     per-rule hit count + last match
POST       /api/merchant-rules/train     rebuild every rule from history

GET/POST   /api/transactions          ?month, months, category_ids, type, q,
PUT/DELETE /api/transactions/<id>      date_from, date_to, amount_min, amount_max,
                                       sort, dir, page, per_page
GET        /api/transactions/facets   same params → per-value counts for the
                                       filter rail (categories, types, months);
                                       each facet omits its own filter

GET        /api/dashboard/monthly-summary
GET        /api/dashboard/top-expenses
GET        /api/dashboard/category-trends
GET        /api/dashboard/category-breakdown   ?month, months, year, type
                                               (type = expense | income; default expense)
                                               single month → each item also carries
                                               `median` (the 6 months before) + `fixed`
GET        /api/dashboard/heatmap          ?year

GET        /api/reports/annual
GET        /api/trends/category    ?category_ids, months → one category (or
                                    several) over a range; `category.type` comes
                                    back resolved, and the page reads it

GET        /api/recurring               ?lookback_months, min_occurrences
POST       /api/recurring/dismiss               (body: {signature})  hide a series
DELETE     /api/recurring/dismiss/<signature>                       un-hide a series
POST       /api/subscriptions          (body: {store, amount, cadence,
                                        category?, type?}) add one by hand
DELETE     /api/subscriptions/<id>                        remove a hand-added one

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
POST       /api/networth/import-investments/preview   (multipart: files[])
                                         parse broker exports, no DB writes
POST       /api/networth/import-investments/confirm   (body: {accounts: [...]})
                                         write the reviewed snapshot

POST       /api/import/upload                      unknown layout → 200
                                                   {needs_mapping: true, ...}
POST       /api/import/upload-mapped               (multipart: file +
                                        date_col, amount_col, store_col?,
                                        amount_sign, remember) import by hand-
                                        mapped columns
GET        /api/import/formats                     saved column mappings
DELETE     /api/import/formats/<id>                forget one
GET        /api/import/staging/<batch_id>
POST       /api/import/confirm
DELETE     /api/import/staging/<item_id>
GET        /api/import/batches                     past imports, newest first
DELETE     /api/import/batch/<batch_id>            discard a pending review
POST       /api/import/batch/<batch_id>/undo       remove what a confirmed import added

GET        /api/import/bank/status                 connection state + cached accounts
GET        /api/import/bank/connect                302 → bank consent (mints CSRF state)
GET        /api/import/bank/callback               consent return; verifies state, upserts session
POST       /api/import/bank/fetch                  (body: {account_uid, date_from, date_to}) → stages txns
POST       /api/import/bank/disconnect             drops the user's bank session

GET        /api/chat/status                        whether the assistant is
                                                   configured, and `accelerator`
                                                   (gpu | cpu) with the reason
                                                   when it is the slow one
POST       /api/chat                               (body: {messages:[{role,content}]})
POST       /api/chat/download                      fetch the model, once. Returns at
                                                   once; watch /api/chat/status
POST       /api/chat/stream                        the same turn as server-sent
                                                   events: each lookup as it runs,
                                                   then the answer as it is written

GET/PUT    /api/notes/<YYYY-MM>
GET        /api/notes

GET        /api/me                     CSRF token + app version
GET        /api/backups                what is in the backups folder
POST       /api/backups                (body: {reason}) make one now
POST       /api/quit

GET        /healthz                    liveness, no DB
GET        /healthz/db                 runs SELECT 1; 503 if that fails
```

`/healthz` and `/healthz/db` are the last of the hosted deploy — Render's
liveness check and the ping that kept a free-tier Supabase project awake.
Nothing calls them in a desktop app; they cost nothing and have simply not been
taken out.
