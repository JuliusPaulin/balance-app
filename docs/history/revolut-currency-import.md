# Revolut and currency conversion

CSV imports now detect the English Revolut account-statement layout. They use
Started Date, keep completed rows, skip currency exchanges and report the skipped
row counts. Transfers and refunds remain in review. The parser reads ISO
timestamps directly so day-first parsing cannot swap their month and day.

Balance continues to store totals in EUR. Settings saves a default **source**
currency in `import_preferences`; the upload form can override it. A currency
column wins over that fallback. Finnair's dedicated EUR amount column always
stays in EUR. Existing amounts and account balances do not change.

`services/exchange_rates.py` calls Frankfurter's v2 ECB pair endpoint with the
transaction date. It sends no amounts or merchant names. The response must match
the pair and have a positive, finite rate dated no later than the transaction
and no more than seven days earlier. Weekend and holiday rows use that earlier
published date. No valid rate means the entire batch rolls back.

Conversion uses Decimal and rounds to cents with ROUND_HALF_UP. A Revolut fee
reduces the signed statement amount before conversion. Past lookups are cached
in `import_exchange_rates`; today's fallback rate is not saved as final.

`import_fx` holds the original conversion on staging and confirmed rows. Resume
shows it again. Edits and splits change the EUR amount while the source record
stays fixed; confirmation copies that record from staging, never from the
client. `import_batches.import_summary` keeps the skipped and converted counts.
All schema changes are additive and run at startup.

Tests cover dates, states, fees, refunds, mixed currencies, defaults, mapping,
Finnair, caching, resume, confirm, undo, malformed responses, network failures,
rounding and upgrading an existing database. A separate temporary database also
checked the supplied September 2026 sample against live rates: 13 rows staged,
four incomplete rows and one exchange skipped. No real transactions were added.

Rate API reference: [Frankfurter](https://frankfurter.dev/).
