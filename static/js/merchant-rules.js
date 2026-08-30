// Balance — merchant-rules.js
//
// The store-name patterns that auto-assign a category.
//
// One of the ordered classic scripts index.html loads. They share one global
// scope on purpose: index.html wires ~95 inline handlers straight to these
// names, so these are plain scripts and not modules.

// ── Merchant Rules ──────────────────────────────────────────────────
function renderMerchantRules() {
    populateMerchantRuleCatFilter();
    filterMerchantRulesView();
}

function populateMerchantRuleCatFilter() {
    const sel = document.getElementById("merchant-rules-cat-filter");
    if (!sel) return;
    const catNames = [...new Set(merchantRules.map(r => r.category_name))].sort();
    const cur = sel.value;
    sel.innerHTML = `<option value="">All categories (${catNames.length})</option>` +
        catNames.map(n => `<option value="${n}" ${n === cur ? "selected" : ""}>${n}</option>`).join("");
}

function filterMerchantRulesView() {
    const container = document.getElementById("merchant-rules-list");
    const countEl = document.getElementById("merchant-rules-count");
    const search = (document.getElementById("merchant-rules-search")?.value || "").toLowerCase();
    const catFilter = document.getElementById("merchant-rules-cat-filter")?.value || "";
    const typeFilter = document.getElementById("merchant-rules-type-filter")?.value || "";
    const deadOnly = document.getElementById("merchant-rules-dead-toggle")?.classList.contains("active") || false;

    let filtered = merchantRules;
    if (search) filtered = filtered.filter(r => r.pattern.toLowerCase().includes(search));
    if (catFilter) filtered = filtered.filter(r => r.category_name === catFilter);
    if (typeFilter) filtered = filtered.filter(r => r.match_type === typeFilter);
    if (deadOnly) filtered = filtered.filter(r => (r.hit_count || 0) === 0);

    const deadCount = merchantRules.filter(r => (r.hit_count || 0) === 0).length;
    const banner = document.getElementById("merchant-rules-dead-banner");
    if (banner) {
        if (deadCount > 0) {
            banner.style.display = "flex";
            banner.querySelector(".dead-count").textContent = deadCount;
        } else {
            banner.style.display = "none";
        }
    }

    if (countEl) countEl.textContent = `${filtered.length} of ${merchantRules.length} rules`;

    if (!filtered.length) {
        container.innerHTML = `<div style="text-align:center;padding:24px;color:var(--text-tertiary);font-size:var(--text-subhead)">${merchantRules.length ? "No matching rules" : "No rules yet"}</div>`;
        return;
    }

    const grouped = {};
    filtered.forEach(r => {
        if (!grouped[r.category_name]) grouped[r.category_name] = [];
        grouped[r.category_name].push(r);
    });

    const sortedGroups = Object.keys(grouped).sort();
    // Groups collapse by default (666 rules is a wall); any active search or
    // filter expands them so results are visible (design #22).
    const filtering = !!(search || catFilter || typeFilter || deadOnly);
    container.innerHTML = sortedGroups.map(catName => {
        const rules = grouped[catName];
        const catId = categories.find(c => c.name === catName)?.id;
        return `<div class="merchant-rule-group ${filtering ? "" : "collapsed"}">
            <div class="merchant-rule-group-header" onclick="this.parentElement.classList.toggle('collapsed')" style="cursor:pointer">
                <span class="rule-group-caret">▸</span>
                <span class="category-tag"><span class="cat-dot" style="background:${catDotColor(catId)}"></span>${catName}</span>
                <span style="font-size:var(--text-caption);color:var(--text-tertiary);margin-left:6px">${rules.length} rule${rules.length !== 1 ? "s" : ""}</span>
            </div>
            ${rules.map(r => {
                const hits = r.hit_count || 0;
                const dead = hits === 0;
                const last = r.last_match ? ` · last ${r.last_match}` : "";
                return `<div class="merchant-rule-row ${dead ? "dead-rule" : ""}">
                <span class="merchant-rule-pattern" title="${r.pattern}${last}">${highlightMatch(r.pattern, search)}</span>
                <div class="merchant-rule-meta">
                    <span class="rule-hit-badge ${dead ? "dead" : ""}" title="${last ? `Last match: ${r.last_match}` : "No matches in history"}">${hits} hit${hits !== 1 ? "s" : ""}</span>
                    <span class="match-type-badge ${r.match_type}">${r.match_type}</span>
                    <div class="btn-group">
                        <button class="btn-icon" onclick="applyRuleToHistory(${r.id})" title="Re-apply to historical transactions">
                            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21 12a9 9 0 11-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>
                        </button>
                        <button class="btn-icon" onclick="editMerchantRule(${r.id})" title="Edit">
                            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                        </button>
                        <button class="btn-icon" onclick="deleteMerchantRule(${r.id})" title="Delete">
                            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
                        </button>
                    </div>
                </div>
            </div>`;
            }).join("")}
        </div>`;
    }).join("");
}

function toggleDeadRulesFilter() {
    const btn = document.getElementById("merchant-rules-dead-toggle");
    btn.classList.toggle("active");
    filterMerchantRulesView();
}

async function applyRuleToHistory(ruleId) {
    const rule = merchantRules.find(r => r.id === ruleId);
    if (!rule) return;
    if (!await confirmDialog({
        title: "Re-apply this rule?",
        body: `Every transaction in your history matching "${rule.pattern}" moves to ${rule.category_name}.`,
        confirmLabel: "Re-apply",
    })) return;
    const res = await api(`/api/merchant-rules/${ruleId}/apply`, { method: "POST" });
    toast(res.updated > 0 ? `${res.updated} transaction${res.updated !== 1 ? "s" : ""} re-categorized` : "No changes — already categorized");
    await loadCategories();
}

function highlightMatch(text, search) {
    // Returns HTML — escape every user-text slice so a store/pattern can't inject.
    if (!search) return escapeHtml(text);
    const idx = text.toLowerCase().indexOf(search);
    if (idx === -1) return escapeHtml(text);
    return escapeHtml(text.slice(0, idx)) + `<mark style="background:${rgbaVar("--accent", 0.3)};color:var(--text-primary);border-radius:2px;padding:0 1px">${escapeHtml(text.slice(idx, idx + search.length))}</mark>` + escapeHtml(text.slice(idx + search.length));
}

function openMerchantRuleModal(rule = null) {
    const isEdit = rule !== null;
    const expenseCats = categories.filter(c => c.type === "expense");
    const allCats = [...expenseCats, ...categories.filter(c => c.type === "income")];
    const catOptions = allCats.map(c =>
        `<option value="${c.id}" ${rule && rule.category_id === c.id ? "selected" : ""}>${escapeHtml(c.name)} (${c.type})</option>`
    ).join("");

    const html = `<div class="modal-overlay" onclick="if(event.target===this)this.remove()">
        <div class="modal" style="max-width:560px">
            <div class="modal-title">${isEdit ? "Edit" : "Add"} Merchant Rule</div>
            <div class="form-group">
                <label class="form-label">Merchant Pattern</label>
                <input class="form-input" id="modal-rule-pattern" value="${rule ? rule.pattern : ""}" placeholder="e.g. K-Market" oninput="debouncedRulePreview()">
            </div>
            <div class="form-group">
                <label class="form-label">Category</label>
                <select class="form-select" id="modal-rule-category">${catOptions}</select>
            </div>
            <div class="form-group">
                <label class="form-label">Match Type</label>
                <select class="form-select" id="modal-rule-matchtype" onchange="debouncedRulePreview()">
                    <option value="exact" ${!rule || rule.match_type === "exact" ? "selected" : ""}>Exact — store name must match exactly</option>
                    <option value="contains" ${rule && rule.match_type === "contains" ? "selected" : ""}>Contains — store name contains pattern</option>
                    <option value="smart" ${rule && rule.match_type === "smart" ? "selected" : ""}>Smart — fuzzy match (≥72% similarity)</option>
                </select>
            </div>
            <div class="form-group">
                <label class="form-label">Preview <span id="rule-preview-count" style="color:var(--text-tertiary);font-weight:400">— enter a pattern</span></label>
                <div id="rule-preview-list" style="max-height:200px;overflow-y:auto;border:0.5px solid var(--border);border-radius:var(--radius-sm);padding:6px 8px;background:var(--bg-secondary);font-size:12px;color:var(--text-secondary)">
                    <div style="text-align:center;padding:14px 0;color:var(--text-tertiary)">No pattern yet</div>
                </div>
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
                <button class="btn btn-primary" onclick="saveMerchantRule(${rule ? rule.id : "null"})">${isEdit ? "Save" : "Add Rule"}</button>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML("beforeend", html);
    document.getElementById("modal-rule-pattern").focus();
    if (rule) refreshRulePreview();
}

let rulePreviewTimer = null;
function debouncedRulePreview() {
    clearTimeout(rulePreviewTimer);
    rulePreviewTimer = setTimeout(refreshRulePreview, 280);
}

async function refreshRulePreview() {
    const pInput = document.getElementById("modal-rule-pattern");
    const tInput = document.getElementById("modal-rule-matchtype");
    const countEl = document.getElementById("rule-preview-count");
    const listEl = document.getElementById("rule-preview-list");
    if (!pInput || !listEl) return;

    const pattern = pInput.value.trim();
    const match_type = tInput.value;
    if (!pattern) {
        countEl.textContent = "— enter a pattern";
        listEl.innerHTML = `<div style="text-align:center;padding:14px 0;color:var(--text-tertiary)">No pattern yet</div>`;
        return;
    }

    countEl.textContent = "— checking…";
    const data = await api(`/api/merchant-rules/preview?pattern=${encodeURIComponent(pattern)}&match_type=${match_type}&limit=15`);
    countEl.textContent = `— ${data.match_count} match${data.match_count !== 1 ? "es" : ""} across ${data.distinct_stores} store${data.distinct_stores !== 1 ? "s" : ""}`;

    if (!data.match_count) {
        listEl.innerHTML = `<div style="text-align:center;padding:14px 0;color:var(--text-tertiary)">No transactions match this pattern</div>`;
        return;
    }
    listEl.innerHTML = data.matches.map(m => `
        <div style="display:flex;justify-content:space-between;gap:8px;padding:3px 0;border-bottom:0.5px solid var(--separator)">
            <span style="white-space:nowrap;color:var(--text-tertiary);font-variant-numeric:tabular-nums">${m.date}</span>
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(m.store)}">${escapeHtml(m.store)}</span>
            <span style="color:var(--text-tertiary);white-space:nowrap">${escapeHtml(m.category_name)}</span>
            <span style="white-space:nowrap;font-variant-numeric:tabular-nums">${fmt(m.amount)}</span>
        </div>`).join("") + (data.match_count > data.matches.length
            ? `<div style="text-align:center;padding:6px 0;color:var(--text-tertiary)">+ ${data.match_count - data.matches.length} more…</div>`
            : "");
}

function editMerchantRule(id) {
    const rule = merchantRules.find(r => r.id === id);
    if (rule) openMerchantRuleModal(rule);
}

async function saveMerchantRule(id) {
    const pattern = document.getElementById("modal-rule-pattern").value.trim();
    const category_id = parseInt(document.getElementById("modal-rule-category").value);
    const match_type = document.getElementById("modal-rule-matchtype").value;
    if (!pattern) { toast("Pattern required"); return; }

    if (id) {
        await api(`/api/merchant-rules/${id}`, { method: "PUT", body: { pattern, category_id, match_type } });
        toast("Rule updated");
    } else {
        await api("/api/merchant-rules", { method: "POST", body: { pattern, category_id, match_type } });
        toast("Rule added");
    }
    document.querySelector(".modal-overlay").remove();
    merchantRules = await api("/api/merchant-rules");
    renderMerchantRules();
}

async function deleteMerchantRule(id) {
    if (!await confirmDialog({
        title: "Delete this rule?",
        body: "Transactions it already categorised keep their category.",
        confirmLabel: "Delete",
        danger: true,
    })) return;
    await api(`/api/merchant-rules/${id}`, { method: "DELETE" });
    merchantRules = merchantRules.filter(r => r.id !== id);
    renderMerchantRules();
    toast("Rule deleted");
}

