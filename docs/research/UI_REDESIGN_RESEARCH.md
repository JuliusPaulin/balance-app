# UI Redesign Research

Separate visual-redesign research track. This is NOT the feature-research file (see `RESEARCH.md` for feature ideas). Everything here concerns BOLD, distinctive visual redesign directions for the whole-app look.

## Project Context

- App: personal expense tracker. Flask + SQLite backend, vanilla HTML/CSS/JS frontend (no framework) rendered in a pywebview native macOS window; also usable as a plain web app. Charts via Chart.js.
- Current look: clean Apple/iOS-style — `#ffffff` bg, `#f5f5f7` secondary, `#1d1d1f` text, `#0071e3` accent, 8px grid, rounded cards, subtle shadows, sidebar nav. Pleasant but generic.
- Single-user, local-first, privacy-focused. Data-dense screens: multi-chart dashboards, transaction tables, trends, recurring-subscriptions list, net-worth tab. Finland / EUR / fi-FI / 0-decimal.
- Hard constraint: must remain buildable in HTML/CSS/JS. A framework/tooling swap (Tailwind, or a CSS lib) may be MENTIONED, but no native-only paradigms. Charts must stay Chart.js-compatible (it is fully themeable).

## Status

**VERDICT (2026-05-31): UI-01 Dark Fintech ACCEPTED**, to be implemented in BOTH a
light and a dark theme with a user-facing light/dark toggle. Interactive mockups
built in `mockups/` (`index.html` chooser + `ui-01-dark-fintech.html`,
`ui-01-light-fintech.html`, and UI-02..05). The light variant
(`ui-01-light-fintech.html`) is the source of truth for the light palette; the
dark variant for the dark palette. Implementation brief: `../plans/UI_FINTECH_IMPLEMENTATION.md`.
Handed to the code-agent to execute on branch `feat/fintech-theme`.

UI-02..UI-05 were NOT selected this round but are NOT rejected — they remain
available for future rounds (no permanent exclusions recorded).

---

## Pending Review — 5 Redesign Directions

### UI-01 — Dark Fintech (dark-mode-first, neon accent)
- **Look:** Near-black / deep-navy canvas (`#0B0E14`–`#111827`), elevated surfaces via subtle lighter panels + thin 1px borders instead of shadows. One or two vivid accents (electric green `#00E599`, or violet/cyan) used sparingly for positive/negative balances, active nav, key KPIs. Inter / Geist for UI, tabular-figures for numbers. Charts: dark canvas, glowing line/area gradients, muted gridlines. Tables: zebra-free, hairline row dividers, color-coded amounts.
- **Why it suits:** This is the de-facto "premium finance" idiom (Copilot, Revolut dark, crypto dashboards). Reduces eye strain for long sessions; data and color-coded numbers pop against dark. Luminance-based hierarchy keeps dense dashboards calm. Trade-off: dark mode needs careful contrast tuning for accessibility; one accent must do a lot of work.
- **Effort:** Medium. Custom CSS only — mostly a re-skin of existing tokens (swap palette, borders-over-shadows, re-theme Chart.js). No framework required.
- **Sources:**
  - Copilot Money (Apple Design Award Finalist 2024) — https://www.copilot.money/
  - Dribbble "Fintech Dashboard | Dark Theme" by Ronas IT — https://dribbble.com/shots/15120400-Fintech-Dashboard-Dark-Theme-User-Interface
  - Dribbble dark-fintech tag — https://dribbble.com/tags/dark-fintech
  - Eleken "Fintech UI examples [15 real apps]" — https://www.eleken.co/blog-posts/trusted-fintech-ui-examples
  - Mobbin Finance (mobile) — https://mobbin.com/explore/mobile/app-categories/finance

### UI-02 — Bento-grid Dashboard (modern-trendy, modular)
- **Look:** The dashboard becomes a tiled "bento box" — asymmetric rounded tiles of varying column/row spans. Big hero tiles for net worth / monthly spend, smaller tiles for KPIs, a wide tile for the trend chart, a tall tile for the subscriptions list. Generous gaps, soft radius, light or dark. Tile size encodes importance (the bigger the tile, the more important). Light motion: tiles lift/scale slightly on hover.
- **Why it suits:** Purpose-built for data-dense dashboards with many heterogeneous widgets — exactly your multi-chart home screen. Strong visual hierarchy lets you scan financial health in seconds. Works beautifully with CSS Grid. Trade-off: mainly a dashboard/home pattern; transaction tables and detail views still need their own treatment. Discipline needed (max ~2 hero tiles per section).
- **Effort:** Low–Medium. Pure CSS Grid (`grid-template-areas` / column spans) — no framework needed. Mostly re-layout of existing cards.
- **Sources:**
  - Senorit "Bento Grid Design trend 2025" — https://senorit.de/en/blog/bento-grid-design-trend-2025
  - Galaxy UX "Bento Grids: New Standard for Modular UI" — https://www.galaxyux.studio/blog/bento-grids-the-new-standard-for-modular-ui-design/
  - Orbix Studio "Bento Grid Dashboard Design" (cites Payhawk fintech bento) — https://www.orbix.studio/blogs/bento-grid-dashboard-design-aesthetics
  - Superfiles "Bento Grid UI Design Guide" — https://superfiles.in/bento-grid-ui-design-trend.php

### UI-03 — Editorial / Swiss (ultra-minimal, typographic)
- **Look:** Off-white / warm paper canvas, strict typographic grid, lots of whitespace. A real type system: large modern serif (e.g. a Tiempos / Source Serif feel) for headings and big balance numbers, clean grotesque (Helvetica Now / Inter) for body and labels, tabular figures everywhere. Minimal color — mostly black/ink with one restrained accent; data communicated through type weight, rules (hairlines) and spacing rather than boxes. Charts: thin, restrained, axis-light, almost print-like. Tables read like a financial statement.
- **Why it suits:** Conveys calm, trust and "serious money" without fintech flash; the serif-number revival reads premium/editorial. Excellent for a net-worth / statement view. Trade-off: less "appy", relies on disciplined typography and a quality typeface; whitespace can fight true density, so tables need tight, well-set rows.
- **Effort:** Medium. Custom CSS + 1–2 licensed/Google webfonts. No framework — it's a typography + grid exercise. Biggest cost is sourcing good fonts and tuning the type scale.
- **Sources:**
  - PRINT Magazine "Swiss Style: Principles, Typefaces & Designers" — https://www.printmag.com/featured/swiss-style-principles-typefaces-designers/
  - Pixeldarts "Swiss Web Design guide 2025" — https://www.pixeldarts.com/post/swiss-style-web-design-a-comprehensive-guide
  - Creative Boom "Font trends 2025" (serif/editorial revival) — https://www.creativeboom.com/insight/font-trends-2025/
  - Mew Design "Swiss Design Style: Grids, Clarity, Order" — https://docs.mew.design/blog/swiss-design-style/

### UI-04 — Terminal / Bloomberg-dense (data-dense, pro power-user)
- **Look:** Maximum information density. Monospace or tabular type (IBM Plex Mono / JetBrains Mono) for all numbers, tight row heights, very small gutters, everything visible at once — minimal whitespace by design. Dark phosphor palette (charcoal bg, green/amber/red status colors) or a "modern terminal" charcoal+accent variant. Multi-pane layout: live-ish panels, sortable dense tables, sparkline-style mini charts inline in table cells. Keyboard-first affordances (command palette, ticker-style quick entry).
- **Why it suits:** You have genuinely dense data (transactions, recurring, trends, net worth) and you're the only, expert user — the classic case where density beats whitespace. Lets you see the whole financial picture at a glance. Trade-off: intimidating, low "delight", weakest on the macOS-native-app feel; needs careful typography to avoid looking like a spreadsheet. Niche but very distinctive.
- **Effort:** Medium–High. Custom CSS (mono font, tight density tokens, inline sparklines) plus more JS for sortable tables / command palette / inline mini-charts. No framework strictly required, but a tiny table/grid helper would help.
- **Sources:**
  - Bloomberg "How Terminal UX designers conceal complexity" — https://www.bloomberg.com/company/stories/how-bloomberg-terminal-ux-designers-conceal-complexity/
  - Hacker News "info-dense apps / Bloomberg terminal" discussion — https://news.ycombinator.com/item?id=19153875
  - jmrothberg/bloomberg-terminal (single-file web clone, IBM Plex Mono) — https://github.com/jmrothberg/bloomberg-terminal
  - Dribbble "bloomberg-terminal" inspiration — https://dribbble.com/search/bloomberg-terminal

### UI-05 — Linear-style (bold/expressive, dark gradient + glass)
- **Look:** Dark-first like UI-01 but more expressive: subtle multi-stop gradients, soft glows, light glassmorphism (frosted translucent panels with blur), accent-color hierarchy, micro-motion (gentle transitions, animated focus states). Borders-over-shadows for depth, luminance hierarchy over weight. Inter/Geist UI font, crisp tabular numbers. Charts: gradient-filled areas glowing against the dark canvas, blurred backdrop layers. Feels like Linear / Arc / Raycast / Warp.
- **Why it suits:** The most "current/2025–26 cool" option; reads as a polished modern product, great for screenshots and personal pride. Shares dark-mode ergonomics with UI-01 but with more personality. Trade-off: gradients/blur/glass risk hurting legibility on data-dense tables and can feel heavy if overused; backdrop-filter blur has minor performance cost. Keep effects on chrome, not on dense data.
- **Effort:** Medium–High. Custom CSS (gradients, `backdrop-filter`, transitions) — no framework needed, but more polish/tuning than UI-01. Restraint required on table/chart areas.
- **Sources:**
  - LogRocket "Linear design: the SaaS design trend" — https://blog.logrocket.com/ux-design/linear-design/
  - Medium (Bootcamp) "The rise of Linear style design" — https://medium.com/design-bootcamp/the-rise-of-linear-style-design-origins-trends-and-techniques-4fd96aab7646
  - Medium "Dark Glassmorphism: the aesthetic that will define UI in 2026" — https://medium.com/@developer_89726/dark-glassmorphism-the-aesthetic-that-will-define-ui-in-2026-93aa4153088f
  - Figma Community "Linear App Style landing page collection" — https://www.figma.com/community/file/1367670334751609522/linear-app-style-landing-page-collection-50-sections-100-editable-free
  - Reference apps: Linear (linear.app), Arc, Raycast, Warp

---

## Diversity check
- Dark-mode-first: UI-01 (clean) and UI-05 (expressive)
- Bold / expressive: UI-05; also UI-04 in a different (pro) register
- Ultra-minimal / editorial: UI-03
- Data-dense / pro: UI-04; UI-02 is the modular middle ground
- Modern-trendy: UI-02 (bento) and UI-05 (linear/glass)

## Accepted Directions
- **UI-01 Dark Fintech** — accepted 2026-05-31, to ship as light + dark themes with a
  toggle. See `../plans/UI_FINTECH_IMPLEMENTATION.md`.

## Rejected Directions
_(none — UI-02..05 not selected but still available for future rounds)_

## Research Log
- 2026-05-31 — Round 1. Researched bold whole-app redesign directions via web search (Dribbble, Mobbin, Bloomberg UX, LogRocket, design-trend writeups). Produced 5 distinct directions UI-01..UI-05 spanning dark/clean, modular, editorial, dense-pro, and expressive-glass. All pending the owner's verdict. No implementation, no handoff.
- 2026-05-31 — Built interactive HTML mockups for all 5 directions in `mockups/` (shared sample data, real Chart.js, cross-link switcher). The owner reviewed, liked UI-01, requested a light variant → built `ui-01-light-fintech.html`. **Verdict: UI-01 accepted, ship light + dark with toggle.** Wrote `../plans/UI_FINTECH_IMPLEMENTATION.md` and handed to code-agent on branch `feat/fintech-theme`.
