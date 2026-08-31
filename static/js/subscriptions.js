// Balance — subscriptions.js
//
// Recurring charges: detected, dismissed and hand-added.
//
// One of the ordered classic scripts index.html loads. They share one global
// scope on purpose: index.html wires ~95 inline handlers straight to these
// names, so these are plain scripts and not modules.

// ── Recurring & subscriptions ───────────────────────────────────────
const RECURRING_STATUS = {
    active:        { label: "Active",     cls: "recurring-active" },
    due_soon:      { label: "Due soon",   cls: "recurring-due" },
    overdue:       { label: "Overdue",    cls: "recurring-overdue" },
    stopped:       { label: "Stopped",    cls: "recurring-stopped" },
    price_changed: { label: "Price ↑",    cls: "recurring-price" },
};

async function loadRecurring() {
    const body    = document.getElementById("recurring-body");
    body.innerHTML = `<tr><td colspan="6" style="color:var(--text-tertiary);padding:16px">Scanning…</td></tr>`;
    let data;
    try {
        data = await api("/api/recurring");
    } catch (e) {
        body.innerHTML = `<tr><td colspan="6" style="color:var(--red);padding:16px">Could not load recurring data</td></tr>`;
        return;
    }
    renderRecurring(data);
}

function recurringRow(i) {
    const s = RECURRING_STATUS[i.status] || { label: i.status, cls: "" };
    const cat = i.category ? ` · ${i.category}` : "";
    const typeTag = i.type === "income" ? ` <span class="recurring-income">income</span>` : "";
    const manualTag = i.is_manual ? ` <span class="recurring-manual">added</span>` : "";
    const seen = i.is_manual ? "Added manually" : `${i.occurrences}× seen`;
    // A "next due" in the past means the series looks lapsed — show the date
    // in orange with a hint instead of a calm gray (design #14). A stopped
    // series is the exception: its next date is a prediction that will never
    // come, so orange would contradict the grey badge sitting beside it.
    const todayIso = new Date().toISOString().slice(0, 10);
    const duePast = i.next_date && i.next_date < todayIso && i.status !== "stopped";
    const nextDue = i.next_date
        ? (duePast
            ? `<span style="color:var(--orange)" title="Expected date has passed — the series may have lapsed">${fmtDate(i.next_date)}</span>`
            : i.status === "stopped"
                ? `<span style="color:var(--text-tertiary)" title="The charge expected on this date never arrived">${fmtDate(i.next_date)}</span>`
                : fmtDate(i.next_date))
        : "—";
    const remove = i.is_manual
        ? { fn: `deleteSubscription(${i.manual_id})`, title: "Remove this subscription" }
        : { fn: `dismissRecurring('${encodeURIComponent(i.signature || "")}')`, title: "Hide this series" };
    return `<tr>
        <td><div style="font-weight:600">${escapeHtml(i.store)}${typeTag}${manualTag}</div>
            <div style="font-size:11px;color:var(--text-tertiary)">${seen}${escapeHtml(cat)}</div></td>
        <td style="text-transform:capitalize">${i.cadence}</td>
        <td style="text-align:right;font-weight:600" title="Typical charge: ${fmt(i.avg_amount)}">${fmt(i.monthly_cost)}</td>
        <td>${nextDue}</td>
        <td><span class="recurring-badge ${s.cls}">${s.label}</span></td>
        <td style="text-align:right"><button class="recurring-hide" title="${remove.title}"
            onclick="${remove.fn}">✕</button></td>
    </tr>`;
}

function renderRecurring(data) {
    const body    = document.getElementById("recurring-body");
    const summary = document.getElementById("recurring-summary");
    const items   = data.items || [];
    const regular   = items.filter(i => !i.is_transfer);
    const transfers = items.filter(i => i.is_transfer);

    summary.innerHTML = items.length
        ? `<span><strong>${fmt(data.summary.monthly_total)}</strong>/mo</span>` +
          `<span><strong>${fmt(data.summary.annual_total)}</strong>/yr</span>` +
          `<span>${data.summary.active_count ?? data.summary.count} active</span>`
        : "";

    if (!items.length) {
        body.innerHTML = `<tr><td colspan="6" style="color:var(--text-tertiary);padding:16px">No recurring charges detected yet.</td></tr>`;
        return;
    }

    let html = regular.map(recurringRow).join("");
    if (transfers.length) {
        html += `<tr><td colspan="6" style="padding:10px 8px 4px;font-size:11px;font-weight:600;
            text-transform:uppercase;letter-spacing:.04em;color:var(--text-tertiary)">
            Transfers &amp; investments <span style="font-weight:400;text-transform:none">(excluded from totals)</span></td></tr>`;
        html += transfers.map(recurringRow).join("");
    }
    body.innerHTML = html;
}

async function dismissRecurring(sig) {
    try {
        await api("/api/recurring/dismiss", { method: "POST", body: { signature: decodeURIComponent(sig) } });
        toast("Hidden");
        loadRecurring();
    } catch (e) {
        toast("Could not hide series");
    }
}

function openAddSubscription() {
    const catOptions = ['<option value="">No category</option>'].concat(
        [...new Set(categories.map(c => c.name))].sort((a, b) => a.localeCompare(b))
            .map(n => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`)
    ).join("");
    const html = `<div class="modal-overlay" onclick="if(event.target===this)this.remove()">
        <div class="modal" style="max-width:440px">
            <div class="modal-title">Add subscription</div>
            <div class="form-group">
                <label class="form-label">Merchant</label>
                <input class="form-input" id="sub-store" placeholder="e.g. Spotify" autocomplete="off">
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label class="form-label">Amount</label>
                    <input class="form-input" id="sub-amount" type="number" step="0.01" min="0" placeholder="0">
                </div>
                <div class="form-group">
                    <label class="form-label">Cadence</label>
                    <select class="form-select" id="sub-cadence">
                        <option value="monthly">Monthly</option>
                        <option value="quarterly">Quarterly</option>
                        <option value="yearly">Yearly</option>
                    </select>
                </div>
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label class="form-label">Type</label>
                    <select class="form-select" id="sub-type">
                        <option value="expense">Expense</option>
                        <option value="income">Income</option>
                    </select>
                </div>
                <div class="form-group">
                    <label class="form-label">Category</label>
                    <select class="form-select" id="sub-category">${catOptions}</select>
                </div>
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
                <button class="btn btn-primary" onclick="submitAddSubscription(this)">Add</button>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML("beforeend", html);
    document.getElementById("sub-store").focus();
}

async function submitAddSubscription(btn) {
    const store    = document.getElementById("sub-store").value.trim();
    const amount   = parseFloat(document.getElementById("sub-amount").value);
    const cadence  = document.getElementById("sub-cadence").value;
    const type     = document.getElementById("sub-type").value;
    const category = document.getElementById("sub-category").value || null;
    if (!store)              { toast("Enter a merchant name"); return; }
    if (!(amount > 0))       { toast("Enter an amount greater than 0"); return; }
    try {
        await api("/api/subscriptions", { method: "POST", body: { store, amount, cadence, type, category } });
        btn.closest(".modal-overlay").remove();
        toast("Subscription added");
        loadRecurring();
    } catch (e) {
        toast("Could not add subscription");
    }
}

async function deleteSubscription(id) {
    try {
        await api(`/api/subscriptions/${id}`, { method: "DELETE" });
        toast("Removed");
        loadRecurring();
    } catch (e) {
        toast("Could not remove subscription");
    }
}

async function setTrendsPeriod(p) {
    trendsPeriod = p;
    document.querySelectorAll(".trends-period-btn").forEach(b =>
        b.classList.toggle("active", parseInt(b.dataset.period) === p));
    await loadTrendsData();
}

async function setTrendsTopTxPeriod(p) {
    trendsTopTxPeriod = p;
    document.querySelectorAll(".trends-toptx-btn").forEach(b =>
        b.classList.toggle("active", parseInt(b.dataset.period) === p));
    await loadTrendsTopTx();
}

async function loadTrendsData() {
    const empty   = document.getElementById("trends-empty");
    const content = document.getElementById("trends-content");
    if (!trendsSelectedCatIds.length) {
        empty.style.display   = "flex";
        content.style.display = "none";
        return;
    }
    empty.style.display   = "none";
    content.style.display = "block";

    const idsParam = trendsSelectedCatIds.join(",");
    const data     = await api(`/api/trends/category?category_ids=${idsParam}&months=${trendsPeriod}`);
    trendsCachedData = data;

    const fkey = JSON.stringify([...trendsSelectedCatIds].sort());
    if (fkey !== trendsForecastKey) {
        trendsForecastAllData = await api(`/api/trends/category?category_ids=${idsParam}&months=0`);
        trendsForecastKey     = fkey;
    }

    renderTrendsStatCards(data);
    renderTrendsMonthlyChart(data);
    renderTrendsMomChart(data);
    renderTrendsMerchants(data);
    renderTrendsFreqChart(data);
    renderTrendsForecast(trendsForecastAllData);
    await loadTrendsTopTx();
}

async function loadTrendsTopTx() {
    if (!trendsSelectedCatIds.length) return;
    let url = `/api/transactions?category_ids=${trendsSelectedCatIds.join(",")}&sort=amount&dir=desc&per_page=25`;
    if (trendsTopTxPeriod > 0) {
        const d = new Date();
        d.setMonth(d.getMonth() - trendsTopTxPeriod);
        url += `&date_from=${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-01`;
    }
    const data = await api(url);
    renderTrendsTopTx(data ? data.items : []);
}

