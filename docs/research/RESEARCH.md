# Expense Tracker — Feature Research

Single source of truth for feature/QoL research. Read this first before any research round.

## Project Context

- Flask + SQLite personal expense tracker, packaged as native macOS app (pywebview), with a read-only SwiftUI iOS companion that syncs via an iCloud Drive JSON snapshot.
- Single-user, local-first, privacy-focused. No cloud backend, no bank aggregation, no network entitlement on iOS.
- Apple-style clean design. Finland / EUR / fi-FI locale / 0-decimal currency formatting.
- Already implemented (do not re-recommend): transaction CRUD with rich filtering; CSV import for 3 Finnish/Nordic bank formats with staging/review flow; merchant auto-categorization rules (exact/contains/fuzzy + historical fallback, 567 auto-generated rules); dashboard with Chart.js (monthly bar, over-time line, top categories, category breakdown with drill-down, daily totals); annual reports; per-month notes; split-costs (÷2) feature.

### Hard Constraints (exclusion filter for all research)
- No cloud backend / no server-side storage.
- No third-party bank aggregation (Plaid, etc.) or live bank connections.
- Privacy-first: data stays local.
- Must fit single-user usage.

## Accepted Recommendations

User verdict 2026-05-31.

- **R1-04 Recurring transaction detection** — ✅ Accepted. Plan drafted in
  `../plans/RECURRING_DETECTION_PLAN.md`. (Note: subscription-list view R1-05 folded in
  as the front-end of this feature rather than a separate item.)
- **R1-08 Net worth tracking** — ✅ Accepted, **to be built as its own dedicated
  tab**. Plan drafted in `../plans/NET_WORTH_PLAN.md`.

## Rejected Recommendations

User verdict 2026-05-31.

- **R1-01 Monthly category budgets** — ❌ Not interested in budgeting features.
- **R1-02 Category rollover / savings envelopes** — ❌ Not interested in budgeting.
- **R1-03 Savings goals with target + date** — ❌ Not interested in budgeting.
- **R1-05 Subscription manager view** — ❌ as a standalone item; import/automation
  is considered good enough. (Its useful part — the recurring/subscription list —
  is absorbed into R1-04's UI.)
- **R1-06 Rule-learning from manual recategorization** — ❌ Import/categorization
  is already good; not needed.

## Deferred — needs more concrete examples

User said analytics "can always be improved but I need more concrete examples."
Researcher to come back with sharper, specific proposals (mockup-level) before a
verdict.

- **R1-07 End-of-month balance / spend projection** — ⏸ Deferred pending concrete
  examples.
- **R1-09 Custom/saved reports + saved filters** — ⏸ Deferred pending concrete
  examples.

## Pending Review

No verdict given yet on the UX/QoL items — still awaiting user decision.

### UX / QoL
- **R1-10 Transaction tags (cross-category)** — color-coded tags to group transactions across categories (e.g. "Trip to Lapland", "Birthday") without one-off categories. Complexity: M | Impact: M. Source: Lunch Money tags.
- **R1-11 Undo for destructive actions + bulk edit on main list** — undo delete (toast), and bulk category/type edit + bulk delete on the main transactions list (currently bulk edit exists only in import review). Complexity: L | Impact: M. Source: Dime (undo delete), Kuku (bulk edit).
- **R1-12 Command palette / keyboard shortcuts + dark mode** — Cmd+K palette for quick add/search/navigate, Cmd+N new expense, and a dark mode honoring system appearance. Complexity: M | Impact: M. Source: Budget Flow (Mac keyboard shortcuts), command palette pattern, Dime/ExpenseOwl dark mode.

## Research Log

### 2026-05-31 — Round 1
- Reviewed CLAUDE.md; no prior RESEARCH.md existed (created this file). No prior accept/reject decisions.
- Sources consulted: Actual Budget (vs YNAB blog, features), Lunch Money (budgeting, tags, rollover features), Monarch, Rocket Money, Copilot Money, Quicken Simplifi, Finny, Pocket Clear, SenticMoney, Dime, Kuku, Budget Flow, ExpenseOwl, command-palette UX references.
- Filtered out anything requiring cloud backend / bank aggregation (e.g. live sync, Plaid, encrypted cloud backup, AI assistant cloud features).
- Produced 12 candidate recommendations across Budgeting, Import/Automation, Analytics, UX/QoL. All pending user verdict.

### 2026-05-31 — Round 1 verdict
- **Accepted:** R1-04 (recurring detection), R1-08 (net worth, as its own tab). Plans drafted: `../plans/RECURRING_DETECTION_PLAN.md`, `../plans/NET_WORTH_PLAN.md`.
- **Rejected:** R1-01/02/03 (no interest in budgeting); R1-05, R1-06 (import/categorization already good — R1-05's list folded into R1-04).
- **Deferred (needs concrete examples):** R1-07, R1-09 — user open to analytics but wants sharper, specific proposals next round.
- **Still pending:** R1-10, R1-11, R1-12 (UX/QoL) — no verdict yet.
