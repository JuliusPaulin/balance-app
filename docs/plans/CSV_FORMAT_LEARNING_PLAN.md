# CSV Import — Learn Unknown Formats (column-mapping + remember)

Goal: when a CSV's columns aren't auto-detected, instead of erroring, open a view where the user **maps which column is Date / Merchant / Amount**, import succeeds, and the app **remembers that format** so the next file with the same header layout auto-imports.

## Current behaviour (measured, `app.py`)
- `POST /api/import/upload` (`upload_csv`, ~line 2031): decode → `_detect_delimiter` → read `headers` → `detect_columns(headers)` maps `date/amount/store/message/category` by **header-name aliases** → Finnair EUR override.
- If `date` or `amount` isn't detected → returns **400** `{"error":"Could not detect required columns (date, amount)", "detected_headers": headers}` and deletes the batch. **This is the dead-end we're replacing.**
- Frontend `static/js/app.js`: `csv-input` → POST to `/api/import/upload` (~line 960); on success renders the review/staging table; on error shows the message.
- Helpers: `parse_date` (handles `YYYY-MM-DD`, `YYYY/MM/DD`, `DD.M.YYYY`, dateutil fallback), `parse_amount` (sign → expense/income), `_detect_delimiter`.

## Design

### 1. Data model — learned formats (new table)
`import_formats(id, user_id FK, signature TEXT, delimiter TEXT, date_col INT, amount_col INT, store_col INT NULL, message_col INT NULL, category_col INT NULL, amount_sign TEXT DEFAULT 'neg_expense', date_hint TEXT NULL, created_at TEXT, UNIQUE(user_id, signature))`.
- **`signature`** = stable fingerprint of the layout: lowercased, trimmed, joined header names + delimiter (e.g. `sha1("|".join(h.strip().lower() for h in headers) + "|" + delimiter)`). Same bank export → same signature.
- Per-user (each user teaches their own formats). Add to `database.py` schema + `migrate_db()` (idempotent `CREATE TABLE IF NOT EXISTS`).

### 2. Upload flow changes (`upload_csv`)
- After computing `headers`/`delimiter`/`detect_columns`: if required cols missing, **first look up a learned format** by `(user_id, signature)`. If found → apply its column map (+ sign/date hint) and parse normally (no prompt). This is the "learned" fast-path.
- If still unmapped → **do NOT 400**. Return `200` with `{"needs_mapping": true, "signature": ..., "delimiter": ..., "headers": [...], "sample_rows": [first ~5 data rows as arrays], "guess": {date: i|null, amount: i|null, store: i|null}}` (use `detect_columns`/heuristics to pre-select dropdowns where possible). Keep the created `import_batch` OR defer batch creation until mapping is confirmed (cleaner: **don't create a batch until we can parse** — return the preview without a batch, re-parse on confirm).
- New endpoint **`POST /api/import/upload-mapped`** (or extend upload with a `mapping` field): body carries the file again (re-upload — files are small) OR the server-cached content + the chosen `mapping` `{date_col, amount_col, store_col?, amount_sign, remember: bool}`. Validate indices in range; parse rows with the mapping via the existing `parse_date`/`parse_amount`; stage exactly like the normal path; if `remember` → upsert into `import_formats` (`ON CONFLICT (user_id, signature) DO UPDATE`). Return the staging items (same shape as `upload_csv`) so the UI flows into the normal review table.
- Keep everything **user-scoped** (`user_id` on the lookup, the batch, staging, and the saved format) and CSRF-protected (it's a POST; global CSRF applies). Re-use the rate limit.

### 3. Frontend — column-mapping view
- When `/api/import/upload` returns `needs_mapping`, open a **mapping modal/section** (style with existing `.card`/`.modal`, mobile-friendly):
  - A small **preview table** of `sample_rows` with the header names as columns.
  - Three required dropdowns — **Date column**, **Merchant column** (optional/“none”), **Amount column** — each listing the headers, pre-selected from `guess`.
  - An **Amount sign** toggle: "negative = expense" (default) vs "positive = expense" (some banks). Optional **date format** hint if auto-parse struggles.
  - A **"Remember this format"** checkbox (default on).
  - Buttons: **Import** (→ POST `/api/import/upload-mapped`) and Cancel.
- On success → render the normal review/staging table (reuse the existing flow). On a later upload of the same layout, the backend auto-maps and the user never sees the modal.
- Add a tiny **"Saved formats"** affordance in Settings or the Import page (list learned formats with a delete button → `DELETE /api/import/formats/<id>`), so a bad mapping can be removed. (Optional, low priority.)

### 4. Tests
- Unknown-format upload returns `needs_mapping` (not 400) with headers + sample rows.
- `upload-mapped` with a valid mapping stages the rows correctly (date/amount/merchant), respects the sign toggle, and (with `remember`) creates an `import_formats` row.
- A second upload of the **same layout** auto-maps (no `needs_mapping`) using the saved format.
- Index validation (out-of-range mapping → 400); per-user isolation (user A's saved formats don't apply to user B); CSRF/rate-limit unaffected; existing recognized formats (Finnair/EtuTili/Nordea) still import unchanged.
- Keep the suite green (121 currently).

### 5. Steps for the code agent
A. Schema: `import_formats` table + `migrate_db()` + helper to compute `signature`.
B. Backend: refactor `upload_csv` to (learned-lookup → needs_mapping preview), add `upload-mapped` + `DELETE /api/import/formats/<id>`, keep parsing DRY (shared `_stage_rows(...)`).
C. Frontend: mapping modal + wiring + (optional) saved-formats list.
D. Tests + a Playwright screenshot check of the mapping modal (desktop + mobile).

## Notes / decisions
- v1 = single amount column with a sign toggle. Separate debit/credit columns and multi-currency EUR-column cases can be a follow-up (Finnair already handled specially).
- Merchant column is optional (some statements have only a message/reference) — fall back to empty store, category auto-suggest still runs.
- Don't create an `import_batch` until we can actually parse, so an abandoned mapping leaves no orphan batch.
