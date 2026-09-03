# Expense Tracker App — Plan

## Tech Stack
- **Backend:** Python, Flask, SQLite
- **Frontend:** HTML/CSS/JS (vanilla, Apple-style design)
- **Desktop shell:** pywebview (native window wrapping web UI)
- **Charts:** Chart.js

## Data Model

### Tables
- **transactions** — id, date, store, category, amount, type (expense/income), created_at, updated_at
- **categories** — id, name, type (expense/income), is_default
- **import_staging** — id, date, store, suggested_category, amount, type, confirmed (bool), final_category, import_batch_id
- **import_batches** — id, filename, imported_at, status

## Features (Priority Order)

### Phase 1 — Core Data Layer
- [x] SQLite database setup with migrations
- [x] Category seeding (predefined expense + income categories)
- [x] Transaction CRUD API
- [x] Category CRUD API

### Phase 2 — CSV Import & Review
- [x] CSV parser supporting multiple formats (flexible column mapping)
- [x] Import staging: parse CSV -> staging table with suggested categories
- [x] Review UI: show staged imports, suggest categories, require manual confirmation
- [x] Category suggestion engine (fuzzy match store name to past categorizations)
- [x] Bulk confirm / edit / reject staged imports

### Phase 3 — Manual Entry
- [x] Add expense/income form (date, store, category, amount)
- [x] Edit existing transactions
- [x] Delete transactions

### Phase 4 — Dashboard
- [x] Monthly expense vs income summary (bar chart)
- [x] Expense vs income over time (line chart, x=month, y=amount)
- [x] Top 5 expenses trend — find top 5 categories in latest month, show their trend over past months
- [x] Expenses by category for last month (pie/donut chart)

### Phase 5 — Category Management
- [x] View all categories
- [x] Add / edit / delete custom categories
- [x] Reassign transactions when deleting a category

## Design Principles
- Apple-style: clean whites, subtle shadows, SF-style fonts, rounded corners
- Minimal chrome — content first
- Sidebar navigation
- Smooth transitions
- Consistent spacing (8px grid)
- Color palette: white bg, #f5f5f7 secondary bg, #1d1d1f text, #0071e3 accent

## Default Categories

### Expense
Car charging, Car maintenance, Car parking, Car payment, Clothing, Condo fees, Debt, Dog, Electronics, Entertainment, Exercise, Gas, Gifts, Going out, Groceries, Home maintenance, Insurance, Investments, Lunch, Medical, Other, Public transportation, Rent, Restaurant, Telecom, Travel, Utilities, Work

### Income
Job, Side project, Kela, Expense reimbursement, Other, Investments

## CSV Import Format Support
Primary format: Date, Store, Category, Amount
Flexible: auto-detect column order via header matching, handle various date formats, handle comma/dot decimal separators
