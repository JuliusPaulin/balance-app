# Design changes — 2026-07-31

One numbered section per change (numbers match the design review). Each says
what changed, which files/functions, and how to revert just that change.
Changes are independent unless a section says otherwise.

Baseline: branch `feat/local-sqlite` at commit 131bb73 plus the import-redesign
work from earlier tonight (split fix, day-first dates, Ledger review UI,
auto-retrain).

Tip: the fastest surgical revert is `git diff` on the named file and dropping
only the hunk that matches the section.

---

## 1. Retire red-as-expense; drop the Type pill

**What:** The Transactions table no longer has the Type column (red
"expense" pill on 2,800 rows). Sign and color carry the type: income green
with `+`, expense plain ink with `−`. The mobile rule that colored expense
amounts red is gone. Red now means warnings only.

**Where:**
- `templates/index.html` — removed `<th>Type</th>`
- `static/js/app.js` `loadTransactions()` — removed the Type cell; empty-state
  colspan 6→5
- `static/css/style.css` — removed `#transactions-body td.amount.expense
  { color: var(--red) }` from the mobile block

**Revert:** Re-add the `<th>Type</th>` header, the row cell
`<td data-label="Type"><span class="badge badge-${t.type}">${t.type}</span></td>`,
colspan back to 6, and the mobile red rule. `.badge-expense`/`.badge-income`
CSS still exists.

---

## 2. One category = one color, everywhere

**What:** `catDotColor(catId)` is now the single source of category color:
a color stored on the category wins (see #20), else a stable id-keyed pick
from the active palette. Used by: import review chips, Transactions rows,
dashboard breakdown dots, top-5 trends lines, report category/income bars,
merchant-rule group headers, Categories page swatches. Charts no longer
color by per-chart index (same category used to change color between cards).

**Where:** `static/js/app.js` — `catDotColor()` (upgraded), `loadTransactions()`,
`renderCategoryBars()`, `drawTrendsFromData()`, `renderReportCategoryBars()`,
`renderReportIncomeBars()`, `filterMerchantRulesView()`. `static/css/style.css`
— `.cat-dot`.

**Also:** `distinctCatColors(ids)` — 34 categories share a 12-color palette, so
two ids can land on the same slot (Rent id 23 and Exercise id 11 both hit
slot 11, and drew two identical red lines in the trends chart). Any id-modulo
scheme collides for ids differing by the palette length, so multi-series charts
run their colors through this helper: identity color where possible, nearest
unused palette entry when it would repeat one already used in that chart.

**Revert:** Restore `CHART_COLORS[i % CHART_COLORS.length]` (index-based) in
those renderers and remove the `.cat-dot` spans. To drop only the stored-color
override, delete the `stored` lookup inside `catDotColor()`. To drop only the
de-duplication, call `catDotColor(cat.id)` directly in `drawTrendsFromData()`.

---

## 3. Sidebar double-active — checked, not a bug

Verified with real clicks + computed styles: only one nav item ever holds
`.active`; the "bold previous item" in review screenshots was JPEG
artifacting. No change made.

---

## 4. No more full-screen blackout while loading

**What:** Two parts.
(a) The global blur overlay now appears only when a request takes longer
than 400 ms, so page switches render instantly; long work is still covered.
(b) `/api/recurring` results are cached server-side in memory and reused
until the data changes — the ~7 s Subscriptions scan runs once, then the page
is instant. Every route that writes transactions (create/update/delete,
import confirm, rule re-apply, category delete-reassign, dismiss/undismiss,
manual subscription add/remove) bumps `_data_version`, which invalidates the
cache.

**Where:**
- `static/js/app.js` — `beginLoading()`/`endLoading()` (+`LOADING_DELAY_MS`)
- `app.py` — `_data_version`, `_recurring_cache`, `_bump_data_version()` and
  the `recurring()` route; `_bump_data_version()` calls in the write routes

**Revert:** (a) restore the two-liner beginLoading/endLoading (add/remove the
class immediately). (b) In `recurring()`, drop the cache lookup and always
call `detect_recurring`; the `_bump_data_version()` calls are harmless to
leave or remove.

---

## 5. Remaining native date inputs replaced with day-first text

**What:** All `<input type="date">` (locale-rendered, month-first under
en_FI WKWebView) became day-first text fields (`31.7.2026`) with
`isoToFi`/`fiToIso` conversion at every read/write: transaction modal,
Transactions advanced-search from/to, bank import range, Net Worth
"Balances as of", investment import "as of".

**Where:** `templates/index.html` (5 inputs), `static/js/app.js` —
`loadTransactions()`, `saveTransaction` body, bank-range preset + fetch,
`loadNetWorth()`, `saveNetWorthBalances()`, investment preview/confirm.

**Revert:** Restore `type="date"` on the inputs and drop the `isoToFi`/
`fiToIso` wrappers at the listed call sites (values go back to raw ISO).

---

## 6. Row action icons show on hover only (desktop)

**What:** Edit/delete/re-apply icons in Transactions rows, Categories rows,
and Merchant-rule rows are invisible until the row is hovered or focused.
Pointerless/mobile layouts keep them always visible.

**Where:** `static/css/style.css` — the
`@media (hover: hover) and (min-width: 1025px)` block near `.cat-dot`.

**Revert:** Delete that media block.

---

## 7. Cents on row-level money

**What:** New `fmt2()` (2 decimals). Used for individual transactions:
Transactions table, category/day/merchant drill-down modals, Reports top-10
lists. Aggregates and charts keep whole-euro `fmt()`.

**Where:** `static/js/app.js` — `fmt2()` next to `fmt()`, plus the listed
render sites.

**Revert:** Replace `fmt2(` back with `fmt(` at those sites (or delete
`fmt2` and grep for its uses).

---

## 8. Category breakdown bars: quiet bars, identity dots, merged column

**What:** Dashboard "Expenses by category" (and the Reports category/income
bars): every bar is one muted color (accent at 55 %; income bars green at
55 %), the category's identity color moved into a label dot, `€` and `%`
merged into one column ("421 € · 12.8%"), and bars keep a 2 % minimum width
so tail rows stop looking like buttons.

**Where:** `static/js/app.js` — `renderCategoryBars()`,
`renderReportCategoryBars()`, `renderReportIncomeBars()`,
`renderTrendsMerchants()` (merchants have no identity color, so they get the
muted bar too, with the transaction count merged in as "· 12×").
`static/css/style.css` — `.cat-bar-pct-inline`, `.cat-bar-label` (flex+gap),
`.cat-bar-amount` (width 85px → 165px + `nowrap`, so "4 400 € · 17.1% −50%"
stays on one line).

**Revert:** Restore per-row `CHART_COLORS[idx]` backgrounds, the separate
`.cat-bar-pct` column div, the 85px `.cat-bar-amount` width, and remove
`Math.max(2, …)`.

---

## 9. Top-5 trends chart: lines only

**What:** Removed the overlapping area fills (`fill: true` + 18-alpha
backgrounds) that turned to mud where series crossed. Lines use stable
category colors (#2), smaller points.

**Where:** `static/js/app.js` — `drawTrendsFromData()` datasets.

**Revert:** Restore `backgroundColor: color + "18", fill: true,
pointRadius: 4`.

---

## 10. One heat encoding + compact calendar

**What:** (a) The spending heatmap dropped its blue ramp + red top bucket
and now uses the same theme-aware green ramp as the cash-flow calendar
(`--heat-0..3`, top level solid accent). (b) Calendar cells went from
`aspect-ratio: 1` (~160 px tall) to 56 px min-height; the amount is now the
primary figure (subhead, bold), the day number a small tertiary label.

**Where:** `static/css/style.css` — `.heatmap-cell[data-level]` colors,
`.calendar-day`, `.calendar-day-num`, `.calendar-day-amount`;
`templates/index.html` — the calendar legend dots (were still blue/red from
the old encoding, now `--heat-1` / `--heat-3`).

**Revert:** Restore the old `rgba(0,122,255,…)` levels +
`rgba(255,59,48,.85)` for level 4; restore `aspect-ratio: 1`, the old font
sizes, and the blue/red legend dots.

---

## 11. Transactions sorted newest-first (real bug fix)

**What:** `sort=date&dir=desc` returned oldest-first. Cause:
`CAST(t.date AS date)` — SQLite has no date type, so the cast collapses
every ISO string to the integer 2026; all rows tie and the id tiebreak
decides. Fixed by ordering on the raw ISO string `t.date` (sorts
chronologically on both engines).

**Where:** `app.py` `get_transactions()` — `SORT_COLS["date"]`.

**Revert:** `"date": "t.date"` → `"date": "CAST(t.date AS date)"`.

---

## 12. Filtered totals next to the result count

**What:** `GET /api/transactions` now returns `sum_expense` / `sum_income`
for the whole filtered set (one aggregate query alongside the count), and
the header shows "2,809 results · −25 739 € / +26 475 €".

**Where:** `app.py` `get_transactions()` (COUNT → aggregate row);
`static/js/app.js` `loadTransactions()` count line.

**Revert:** Restore the plain `SELECT COUNT(*)` and the count-only header
text. Extra response keys are additive — nothing else reads them.

---

## 13. Subscriptions: one money column

**What:** Dropped the near-duplicate "Typical" column; "Per month" remains
and its cell tooltip shows the typical charge. Table is 6 columns now.

**Where:** `templates/index.html` (header), `static/js/app.js`
`recurringRow()` + the four colspans (7→6).

**Revert:** Re-add the header, the `avg_amount` cell, colspans back to 7.

---

## 14. Stale "next due" dates get flagged; Due soon is orange

**What:** A next-due date in the past renders orange with a "may have
lapsed" tooltip instead of calm gray. The "Due soon" pill moved from the
positive accent color to orange (attention).

**Where:** `static/js/app.js` `recurringRow()`; `static/css/style.css`
`.recurring-due`.

**Revert:** Drop the `duePast` branch; restore
`.recurring-due { background: rgba(0,122,255,.10); color: var(--accent) }`.

---

## 15. Reports YoY: sign bug fixed, colors mean good/bad

**What:** "Net vs previous year 736 € +-137.7%" — the badge concatenated a
"+" with a ratio that had a negative base. Now the percent uses
`|diff| / |prev|`, the sign comes from the direction of change, and color
encodes good/bad: income up = green, expenses up = red (so expenses **down**
is green), net up = green.

**Where:** `static/js/app.js` `renderReportYoY()` — `yoyBadge(cur, prev,
goodWhenUp)`.

**Revert:** Restore the old `yoyBadge` (sign + `diff/prev`, class by
`diff >= 0`).

---

## 16. Reports: per-category YoY deltas

**What:** The annual endpoint now also returns `prev_categories` (previous
year's expense total per category) and each category bar shows its delta vs
last year (green when spending fell). Note: the review also suggested a
per-month table — that already existed on the page, so nothing was added
there; the duplicated KPI row/monthly chart were left in place (year-scoped,
harmless).

**Where:** `app.py` `annual_report()` (one query + response key);
`static/js/app.js` `renderReportCategoryBars()`.

**Revert:** Remove the `prev_categories` query/key and the `delta` span.

---

## 17. Net Worth chart starts at the first recorded balance

**What:** The history no longer draws months before the first
`account_balances` row (nine months of misleading flat 0 €). The series
starts at the first month with data (or the window start, whichever is
later).

**Where:** `networth.py` `compute_history()` — `first_month` lookup + skip.
`test_networth.py::test_carry_forward_and_summary` asserted the old
"leading months are 0" behaviour and was updated to assert those months are
absent instead.

**Revert:** Delete the `first_row`/`first_month` block and the
`if first_month and ym < first_month: continue` line, and restore the test's
`assert by_month["2026-02"]["net_worth"] == 0`.

---

## 18. Net Worth KPI emphasis

**What:** Net Worth (the headline) is the accent-colored number; Assets and
Liabilities are neutral (liabilities shown with −); "Change vs last month"
is green/red by its own sign.

**Where:** `static/js/app.js` `renderNetWorthCards()`.

**Revert:** Restore the old card HTML (`value` plain for net worth,
`income`/`expense` classes on assets/liabilities).

---

## 19. Liability signs consistent; "Shown" → "In total"

**What:** Liability account rows display their balance as negative
(−19 040 €), matching group subtotals and the KPI. The opaque "Shown"
column header became "In total" with a tooltip.

**Where:** `static/js/app.js` `nwAccountRow()`; `templates/index.html`
accounts table header.

**Revert:** Drop the `(a.type === "liability" ? "−" : "")` prefix; rename
the header back.

---

## 20. Stored category colors + picker

**What:** `categories` gained a nullable `color` column (SQLite additive
migration runs on startup; PG DDL + migration updated too). The Categories
page shows a color swatch per category; clicking opens a palette popover
(current palette's colors + "Auto" to clear). Stored colors win everywhere
via `catDotColor` (#2). API: GET includes `color`; PUT is now a partial
update (`{"color": …}` and/or `{"name": …}`); POST accepts `color`.

**Where:** `database.py` (both DDLs, `_MIGRATION_DDL`,
`_ensure_sqlite_columns()` called from `init_db`); `app.py`
`get_categories()`, `create_category()`, `update_category()`;
`static/js/app.js` `categoryRow()`, `openCatColorPicker()`,
`setCategoryColor()`; `static/css/style.css` `.cat-swatch`,
`.cat-color-pop`, `.cat-swatch-opt`.

**Revert:** UI: remove the swatch button + picker functions. API: restore
the fixed-field `update_category`. Schema: the extra nullable column is
harmless to leave; drop it only by rebuilding the table (SQLite).

---

## 21. Category usage info

**What:** `GET /api/categories` returns `tx_count` and `last_used` per
category (LEFT JOIN, one query). The Categories page shows "n transactions ·
last <date>" under each name, and the delete dialog says how many
transactions will be reassigned.

**Where:** `app.py` `get_categories()`; `static/js/app.js` `categoryRow()`,
`deleteCategory()`.

**Revert:** Restore the plain `SELECT *` query and the old row/dialog
markup. Extra keys are additive.

---

## 22. Merchant rules collapse; "Start Training" renamed

**What:** Rule groups render collapsed (header + count + caret) by default;
clicking a header toggles; any active search/filter expands all groups.
Group headers show the category dot. Button/copy renamed: "Start Training"
→ "Rebuild rules" ("this also happens automatically after each import").

**Where:** `static/js/app.js` `filterMerchantRulesView()` + copy strings;
`templates/index.html` button label; `static/css/style.css`
`.rule-group-caret`, `.merchant-rule-group.collapsed`.

**Revert:** Remove the `collapsed` class logic + CSS; rename the copy back.

---

## Verification

- Full suite: **71 failed / 57 passed — identical to the pre-change baseline**
  (stale multiuser-era tests; `test_auth.py`/`test_mailer.py` don't even
  collect since `auth.py` was deleted in the local port). The one test these
  changes did break (`test_networth`) was updated deliberately — see #17.
- Every page walked in the browser against a copy of the real DB, light and
  dark: dashboard, transactions, import, categories, trends, subscriptions,
  reports, net worth, settings.
- Checked live: newest-first sort + filtered totals; cents in rows; stored
  category color persisting and propagating to other pages; "Auto" reset;
  merchant-rule groups collapsing, expanding on click, expanding on search and
  re-collapsing on clear; recurring cache (first call 1.8 s → second 0.015 s);
  net-worth history starting at the first balance.

## Also in this session (pre-review, import redesign)

Import split fix (parts inherit edited amount/category no more; auto-balance
on 2 parts; modal scoped to top overlay; "+ Add part" quote bug), day-first
dates in the import review + server-side date normalization on confirm,
Ledger Classic review UI, auto-retrain merchant rules on confirm. Mockups in
`mockups/import-redesign/`.

---

# Net worth: updating balances, and selling things

A later, separate change from the 22 above. Revert notes are per item.

## 23. Balances update in place; blank means "keep"

**What:** No behaviour change on the server — entering a balance for an account
on a date that already has one overwrites it (`ON CONFLICT (account_id, as_of)
DO UPDATE`), and accounts you leave blank get no new row, so carry-forward keeps
their old value. That was already true but invisible, and a blank field read as
zero. Now the placeholder spells out what carries ("keep 9 109 €"), the header
above the table says the same, and the toast reports both halves: "Updated 3,
kept 7 unchanged".

**Where:** `static/js/app.js` `nwAccountRow()` (placeholder + title),
`saveNetWorthBalances()` (kept counter, toast); `templates/index.html`
`.nw-hint` line above the accounts table; `static/css/style.css` `.nw-hint`.

**Revert:** Restore the numeric placeholder (`a.latest_balance`), drop the
`kept` counter and the hint line. Nothing in the database changes either way.

---

## 24. Closing an account you sold or paid off

**What:** New ⊘ action per row. It writes a **zero balance at the as-of date**
and marks the account closed. Carry-forward then leaves it out of that month and
every later one, while earlier months still count what you actually held. ↩
reopens it. The old "In total" checkbox is gone: it toggled `is_archived`, and
net worth skipped archived accounts at *every* date — so unticking it erased the
account from last January too. The Status column shows Open/Closed instead.

Deleting (✕) still exists as the destructive option and now says what it does:
it erases the history as well.

**Where:**
- `networth.py` `_totals_as_of()` — dropped `AND a.is_archived = 0`; `summary()`
  returns closed accounts too (with `is_archived`), ordered last.
- `app.py` — new `POST /api/accounts/<id>/close` and `POST /api/accounts/<id>/reopen`.
- `database.py` `_ensure_closed_accounts_zeroed()` — startup migration giving any
  account archived under the old rules a zero the day after its last balance, so
  it stays out of current totals under the new ones. No-op on this DB (nothing
  was archived).
- `static/js/app.js` — `closeNetWorthAccount()`, `reopenNetWorthAccount()`,
  `nwAccountName()`; `nwAccountRow()` status pill + actions; `toggleAccountShown()`
  removed; closed accounts no longer auto-expand their holdings.
- `templates/index.html` — "In total" → "Status"; `static/css/style.css` —
  `.nw-status`, `.nw-account-closed` replace `.nw-shown-toggle`.

**Revert:** Put `AND a.is_archived = 0` back in `_totals_as_of` and
`WHERE a.user_id = %s AND a.is_archived = 0` back in `summary()`; delete the two
routes and the migration; restore the checkbox + `toggleAccountShown()`. Zero
balances already written by ⊘ stay in `account_balances` — harmless, but delete
them if you want the old numbers exactly.

---

## 25. Removing a single holding

**What:** ✕ on each row of the holdings drill-down deletes that holding from
that snapshot and recomputes the account total for the date (the total was the
sum of its holdings). It corrects a snapshot; it is not the way to record a sale
— the confirm dialog and the panel note both say so and point at importing a
newer statement, which drops the holding from the new snapshot and leaves the
history true.

**Where:** `app.py` `DELETE /api/networth/holdings/<id>` (+ `id` added to the
holdings GET); `static/js/app.js` `deleteHolding()` and the drill-down table.

**Revert:** Drop the route and the ✕ column.

---

## Verification (23–25)

- `test_networth.py`: 5 passed. `test_archived_excluded` was replaced by
  `test_closed_account_leaves_total_but_keeps_history`, which asserts the new
  rule directly: March/April still read 10 499 with the fund, May reads 500.
- Full suite still **71 failed / 57 passed** — the same baseline as above.
- Against a copy of the real DB (10 accounts, 46 976 € net worth):
  entering one balance for one account moved net worth by exactly that
  difference and left the other nine on their old date; re-entering the same
  account and date updated in place (2 balance rows, not 3); closing Nordnet
  (35 390 €) dropped August to 13 115 € and left June and July untouched;
  reopening restored the row without moving the total; declining either confirm
  changed nothing.
