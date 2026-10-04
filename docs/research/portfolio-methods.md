# Portfolio calculations

Portfolio reads the same holdings and account-balance snapshots as Net Worth. It does not maintain a second portfolio ledger or change the broker import flow. Settings, public market prices and explicitly recorded investment cash flows stay in the local SQLite database.

## Valuation and allocation

Included asset accounts contribute their latest balance once. Matching holdings explain that balance; they are never added to it. A newer balance without matching holdings is unclassified. Small import rounding differences are tolerated; larger discrepancies remain visible. Closing an account contributes zero from its closing date and preserves earlier snapshots.

Accounts with holdings and investment/personnel funds are included by default. Bank accounts default to emergency funds and stay outside allocation, risk and the frontier. Account roles can be changed explicitly. Unclassified investments remain in the denominator and lower the reported analysis coverage.

Fund mandate is separate from underlying country exposure. Country figures describe the issuers of underlying securities, not fund domicile or trading currency. Public metadata is dated, source-linked and labelled when matched by name or estimated. Undisclosed composition stays unknown. Historic allocation comparisons use the same cached composition information and therefore estimate past look-through exposure, rather than reconstructing historic fund constituents.

Costs apply the published annual fund charge to the recorded value. The known-cost subtotal is not a complete bill: platform fees, trading costs, taxes and personal discounts may differ.

## Recorded value and performance

Snapshot value changes include contributions, withdrawals and market movements. They are not investment returns. Broker `return_pct` is not annualised or used as a return series.

Investment cash flows must be recorded separately and their completeness confirmed for a bounded period before a money-weighted calculation can be supported. Transfers recorded in the expense categories do not establish investment cash flows. A projection of contributions assumes unchanged prices and composition; it does not write holdings or place orders.

Opening and closing valuations must exist for every included account. Cash flows follow end-of-day boundaries: the opening date is excluded and the closing date included. In-period ledger changes or account inclusion changes clear the completeness confirmation. Non-conventional cash flows with multiple sign changes leave money-weighted return unavailable because XIRR can have ambiguous roots; gains and net contributions can still be shown.

## Proxy risk and frontier

Public Yahoo adjusted monthly prices account for distributions and splits as reported by the provider. USD proxy prices are divided by aligned EUR/USD exchange rates. Incomplete current months and non-consecutive return intervals are excluded. Cached prices remain usable after provider failure; refresh attempts do not retry in a loop.

Risk estimates describe a hypothetical monthly rebalanced portfolio of market proxies at the displayed weights. They are not the investor's realised performance. Monthly returns are aligned before sample covariance is estimated and multiplied by 12. Volatility uses portfolio returns and their correlations. Historical Sharpe uses the displayed risk-free reference converted to a monthly rate; maximum drawdown uses compounded monthly wealth including its initial value. Monthly drawdown can miss larger losses within a month. At least 24 common monthly observations are required.

The proxy universe includes World, US, Europe, broad emerging markets, Finland, Norway, Sweden, Apple and optional EUR-hedged global investment-grade bonds. Nordic ETF indices differ from Nordnet funds; the EM proxy includes China and cannot reproduce an ex-China mandate. Nordea's active portfolio is not identical to MSCI World. These differences remain visible in the source and method explanations.

The frontier minimises covariance-based variance under long-only weights, no borrowing, weights summing to one and editable per-asset caps. Expected annual arithmetic returns are explicit assumptions. Historical means are not substituted as forecasts. Equal default equity assumptions are an illustrative neutral comparison, not saved allocation goals. Approximate compound growth subtracts half the model variance from the assumed arithmetic mean; it is not a forecast. A bond alternative is hypothetical and does not imply current bond ownership. Optimiser weights are diagnostics, not trade instructions.

Changing the sample window or return assumptions can change the apparent frontier and same-return improvement. The view exposes these sensitivities and data coverage. Unknown holdings are never replaced by cash or assigned zero risk.

## Development safety

Set `SQLITE_PATH` before importing configuration or database modules. UI verification uses a consistent copy at `/private/tmp/balance-portfolio-dev.db`. Pytest creates its own temporary database through `conftest.py`. Public provider calls contain instrument symbols only, never account identifiers, balances, holdings values or targets.

## Verification on 4 October 2026

The full suite passed: 545 tests. Portfolio tests cover reconciliation, emergency-cash exclusion, partial country exposure, preserved imports and snapshots, cash-flow boundaries, covariance and correlations, EUR conversion, drawdown, missing history, provider failure/backoff and frontier constraints. A public-price regression checks the tied-return frontier endpoint against the minimum-risk equity solution.

The rendered Flask app was inspected using the copied database. Saved targets, twelve-step World contribution previews, country-basis switching, edited frontier assumptions and existing Transactions navigation were exercised. The real database file hash remained unchanged. Independent review found no remaining material issues. The packaged macOS application was not rebuilt or exercised; its specification includes the new modules, public data files and numerical dependencies.
