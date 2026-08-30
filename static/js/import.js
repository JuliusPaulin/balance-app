// Balance — import.js
//
// CSV and bank import: upload, the review ledger, the category picker,
// bulk select, split, and the import history card.
//
// One of the ordered classic scripts index.html loads. They share one global
// scope on purpose: index.html wires ~95 inline handlers straight to these
// names, so these are plain scripts and not modules.

// ── CSV Import ──────────────────────────────────────────────────────
const dropZone = document.getElementById("drop-zone");
const csvInput = document.getElementById("csv-input");

dropZone.addEventListener("click", () => csvInput.click());
dropZone.addEventListener("dragover", e => { e.preventDefault(); dropZone.classList.add("drag-over"); });
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
dropZone.addEventListener("drop", e => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    if (e.dataTransfer.files.length) uploadCSV(e.dataTransfer.files[0]);
});
csvInput.addEventListener("change", () => {
    if (csvInput.files.length) uploadCSV(csvInput.files[0]);
});

async function uploadCSV(file) {
    stagingMeta.filename = file.name;
    const formData = new FormData();
    formData.append("file", file);

    const res = await fetch("/api/import/upload", { method: "POST", body: formData });
    const data = await res.json();

    if (!res.ok) {
        toast(data.error || "Import failed");
        return;
    }

    // Unrecognized layout → let the user map the columns (and optionally
    // remember the format for next time).
    if (data.needs_mapping) {
        openColumnMappingModal(file, data);
        return;
    }

    showStagingFromResponse(data);
}

// Feed an upload/upload-mapped/bank-fetch response into the review table.
// The single entry point for the review pipeline, shared by the CSV upload,
// the column-mapping path, and the bank-import fetch.
function enterReview(data) {
    stagingBatchId = data.batch_id;
    stagingItems = data.items;
    stagingHalved = false;
    syncHalveButton();
    const history = document.getElementById("import-history");
    if (history) history.style.display = "none";
    renderStaging();
    document.getElementById("import-upload").style.display = "none";
    const bankCard = document.getElementById("import-bank");
    if (bankCard) bankCard.style.display = "none";
    document.getElementById("import-review").style.display = "block";
    syncBulkBar();
    populateBulkCategorySelect();
    toast(`${data.count} items ready for review`);
}

// Back-compat alias for the existing CSV callers.
function showStagingFromResponse(data) {
    enterReview(data);
}

// ── Import from bank (Enable Banking) ───────────────────────────────
// Reflect the server's bank-connection state into the Import page card.
async function loadBankStatus() {
    let status;
    try {
        status = await api("/api/import/bank/status");
    } catch (e) {
        return;  // leave the card hidden on transient errors
    }
    const card = document.getElementById("import-bank");
    if (!card) return;

    // Hide the whole card when the server has no Enable Banking credentials.
    if (status.configured === false) {
        card.style.display = "none";
        return;
    }
    card.style.display = "block";

    const disc = document.getElementById("bank-disconnected");
    const exp = document.getElementById("bank-expired");
    const conn = document.getElementById("bank-connected");
    disc.style.display = "none";
    exp.style.display = "none";
    conn.style.display = "none";

    if (status.connected) {
        conn.style.display = "block";
        const sel = document.getElementById("bank-account-select");
        sel.innerHTML = (status.accounts || []).map(a => {
            const label = [a.name, a.iban].filter(Boolean).join(" · ") || a.uid;
            return `<option value="${escapeHtml(a.uid)}">${escapeHtml(label)}</option>`;
        }).join("");
        // Default range: today-90d .. today.
        const today = new Date();
        const past = new Date(today.getTime() - 90 * 24 * 60 * 60 * 1000);
        document.getElementById("bank-date-from").value = isoToFi(isoDate(past));
        document.getElementById("bank-date-to").value = isoToFi(isoDate(today));
        const meta = document.getElementById("bank-conn-meta");
        const vu = status.valid_until ? new Date(status.valid_until) : null;
        meta.textContent = (status.aspsp_name || "Bank")
            + (vu && !isNaN(vu) ? ` · consent valid until ${vu.toLocaleDateString()}` : "");
    } else if (status.expired) {
        exp.style.display = "block";
    } else {
        disc.style.display = "block";
    }
}

function isoDate(d) {
    return d.toISOString().slice(0, 10);
}

// Full-page navigation to the consent flow; the server 302s to the bank and
// the callback returns to /#import?bank=connected.
function connectBank() {
    window.location = "/api/import/bank/connect";
}

async function disconnectBank() {
    if (!await confirmDialog({
        title: "Disconnect your bank?",
        body: "You'll have to connect again before the next bank import. Transactions you already imported stay.",
        confirmLabel: "Disconnect",
        danger: true,
    })) return;
    await api("/api/import/bank/disconnect", { method: "POST" });
    toast("Bank disconnected");
    loadBankStatus();
}

async function fetchBankTransactions() {
    const account_uid = document.getElementById("bank-account-select").value;
    const date_from = fiToIso(document.getElementById("bank-date-from").value);
    const date_to = fiToIso(document.getElementById("bank-date-to").value);
    if (!account_uid) { toast("Pick an account"); return; }
    if (!date_from || !date_to) { toast("Pick a date range"); return; }

    const res = await fetch("/api/import/bank/fetch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_uid, date_from, date_to }),
    });
    const data = await res.json();
    if (!res.ok) {
        if (data.error === "session_expired" || data.error === "not_connected") {
            toast("Bank connection expired — reconnect");
            loadBankStatus();
        } else if (data.error === "bank_auth") {
            toast(BANK_RETURN_MESSAGES.auth_error);
        } else {
            toast(data.error || "Fetch failed");
        }
        return;
    }
    if (!data.count) {
        toast("No new transactions in that range");
        return;
    }
    stagingMeta.filename = "Bank import (Nordea)";
    enterReview(data);
}

// Column-mapping modal shown when a CSV's columns weren't auto-detected.
// Lets the user pick Date / Merchant / Amount over a preview of the file, choose
// the amount-sign convention, and remember the format. Re-posts the same file to
// /api/import/upload-mapped (CSRF header auto-added by the global fetch wrapper).
function openColumnMappingModal(file, resp) {
    const headers = resp.headers || [];
    const guess = resp.guess || {};

    const colOptions = (selected, allowNone) => {
        let html = allowNone ? `<option value="">— none —</option>` : "";
        html += headers.map((h, i) =>
            `<option value="${i}" ${selected === i ? "selected" : ""}>${escapeHtml(h || ("Column " + (i + 1)))}</option>`
        ).join("");
        return html;
    };

    const previewHead = headers.map(h => `<th>${escapeHtml(h)}</th>`).join("");
    const previewBody = (resp.sample_rows || []).map(row =>
        `<tr>${headers.map((_, i) => `<td>${escapeHtml(String(row[i] == null ? "" : row[i]))}</td>`).join("")}</tr>`
    ).join("");

    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
        <div class="modal" style="max-width:620px">
            <div class="modal-title">Map your CSV columns</div>
            <p style="color:var(--text-secondary);font-size:13px;margin-bottom:14px">
                We didn't recognize this format. Tell us which columns to use — we'll
                remember it for next time.
            </p>
            <div style="overflow-x:auto;border:0.5px solid var(--separator);border-radius:var(--radius-sm);margin-bottom:16px">
                <table class="map-preview"><thead><tr>${previewHead}</tr></thead><tbody>${previewBody}</tbody></table>
            </div>
            <div class="map-fields">
                <label>Date column
                    <select id="map-date">${colOptions(guess.date, false)}</select>
                </label>
                <label>Merchant column <span style="color:var(--text-tertiary)">(optional)</span>
                    <select id="map-store">${colOptions(guess.store, true)}</select>
                </label>
                <label>Amount column
                    <select id="map-amount">${colOptions(guess.amount, false)}</select>
                </label>
                <label>Amount sign
                    <select id="map-sign">
                        <option value="neg_expense">Negative = expense (default)</option>
                        <option value="pos_expense">Positive = expense</option>
                    </select>
                </label>
            </div>
            <label style="display:flex;align-items:center;gap:8px;margin-top:14px;font-size:13px;cursor:pointer">
                <input type="checkbox" id="map-remember" checked> Remember this format
            </label>
            <div class="modal-actions">
                <button class="btn btn-secondary" id="map-cancel">Cancel</button>
                <button class="btn" id="map-import">Import</button>
            </div>
        </div>`;
    document.body.appendChild(overlay);

    const close = () => { overlay.remove(); if (csvInput) csvInput.value = ""; };
    overlay.addEventListener("click", e => { if (e.target === overlay) close(); });
    overlay.querySelector("#map-cancel").addEventListener("click", close);

    overlay.querySelector("#map-import").addEventListener("click", async () => {
        const dateCol = overlay.querySelector("#map-date").value;
        const amountCol = overlay.querySelector("#map-amount").value;
        if (dateCol === "" || amountCol === "") {
            toast("Pick the Date and Amount columns");
            return;
        }
        const fd = new FormData();
        fd.append("file", file);
        fd.append("date_col", dateCol);
        fd.append("amount_col", amountCol);
        fd.append("store_col", overlay.querySelector("#map-store").value);
        fd.append("amount_sign", overlay.querySelector("#map-sign").value);
        fd.append("remember", overlay.querySelector("#map-remember").checked ? "1" : "0");
        const r = await fetch("/api/import/upload-mapped", { method: "POST", body: fd });
        const d = await r.json();
        if (!r.ok) { toast(d.error || "Import failed"); return; }
        overlay.remove();
        stagingMeta.filename = file.name;
        showStagingFromResponse(d);
    });
}

// ── Import: day-first date helpers ──────────────────────────────────
// The review table shows dates as D.M.YYYY (Finnish, day-first) because the
// native <input type="date"> renders month-first under the en_FI WebKit
// locale, which reads as swapped day/month. Storage stays ISO YYYY-MM-DD.
function isoToFi(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
    if (!m) return iso || "";
    return `${+m[3]}.${+m[2]}.${m[1]}`;
}

function fiToIso(s) {
    s = (s || "").trim();
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;             // already ISO
    const m = /^(\d{1,2})\.(\d{1,2})\.(\d{4})$/.exec(s);      // D.M.YYYY
    if (m) {
        const d = +m[1], mo = +m[2];
        if (d >= 1 && d <= 31 && mo >= 1 && mo <= 12) {
            return `${m[3]}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
        }
    }
    return null;                                              // unparseable
}

// The row's amount box, read the way fiToIso reads its date box. A comma is a
// decimal separator here: the app prints amounts as "16,05" and the CSVs it
// imports are written "-25,00", so a comma is what a Finnish hand types. The
// old type="number" input threw one away before any of our code saw it.
// Returns null when the value cannot stand as an amount.
function parseAmountInput(raw) {
    const s = String(raw ?? "").trim().replace(",", ".");
    if (s === "") return null;
    const n = Number(s);
    return Number.isFinite(n) && n > 0 ? Math.round(n * 100) / 100 : null;
}

function syncStagingFromDom() {
    stagingItems.forEach(item => {
        const typeSel   = document.querySelector(`[data-staging-type="${item.id}"]`);
        const storeInp  = document.querySelector(`[data-staging-store="${item.id}"]`);
        const dateInp   = document.querySelector(`[data-staging-date="${item.id}"]`);
        const amountInp = document.querySelector(`[data-staging-amount="${item.id}"]`);
        if (typeSel)   item._selectedType  = typeSel.value;
        if (storeInp)  item._editedStore   = storeInp.value;
        if (dateInp)   item._editedDate    = fiToIso(dateInp.value) || item._editedDate || item.date;
        if (amountInp) item._editedAmount  = parseAmountInput(amountInp.value)
                                             ?? item._editedAmount ?? item.amount;
    });
}

// ── Import review: ledger renderer ──────────────────────────────────
// Effective (edited-else-original) accessors for a staging item. One category
// control per row: picking a category from the income optgroup makes the row
// income, and vice versa — there is no separate Type select.
function catById(id) { return categories.find(c => c.id === id) || null; }

function effDate(item)   { return item._editedDate   ?? item.date; }
function effStore(item)  { return item._editedStore  ?? item.store ?? ""; }
function effAmount(item) { return item._editedAmount ?? item.amount; }

function effCatId(item) {
    if (item._selectedCatId) return item._selectedCatId;
    if (item._isSplit && item._splitCategoryId) return item._splitCategoryId;
    return categories.find(c => c.name === item.suggested_category)?.id ?? null;
}

function effType(item) {
    const cat = catById(effCatId(item));
    return cat ? cat.type : (item._selectedType || item.type || "expense");
}

// One category = one color, everywhere. A color stored on the category wins;
// otherwise fall back to a stable id-keyed pick from the active palette.
function catDotColor(catId) {
    if (!catId) return "transparent";
    const stored = catById(catId)?.color;
    return stored || CHART_COLORS[catId % CHART_COLORS.length];
}

// Colors for several categories drawn together (multi-series charts). There are
// more categories than palette entries, so two ids can land on the same slot
// (any id-modulo scheme collides for ids differing by the palette length).
// Identity colors are kept where possible; a series that would repeat a color
// already used in this chart takes the nearest unused palette entry instead.
function distinctCatColors(catIds) {
    const used = new Set();
    return catIds.map(id => {
        let color = catDotColor(id);
        if (used.has(color)) {
            const free = CHART_COLORS.find(c => !used.has(c));
            if (free) color = free;
        }
        used.add(color);
        return color;
    });
}

// Category <select> with Expense/Income optgroups; empty selection allowed.
// ── Category picker ─────────────────────────────────────────────────
// Replaces the per-row <select>. Shows every category at once in a colour-dotted
// grid and filters as you type: with 34 categories, scrolling a native dropdown
// was the slowest part of reviewing an import.
let _catPop = null;

function closeCatPicker() {
    if (!_catPop) return;
    document.removeEventListener("mousedown", _catPop.onDocDown, true);
    window.removeEventListener("scroll", _catPop.onScroll, true);
    _catPop.el.remove();
    _catPop = null;
}

function positionCatPop(el, anchor) {
    const r = anchor.getBoundingClientRect();
    const w = el.offsetWidth, h = el.offsetHeight;
    el.style.left = Math.max(8, Math.min(r.left + window.scrollX,
                                         window.scrollX + window.innerWidth - w - 12)) + "px";
    const fitsBelow = r.bottom + h + 12 < window.innerHeight;
    el.style.top = (fitsBelow ? r.bottom + window.scrollY + 6
                              : Math.max(window.scrollY + 8, r.top + window.scrollY - h - 6)) + "px";
}

function openCatPicker(anchor, currentId, onPick) {
    const sameChip = _catPop && _catPop.anchor === anchor;
    closeCatPicker();
    if (sameChip) return;   // clicking the open chip again closes it

    const el = document.createElement("div");
    el.className = "cat-pop";
    el.innerHTML = `<input class="cat-pop-search" placeholder="Search categories…" autocomplete="off">
        <div class="cat-pop-body"></div>
        <div class="cat-pop-foot"><span><kbd>↑</kbd><kbd>↓</kbd> move</span>
            <span><kbd>⏎</kbd> pick</span><span><kbd>esc</kbd> close</span></div>`;
    document.body.appendChild(el);

    const input = el.querySelector(".cat-pop-search");
    const body  = el.querySelector(".cat-pop-body");
    let hits = [], cursor = 0;

    function pick(id) {
        const cat = catById(id);
        closeCatPicker();
        if (cat) onPick(cat);
    }

    function draw() {
        const q = input.value.trim().toLowerCase();
        hits = categories.filter(c => c.name.toLowerCase().includes(q));
        if (cursor >= hits.length) cursor = Math.max(0, hits.length - 1);
        if (!hits.length) {
            body.innerHTML = `<div class="cat-pop-empty">Nothing matches “${escapeHtml(input.value)}”</div>`;
            return;
        }
        const section = (type, label) => {
            const cells = hits.map((c, i) => c.type !== type ? "" :
                `<button type="button" class="cat-pop-item ${i === cursor ? "on" : ""}" data-i="${i}">
                    <span class="dot" style="background:${catDotColor(c.id)}"></span>
                    <span class="n">${escapeHtml(c.name)}</span>
                    ${c.id === currentId ? `<span class="tick">✓</span>` : ""}
                </button>`).join("");
            return cells ? `<div class="cat-pop-group">${label}</div>
                            <div class="cat-pop-grid">${cells}</div>` : "";
        };
        body.innerHTML = section("expense", "Expense") + section("income", "Income");
        body.querySelectorAll(".cat-pop-item").forEach(b => {
            b.onmousedown = ev => { ev.preventDefault(); pick(hits[parseInt(b.dataset.i)].id); };
        });
        // Scroll the panel's own list, not the page: scrollIntoView() walks up to
        // the window and would jump the import list out from under the cursor.
        const on = body.querySelector(".cat-pop-item.on");
        if (on) {
            const top = on.offsetTop, bottom = top + on.offsetHeight;
            if (top < body.scrollTop) body.scrollTop = top;
            else if (bottom > body.scrollTop + body.clientHeight) body.scrollTop = bottom - body.clientHeight;
        }
    }

    input.oninput = () => { cursor = 0; draw(); };
    input.onkeydown = e => {
        if (e.key === "ArrowDown")    { e.preventDefault(); cursor = Math.min(cursor + 1, hits.length - 1); draw(); }
        else if (e.key === "ArrowUp") { e.preventDefault(); cursor = Math.max(cursor - 1, 0); draw(); }
        else if (e.key === "Enter")   { e.preventDefault(); if (hits[cursor]) pick(hits[cursor].id); }
        else if (e.key === "Escape")  { e.preventDefault(); closeCatPicker(); anchor.focus({ preventScroll: true }); }
    };

    el.style.visibility = "hidden";
    draw();
    positionCatPop(el, anchor);
    el.style.visibility = "";
    input.focus({ preventScroll: true });

    const onDocDown = ev => {
        if (!el.contains(ev.target) && ev.target !== anchor && !anchor.contains(ev.target)) closeCatPicker();
    };
    // Follow the chip when the page scrolls rather than closing: a stray trackpad
    // nudge should not throw away a half-typed search.
    const onScroll = () => positionCatPop(el, anchor);
    document.addEventListener("mousedown", onDocDown, true);
    window.addEventListener("scroll", onScroll, true);
    _catPop = { el, anchor, onDocDown, onScroll };
}

function catChipInner(catId, placeholder) {
    const cat = catById(catId);
    return `<span class="dot" style="background:${catDotColor(catId)}"></span>
        <span class="cat-chip-label">${cat ? escapeHtml(cat.name) : placeholder}</span>
        <span class="cat-chip-caret">⌄</span>`;
}

function openStagingCatPicker(btn, itemId) {
    const item = stagingItems.find(i => String(i.id) === String(itemId));
    if (!item) return;
    openCatPicker(btn, effCatId(item), cat => {
        item._selectedCatId = cat.id;
        item._selectedType  = cat.type;
        renderStaging();
    });
}

function weekdayLabel(iso) {
    const d = new Date(iso + "T00:00:00");
    return isNaN(d) ? "" : d.toLocaleDateString("en-US", { weekday: "long" });
}

function onStagingDateChange(inp, itemId) {
    const item = stagingItems.find(i => String(i.id) === String(itemId));
    if (!item) return;
    const iso = fiToIso(inp.value);
    if (!iso) { toast("Use day.month.year, e.g. 31.7.2026"); inp.value = isoToFi(effDate(item)); return; }
    item._editedDate = iso;
    renderStaging();
}

function onStagingStoreChange(inp, itemId) {
    const item = stagingItems.find(i => String(i.id) === String(itemId));
    if (item) item._editedStore = inp.value;
}

function onStagingAmountChange(inp, itemId) {
    const item = stagingItems.find(i => String(i.id) === String(itemId));
    if (!item) return;
    const v = parseAmountInput(inp.value);
    // Snapping the box back with nothing said is how an edit disappears. The
    // date box beside this one has always explained itself; so does this now.
    if (v === null) {
        toast("Amount must be more than 0 — e.g. 12,50");
        inp.value = effAmount(item);
        return;
    }
    item._editedAmount = v;
    renderStaging();
}

function renderStagingRow(item) {
    const catId  = effCatId(item);
    const type   = effType(item);
    const isPart = item._isSplit;
    const needsCat = catId == null;

    const partBadge = isPart
        ? `<span class="split-part-badge">SPLIT</span>` : "";
    const splitBtn = isPart ? "" :
        `<button class="btn-icon" onclick="openSplitModal('${item.id}')" title="Split transaction">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 3h5v5M8 3H3v5M3 16v5h5m13-5v5h-5"/><path d="M21 3L3 21"/></svg>
        </button>`;

    return `<div class="staging-row ${isPart ? "split-child" : ""}">
        <input type="checkbox" class="staging-checkbox" data-id="${item.id}" onchange="syncBulkBar()">
        <input type="text" inputmode="numeric" class="cell-input cell-date" data-staging-date="${item.id}"
               value="${isoToFi(effDate(item))}" placeholder="31.7.2026" title="Day.Month.Year"
               onchange="onStagingDateChange(this, '${item.id}')">
        <span class="cell-store-wrap">
            <input type="text" class="cell-input" data-staging-store="${item.id}"
                   value="${effStore(item).replace(/"/g, "&quot;")}" placeholder="Store"
                   onchange="onStagingStoreChange(this, '${item.id}')">
            ${partBadge}
        </span>
        <span class="chip-cat ${needsCat ? "review" : ""}">
            <button type="button" class="cat-chip-btn" data-staging-cat="${item.id}"
                    onclick="openStagingCatPicker(this, '${item.id}')">
                ${catChipInner(catId, "Pick category")}
            </button>
        </span>
        <span class="cell-amount ${type}">
            <span class="sign">${type === "income" ? "+" : "−"}</span>
            <input type="text" inputmode="decimal" class="cell-input" data-staging-amount="${item.id}"
                   value="${effAmount(item)}" title="Amount — comma or dot, e.g. 12,50"
                   onchange="onStagingAmountChange(this, '${item.id}')">
            <span class="sign">€</span>
        </span>
        <span class="staging-actions">
            ${splitBtn}
            <button class="btn-icon" onclick="removeStagingItem('${item.id}')" title="Remove">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M18 6L6 18M6 6l12 12"/></svg>
            </button>
        </span>
    </div>`;
}

function renderStaging() {
    const wrap = document.getElementById("staging-groups");
    if (!wrap) return;

    // Preserve row selection across re-renders.
    const checkedIds = new Set([...document.querySelectorAll(".staging-checkbox:checked")].map(cb => cb.dataset.id));

    // Group by effective date, newest day first; keep item order inside a day.
    const byDay = new Map();
    for (const item of stagingItems) {
        const d = effDate(item);
        if (!byDay.has(d)) byDay.set(d, []);
        byDay.get(d).push(item);
    }
    const days = [...byDay.keys()].sort().reverse();

    wrap.innerHTML = days.map(day => {
        const items = byDay.get(day);
        const net = items.reduce((s, it) => s + (effType(it) === "income" ? 1 : -1) * effAmount(it), 0);
        const splits = items.filter(i => i._isSplit).length;
        const countLabel = `${items.length} transaction${items.length === 1 ? "" : "s"}${splits ? ` · ${splits} split` : ""}`;
        return `<div class="import-day">
            <div class="import-day-head">
                <b>${weekdayLabel(day)} ${isoToFi(day)}</b>
                <span>${countLabel}</span>
                <span class="day-total">${net >= 0 ? "+" : "−"}${fmt(Math.abs(net))}</span>
            </div>
            ${items.map(renderStagingRow).join("")}
        </div>`;
    }).join("");

    checkedIds.forEach(id => {
        const cb = document.querySelector(`.staging-checkbox[data-id="${id}"]`);
        if (cb) cb.checked = true;
    });
    updateImportSummary();
    syncBulkBar();
}

function updateImportSummary() {
    const n = stagingItems.length;
    let out = 0, inc = 0, needCat = 0;
    let minD = null, maxD = null;
    for (const item of stagingItems) {
        const a = effAmount(item);
        if (effType(item) === "income") inc += a; else out += a;
        if (effCatId(item) == null) needCat++;
        const d = effDate(item);
        if (!minD || d < minD) minD = d;
        if (!maxD || d > maxD) maxD = d;
    }
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    set("import-file-name", stagingMeta.filename || "Import");
    set("import-file-meta", n ? `${n} transaction${n === 1 ? "" : "s"} · ${isoToFi(minD)} – ${isoToFi(maxD)}` : "");
    set("import-sum-out", "−" + fmt(out));
    set("import-sum-in", "+" + fmt(inc));
    const rev = document.getElementById("import-sum-review");
    if (rev) {
        rev.textContent = needCat ? `${needCat} need review` : "All matched";
        rev.classList.toggle("warn", needCat > 0);
    }
    set("import-footer-note", needCat
        ? `${needCat} transaction${needCat === 1 ? "" : "s"} without a category will be saved as Other.`
        : "Confirming saves the transactions and retrains merchant rules.");
    const btn = document.getElementById("confirm-all-btn");
    if (btn) btn.textContent = `Confirm ${n} transaction${n === 1 ? "" : "s"}`;
}

// ── Import: Bulk select ─────────────────────────────────────────────
function syncBulkBar() {
    const checked = document.querySelectorAll(".staging-checkbox:checked").length;
    const total   = document.querySelectorAll(".staging-checkbox").length;
    const bar     = document.getElementById("bulk-bar");
    if (!bar) return;
    bar.classList.toggle("hidden", total === 0);
    document.getElementById("bulk-count").textContent = `${checked} of ${total} selected`;
    // keep header checkbox in sync
    const headerCb = document.getElementById("select-all-check-header");
    if (headerCb) headerCb.checked = total > 0 && checked === total;
    const topCb = document.getElementById("select-all-check");
    if (topCb) topCb.checked = total > 0 && checked === total;
}

function toggleSelectAll(checked) {
    document.querySelectorAll(".staging-checkbox").forEach(cb => cb.checked = checked);
    // keep both checkboxes in sync
    ["select-all-check", "select-all-check-header"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.checked = checked;
    });
    syncBulkBar();
}

// Category chosen in the toolbar, waiting to be applied to the ticked rows.
let bulkCatId = null;

function populateBulkCategorySelect() {
    bulkCatId = null;
    renderBulkCatBtn();
}

function renderBulkCatBtn() {
    const btn = document.getElementById("bulk-category-btn");
    if (btn) btn.innerHTML = catChipInner(bulkCatId, "Assign category…");
}

function openBulkCatPicker(btn) {
    openCatPicker(btn, bulkCatId, cat => { bulkCatId = cat.id; renderBulkCatBtn(); });
}

function applyBulkCategory() {
    const cat = catById(bulkCatId);
    if (!cat) { toast("Pick a category first"); return; }
    const checked = [...document.querySelectorAll(".staging-checkbox:checked")];
    if (!checked.length) { toast("Select rows first"); return; }
    checked.forEach(cb => {
        const item = stagingItems.find(i => String(i.id) === String(cb.dataset.id));
        if (item) { item._selectedCatId = cat.id; item._selectedType = cat.type; }
    });
    renderStaging();
    toast(`${cat.name} applied to ${checked.length} row${checked.length === 1 ? "" : "s"}`);
}

// ── Import: Split ───────────────────────────────────────────────────
let splitVirtualCounter = -1;
let _splitCatOpts = "";   // category <option> HTML for the open split modal

function openSplitModal(itemId) {
    syncStagingFromDom();   // pick up any inline edits so the split total matches the table
    const item = stagingItems.find(i => String(i.id) === String(itemId));
    if (!item) return;

    const expenseCats = categories.filter(c => c.type === "expense");
    const currentCatId = effCatId(item);
    const catOpts = expenseCats.map(c =>
        `<option value="${c.id}" ${c.id === currentCatId ? "selected" : ""}>${escapeHtml(c.name)}</option>`
    ).join("");
    _splitCatOpts = catOpts;
    const total = item._editedAmount ?? item.amount;
    const half = (total / 2).toFixed(2);

    const html = `<div class="modal-overlay" onclick="if(event.target===this)this.remove()">
        <div class="modal" style="max-width:520px">
            <div class="modal-title">Split Transaction — ${fmt(total)}</div>
            <p style="font-size:var(--text-subhead);color:var(--text-tertiary);margin-bottom:14px">${item.store || "—"} &nbsp;·&nbsp; ${fmtDate(item.date)}</p>
            <div id="split-rows-wrap">
                ${splitRowHtml(0, half, catOpts)}
                ${splitRowHtml(1, half, catOpts)}
            </div>
            <div style="margin:10px 0;display:flex;justify-content:space-between;align-items:center">
                <button class="btn btn-ghost btn-sm" onclick="addSplitRow()">+ Add part</button>
                <span id="split-remaining" style="font-size:var(--text-subhead);color:var(--text-tertiary)"></span>
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
                <button class="btn btn-primary" onclick="confirmSplit('${item.id}', ${total})">Split</button>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML("beforeend", html);
    updateSplitRemaining(total);
}

function splitRowHtml(idx, amount, catOpts) {
    return `<div class="split-row" data-split-idx="${idx}">
        <span style="font-size:var(--text-caption);color:var(--text-tertiary);width:20px;flex-shrink:0">${idx + 1}.</span>
        <input type="number" class="form-input split-amount-input" step="0.01" min="0.01" value="${amount}"
               style="width:110px;flex-shrink:0" oninput="onSplitAmountInput(this)">
        <select class="form-select split-cat-select" style="flex:1">${catOpts}</select>
        ${idx > 1 ? `<button class="btn-icon" onclick="this.closest('.split-row').remove();updateSplitRemaining()">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
        </button>` : ""}
    </div>`;
}

// All split-modal lookups are scoped to the top-most overlay so a stray or
// stacked modal can never leak its inputs into this one's sums.
function topModal() {
    return [...document.querySelectorAll(".modal-overlay")].pop() || document;
}

function addSplitRow() {
    const wrap = topModal().querySelector("#split-rows-wrap");
    const idx  = wrap.querySelectorAll(".split-row").length;
    wrap.insertAdjacentHTML("beforeend", splitRowHtml(idx, "0.00", _splitCatOpts));
    updateSplitRemaining();
}

function onSplitAmountInput(input) {
    // With exactly two parts, editing one auto-fills the other with the
    // remainder so changing the ratio is a single edit. With 3+ parts the
    // "Remaining" indicator guides the user instead.
    const modal = topModal();
    const inputs = [...modal.querySelectorAll(".split-amount-input")];
    const el = modal.querySelector("#split-remaining");
    const total = parseFloat(el?.dataset.total) || 0;
    if (inputs.length === 2 && total > 0) {
        const other = inputs.find(i => i !== input);
        const v = parseFloat(input.value);
        if (other && !isNaN(v)) {
            other.value = Math.max(0, Math.round((total - v) * 100) / 100).toFixed(2);
        }
    }
    updateSplitRemaining();
}

function updateSplitRemaining(total) {
    const modal = topModal();
    const inputs = modal.querySelectorAll(".split-amount-input");
    const sum = [...inputs].reduce((s, i) => s + (parseFloat(i.value) || 0), 0);
    const el  = modal.querySelector("#split-remaining");
    if (!el) return;
    if (total !== undefined) el.dataset.total = total;
    const t = parseFloat(el.dataset.total) || 0;
    const diff = Math.round((t - sum) * 100) / 100;
    el.textContent = diff === 0 ? "✓ Balanced" : `Remaining: ${fmt(diff)}`;
    el.style.color = diff === 0 ? "var(--green)" : "var(--red)";
}

function confirmSplit(itemId, totalAmount) {
    syncStagingFromDom();
    const modal = topModal();
    const inputs  = [...modal.querySelectorAll(".split-amount-input")];
    const catSels = [...modal.querySelectorAll(".split-cat-select")];

    const parts = inputs.map((inp, i) => ({
        amount: parseFloat(inp.value) || 0,
        category_id: parseInt(catSels[i].value),
    })).filter(p => p.amount > 0);

    const sum = parts.reduce((s, p) => s + p.amount, 0);
    if (Math.abs(sum - totalAmount) > 0.01) {
        toast(`Parts must sum to ${fmt(totalAmount)}`);
        return;
    }

    // Find the original item in stagingItems
    const origIdx = stagingItems.findIndex(i => String(i.id) === String(itemId));
    if (origIdx === -1) return;
    const orig = stagingItems[origIdx];

    // Replace original with virtual split items. syncStagingFromDom() above
    // stamped the table's full amount/category onto `orig` (_editedAmount,
    // _selectedCatId) — those must NOT leak into the parts, or every part
    // renders and confirms with the original's full amount and category.
    const splitItems = parts.map((p, i) => ({
        ...orig,
        id: `split_${itemId}_${i}`,
        _isSplit: true,
        _stagingId: orig.id,
        _splitCategoryId: p.category_id,
        _editedAmount: undefined,
        _selectedCatId: undefined,
        amount: p.amount,
        suggested_category: categories.find(c => c.id === p.category_id)?.name || "",
    }));

    stagingItems.splice(origIdx, 1, ...splitItems);
    if (modal !== document) modal.remove();
    renderStaging();
    syncBulkBar();
    toast(`Split into ${parts.length} transactions`);
}

async function confirmAllImports() {
    syncStagingFromDom();
    const items = stagingItems.map(item => {
        const type = effType(item);
        // Rows left without a category fall back to this type's "Other"
        // (the pre-redesign behaviour, now called out in the footer note).
        const catId = effCatId(item)
            || categories.find(c => c.name === "Other" && c.type === type)?.id
            || categories.find(c => c.type === type)?.id;
        const entry = {
            category_id: catId,
            type,
            store: effStore(item),
            date: effDate(item),
            amount: effAmount(item),
        };
        if (item._isSplit) entry.staging_id = item._stagingId;
        else entry.id = item.id;
        return entry;
    });

    const res = await api("/api/import/confirm", { method: "POST", body: { items, batch_id: stagingBatchId } });
    toast(res?.rules_retrained != null
        ? `All imports confirmed · ${res.rules_retrained} merchant rules retrained`
        : "All imports confirmed");
    cancelImport();
}

// "÷2 Split costs" is for a statement you share with someone: your half of the
// costs. It used to halve every row, salary included, which is not a cost and
// is not shared. It also compounded — a second click quartered the import with
// nothing on screen to say the first had landed — so it is a toggle now, and
// undoing restores the amounts exactly rather than doubling a rounded half.
let stagingHalved = false;

function halveAllAmounts() {
    syncStagingFromDom();
    const shared = stagingItems.filter(i => effType(i) === "expense");
    if (!shared.length) { toast("Nothing to split — no expenses in this import"); return; }

    if (stagingHalved) {
        shared.forEach(item => {
            if (item._preHalveAmount != null) item._editedAmount = item._preHalveAmount;
            delete item._preHalveAmount;
        });
        stagingHalved = false;
    } else {
        shared.forEach(item => {
            item._preHalveAmount = effAmount(item);
            item._editedAmount = Math.round((effAmount(item) / 2) * 100) / 100;
        });
        stagingHalved = true;
    }
    renderStaging();
    syncHalveButton();
    const kept = stagingItems.length - shared.length;
    toast(stagingHalved
        ? `Expenses halved${kept ? ` · ${kept} income row${kept === 1 ? "" : "s"} left alone` : ""}`
        : "Amounts restored");
}

function syncHalveButton() {
    const btn = document.getElementById("halve-btn");
    if (!btn) return;
    btn.classList.toggle("active-filter", stagingHalved);
    btn.textContent = stagingHalved ? "÷2 Halved — undo" : "÷2 Split costs";
}

// ── Import history ──────────────────────────────────────────────────
// import_batches was written by three code paths and read by none: an
// abandoned review disappeared with nowhere to resume from, and a finished one
// left no record of what it brought in. This is the reader.
async function loadImportHistory() {
    const card = document.getElementById("import-history");
    const list = document.getElementById("import-history-list");
    if (!card || !list) return;
    let batches;
    try {
        batches = await api("/api/import/batches");
    } catch (e) {
        card.style.display = "none";
        return;
    }
    if (!batches.length) { card.style.display = "none"; return; }
    card.style.display = "block";
    list.innerHTML = batches.map(renderImportHistoryRow).join("");
}

function renderImportHistoryRow(b) {
    const when = b.imported_at ? fmtDate(b.imported_at.slice(0, 10)) : "";
    let state, action = "", note;
    if (b.status === "pending") {
        state = `<span class="recurring-badge recurring-due">Unfinished</span>`;
        note  = `${b.staged} row${b.staged === 1 ? "" : "s"} waiting`;
        action = `<button class="import-link-btn" onclick="resumeImport(${b.id})">Resume</button>
                  <button class="import-link-btn danger" onclick="discardBatch(${b.id})">Discard</button>`;
    } else if (b.status === "undone") {
        state = `<span class="recurring-badge recurring-stopped">Undone</span>`;
        note  = "its transactions were removed";
    } else {
        state = `<span class="recurring-badge recurring-active">Imported</span>`;
        note  = b.imported
            ? `${b.imported} transaction${b.imported === 1 ? "" : "s"} · −${fmt(b.sum_expense)} / +${fmt(b.sum_income)}`
            : "too old to undo — not linked to its transactions";
        // Only offer to take back an import we can actually identify.
        if (b.imported) {
            action = `<button class="import-link-btn danger" onclick="undoImport(${b.id}, ${b.imported})">Undo</button>`;
        }
    }
    return `<div class="import-history-row">
        <span class="ih-file" title="${escapeHtml(b.filename)}">${escapeHtml(b.filename)}</span>
        <span class="ih-when">${when}</span>
        <span class="ih-state">${state}</span>
        <span class="ih-note">${note}</span>
        <span class="ih-actions">${action}</span>
    </div>`;
}

// Pick an unfinished review back up where it was left.
async function resumeImport(batchId) {
    const data = await api(`/api/import/staging/${batchId}`);
    if (!data.items || !data.items.length) {
        toast("Nothing left to review in that import");
        loadImportHistory();
        return;
    }
    stagingMeta.filename = "Unfinished import";
    enterReview(data);
}

async function discardBatch(batchId) {
    if (!await confirmDialog({
        title: "Discard this unfinished import?",
        body: "The rows still waiting for review are thrown away. Nothing was added to your transactions.",
        confirmLabel: "Discard",
        danger: true,
    })) return;
    await api(`/api/import/batch/${batchId}`, { method: "DELETE" });
    toast("Unfinished import discarded");
    loadImportHistory();
}

async function undoImport(batchId, count) {
    if (!await confirmDialog({
        title: `Remove ${count} transaction${count === 1 ? "" : "s"}?`,
        body: "This import added them. Removing them cannot be undone.",
        confirmLabel: "Remove",
        danger: true,
    })) return;
    const res = await api(`/api/import/batch/${batchId}/undo`, { method: "POST" });
    toast(`${res.removed} transaction${res.removed === 1 ? "" : "s"} removed`);
    loadImportHistory();
}

// Cancel means cancel. Without this the batch and its staged rows outlived the
// screen that made them: nothing reads them back, nothing cleans them up, and
// they pile up unseen. confirmAllImports() deliberately does NOT come through
// here — a confirmed batch is the record of what was imported.
async function discardImport() {
    const id = stagingBatchId;
    cancelImport();
    if (id == null) return;
    try {
        await api(`/api/import/batch/${id}`, { method: "DELETE" });
    } catch (e) {
        // The rows are already off the screen; a failed cleanup is not worth
        // dragging the user back into a review they asked to leave.
        console.warn("Could not discard import batch", id, e);
    }
}

function cancelImport() {
    stagingBatchId = null;
    stagingItems = [];
    stagingMeta = { filename: "" };
    document.getElementById("import-upload").style.display = "block";
    document.getElementById("import-review").style.display = "none";
    const bar = document.getElementById("bulk-bar");
    if (bar) bar.classList.add("hidden");
    csvInput.value = "";
    // Restore the bank card (enterReview hid it) to its state-driven view.
    loadBankStatus();
    // Leaving a review always changes the history: a batch was just confirmed,
    // discarded, or left unfinished.
    loadImportHistory();
}

async function removeStagingItem(id) {
    syncStagingFromDom();
    const item = stagingItems.find(i => String(i.id) === String(id));
    // only hit the API for real DB ids (not virtual split items)
    if (item && !item._isSplit) {
        await api(`/api/import/staging/${id}`, { method: "DELETE" });
    }
    stagingItems = stagingItems.filter(i => String(i.id) !== String(id));
    renderStaging();
    syncBulkBar();
}

