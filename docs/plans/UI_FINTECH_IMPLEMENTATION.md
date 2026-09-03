# Fintech Theme — Implementation Brief

Reskin the Expense Tracker to the **Fintech** visual style (from the accepted
mockup UI-01) and ship it in **both a light and a dark theme** with a user-facing
**theme toggle** (Light / Dark / Auto). See `../research/UI_REDESIGN_RESEARCH.md` for the verdict.

## Source of truth (the look)
- **Light palette:** `mockups/ui-01-light-fintech.html` `:root`
- **Dark palette:** `mockups/ui-01-dark-fintech.html` `:root`
- The mockups also show the target component idiom: hairline borders + soft shadow
  in light / borders-over-shadows in dark, green accent, tabular-nums on all numbers,
  KPI chips, slim category bars, area net-worth chart.

## Hard constraints
- HTML / CSS / vanilla JS only. **Chart.js stays** (it's already loaded). No frameworks, no build step.
- **Do not touch the Python backend** (`app.py`, `database.py`, `recurring.py`, `networth.py`, `icloud_export.py`) or the `ios/` app. This is purely a frontend reskin + theme system.
- Existing functionality and DOM ids/classes used by `app.js` must keep working — rename nothing that JS queries.
- The app runs at `http://localhost:5050` (pywebview shell). It must look right both in the native window and a plain browser.

## Files in scope
1. `static/css/style.css` — palette tokens + dark override + component restyle.
2. `templates/index.html` — anti-FOUC head script, theme toggle UI, fix stray hardcoded colors.
3. `static/js/app.js` — theme module (persist/apply), chart theming, re-render on toggle.

---

## Task 1 — CSS theming (`static/css/style.css`)

The file is already token-driven (~259 `var()` uses). Strategy: **redefine the
existing token values to the light-fintech palette in `:root`, then add a
`:root[data-theme="dark"]` block overriding the same tokens with the dark-fintech
palette.** Because components already use the tokens, most of the app re-themes for free.

**Token mapping (existing name → light value / dark value):**

| Token | Light | Dark |
|---|---|---|
| `--bg` (card/surface) | `#ffffff` | `#121722` |
| `--bg-secondary` (app canvas) | `#f6f7f9` | `#0b0e14` |
| `--bg-tertiary` | `#f0f2f5` | `#171d2a` |
| `--bg-grouped` | `#f6f7f9` | `#0b0e14` |
| `--text-primary` | `#0d1320` | `#e7ebf2` |
| `--text-secondary` | `#5e6678` | `#8a93a6` |
| `--text-tertiary` | `#9aa1b1` | `#5d6677` |
| `--text-quaternary` | `#b6bcc9` | `#454d5e` |
| `--accent` | `#00a06b` | `#00e599` |
| `--accent-hover` | `#008f5f` | `#00c886` |
| `--green` | `#00a06b` | `#00e599` |
| `--red` | `#e0445b` | `#ff5c72` |
| `--separator` | `#e6e9ee` | `rgba(255,255,255,.08)` |
| `--separator-opaque` | `#dce0e7` | `#222a38` |
| `--border` | `#e6e9ee` | `rgba(255,255,255,.10)` |
| `--sidebar-bg` | `#ffffff` | `#0c1019` |
| `--shadow-sm/md/lg/card` | keep soft (rgba ~0.04–0.10) | set to `none` / near-transparent — **dark uses borders, not shadows** |

- Keep the categorical accent colors (`--orange`, `--purple`, `--teal`, `--indigo`,
  `--yellow`, `--pink`) but verify they read OK on the dark canvas; nudge lightness if muddy.
- Add `color-scheme: light` to `:root` and `color-scheme: dark` to the dark block
  (so native form controls / scrollbars match).
- Add a smooth theme transition: `body, .card, .sidebar { transition: background-color .2s ease, border-color .2s ease, color .2s ease; }` (avoid transitioning `all`).
- Apply `font-variant-numeric: tabular-nums` to money/number elements (KPI values, table amounts, axis ticks). Reuse the mockups' `.num` convention or target the existing amount classes.
- Component nudges to match the mockup (light touch — don't restructure layout):
  - Active sidebar nav item: tinted accent background + accent text (see mockup `.nav.active`).
  - KPI / summary value chips: pill chips, green for positive, red for negative (mockup `.chip.up/.down`).
  - Cards: 1px `var(--border)` + `var(--shadow-card)` in light; border-only in dark.
  - Category bars: slim 7px rounded track with accent-gradient fill.

**Sweep for hardcoded colors** that bypass tokens (these break dark mode):
- In `style.css`: `#f0f6ff`, `#e3eeff`, `#B36000`, any stray `#fff`/`#FFFFFF` outside `:root`. Replace with tokens.
- In `templates/index.html`: the guide modal uses inline `background:#fff` and `rgba(0,0,0,0.5)`; dropdown/menu/toast inline styles may hardcode white. Replace inline `background:#fff` with `var(--bg)` and audit modals, dropdowns, toasts, the import review table, and form inputs so nothing renders white-on-white in dark.

---

## Task 2 — Theme toggle + anti-FOUC (`templates/index.html`)

1. **Anti-FOUC head script** — add as the FIRST thing in `<head>` (before the stylesheet link) so the theme is set before first paint:
   ```html
   <script>
   (function(){
     var pref = localStorage.getItem('theme') || 'auto';
     var dark = pref === 'dark' || (pref === 'auto' &&
       window.matchMedia('(prefers-color-scheme: dark)').matches);
     document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
   })();
   </script>
   ```
2. **Toggle UI** — two entry points:
   - A **segmented control (Light / Dark / Auto)** as a new card on the Settings page (`#page-settings`), styled like the existing buttons.
   - A **quick toggle button** in the sidebar footer (near Settings / iCloud), e.g. a sun/moon icon that flips light↔dark. Use inline SVG matching the existing icon style.
   - Wire both to the JS theme module (Task 3); keep them in sync (re-render the control's active state on change).

---

## Task 3 — Theme module + chart theming (`static/js/app.js`)

1. **Theme module:**
   - `applyTheme(pref)` where pref ∈ `'light'|'dark'|'auto'`: resolve `auto` via `matchMedia`, set `document.documentElement.dataset.theme`, persist pref to `localStorage.theme`, update toggle UI, then call `refreshChartsForTheme()`.
   - On load, initialize the toggle controls to the stored pref.
   - Add a `matchMedia('(prefers-color-scheme: dark)')` listener so `auto` follows the OS live.
2. **Chart theming** — charts currently hardcode ~30 colors (grid `#AEAEB2`/`#8E8E93`,
   tooltip `rgba(28,28,30,.92)`, gridline `rgba(60,60,67,.06)`, dataset accents
   `#007AFF`, `#FF9500`, `#AF52DE`, `rgba(52,199,89,…)`, etc.).
   - Add `chartTheme()` that reads the **live CSS variables** via
     `getComputedStyle(document.documentElement).getPropertyValue('--…')` and returns
     `{ text, grid, accent, accentFill, green, red, tooltipBg, tooltipBorder, ... }`.
   - Refactor `chartOptions()` and every `new Chart()` dataset/scale color to pull from
     `chartTheme()` instead of literals. Map: gridlines → `grid`, tick labels → `text`,
     tooltip bg/border → tokens, primary series (net worth, income) → `accent`/`green`,
     expenses → `red`, keep categorical series but source from a small theme-aware palette.
   - For the net-worth area chart, build the fill gradient from the accent at runtime
     (as the mockups do) so it works in both themes.
3. **Re-render on toggle** — `refreshChartsForTheme()` must re-apply colors to all live
   charts. Simplest robust approach: for each key in the `charts` registry that exists,
   re-invoke its render function (they already destroy+recreate). Guard each call so a
   not-yet-rendered page doesn't error. Alternative: re-trigger the current page's loader.
   Charts must visibly recolor immediately on toggle without a manual reload.

---

## Acceptance criteria
- Toggle flips the **entire** UI light↔dark — sidebar, cards, tables, inputs, buttons,
  modals (guide), dropdowns, toasts, import review — with **no white-on-white** or
  unstyled flash on load (anti-FOUC works).
- Choice **persists** across reloads; `Auto` follows the OS setting live.
- **All 10 charts** recolor correctly in both themes (gridlines, ticks, tooltips,
  series, net-worth gradient) immediately on toggle.
- Visual match to the mockups: green accent, hairline borders, soft-shadow-in-light /
  border-only-in-dark, tabular numbers, KPI chips.
- No backend changes; `python3 -m pytest test_recurring.py test_networth.py` still green.

## Workflow
- Work on a new branch **`feat/fintech-theme`** (do not commit to `main`).
- Verify by launching the app (`python3 main.py`, or open `http://localhost:5050`)
  and toggling through every page in both themes; confirm charts and modals.
- Leave it on the branch for the owner to review — **do not merge**. Report: files
  changed, how the toggle works, anything deferred, and the test result.
