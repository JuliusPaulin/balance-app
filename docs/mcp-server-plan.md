# Local MCP server

A stdio MCP server so an assistant on this Mac can read the Balance database
and import or categorise through the app's own code, instead of opening
`expenses.db` and guessing at the schema.

The server is a separate process. It does not listen on a port, and it does
not add a screen to the app. The desktop app keeps working as it does today.

## What is already true

- One SQLite file, path from `SQLITE_PATH`, otherwise
  `~/Library/Application Support/Balance/expenses.db`. `data/sqlite.py` opens
  it in WAL mode with a 5 second busy timeout and a 30 second connect timeout.
  That is already what makes a second process safe to have open beside the app.
- Schema changes are `CREATE TABLE IF NOT EXISTS` in `data/schema.py`, plus
  `_ensure_sqlite_columns()` for columns on tables that already exist. Both
  run from `init_db()`, which the app calls on launch. There is no separate
  migration runner.
- Spending rows live in `transactions`. Every row has a category. There is no
  "uncategorised" state and no account column. "Other" (one expense, one
  income) is where an import files a store nothing knows about.
- `accounts` / `account_balances` / `holdings` are net worth, entered by hand
  or by a broker export. They are not the bank accounts a statement comes from.
- CSV import (`routes/csv_import.py`) recognises Finnish bank statements,
  Nordea account exports, Nordea Platinum, Finnair, and Revolut. Unknown
  layouts return `needs_mapping` and can be taught once. Confirming an import
  in the app retrains merchant rules, and that retrain deletes the user's
  rules first.
- A suggestion (`suggest_category`) is a merchant rule, then the store's own
  history if at least 70% of it agrees. It is deterministic and it never
  leaves the machine.
- The app does not skip duplicate transactions on import. A second copy of a
  statement imports again.
- Non-EUR amounts are converted with a saved ECB rate or, if none is saved, a
  fetch from Frankfurter. That fetch is the one network call on the import path.

## Architecture

```
python -m balance_mcp          stdio only
        |
        v
balance_mcp/server.py          MCP SDK (mcp.server.mcpserver.MCPServer)
        |
        v
balance_mcp/service.py         the tools, as plain functions
        |
        +-- data.schema.init_db / seed_local_user     same startup as the app
        +-- data.db_conn                              same engine, WAL, busy timeout
        +-- routes.csv_import                         parse, stage, suggest
        +-- routes.merchant_rules._matches_rule       what a rule would hit
        +-- services.networth                         totals and carry-forward
        +-- services.exchange_rates                   saved rates only (see below)
```

`server.py` is the only module that imports the MCP SDK. The tool functions
return dicts and raise `BalanceMcpError` for a refusal the assistant should
read (bad date, unknown category, file needs a column mapping). The server
turns that into a tool error. Anything else is a bug and stays off the wire.

The package is not imported by `main.py`, so it is not part of `Balance.app`
and it is not listed in `Balance.spec`.

On startup the server calls `init_db()` and `seed_local_user()`, the same pair
`main.py` calls. That creates `mcp_audit` on an existing database and does not
rewrite transactions. It also runs the app's existing closed-account backfill,
because that backfill lives in `init_db()` and skipping it would mean a second
startup path.

### Audit table

Added to `SCHEMA_DDL_SQLITE`, so an old database gains it the next time either
the app or the server starts. No column is added to `transactions` or any
other existing table. Old app versions ignore a table they do not know about.

```sql
CREATE TABLE IF NOT EXISTS mcp_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    action       TEXT NOT NULL,
    summary      TEXT NOT NULL,
    undone       INTEGER NOT NULL DEFAULT 0,
    undone_at    TEXT,
    undo_kind    TEXT NOT NULL,
    undo_payload TEXT NOT NULL
);
```

`undo_payload` is the minimum needed to reverse the write: transaction ids and
the category they had, the old category name, the rule id, the import batch id.
It does not store a copy of the bank file.

Every write and its audit row commit in one `BEGIN IMMEDIATE` transaction, or
they both roll back. A second undo of the same row is refused. Undoing is
itself an audit row (`undo_kind = 'none'`) so the log says it happened, and
that row cannot be undone.

## Tools

Reads are safe to call any time. Writes that can touch many rows take the
count from a preview and refuse if the file or the database has moved.

Transactions have no account, so none of the transaction tools take one.
Accounts are the net-worth tools.

| Tool | Kind | Inputs | Output |
|------|------|--------|--------|
| `list_accounts` | read | `account_id?` | Accounts with latest balance. One id also returns that account's balance history. |
| `net_worth` | read | `months` (default 12, max 120) | Assets, liabilities, net, change vs the previous month, and the monthly carry-forward series. Archived accounts stay in past months; the service already does this and the server does not filter them out. |
| `search_transactions` | read | `date_from`, `date_to`, `month`, `category` / `category_id` + `category_type`, `merchant`, `amount_min`, `amount_max`, `txn_type`, `limit` (default 50, max 200), `offset` | Page of rows plus `total`, `sum_expense`, `sum_income`. Merchant matches the store only. |
| `spending_by_category` | read | `txn_type` (default expense), and one of `month`, `year`, `date_from`/`date_to`. With none of those, the latest month that has rows of that type. | Category totals and a period total. |
| `cash_flow` | read | `date_from?`, `date_to?` | Income, expense, and net per month. Expense includes the Investments category; `investments` is also reported on its own so a transfer into a fund is not read as spending. |
| `list_categories` | read | `category_type?` | id, name, type, whether it is a default, how many transactions, last used. |
| `list_category_rules` | read | — | pattern, match type, category. |
| `preview_bank_import` | read | `path`, `include_duplicates` (default false), optional `date_col` / `amount_col` / `store_col` / `amount_sign` | Counts, duplicate count, sums of what would be new, up to 8 sample rows and 5 duplicate samples. Unknown layout: `needs_mapping` with headers, five sample rows, and a column guess. No database write. |
| `commit_bank_import` | write | the same, plus `expected_new` and `remember_format` (default false) | Inserts the new rows, or refuses if `expected_new` is not the preview's `to_import`. |
| `set_transaction_category` | write | `transaction_id`, category by id or by name + type | The row. Same category is not a write. Type must match. |
| `create_category` | write | `name`, `category_type` | The category. Refuses a second category with the same name and type. |
| `rename_category` | write | `category_id`, `name` | The category. |
| `create_category_rule` | write | `pattern`, `match_type` (`exact` / `contains` / `smart`), category | The rule. Does not change any transaction. `contains` and `smart` patterns shorter than 3 characters are refused. |
| `preview_recategorise` | read | `rule_id`, or a pattern + match type + category | How many rows match, how many would change, how many were skipped because the category is the other type, and a sample. |
| `apply_recategorise` | write | the same, plus `expected_updates` | Updates those rows, or refuses if the live count is not `expected_updates`. |
| `suggest_categories` | read | `limit` (default 100, max 500) | Transactions currently in Other, with the category `suggest_category` would pick and whether it came from a rule or from history. |
| `apply_category_suggestions` | write | `transaction_ids` | Recomputes each suggestion and applies it. Ids that no longer suggest anything are skipped and listed. |
| `list_audit_log` | read | `limit` (default 20, max 100) | Newest first: id, time, action, summary, whether it was undone. |
| `undo_change` | write | `audit_id` | Reverses that row. |

`preview_bank_import` and `commit_bank_import` both re-read the file. Nothing
about the preview is stored, so a preview the assistant walks away from leaves
no batch behind.

### Import behaviour

The parse is the app's parse. `preview` / `commit` call `_decode_csv_bytes`,
the delimiter and column detectors, the Finnair and Revolut special cases,
saved `import_formats` when detection fails, and `_stage_rows`. A recognised
Nordea or Finnair file is never remapped by a caller-supplied column index;
that is the same rule as `POST /api/import/upload`. A manual mapping is used
only when detection fails, and `remember_format` writes `import_formats` the
way `upload-mapped` does.

Preview stages inside a connection and rolls it back. Commit stages again and
commits. The second pass checks `expected_new` again, so a statement imported
from the app between the two calls is caught.

Duplicates are skipped unless `include_duplicates` is true. The key is the
date, the store with case and surrounding space ignored, the amount in cents,
and expense versus income. The first copy in the file is the one that can be
new; a later copy of the same key is a duplicate, and so is a row that already
exists in `transactions`. The app has no bank-reference column to do better
than this. Category is not part of the key.

A new row's category is the suggestion for its type, and Other of that type
when there is no suggestion. Commit does **not** call `_rebuild_merchant_rules`.
The in-app Confirm button does, and that deletes hand-written rules. An
assistant importing a file must not wipe those. Suggestions at stage time
already use the rules that exist.

Before any conversion, `_stage_rows(..., offline=True)` is allowed to use a
rate already stored in `import_exchange_rates` and is not allowed to call
Frankfurter. A file that still needs a rate is refused, with no rows written.
Importing that file in the app remains the way to fetch the rate.

`backup_db("pre-import")` runs only once a commit has decided it will write,
the same snapshot the app takes before an import.

### Categorisation

`suggest_category` stays the one function that decides a name. It grows a
sibling, `suggest_category_detail`, that also says `rule` or `history`. The
name it returns does not change.

Bulk recategorise uses `_matches_rule` and the same cross-type skip as
`POST /api/merchant-rules/<id>/apply`: an expense rule does not move income.

### One small query extension

`_filter_clauses` gains an optional `merchant` argument, matched against the
store only. The transactions screen does not send it. Existing calls are
unchanged. The MCP search uses it so a merchant filter does not also match a
category name, which is what the screen's `q` parameter does.

## Safety

- Transport is stdio. `run(transport="stdio")` is the only way the module
  starts, and that call does not bind a socket.
- No URL is fetched. The offline flag on the CSV stage path is what holds that
  for imports. The server never imports the Open Banking client.
- The file argument has to be a local `.csv` path, at most 16 MB (the app's
  upload limit), and it cannot be the database file or its `-wal` / `-shm`
  siblings.
- Writes take `BEGIN IMMEDIATE` on the shared connection, so they wait for the
  app's write lock and then either commit whole or roll back. A lock that
  outlasts the busy timeout comes back as an error, not a partial import.
- The server does not delete transactions or categories except by undoing a
  change it recorded. Undoing an import deletes the rows that import created
  (the same outcome as the app's import undo) and marks the batch `undone`.
- Tool annotations mark reads and previews `readOnlyHint`, and
  `openWorldHint` is false on every tool.

## Out of scope

- Open Banking. It needs the Enable Banking API.
- Broker holdings (`Omistukset.xlsx`, Nordnet). Those write net worth, not
  transactions, and they already have their own import.
- Deleting or editing a transaction's amount, date, or store.
- Editing or deleting a rule in place. Undo of `create_category_rule` removes
  it; the app's rule screen still edits one.
- Retraining every rule from history. That deletes hand-written rules.
- Serving the server inside `Balance.app`.

## Risks

- The duplicate key will treat two genuine same-day charges at the same shop
  for the same amount as one charge. `include_duplicates` is the escape, and
  the preview says how many rows it would skip.
- Undo is per audit row, not a stack. Undoing an older category change after
  a newer one fails cleanly when the old category is gone, and otherwise puts
  back the category recorded at the time. That can overwrite a later edit.
  The summary names the row so this is visible before the call.
- Undoing an import removes those transactions even if a category was changed
  afterwards. That matches the app's own import undo.
- A screen the app already has open does not refresh itself. The next read the
  app does sees the commit, because both use the same WAL database.
- `init_db()` on server start runs the closed-account zero backfill. That is
  existing app startup behaviour, not a new rewrite of history.
- `mcp` 2.x renamed `FastMCP` to `MCPServer`. The dependency is pinned to
  `mcp>=2.3,<3`.

## Tests

`tests/test_mcp.py`, against the throwaway database the rest of the suite
uses. Fixtures are synthetic CSV strings, not bank files.

- Reads: accounts and balances, net worth carry-forward, transaction filters,
  spending by category, cash flow including the investments split, categories,
  rules. Empty database returns zeros and empty lists.
- Import: a Nordea-shaped file and a Finnish bank file preview to the same
  rows the HTTP upload stages. Preview writes nothing. Commit with the wrong
  `expected_new` writes nothing. Commit skips database duplicates and a second
  copy inside the file. `include_duplicates` keeps them. Unknown layout asks
  for a mapping and does not create a batch; a mapping then imports. A
  hand-written rule is still there after commit.
- Currency: an uncached non-EUR file is refused and `requests.get` is not
  called. A rate already in `import_exchange_rates` converts with no request.
- Categories: create, refuse a duplicate, rename, set one transaction, refuse
  a type mismatch.
- Rules: create, refuse a one-character contains pattern, preview, refuse an
  apply whose `expected_updates` is stale, apply, and leave an income row of
  the other type alone.
- Suggestions: a consistent history is offered for an Other row, an ambiguous
  store is not, a rule wins over history, apply changes only the ids given.
- Undo: import, category edit, rename, create, rule, and bulk apply each
  reverse. A second undo is refused. A category deleted out from under an
  undo rolls the audit row back to not-undone.
- Schema: dropping `mcp_audit` and calling `init_db()` recreates it and leaves
  a transaction in place. The connection reports WAL and a 5000 ms busy timeout.
- The server object lists these tools with the read-only hints, and
  `list_categories` through `call_tool` returns the seeded Groceries category.

`python3 -m pytest tests/` stays green. The server is not started as a
subprocess in the suite; `call_tool` on the in-process server covers the SDK
wiring, and stdio is the default of `MCPServer.run`.

## Decisions worth a look

1. Duplicate means date + store + amount in cents + type. There is no bank
   reference on `transactions` to key on instead.
2. "Uncategorised" means the Other category of the right type.
3. An MCP import does not retrain merchant rules.
4. Non-EUR files import only when a rate is already saved. Otherwise the app
   imports them.
5. Net-worth accounts and spending transactions stay unrelated, because that
   is how the database is shaped.
