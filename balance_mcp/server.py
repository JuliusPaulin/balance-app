"""MCP wiring. The tool bodies live in :mod:`balance_mcp.service`.

``MCPServer.run`` defaults to stdio and is the only transport this module starts.
"""

import functools
import inspect

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from balance_mcp.errors import BalanceMcpError
from balance_mcp import service

_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
_ADD = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
_CHANGE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

_INSTRUCTIONS = (
    "Balance is the local money file on this Mac. Read tools only read it. "
    "preview_bank_import and preview_recategorise change nothing. "
    "commit_bank_import requires expected_new equal to the preview's to_import, "
    "and apply_recategorise requires expected_updates equal to would_update. "
    "Every row in a file is imported, including repeats. "
    "Imports do not fetch exchange rates and do not retrain category rules. "
    "Every write is listed by list_audit_log and reversed by undo_change. "
    "Spending transactions are not tied to net-worth accounts. "
    "A transaction filed under Other has no confident category yet."
)


def _expose(fn):
    """Turn a BalanceMcpError into a tool error the assistant can read."""

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BalanceMcpError as exc:
            raise ToolError(str(exc)) from exc

    wrapped.__signature__ = inspect.signature(fn)
    return wrapped


def build_server() -> MCPServer:
    mcp = MCPServer(
        name="balance",
        instructions=_INSTRUCTIONS,
        log_level="WARNING",
    )

    @mcp.tool(annotations=_READ, structured_output=False)
    @_expose
    def list_accounts(account_id: int | None = None) -> dict:
        """List net-worth accounts and the latest balance of each.

        Pass account_id to get that account's full balance history as well.
        These are not the bank accounts a statement is imported from. Spending
        transactions are not linked to them.
        """
        return service.list_accounts(account_id)

    @mcp.tool(annotations=_READ, structured_output=False)
    @_expose
    def net_worth(months: int = 12) -> dict:
        """Current assets, liabilities, and net worth, plus a monthly history.

        months is 1 to 120 and defaults to 12. Balances carry forward. An
        archived account stays in the months before it was closed.
        """
        return service.net_worth(months)

    @mcp.tool(annotations=_READ, structured_output=False)
    @_expose
    def search_transactions(
        date_from: str | None = None,
        date_to: str | None = None,
        month: str | None = None,
        category: str | None = None,
        category_id: int | None = None,
        category_type: str | None = None,
        merchant: str | None = None,
        amount_min: float | None = None,
        amount_max: float | None = None,
        txn_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Search transactions. Amounts are positive euros; txn_type says which way.

        date_from and date_to are YYYY-MM-DD. month is YYYY-MM. txn_type is
        expense or income. merchant matches the store name only. category is
        the category name; Other and Investments exist on both sides, so pass
        category_type for those. limit is 1 to 200 (default 50). There is no
        account filter: transactions are not tied to net-worth accounts.
        """
        return service.search_transactions(
            date_from, date_to, month, category, category_id, category_type,
            merchant, amount_min, amount_max, txn_type, limit, offset,
        )

    @mcp.tool(annotations=_READ, structured_output=False)
    @_expose
    def spending_by_category(
        txn_type: str = "expense",
        date_from: str | None = None,
        date_to: str | None = None,
        month: str | None = None,
        year: str | None = None,
    ) -> dict:
        """Totals by category for one period.

        txn_type is expense (default) or income. Pass a month (YYYY-MM), a
        four-digit year, or a date range, and only one of those. With none,
        the latest month that has rows of that type is used.
        """
        return service.spending_by_category(txn_type, date_from, date_to, month, year)

    @mcp.tool(annotations=_READ, structured_output=False)
    @_expose
    def cash_flow(date_from: str | None = None, date_to: str | None = None) -> dict:
        """Income, expense, and net by month.

        Expense includes the Investments category. investments on each month
        is that subset, so money moved into a fund is visible on its own.
        Dates are YYYY-MM-DD. Omit them for every month on file.
        """
        return service.cash_flow(date_from, date_to)

    @mcp.tool(annotations=_READ, structured_output=False)
    @_expose
    def list_categories(category_type: str | None = None) -> dict:
        """Categories, with how many transactions use each and when one last did.

        category_type is expense or income. Omit it for both.
        """
        return service.list_categories(category_type)

    @mcp.tool(annotations=_READ, structured_output=False)
    @_expose
    def list_category_rules() -> dict:
        """Merchant rules: the pattern, how it matches, and the category it files under."""
        return service.list_category_rules()

    @mcp.tool(annotations=_READ, structured_output=False)
    @_expose
    def preview_bank_import(
        path: str,
        date_col: int | None = None,
        amount_col: int | None = None,
        store_col: int | None = None,
        amount_sign: str = "neg_expense",
    ) -> dict:
        """Read a bank CSV and report what an import would do. Writes nothing.

        path is a local .csv file. Every parsed row is reported, including
        repeats of the same date, store, and amount. A recognised Nordea,
        Finnish bank, Finnair, or Revolut file is parsed on its own. An unknown
        layout returns needs_mapping; pass date_col and amount_col (0-based),
        and store_col if there is a merchant column. amount_sign is
        neg_expense (default) or pos_expense. Non-EUR amounts import only when
        a rate is already saved.
        """
        return service.preview_bank_import(
            path, date_col, amount_col, store_col, amount_sign,
        )

    @mcp.tool(annotations=_CHANGE, structured_output=False)
    @_expose
    def commit_bank_import(
        path: str,
        expected_new: int,
        date_col: int | None = None,
        amount_col: int | None = None,
        store_col: int | None = None,
        amount_sign: str = "neg_expense",
        remember_format: bool = False,
    ) -> dict:
        """Import a bank CSV. Refuses unless expected_new equals the preview's to_import.

        Use the same path and column mapping as the preview. Every parsed row
        is imported, including repeats. remember_format saves a manual column
        mapping for next time; it does nothing for a layout the parser already
        recognises. Does not retrain category rules and does not fetch exchange
        rates. The returned audit_id reverses the whole batch.
        """
        return service.commit_bank_import(
            path, expected_new, date_col, amount_col, store_col,
            amount_sign, remember_format,
        )

    @mcp.tool(annotations=_CHANGE, structured_output=False)
    @_expose
    def set_transaction_category(
        transaction_id: int,
        category_id: int | None = None,
        category: str | None = None,
        category_type: str | None = None,
    ) -> dict:
        """Set one transaction's category. The category's type must match the transaction.

        Pass category_id, or category plus category_type when the name exists
        as both expense and income (Other, Investments). Setting the category
        it already has is not recorded.
        """
        return service.set_transaction_category(
            transaction_id, category_id, category, category_type,
        )

    @mcp.tool(annotations=_ADD, structured_output=False)
    @_expose
    def create_category(name: str, category_type: str) -> dict:
        """Create a category. category_type is expense or income.

        Refuses a second category with the same name and type.
        """
        return service.create_category(name, category_type)

    @mcp.tool(annotations=_CHANGE, structured_output=False)
    @_expose
    def rename_category(category_id: int, name: str) -> dict:
        """Rename a category. Transactions keep pointing at it."""
        return service.rename_category(category_id, name)

    @mcp.tool(annotations=_ADD, structured_output=False)
    @_expose
    def create_category_rule(
        pattern: str,
        match_type: str = "exact",
        category_id: int | None = None,
        category: str | None = None,
        category_type: str | None = None,
    ) -> dict:
        """Create a merchant rule. Does not change existing transactions.

        match_type is exact, contains, or smart. contains and smart patterns
        must be at least 3 characters. Pass category_id, or category and
        category_type. Apply it later with preview_recategorise and
        apply_recategorise.
        """
        return service.create_category_rule(
            pattern, match_type, category_id, category, category_type,
        )

    @mcp.tool(annotations=_READ, structured_output=False)
    @_expose
    def preview_recategorise(
        rule_id: int | None = None,
        pattern: str | None = None,
        match_type: str = "exact",
        category_id: int | None = None,
        category: str | None = None,
        category_type: str | None = None,
    ) -> dict:
        """Show what a rule would recategorise. Writes nothing.

        Pass rule_id, or a pattern, match_type, and category. would_update is
        the count to pass to apply_recategorise. Rows of the other type are
        counted in skipped_other_type and are not changed.
        """
        return service.preview_recategorise(
            rule_id, pattern, match_type, category_id, category, category_type,
        )

    @mcp.tool(annotations=_CHANGE, structured_output=False)
    @_expose
    def apply_recategorise(
        expected_updates: int,
        rule_id: int | None = None,
        pattern: str | None = None,
        match_type: str = "exact",
        category_id: int | None = None,
        category: str | None = None,
        category_type: str | None = None,
    ) -> dict:
        """Recategorise matching transactions. Refuses if expected_updates is stale.

        expected_updates must equal would_update from preview_recategorise for
        the same rule or pattern. Income is not moved by an expense category,
        and the reverse. The returned audit_id reverses the category changes
        and does not delete the rule.
        """
        return service.apply_recategorise(
            expected_updates, rule_id, pattern, match_type, category_id, category, category_type,
        )

    @mcp.tool(annotations=_READ, structured_output=False)
    @_expose
    def suggest_categories(limit: int = 100) -> dict:
        """Suggest categories for transactions currently filed under Other.

        Uses merchant rules, then a store's own history when at least 70% of it
        agrees. No network and no model. limit is 1 to 500. Apply with
        apply_category_suggestions and the transaction ids returned here.
        """
        return service.suggest_categories(limit)

    @mcp.tool(annotations=_CHANGE, structured_output=False)
    @_expose
    def apply_category_suggestions(transaction_ids: list[int]) -> dict:
        """Apply suggestions for these transaction ids, recomputed at apply time.

        Ids that are no longer in Other, or that no longer have a suggestion,
        are skipped and listed. One audit entry covers the ones that changed.
        """
        return service.apply_category_suggestions(transaction_ids)

    @mcp.tool(annotations=_READ, structured_output=False)
    @_expose
    def list_audit_log(limit: int = 20) -> dict:
        """Recent writes from this server, newest first. limit is 1 to 100.

        undone marks an entry that undo_change has already reversed. Pass id
        to undo_change.
        """
        return service.list_audit_log(limit)

    @mcp.tool(annotations=_CHANGE, structured_output=False)
    @_expose
    def undo_change(audit_id: int) -> dict:
        """Reverse one write from list_audit_log.

        Undoing an import deletes the transactions that import created. Undoing
        a category change puts the previous category back. A second undo of the
        same id is refused.
        """
        return service.undo_change(audit_id)

    return mcp
