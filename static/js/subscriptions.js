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

// The groups, in the order they earn their place on the page. Detection finds
// every charge that repeats; these say what kind of thing each one is, which is
// the difference between a page that answers "what am I subscribed to" and one
// that lists rent at the top under the wrong heading.
const RECURRING_GROUPS = [
    { key: "subscription", label: "Subscriptions",
      blurb: "Services you pay for and could stop" },
    { key: "bill",         label: "Bills & housing",
      blurb: "Fixed obligations — real money, but not a decision" },
    { key: "spending",     label: "Regular spending",
      blurb: "Shops you visit on a rhythm. Nothing to cancel" },
    { key: "transfer",     label: "Transfers & investments",
      blurb: "Money moved, not money spent" },
    { key: "income",       label: "Income",
      blurb: "A salary repeats monthly too. It is not a subscription" },
];
const RECURRING_GROUP_LABEL =
    Object.fromEntries(RECURRING_GROUPS.map(g => [g.key, g.label]));

let recurringData    = null;
let recurringShowEnded  = false;
let recurringShowHidden = false;

async function loadRecurring() {
    const body = document.getElementById("recurring-body");
    body.innerHTML = `<tr><td colspan="6" style="color:var(--text-tertiary);padding:16px">Scanning…</td></tr>`;
    let data;
    try {
        data = await api("/api/recurring");
    } catch (e) {
        body.innerHTML = `<tr><td colspan="6" style="color:var(--red);padding:16px">Could not load recurring data</td></tr>`;
        return;
    }
    recurringData = data;
    renderRecurring(data);
    renderUpcoming(data);
    loadRecurringHistory();
}

// ── The table ───────────────────────────────────────────────────────
function recurringRow(i) {
    const s = RECURRING_STATUS[i.status] || { label: i.status, cls: "" };
    const cat = i.category ? ` · ${i.category}` : "";
    const manualTag = i.is_manual ? ` <span class="recurring-manual">added</span>` : "";
    const movedTag  = i.moved ? ` <span class="recurring-manual" title="You filed this here">moved</span>` : "";
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
    // A price change used to be a bare "Price ↑" badge: it told you something
    // moved and refused to say what. The two amounts are the only part worth
    // reading — YouTube went 9,99 € → 14,99 € under that badge for months.
    const priceMove = i.prev_amount
        ? `<div class="recurring-pricemove" title="Charged ${fmt(i.prev_amount)} until ${fmtDate(i.price_changed_on)}">
               ${fmt(i.prev_amount)} → <strong>${fmt(i.last_amount)}</strong>
               <span class="recurring-pricepct">${i.price_change_pct > 0 ? "+" : ""}${i.price_change_pct}%</span>
           </div>`
        : "";
    // A yearly bill reads as a small monthly number and is the easiest thing to
    // forget you own — so the cadence column says what actually leaves the
    // account, and when, rather than only the smoothed per-month figure.
    const cadenceCell = i.cadence === "monthly"
        ? `<span style="text-transform:capitalize">${i.cadence}</span>`
        : `<span style="text-transform:capitalize">${i.cadence}</span>
           <div class="recurring-cadence-note">${fmt(i.last_amount)} at a time</div>`;
    const remove = i.is_manual
        ? { fn: `deleteSubscription(${i.manual_id})`, title: "Remove this subscription" }
        : { fn: `dismissRecurring('${encodeURIComponent(i.signature || "")}')`, title: "Hide this series" };
    const move = i.signature
        ? `<button class="recurring-hide recurring-move" title="Move to another group"
              onclick="openMoveRecurring('${encodeURIComponent(i.signature)}')">⇄</button>`
        : "";
    return `<tr>
        <td><div style="font-weight:600">${escapeHtml(i.store)}${manualTag}${movedTag}</div>
            <div style="font-size:11px;color:var(--text-tertiary)">${seen}${escapeHtml(cat)}</div>
            ${priceMove}</td>
        <td>${cadenceCell}</td>
        <td style="text-align:right;font-weight:600" title="Typical charge: ${fmt(i.avg_amount)}">${fmt(i.monthly_cost)}</td>
        <td>${nextDue}</td>
        <td><span class="recurring-badge ${s.cls}">${s.label}</span></td>
        <td style="text-align:right;white-space:nowrap">${move}<button class="recurring-hide" title="${remove.title}"
            onclick="${remove.fn}">✕</button></td>
    </tr>`;
}

function groupHeaderRow(label, blurb, total, count) {
    const right = total != null
        ? `<span class="recurring-group-total">${fmt(total)}/mo</span>`
        : count != null ? `<span class="recurring-group-total">${count}</span>` : "";
    return `<tr class="recurring-group-row"><td colspan="6">
        <div class="recurring-group-head">
            <div><span class="recurring-group-label">${escapeHtml(label)}</span>
                 <span class="recurring-group-blurb">${escapeHtml(blurb)}</span></div>
            ${right}
        </div></td></tr>`;
}

function collapsibleHeaderRow(label, count, open, toggle) {
    return `<tr class="recurring-group-row"><td colspan="6">
        <button class="recurring-group-head recurring-group-toggle" onclick="${toggle}">
            <div><span class="recurring-group-label">${escapeHtml(label)}</span>
                 <span class="recurring-group-blurb">${count} ${count === 1 ? "series" : "series"}</span></div>
            <span class="recurring-group-total">${open ? "Hide" : "Show"}</span>
        </button></td></tr>`;
}

function renderRecurring(data) {
    const body    = document.getElementById("recurring-body");
    const summary = document.getElementById("recurring-summary");
    const items   = data.items || [];
    const groups  = (data.summary && data.summary.groups) || {};
    const hidden  = data.dismissed || [];

    // The headline is the subscription group alone. It used to be every expense
    // that had not stopped, which on real data read 774 €/mo against 73 € of
    // actual subscriptions — the rent, the phone bill and a takeaway habit
    // wearing the page's name.
    summary.innerHTML = (items.length || hidden.length)
        ? `<span><strong>${fmt(data.summary.monthly_total)}</strong>/mo</span>` +
          `<span><strong>${fmt(data.summary.annual_total)}</strong>/yr</span>` +
          `<span>${data.summary.active_count} subscription${data.summary.active_count === 1 ? "" : "s"}</span>`
        : "";

    if (!items.length && !hidden.length) {
        body.innerHTML = `<tr><td colspan="6" style="color:var(--text-tertiary);padding:16px">No recurring charges detected yet.</td></tr>`;
        return;
    }

    // Ended series come out of every group and gather at the bottom: a third of
    // the real table was services that stopped months ago, sitting between the
    // ones still charging.
    const live  = items.filter(i => i.status !== "stopped");
    const ended = items.filter(i => i.status === "stopped");

    let html = "";
    for (const g of RECURRING_GROUPS) {
        const rows = live.filter(i => i.group === g.key);
        if (!rows.length) continue;
        const gs = groups[g.key] || {};
        // Only expense groups get a monthly total; summing a salary and calling
        // it a cost would be the same mistake one level up.
        const total = g.key === "income" ? null : gs.monthly_total;
        html += groupHeaderRow(g.label, g.blurb, total, rows.length);
        html += rows.map(recurringRow).join("");
    }
    if (ended.length) {
        html += collapsibleHeaderRow("Ended", ended.length, recurringShowEnded,
                                     "toggleRecurringEnded()");
        if (recurringShowEnded) html += ended.map(recurringRow).join("");
    }
    if (hidden.length) {
        html += collapsibleHeaderRow("Hidden", hidden.length, recurringShowHidden,
                                     "toggleRecurringHidden()");
        if (recurringShowHidden) html += hidden.map(hiddenRow).join("");
    }
    body.innerHTML = html;
}

// A hidden series is not in `items` — detection drops it before it is costed —
// so all the row can show is the name it was hidden under, and the way back.
function hiddenRow(h) {
    return `<tr class="recurring-hidden-row">
        <td><div style="font-weight:600">${escapeHtml(h.store)}</div>
            <div style="font-size:11px;color:var(--text-tertiary)">Hidden — not counted anywhere</div></td>
        <td style="text-transform:capitalize">${escapeHtml(h.cadence || "")}</td>
        <td colspan="3"></td>
        <td style="text-align:right"><button class="btn btn-ghost btn-sm"
            onclick="restoreRecurring('${encodeURIComponent(h.signature)}')">Restore</button></td>
    </tr>`;
}

function toggleRecurringEnded()  { recurringShowEnded  = !recurringShowEnded;  renderRecurring(recurringData); }
function toggleRecurringHidden() { recurringShowHidden = !recurringShowHidden; renderRecurring(recurringData); }

// ── Coming up in the next 30 days ───────────────────────────────────
function renderUpcoming(data) {
    const box   = document.getElementById("recurring-upcoming");
    const total = document.getElementById("recurring-upcoming-total");
    if (!box) return;
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const horizon = new Date(today); horizon.setDate(horizon.getDate() + 30);
    const isoToday   = today.toISOString().slice(0, 10);
    const isoHorizon = horizon.toISOString().slice(0, 10);

    // Only what you are actually subscribed to, and only what has not stopped.
    // `next_date` was already computed for every series and used for nothing
    // but the colour of a table cell.
    const due = (data.items || [])
        .filter(i => i.group === "subscription" && i.status !== "stopped"
                     && i.next_date && i.next_date >= isoToday && i.next_date <= isoHorizon)
        .sort((a, b) => a.next_date.localeCompare(b.next_date));

    if (!due.length) {
        total.textContent = "";
        box.innerHTML = `<div class="sub-upcoming-empty">Nothing due in the next 30 days.</div>`;
        return;
    }
    const sum = due.reduce((a, i) => a + i.last_amount, 0);
    total.textContent = `${fmt(sum)} due`;
    box.innerHTML = due.map(i => {
        const days = Math.round((new Date(i.next_date) - today) / 86400000);
        const when = days === 0 ? "today" : days === 1 ? "tomorrow" : `in ${days} days`;
        const yearly = i.cadence === "yearly"
            ? ` <span class="recurring-manual" title="Billed once a year">yearly</span>` : "";
        return `<div class="sub-upcoming-row">
            <div><div class="sub-upcoming-store">${escapeHtml(i.store)}${yearly}</div>
                 <div class="sub-upcoming-when">${fmtDate(i.next_date)} · ${when}</div></div>
            <div class="sub-upcoming-amt">${fmt(i.last_amount)}</div>
        </div>`;
    }).join("");
}

// ── What subscriptions cost, month by month ─────────────────────────
async function loadRecurringHistory() {
    const canvas = document.getElementById("chart-recurring-history");
    if (!canvas || typeof Chart === "undefined") return;
    let data;
    try {
        data = await api("/api/recurring/history?months=12");
    } catch (e) {
        return;
    }
    renderRecurringHistory(data.months || []);
}

function renderRecurringHistory(months) {
    if (charts.recurringHistory) charts.recurringHistory.destroy();
    const note = document.getElementById("recurring-history-note");
    const ctx  = document.getElementById("chart-recurring-history");
    // The month in progress is not a month yet: on the 1st it is 0, and drawn
    // beside eleven full ones it reads as a collapse in spending that did not
    // happen. It is drawn faint and left out of the comparison.
    const thisMonth = new Date().toISOString().slice(0, 7);
    const full = months.filter(m => m.month !== thisMonth);
    const totals = full.map(m => m.total);
    const nonZero = totals.filter(v => v > 0);

    if (note) {
        if (nonZero.length >= 4) {
            // Where it started against where it is now, over whole months only.
            const firstHalf = nonZero.slice(0, Math.floor(nonZero.length / 2));
            const lastHalf  = nonZero.slice(Math.floor(nonZero.length / 2));
            const a = firstHalf.reduce((x, y) => x + y, 0) / firstHalf.length;
            const b = lastHalf.reduce((x, y) => x + y, 0) / lastHalf.length;
            const pct = a > 0 ? Math.round((b - a) / a * 100) : 0;
            note.innerHTML = Math.abs(pct) < 5
                ? `Steady near ${fmt(b)}/mo`
                : `<span style="color:var(--${pct > 0 ? "red" : "green"})">${pct > 0 ? "↑" : "↓"} ${Math.abs(pct)}%</span> — ${fmt(a)} → ${fmt(b)}/mo`;
        } else {
            note.textContent = "Month by month";
        }
    }

    charts.recurringHistory = new Chart(ctx, {
        type: "bar",
        data: {
            labels: months.map(m => monthLabel(m.month)),
            datasets: [{
                label: "Subscriptions",
                data: months.map(m => m.total),
                backgroundColor: months.map(m => m.month === thisMonth
                    ? rgbaVar("--accent", 0.3) : rgbaVar("--accent", 0.85)),
                borderRadius: 6, borderSkipped: false,
            }],
        },
        options: {
            ...chartOptions(),
            plugins: {
                ...chartOptions().plugins,
                tooltip: {
                    ...chartOptions().plugins.tooltip,
                    callbacks: {
                        label: c => ` ${fmt(c.parsed.y)}`,
                        afterLabel: c => months[c.dataIndex].month === thisMonth
                            ? "  this month so far" : "",
                    },
                },
            },
        },
    });
}

// ── Hide, restore, re-file ──────────────────────────────────────────
async function dismissRecurring(sig) {
    try {
        await api("/api/recurring/dismiss", { method: "POST", body: { signature: decodeURIComponent(sig) } });
        toast("Hidden — see Hidden at the foot of the table");
        loadRecurring();
    } catch (e) {
        toast("Could not hide series");
    }
}

async function restoreRecurring(sig) {
    try {
        await api(`/api/recurring/dismiss/${encodeURIComponent(decodeURIComponent(sig))}`,
                  { method: "DELETE" });
        toast("Restored");
        loadRecurring();
    } catch (e) {
        toast("Could not restore series");
    }
}

// Grouping by category is a guess, and it is wrong for somebody: a gym under
// "Exercise" is a subscription, running shoes bought on a rhythm are not. This
// is the last word, and it is remembered against the same signature a hide uses.
function openMoveRecurring(sigEnc) {
    const sig  = decodeURIComponent(sigEnc);
    const item = (recurringData?.items || []).find(i => i.signature === sig);
    if (!item) return;
    const opts = RECURRING_GROUPS.map(g => `
        <button class="sub-move-opt ${g.key === item.group ? "active" : ""}"
                onclick="moveRecurring('${sigEnc}', '${g.key}')">
            <div class="sub-move-label">${escapeHtml(g.label)}</div>
            <div class="sub-move-blurb">${escapeHtml(g.blurb)}</div>
        </button>`).join("");
    const html = `<div class="modal-overlay" onclick="if(event.target===this)this.remove()">
        <div class="modal" style="max-width:420px">
            <div class="modal-title">Move “${escapeHtml(item.store)}”</div>
            <p class="confirm-text">Only the Subscriptions group counts toward the headline figure.</p>
            <div class="sub-move-list">${opts}</div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
                ${item.moved ? `<button class="btn btn-ghost" onclick="moveRecurring('${sigEnc}', null)">Use the category again</button>` : ""}
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML("beforeend", html);
}

async function moveRecurring(sigEnc, group) {
    try {
        await api("/api/recurring/group",
                  { method: "PUT", body: { signature: decodeURIComponent(sigEnc), group } });
        closeTopOverlay();
        toast(group ? `Moved to ${RECURRING_GROUP_LABEL[group]}` : "Back to the category's own group");
        loadRecurring();
    } catch (e) {
        toast("Could not move it");
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

