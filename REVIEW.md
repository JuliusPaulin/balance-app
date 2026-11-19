# Reviewing a pull request

Read this file from `main`, not from the pull request branch. A change that edits this checklist does not change the list you use for that review.

Each item says which diff it applies to. Skip an item when the diff does not match.

- **Commits and the pull request text** (every pull request). No `Co-authored-by` line, and no line that credits an AI tool or an agent as an author, in the commits or in the pull request text.

- **`data/schema.py`** (any change to the schema or to `init_db`). The change is additive. An existing database still opens, its rows are still there, and running `init_db()` twice does not rewrite them. `tests/test_schema_migration.py` covers that. The change is not applied by hand to someone's `expenses.db` from the agent.

- **The MCP server** (any change under `balance_mcp/`). It still speaks stdio only. It does not listen on a port and it does not fetch a URL. A write is one database transaction and can be undone. An import does not retrain merchant rules.

- **Secrets and real figures** (every pull request, and especially fixtures, tests, and docs). No real bank export, account number, database file, or secret is in the code, the diff, or the chat. Amounts in tests are made-up values that exist only in the test setup.

- **UI** (any visible change under `templates/` or `static/`). The pull request says which screens changed. A behaviour change in the web UI is exercised in the browser, or the pull request says what could not be run.

- **The pull request description** (every pull request). It has a "Verified by" section that says exactly what was run, or "none".
