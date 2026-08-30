// Balance — categories.js
//
// The Categories tab.
//
// One of the ordered classic scripts index.html loads. They share one global
// scope on purpose: index.html wires ~95 inline handlers straight to these
// names, so these are plain scripts and not modules.

// ── Categories ──────────────────────────────────────────────────────
async function loadCategories() {
    categories = await api("/api/categories");
    const [rules, stats] = await Promise.all([
        api("/api/merchant-rules"),
        api("/api/merchant-rules/stats"),
    ]);
    const statsById = Object.fromEntries((stats || []).map(s => [s.rule_id, s]));
    merchantRules = rules.map(r => ({
        ...r,
        hit_count: statsById[r.id]?.hit_count ?? 0,
        last_match: statsById[r.id]?.last_match ?? null,
    }));
    renderCategoryLists();
    renderMerchantRules();
    // The rail's category list comes from the facet counts, not from this
    // array, so it is refreshed by loadFacets() rather than from here.
    renderRailCategories();
}

function renderCategoryLists() {
    const expenseList = document.getElementById("expense-categories-list");
    const incomeList = document.getElementById("income-categories-list");

    expenseList.innerHTML = categories
        .filter(c => c.type === "expense")
        .map(c => categoryRow(c)).join("");

    incomeList.innerHTML = categories
        .filter(c => c.type === "income")
        .map(c => categoryRow(c)).join("");
}

function categoryRow(c) {
    // The Categories page is where a category's identity color lives: the
    // swatch opens a picker (stored on the category, used app-wide). Usage
    // info makes deleting an informed choice (design #20, #21).
    const used = c.tx_count
        ? `${c.tx_count.toLocaleString()} transaction${c.tx_count === 1 ? "" : "s"}${c.last_used ? " · last " + fmtDate(c.last_used) : ""}`
        : "Not used yet";
    return `<div class="category-row" style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--bg-secondary)">
        <button class="cat-swatch" style="background:${catDotColor(c.id)}" title="Change color" onclick="openCatColorPicker(${c.id}, this)"></button>
        <div style="flex:1;min-width:0">
            <div style="font-size:14px">${escapeHtml(c.name)}</div>
            <div style="font-size:11px;color:var(--text-tertiary)">${used}</div>
        </div>
        <div class="btn-group">
            <button class="btn-icon" onclick="editCategory(${c.id},'${c.name.replace(/'/g, "\\'")}','${c.type}')" title="Edit">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
            </button>
            <button class="btn-icon" onclick="deleteCategory(${c.id},'${c.name.replace(/'/g, "\\'")}')" title="Delete">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
            </button>
        </div>
    </div>`;
}

function openCatColorPicker(catId, anchor) {
    document.getElementById("cat-color-pop")?.remove();
    const cat = catById(catId);
    if (!cat) return;
    const swatches = CHART_COLORS.map(col =>
        `<button class="cat-swatch-opt ${cat.color === col ? "on" : ""}" style="background:${col}"
                 onclick="setCategoryColor(${catId}, '${col}')" title="${col}"></button>`
    ).join("");
    const pop = document.createElement("div");
    pop.id = "cat-color-pop";
    pop.className = "cat-color-pop";
    pop.innerHTML = swatches +
        `<button class="import-link-btn" style="font-size:11px" onclick="setCategoryColor(${catId}, null)">Auto</button>`;
    document.body.appendChild(pop);
    const r = anchor.getBoundingClientRect();
    pop.style.top = `${r.bottom + 6 + window.scrollY}px`;
    pop.style.left = `${Math.max(8, r.left + window.scrollX - 8)}px`;
    setTimeout(() => document.addEventListener("click", function close(e) {
        if (!pop.contains(e.target)) { pop.remove(); document.removeEventListener("click", close); }
    }), 0);
}

async function setCategoryColor(catId, color) {
    await api(`/api/categories/${catId}`, { method: "PUT", body: { color } });
    document.getElementById("cat-color-pop")?.remove();
    await loadCategories();
    toast(color ? "Color updated" : "Color reset to automatic");
}

function openCategoryModal(id = null, name = "", type = "expense") {
    const isEdit = id !== null;
    const html = `<div class="modal-overlay" onclick="if(event.target===this)this.remove()">
        <div class="modal">
            <div class="modal-title">${isEdit ? "Edit" : "Add"} Category</div>
            <div class="form-group">
                <label class="form-label">Name</label>
                <input class="form-input" id="modal-cat-name" value="${name}">
            </div>
            <div class="form-group">
                <label class="form-label">Type</label>
                <select class="form-select" id="modal-cat-type" ${isEdit ? "disabled" : ""}>
                    <option value="expense" ${type === "expense" ? "selected" : ""}>Expense</option>
                    <option value="income" ${type === "income" ? "selected" : ""}>Income</option>
                </select>
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
                <button class="btn btn-primary" onclick="saveCategory(${id})">Save</button>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML("beforeend", html);
    document.getElementById("modal-cat-name").focus();
}

function editCategory(id, name, type) {
    openCategoryModal(id, name, type);
}

async function saveCategory(id) {
    const name = document.getElementById("modal-cat-name").value.trim();
    const type = document.getElementById("modal-cat-type").value;
    if (!name) return;

    if (id) {
        await api(`/api/categories/${id}`, { method: "PUT", body: { name } });
    } else {
        await api("/api/categories", { method: "POST", body: { name, type } });
    }
    closeTopOverlay();
    await loadCategories();
    toast(id ? "Category updated" : "Category added");
}

async function deleteCategory(id, name) {
    const others = categories.filter(c => c.id !== id && c.type === categories.find(x => x.id === id)?.type);
    if (others.length === 0) {
        toast("Cannot delete last category of this type");
        return;
    }

    const options = others.map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join("");
    const txCount = categories.find(c => c.id === id)?.tx_count || 0;
    const html = `<div class="modal-overlay" onclick="if(event.target===this)this.remove()">
        <div class="modal">
            <div class="modal-title">Delete "${name}"?</div>
            <p class="text-sm text-muted mb-4">${txCount ? `Reassign its ${txCount.toLocaleString()} transaction${txCount === 1 ? "" : "s"} to:` : "No transactions use this category. Pick a fallback anyway:"}</p>
            <select class="form-select" id="modal-reassign">${options}</select>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
                <button class="btn btn-danger" onclick="confirmDeleteCategory(${id})">Delete</button>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML("beforeend", html);
}

async function confirmDeleteCategory(id) {
    const reassignTo = document.getElementById("modal-reassign").value;
    await api(`/api/categories/${id}?reassign_to=${reassignTo}`, { method: "DELETE" });
    closeTopOverlay();
    await loadCategories();
    toast("Category deleted");
}

