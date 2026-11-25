# Changelog

## 1.17.0

Planning projects your cash flow and net worth from your current age out to the age you choose. Income, spending, rent and today's net worth start from your own figures: the average of the last 12 months, and the net worth the Net Worth tab shows. Every figure is editable, and Reset puts those Balance figures back. Spending leaves out money categorised as Investments, because that is a transfer into savings and the plan invests whatever is left over.

You can add a home, a car, a child, time off, an inheritance, extra income, a one-off cost, a market crash, or a custom event. A custom event can be paid partly with a loan, and it can keep a value that counts in net worth. The summary, the charts and the year-by-year table update as you move the sliders. Today's euros are adjusted for inflation. Future euros are the amounts in the year they happen. A second scenario can be drawn on the net worth chart beside the first.

The plan is saved on this Mac, in the same database as the rest of Balance.

## 1.16.0

Another assistant on this Mac can read your Balance books, import a bank CSV, and change categories, through Balance's own code. The program is called `balance_mcp`. It speaks MCP over standard input and output, so Claude Desktop, Cursor, or a similar assistant on this Mac can use it.

It runs only on this Mac, for the assistant that starts it, and it uses the same database file as the app. You start it from a checkout of this repository. Setup for Claude Desktop and Cursor, including the config to paste, is in [Other assistants on this Mac](https://github.com/JuliusPaulin/balance-app/blob/main/README.md#other-assistants-on-this-mac).

The tools are:

- `list_accounts`, `net_worth`, `search_transactions`, `spending_by_category`, `cash_flow`, `list_categories`, `list_category_rules` — read accounts, net worth, transactions, category totals, cash flow, categories, and merchant rules
- `preview_bank_import`, `commit_bank_import` — preview a bank CSV, then import it
- `set_transaction_category`, `create_category`, `rename_category`, `create_category_rule`, `preview_recategorise`, `apply_recategorise` — change a category, add or rename one, add a merchant rule, and preview or apply recategorising
- `suggest_categories`, `apply_category_suggestions` — suggest categories for transactions in Other, and apply the ones you choose
- `list_audit_log`, `undo_change` — list recent writes and undo one

An import keeps every row the file parsed. The same charge twice in one file becomes two transactions, and importing that file again creates a second batch. Undo removes that batch.

Pull requests and pushes to main now run `python3 scripts/verify.py` (a lint pass and the full test suite). The result is reported on the pull request. Merging stays available until that check is marked required.
