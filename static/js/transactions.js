// Balance — transactions.js
//
// The transaction list, the filter rail and its facet counts.
//
// One of the ordered classic scripts index.html loads. They share one global
// scope on purpose: index.html wires ~95 inline handlers straight to these
// names, so these are plain scripts and not modules.

// ── Search & the filter rail ────────────────────────────────────────
// The filters live in a rail beside the table rather than a drawer above it,
// so nothing opens, nothing closes, and the table never moves under you. Each
// value carries the count it would give — see /api/transactions/facets.
let searchDebounce = null;
let selectedCatIds = new Set();
let selectedMonths = new Set();
let selectedType   = "";           // "" | "expense" | "income"
let periodGrain    = "month";      // "month" | "year"
let facets         = { categories: [], types: [], months: [] };
let railMore       = { period: false, cat: false };
// Sorting used to live in two dropdowns inside the drawer while the table
// headers already sorted on click. The headers won; this is where the state
// they were duplicating now lives.
let txSort = { col: "date", dir: "desc" };

const RAIL_COLLAPSED = 6;          // values shown before "Show N more"

function debounceSearch() {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => { currentPage = 1; loadTransactions(); }, 320);
    const q = document.getElementById("search-q").value;
    document.getElementById("search-clear").style.display = q ? "flex" : "none";
}

function clearSearch() {
    document.getElementById("search-q").value = "";
    document.getElementById("search-clear").style.display = "none";
    currentPage = 1;
    loadTransactions();
}

// Any rail control: reset to page 1 and reload. The counts move too, because
// a count that ignored the other filters would be telling you about a list
// you are not looking at.
function onRailFilterChange() {
    currentPage = 1;
    loadTransactions();
}

// On a narrow window the rail sits above the table and starts shut, so the
// Filters button still has a job there. On a wide one the rail is always
// visible and the button is hidden by CSS.
function toggleRail() {
    document.getElementById("tx-rail").classList.toggle("open");
}

function toggleRailMore(which) {
    railMore[which] = !railMore[which];
    if (which === "cat") renderRailCategories(); else renderRailPeriods();
}

function setPeriodGrain(grain) {
    periodGrain = grain;
    selectedMonths.clear();
    document.querySelectorAll(".rail-grain-btn").forEach(b =>
        b.classList.toggle("active", b.dataset.grain === grain));
    onRailFilterChange();
}

function toggleMonthFilter(key) {
    if (selectedMonths.has(key)) selectedMonths.delete(key);
    else selectedMonths.add(key);
    onRailFilterChange();
}

function setTypeFilter(type) {
    selectedType = selectedType === type ? "" : type;
    onRailFilterChange();
}

function toggleCatFilter(id) {
    if (selectedCatIds.has(id)) selectedCatIds.delete(id);
    else selectedCatIds.add(id);
    onRailFilterChange();
}

function clearPeriodFilter() {
    selectedMonths.clear();
    document.getElementById("search-date-from").value = "";
    document.getElementById("search-date-to").value = "";
    readDateFilter("search-date-from");
    readDateFilter("search-date-to");
    onRailFilterChange();
}

function clearCatFilter() {
    selectedCatIds.clear();
    onRailFilterChange();
}

function clearAmountFilter() {
    document.getElementById("search-amt-min").value = "";
    document.getElementById("search-amt-max").value = "";
    onRailFilterChange();
}

function resetSearch() {
    document.getElementById("search-q").value = "";
    document.getElementById("search-clear").style.display = "none";
    document.getElementById("search-date-from").value = "";
    document.getElementById("search-date-to").value = "";
    document.getElementById("search-amt-min").value = "";
    document.getElementById("search-amt-max").value = "";
    document.getElementById("rail-cat-search").value = "";
    readDateFilter("search-date-from");
    readDateFilter("search-date-to");
    selectedCatIds.clear();
    selectedMonths.clear();
    selectedType = "";
    onRailFilterChange();
}

// ── The rail's own rendering ────────────────────────────────────────
// A month key is "2026-07" and a year key is "2026"; the same set holds both
// because the grain decides which the rail is offering.
function monthsToPeriods() {
    if (periodGrain === "month") {
        const rows = withSelected(facets.months, selectedMonths, m => m.month,
                                  key => ({ month: key, n: 0 }));
        return rows.sort((a, b) => b.month.localeCompare(a.month))
            .map(m => ({ key: m.month, label: fmtMonthLabel(m.month), n: m.n }));
    }
    const byYear = new Map();
    facets.months.forEach(m => {
        const y = m.month.slice(0, 4);
        byYear.set(y, (byYear.get(y) || 0) + m.n);
    });
    selectedMonths.forEach(y => { if (!byYear.has(y)) byYear.set(y, 0); });
    return [...byYear.entries()].sort((a, b) => b[0].localeCompare(a[0]))
        .map(([y, n]) => ({ key: y, label: y, n }));
}

function fmtMonthLabel(key) {
    const [y, m] = key.split("-");
    const names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return `${names[parseInt(m, 10) - 1]} ${y}`;
}

// One row of the rail. `sel` drives both the tick and the weight, so a chosen
// value reads as chosen without relying on the tick alone.
function railItem({ key, label, n, sel, max, onclick, box = true }) {
    const share = max > 0 ? Math.max(3, Math.round((n / max) * 34)) : 0;
    // A value that would return nothing under the filters already on is a
    // dead click. It stays on the list — vanishing options are worse — but
    // says so rather than looking like the others.
    const dead = n === 0 && !sel ? " empty" : "";
    return `<button class="rail-item ${sel ? "sel" : ""}${dead}" onclick="${onclick}" title="${escapeHtml(label)} — ${n} transaction${n !== 1 ? "s" : ""}">
        ${box ? `<span class="rail-box"></span>` : ""}
        <span class="rail-name">${escapeHtml(label)}</span>
        <span class="rail-bar" style="width:${share}px"></span>
        <span class="rail-n">${n.toLocaleString()}</span>
    </button>`;
}

function renderRailPeriods() {
    const el = document.getElementById("rail-periods");
    if (!el) return;
    const all  = monthsToPeriods();
    const max  = Math.max(1, ...all.map(p => p.n));
    const show = railMore.period
        ? all
        : collapseKeeping(all, p => selectedMonths.has(p.key), RAIL_COLLAPSED);
    el.innerHTML = show.map(p => railItem({
        ...p, sel: selectedMonths.has(p.key), max,
        onclick: `toggleMonthFilter('${p.key}')`,
    })).join("") || `<div class="rail-empty">Nothing in range</div>`;
    renderRailMoreBtn("rail-period-more", all.length - show.length, "period");
}

// The collapsed rail shows the first few values — but never at the cost of
// hiding one you have selected. Original order is kept rather than floating
// the selected ones to the top, so the list does not reshuffle under the
// cursor every time you tick something.
function collapseKeeping(all, isSel, limit) {
    const sel  = all.filter(isSel);
    const rest = all.filter(x => !isSel(x)).slice(0, Math.max(0, limit - sel.length));
    const keep = new Set([...sel, ...rest]);
    return all.filter(x => keep.has(x));
}

// A value you have selected has to stay on its list even when the other
// filters have taken it to zero. The facet query groups over rows that exist,
// so a category with nothing left simply is not in the result — and the rail
// would then show no sign of a filter it is applying. Put it back at zero.
function withSelected(rows, selected, keyOf, make) {
    const present = new Set(rows.map(keyOf));
    const missing = [...selected].filter(k => !present.has(k)).map(make).filter(Boolean);
    return [...rows, ...missing];
}

function renderRailCategories() {
    const el = document.getElementById("rail-categories");
    if (!el) return;
    const term = (document.getElementById("rail-cat-search")?.value || "").toLowerCase().trim();
    const rows = withSelected(facets.categories, selectedCatIds, c => c.id, id => {
        const c = categories.find(x => x.id === id);
        return c ? { id: c.id, name: c.name, type: c.type, n: 0 } : null;
    });
    // A category you have picked also stays visible through a search that no
    // longer matches it — otherwise the filter vanishes mid-typing.
    const all = rows.filter(c =>
        selectedCatIds.has(c.id) || !term || c.name.toLowerCase().includes(term));
    const max  = Math.max(1, ...all.map(c => c.n));
    const show = (railMore.cat || term)
        ? all
        : collapseKeeping(all, c => selectedCatIds.has(c.id), RAIL_COLLAPSED);
    el.innerHTML = show.map(c => railItem({
        key: c.id, label: c.name, n: c.n, sel: selectedCatIds.has(c.id), max,
        onclick: `toggleCatFilter(${c.id})`,
    })).join("") || `<div class="rail-empty">No category matches</div>`;
    renderRailMoreBtn("rail-cat-more", term ? 0 : all.length - show.length, "cat");
}

function renderRailTypes() {
    const el = document.getElementById("rail-types");
    if (!el) return;
    const max = Math.max(1, ...facets.types.map(t => t.n));
    const label = { expense: "Expenses", income: "Income" };
    el.innerHTML = ["expense", "income"].map(t => {
        const row = facets.types.find(x => x.type === t) || { n: 0 };
        return railItem({
            key: t, label: label[t], n: row.n, sel: selectedType === t, max,
            onclick: `setTypeFilter('${t}')`,
        });
    }).join("");
}

function renderRailMoreBtn(id, hidden, which) {
    const btn = document.getElementById(id);
    if (!btn) return;
    if (hidden <= 0 && !railMore[which]) { btn.style.display = "none"; return; }
    btn.style.display = "block";
    btn.textContent = railMore[which] ? "Show fewer" : `Show ${hidden} more…`;
}

function renderRail() {
    renderRailPeriods();
    renderRailTypes();
    renderRailCategories();
}

// The tokens say what is narrowing the list in words, above the table. The
// rail already shows it, but on a narrow window the rail is shut — and a
// filter you cannot see is the fault this whole page is fixing.
function renderFilterTokens(active) {
    const el = document.getElementById("tx-tokens");
    if (!el) return;
    el.innerHTML = active.map(t =>
        `<span class="tx-token">${escapeHtml(t.label)}<button class="tx-token-x" onclick="${t.clear}" title="Remove this filter">×</button></span>`
    ).join("");
    el.style.display = active.length ? "flex" : "none";
    const clearBtn = document.getElementById("tx-clear-all");
    if (clearBtn) clearBtn.style.display = active.length ? "block" : "none";
    const count = document.getElementById("filter-count");
    if (count) count.textContent = active.length ? ` · ${active.length}` : "";
}

// ── Transactions ────────────────────────────────────────────────────

// A date the parser can't read is dropped from the query rather than refused,
// so the field has to say so: otherwise the box keeps showing what was typed
// while the list quietly goes back to every transaction. Half-typed dates hit
// this on the way to a valid one, which is why it marks rather than blocks.
function readDateFilter(id) {
    const el = document.getElementById(id);
    if (!el) return "";
    const raw = (el.value || "").trim();
    const iso = fiToIso(raw) || "";
    const bad = raw !== "" && !iso;
    el.classList.toggle("input-invalid", bad);
    el.title = bad ? "Not a date, so this filter is being ignored. Try 31.7.2026." : "";
    return iso;
}

// Every filter on the page, as query params. One builder, used for the list
// and for the facet counts, so the counts can never describe a different
// filter than the table under them.
function txFilterParams() {
    const p = new URLSearchParams();
    const q = document.getElementById("search-q")?.value.trim() || "";
    const dateFrom = readDateFilter("search-date-from");
    const dateTo   = readDateFilter("search-date-to");
    const amtMin   = document.getElementById("search-amt-min")?.value || "";
    const amtMax   = document.getElementById("search-amt-max")?.value || "";

    if (selectedType) p.set("type", selectedType);
    if (q) p.set("q", q);
    if (dateFrom) p.set("date_from", dateFrom);
    if (dateTo) p.set("date_to", dateTo);
    if (amtMin) p.set("amount_min", amtMin);
    if (amtMax) p.set("amount_max", amtMax);
    if (selectedCatIds.size) p.set("category_ids", [...selectedCatIds].join(","));
    if (selectedMonths.size) {
        // A year in the rail is every month it covers — the endpoint only
        // knows about months, and teaching it about years would put the same
        // calendar logic in two places.
        const months = periodGrain === "year"
            ? facets.months.map(m => m.month).filter(m => selectedMonths.has(m.slice(0, 4)))
            : [...selectedMonths];
        if (months.length) p.set("months", months.join(","));
    }
    return p;
}

// What is narrowing the list right now, in words, each with the call that
// takes it off again.
function activeFilterTokens() {
    const out = [];
    const dateFrom = document.getElementById("search-date-from")?.value.trim();
    const dateTo   = document.getElementById("search-date-to")?.value.trim();
    const amtMin   = document.getElementById("search-amt-min")?.value;
    const amtMax   = document.getElementById("search-amt-max")?.value;

    if (selectedType) {
        out.push({ label: selectedType === "income" ? "Income" : "Expenses",
                   clear: `setTypeFilter('${selectedType}')` });
    }
    if (selectedMonths.size) {
        const list = [...selectedMonths].sort();
        const label = list.length === 1
            ? (periodGrain === "year" ? list[0] : fmtMonthLabel(list[0]))
            : `${list.length} periods`;
        out.push({ label, clear: "clearPeriodFilter()" });
    }
    if (dateFrom || dateTo) {
        out.push({ label: `${dateFrom || "…"} – ${dateTo || "…"}`, clear: "clearPeriodFilter()" });
    }
    selectedCatIds.forEach(id => {
        const c = categories.find(x => x.id === id)
               || facets.categories.find(x => x.id === id);
        if (c) out.push({ label: c.name, clear: `toggleCatFilter(${id})` });
    });
    if (amtMin || amtMax) {
        out.push({ label: `${amtMin ? fmt(amtMin) : "0 €"} – ${amtMax ? fmt(amtMax) : "any"}`,
                   clear: "clearAmountFilter()" });
    }
    return out;
}

// The counts move with the filters, so they are refetched alongside the list.
// A failure here must not take the table down with it: the rail keeps its last
// counts and the list still loads.
async function loadFacets() {
    try {
        facets = await api(`/api/transactions/facets?${txFilterParams()}`);
    } catch (e) {
        return;
    }
    renderRail();
}

async function loadTransactions() {
    const params = txFilterParams();
    params.set("page", currentPage);
    params.set("per_page", 50);
    params.set("sort", txSort.col);
    params.set("dir", txSort.dir);

    renderFilterTokens(activeFilterTokens());

    const data = await api(`/api/transactions?${params}`);
    const tbody = document.getElementById("transactions-body");
    loadFacets();

    const countEl = document.getElementById("search-count");
    if (countEl) {
        // Answer "how much?" for the current filter, not just "how many" —
        // and at a size that matches how much that is worth knowing.
        countEl.innerHTML = data.total
            ? `<span class="tx-result-n">${data.total.toLocaleString()} transaction${data.total !== 1 ? "s" : ""}</span>
               <span class="tx-result-money"><span class="out">−${fmt(data.sum_expense)}</span> out · <span class="in">+${fmt(data.sum_income)}</span> in</span>`
            : `<span class="tx-result-n">Nothing matches</span>`;
    }

    if (data.items.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;padding:32px;color:var(--text-tertiary);font-size:14px">No transactions found</td></tr>`;
    } else {
        tbody.innerHTML = data.items.map(t => `<tr class="tx-row" onclick="openEditTransaction(${t.id})">
            <td data-label="Date">${fmtDate(t.date)}</td>
            <td data-label="Store" class="tx-store-cell">${t.store ? escapeHtml(t.store) : "—"}</td>
            <td data-label="Category"><span class="category-tag"><span class="cat-dot" style="background:${catDotColor(t.category_id)}"></span>${escapeHtml(t.category_name)}</span></td>
            <td data-label="Amount" class="amount ${t.type}">${t.type === "income" ? "+" : "−"}${fmt2(t.amount)}</td>
            <td class="tx-actions-cell" onclick="event.stopPropagation()">
                <div class="btn-group">
                    <button class="btn-icon" onclick="openEditTransaction(${t.id})" title="Edit">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                    </button>
                    <button class="btn-icon" onclick="deleteTransaction(${t.id})" title="Delete">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
                    </button>
                </div>
            </td>
        </tr>`).join("");
    }

    const totalPages = Math.ceil(data.total / 50);
    const pag = document.getElementById("transactions-pagination");
    if (totalPages > 1) {
        pag.innerHTML = `
            <button class="btn btn-secondary btn-sm" ${currentPage <= 1 ? "disabled" : ""} onclick="currentPage--;loadTransactions()">Previous</button>
            <span class="page-info">Page ${currentPage} of ${totalPages}</span>
            <button class="btn btn-secondary btn-sm" ${currentPage >= totalPages ? "disabled" : ""} onclick="currentPage++;loadTransactions()">Next</button>`;
    } else {
        pag.innerHTML = "";
    }
    updateTxSortIcons();
}

function openTransactionModal(t = null) {
    const isEdit = t !== null;
    const expenseCats = categories.filter(c => c.type === "expense");
    const incomeCats = categories.filter(c => c.type === "income");

    const catOptions = (type) => {
        const list = type === "income" ? incomeCats : expenseCats;
        return list.map(c => `<option value="${c.id}" ${t && t.category_id === c.id ? "selected" : ""}>${escapeHtml(c.name)}</option>`).join("");
    };

    const html = `<div class="modal-overlay" onclick="if(event.target===this)this.remove()">
        <div class="modal">
            <div class="modal-title">${isEdit ? "Edit" : "Add"} Transaction</div>
            <div class="form-row">
                <div class="form-group">
                    <label class="form-label">Date</label>
                    <input class="form-input" type="text" inputmode="numeric" id="modal-t-date" placeholder="31.7.2026" title="Day.Month.Year" value="${isoToFi(t ? t.date : new Date().toISOString().slice(0, 10))}">
                </div>
                <div class="form-group">
                    <label class="form-label">Type</label>
                    <select class="form-select" id="modal-t-type" onchange="updateCategoryOptions()">
                        <option value="expense" ${!t || t.type === "expense" ? "selected" : ""}>Expense</option>
                        <option value="income" ${t && t.type === "income" ? "selected" : ""}>Income</option>
                    </select>
                </div>
            </div>
            <div class="form-group">
                <label class="form-label">Store / Description</label>
                <input class="form-input" id="modal-t-store" value="${t ? t.store : ""}" placeholder="e.g. K-Market">
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label class="form-label">Category</label>
                    <select class="form-select" id="modal-t-category">
                        ${catOptions(t ? t.type : "expense")}
                    </select>
                </div>
                <div class="form-group">
                    <label class="form-label">Amount (€)</label>
                    <input class="form-input" type="number" step="0.01" min="0" id="modal-t-amount" value="${t ? t.amount : ""}" placeholder="0.00">
                </div>
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
                <button class="btn btn-primary" onclick="saveTransaction(${t ? t.id : "null"})">${isEdit ? "Save" : "Add"}</button>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML("beforeend", html);
}

function updateCategoryOptions() {
    const type = document.getElementById("modal-t-type").value;
    const select = document.getElementById("modal-t-category");
    const list = categories.filter(c => c.type === type);
    select.innerHTML = list.map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join("");
}

async function openEditTransaction(id) {
    const data = await api(`/api/transactions?page=1&per_page=1000`);
    const t = data.items.find(x => x.id === id);
    if (t) openTransactionModal(t);
}

async function saveTransaction(id) {
    const data = {
        date: fiToIso(document.getElementById("modal-t-date").value),
        store: document.getElementById("modal-t-store").value,
        category_id: parseInt(document.getElementById("modal-t-category").value),
        amount: parseFloat(document.getElementById("modal-t-amount").value),
        type: document.getElementById("modal-t-type").value,
    };

    if (!data.date || !data.amount) {
        toast("Please fill in date and amount");
        return;
    }

    if (id) {
        await api(`/api/transactions/${id}`, { method: "PUT", body: data });
    } else {
        await api("/api/transactions", { method: "POST", body: data });
    }
    document.querySelector(".modal-overlay").remove();
    await loadTransactions();
    toast(id ? "Transaction updated" : "Transaction added");
}

async function deleteTransaction(id) {
    if (!await confirmDialog({
        title: "Delete this transaction?",
        confirmLabel: "Delete",
        danger: true,
    })) return;
    await api(`/api/transactions/${id}`, { method: "DELETE" });
    await loadTransactions();
    toast("Transaction deleted");
}

function sortTxCol(col) {
    if (txSort.col === col) {
        txSort.dir = txSort.dir === "asc" ? "desc" : "asc";
    } else {
        txSort = { col, dir: "desc" };
    }
    currentPage = 1;
    loadTransactions();
}

function updateTxSortIcons() {
    const { col, dir } = txSort;
    document.querySelectorAll(".tx-th[data-col]").forEach(th => {
        const c    = th.dataset.col;
        const icon = th.querySelector(".sort-icon");
        if (!icon) return;
        if (c === col) {
            icon.innerHTML = dir === "asc"
                ? `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 19V5M5 12l7-7 7 7"/></svg>`
                : `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 5v14M5 12l7 7 7-7"/></svg>`;
            th.classList.add("sort-active");
        } else {
            icon.innerHTML = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="opacity:0.25"><path d="M12 5v14M5 12l7 7 7-7"/></svg>`;
            th.classList.remove("sort-active");
        }
    });
}

