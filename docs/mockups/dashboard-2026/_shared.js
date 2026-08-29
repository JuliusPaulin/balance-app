/* Mockup data + renderers. Synthetic figures — no real account data. */

const fmt = n => new Intl.NumberFormat("fi-FI", {
    style: "currency", currency: "EUR", maximumFractionDigits: 0,
}).format(n);

const MONTHS = ["2025-09","2025-10","2025-11","2025-12","2026-01","2026-02",
                "2026-03","2026-04","2026-05","2026-06","2026-07","2026-08"];

const MONTHLY = MONTHS.map((m, i) => ({
    month: m,
    income:  [4820,4820,4820,6140,4980,4980,4980,5210,4980,4980,4980,5340][i],
    expense: [3910,4265,3480,5120,4410,3720,4890,3995,4180,5310,3640,3870][i],
}));

const EXPENSE_CATS = [
    ["Rent", 1450], ["Groceries", 742], ["Restaurant", 388], ["Car payment", 305],
    ["Travel", 268], ["Utilities", 184], ["Going out", 162], ["Telecom", 118],
    ["Exercise", 89], ["Dog", 74], ["Clothing", 61], ["Gas", 29],
];
const INCOME_CATS = [
    ["Job", 4320], ["Side project", 610], ["Investments", 232],
    ["Expense reimbursement", 128], ["Kela", 50],
];

const CAT_COLORS = ["--accent","--indigo","--orange","--purple","--teal",
                    "--pink","--yellow","--red","--gray","--green"];
const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const dot = i => cssVar(CAT_COLORS[i % CAT_COLORS.length]);

const monthLabel = m => new Date(m + "-01")
    .toLocaleDateString("en-GB", { month: "short", year: "numeric" });
const monthLabelFull = m => new Date(m + "-01")
    .toLocaleDateString("en-GB", { month: "long", year: "numeric" });

/* ── Summary cards ─────────────────────────────────────────────── */
function renderSummaryCards() {
    const last = MONTHLY[MONTHLY.length - 1];
    const net = last.income - last.expense;
    document.getElementById("summary-cards").innerHTML = `
        <div class="summary-card"><div class="label">Income</div>
            <div class="value income">${fmt(last.income)}</div></div>
        <div class="summary-card"><div class="label">Expenses</div>
            <div class="value expense">${fmt(last.expense)}</div></div>
        <div class="summary-card"><div class="label">Net</div>
            <div class="value net ${net >= 0 ? "positive" : "negative"}">${net >= 0 ? "+" : ""}${fmt(net)}</div></div>
        <div class="summary-card"><div class="label">Savings rate</div>
            <div class="value">${Math.round(net / last.income * 100)}%</div></div>`;
}

/* ── Category bars — one renderer, two cards ───────────────────── */
function renderCatBars(elId, rows, kind) {
    const total = rows.reduce((s, r) => s + r[1], 0);
    const max = rows[0][1];
    document.getElementById(elId).innerHTML = rows.map(([name, amt], i) => {
        const pct = (amt / total * 100).toFixed(1);
        const w = Math.max(2, amt / max * 100).toFixed(1);
        return `<div class="cat-bar-row" onclick="openDrilldown('${name}','${kind}')">
            <div class="cat-bar-label"><span class="cat-dot" style="background:${dot(i)}"></span>${name}</div>
            <div class="cat-bar-track"><div class="cat-bar-fill${kind === "income" ? " income" : ""}" style="width:${w}%"></div></div>
            <div class="cat-bar-amount">${fmt(amt)} <span class="cat-bar-pct-inline">· ${pct}%</span></div>
        </div>`;
    }).join("");
}

/* ── Monthly summary — the whole row opens the month ───────────── */
function renderSummaryTable() {
    document.getElementById("summary-table-head").innerHTML = `<tr>
        <th>Period</th>
        <th style="text-align:right">Income</th>
        <th style="text-align:right">Expenses</th>
        <th style="text-align:right">Difference</th>
        <th style="width:32px"></th>
    </tr>`;
    document.getElementById("summary-table-body").innerHTML =
        MONTHLY.slice().reverse().map(d => {
            const diff = d.income - d.expense;
            return `<tr class="row-clickable" onclick="openMonth('${d.month}')">
                <td>${monthLabel(d.month)}<span class="row-go">&#8250;</span></td>
                <td style="text-align:right" class="amount income">+${fmt(d.income)}</td>
                <td style="text-align:right" class="amount">${fmt(d.expense)}</td>
                <td style="text-align:right" class="${diff >= 0 ? "diff-positive" : "diff-negative"}">${diff >= 0 ? "+" : ""}${fmt(diff)}</td>
                <td><button class="note-btn" onclick="event.stopPropagation()" title="Add note">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                        <path d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5"/>
                        <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                </button></td>
            </tr>`;
        }).join("");
}

/* ── Drill-downs ───────────────────────────────────────────────── */
const STORES = ["K-Market Ullanlinna","Alepa Kamppi","Woltti","R-kioski","Lidl Lauttasaari",
                "Ravintola Sandro","HSL","Nordea korko","Verkkokauppa.com","Kesko Oyj"];

function fakeRows(n, hi, lo) {
    return Array.from({ length: n }, (_, i) => ({
        date: `2026-08-${String(28 - i).padStart(2, "0")}`,
        store: STORES[i % STORES.length],
        amount: +(hi - (hi - lo) * (i / n)).toFixed(2),
    }));
}

function modal(title, subtitle, rows) {
    const total = rows.reduce((s, r) => s + r.amount, 0);
    const body = rows.map(r => `<tr>
        <td style="font-size:13px;white-space:nowrap">${r.date}</td>
        <td style="font-size:13px">${r.store}</td>
        <td class="amount" style="font-size:13px;white-space:nowrap;text-align:right">${r.amount.toFixed(2)} €</td>
    </tr>`).join("");
    document.body.insertAdjacentHTML("beforeend",
    `<div class="modal-overlay" onclick="if(event.target===this)this.remove()">
        <div class="modal" style="max-width:600px">
            <div class="modal-title">${title}</div>
            ${subtitle ? `<div style="font-size:13px;color:var(--text-secondary);margin:-8px 0 12px">${subtitle}</div>` : ""}
            <div style="max-height:440px;overflow-y:auto;margin:0 -4px">
                <table style="width:100%">
                    <thead><tr>
                        <th style="font-size:12px">Date</th>
                        <th style="font-size:12px">Store</th>
                        <th style="font-size:12px;text-align:right">Amount</th>
                    </tr></thead>
                    <tbody>${body}</tbody>
                    <tfoot><tr style="border-top:2px solid var(--border)">
                        <td colspan="2" style="font-size:13px;font-weight:600;padding-top:8px">Total (${rows.length} transactions)</td>
                        <td class="amount" style="font-size:13px;font-weight:600;padding-top:8px;text-align:right">${fmt(total)}</td>
                    </tr></tfoot>
                </table>
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
            </div>
        </div>
    </div>`);
}

function openDrilldown(name, kind) {
    modal(name, kind === "income" ? "Income · August 2026" : "Expenses · August 2026",
          fakeRows(9, 420, 12));
}

/* NEW: clicking a month row lists every transaction in that month */
function openMonth(month) {
    const d = MONTHLY.find(r => r.month === month);
    modal(monthLabelFull(month),
          `${fmt(d.income)} in · ${fmt(d.expense)} out · ${d.income - d.expense >= 0 ? "+" : ""}${fmt(d.income - d.expense)} net`,
          fakeRows(16, 1450, 4));
}

/* ── Charts ────────────────────────────────────────────────────── */
function renderMonthlyChart() {
    const ctx = document.getElementById("chart-monthly");
    new Chart(ctx, {
        type: "bar",
        data: {
            labels: MONTHLY.map(d => monthLabel(d.month)),
            datasets: [
                { label: "Income",  data: MONTHLY.map(d => d.income),  backgroundColor: cssVar("--accent"), borderRadius: 4 },
                { label: "Expenses", data: MONTHLY.map(d => d.expense), backgroundColor: cssVar("--text-quaternary"), borderRadius: 4 },
            ],
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { position: "bottom", labels: { boxWidth: 10, usePointStyle: true, pointStyle: "circle" } } },
            scales: {
                x: { grid: { display: false }, ticks: { color: cssVar("--text-tertiary"), font: { size: 11 } } },
                y: { display: false, max: Math.max(...MONTHLY.map(d => Math.max(d.income, d.expense))) * 1.15 },
            },
        },
    });
}

function renderTrendsChart() {
    const ctx = document.getElementById("chart-trends");
    const series = [["Groceries", 700], ["Restaurant", 360], ["Travel", 240], ["Going out", 150], ["Telecom", 118]];
    new Chart(ctx, {
        type: "line",
        data: {
            labels: MONTHS.map(monthLabel),
            datasets: series.map(([name, base], i) => ({
                label: name,
                data: MONTHS.map((_, j) => Math.round(base * (0.75 + Math.abs(Math.sin(j * 1.7 + i)) * 0.5))),
                borderColor: dot(i), backgroundColor: dot(i),
                fill: false, tension: 0, pointRadius: 2, pointHoverRadius: 6,
            })),
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { position: "bottom", labels: { boxWidth: 10, usePointStyle: true, pointStyle: "circle" } } },
            scales: {
                x: { grid: { display: false }, ticks: { color: cssVar("--text-tertiary"), font: { size: 11 } } },
                y: { grid: { color: cssVar("--separator") }, ticks: { color: cssVar("--text-tertiary"), font: { size: 11 } } },
            },
        },
    });
}

/* ── Heatmap (kept — only the Cash Flow Calendar goes) ─────────── */
function renderHeatmap() {
    const grid = document.getElementById("heatmap-grid");
    if (!grid) return;
    let html = "";
    for (let i = 0; i < 371; i++) {
        const lvl = Math.random() < 0.22 ? 0 : Math.min(4, 1 + Math.floor(Math.random() * 4));
        html += `<span class="heatmap-cell" data-level="${lvl}"></span>`;
    }
    grid.innerHTML = html;
    // One grid cell per week column; the month name sits in the week it starts
    const labels = document.getElementById("heatmap-month-labels");
    const names = ["Sep","Oct","Nov","Dec","Jan","Feb","Mar","Apr","May","Jun","Jul","Aug"];
    if (labels) labels.innerHTML = Array.from({ length: 53 }, (_, w) => {
        const i = names.findIndex((_, j) => Math.round(j * 53 / 12) === w);
        return `<span>${i >= 0 ? names[i] : ""}</span>`;
    }).join("");
    const sum = document.getElementById("heatmap-summary");
    if (sum) sum.textContent = "51 300 € across 284 days";
}

/* ── Theme toggle (mockup convenience) ─────────────────────────── */
function toggleTheme() {
    const r = document.documentElement;
    r.setAttribute("data-theme", r.getAttribute("data-theme") === "dark" ? "light" : "dark");
    document.querySelectorAll("canvas").forEach(c => Chart.getChart(c)?.destroy());
    renderMonthlyChart(); renderTrendsChart();
    renderCatBars("category-bars", EXPENSE_CATS, "expense");
    renderCatBars("income-bars", INCOME_CATS, "income");
}

/* ── Period picker behaviour (shared by all variants) ──────────── */
function setHorizon(el) {
    document.querySelectorAll(".horizon-btn").forEach(b => b.classList.remove("active"));
    el.classList.add("active");
}
function togglePeriodDropdown() {
    const dd = document.getElementById("period-dropdown");
    dd.style.display = dd.style.display === "none" ? "block" : "none";
    if (dd.style.display === "block" && !dd.dataset.filled) {
        dd.querySelector("#period-dropdown-items").innerHTML = MONTHS.slice().reverse()
            .map(m => `<label class="period-option"><input type="checkbox"> ${monthLabelFull(m)}</label>`).join("");
        dd.dataset.filled = "1";
    }
}
document.addEventListener("click", e => {
    const sel = document.getElementById("period-selector");
    if (sel && !sel.contains(e.target)) document.getElementById("period-dropdown").style.display = "none";
});

/* ── Boot ──────────────────────────────────────────────────────── */
window.addEventListener("DOMContentLoaded", () => {
    renderSummaryCards();
    renderMonthlyChart();
    renderCatBars("category-bars", EXPENSE_CATS, "expense");
    renderCatBars("income-bars", INCOME_CATS, "income");
    renderTrendsChart();
    renderHeatmap();
    renderSummaryTable();
});
