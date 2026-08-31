// Balance — reports.js
//
// The annual report.
//
// One of the ordered classic scripts index.html loads. They share one global
// scope on purpose: index.html wires ~95 inline handlers straight to these
// names, so these are plain scripts and not modules.

// ── Annual Report ───────────────────────────────────────────────────
let reportData = null;

async function loadReport() {
    const yearSel = document.getElementById("report-year-select");
    const year = yearSel?.value || new Date().getFullYear();

    const data = await api(`/api/reports/annual?year=${year}`);
    reportData = data;

    // Populate year selector on first load
    if (yearSel && yearSel.options.length === 0) {
        data.available_years.forEach(y => {
            const opt = document.createElement("option");
            opt.value = y;
            opt.textContent = y;
            if (String(y) === String(year)) opt.selected = true;
            yearSel.appendChild(opt);
        });
    }

    renderReportSummaryCards(data);
    renderReportMonthlyChart(data);
    renderReportYoY(data);
    renderReportMonths(data);
    renderReportMonthlyTable(data);
    renderReportCategoryBars(data);
    renderReportIncomeBars(data);
    renderReportTopTransactions(data);
    renderReportTopIncome(data);
}

function renderReportSummaryCards(data) {
    const container = document.getElementById("report-summary-cards");
    const income  = data.totals.income  || 0;
    const expense = data.totals.expense || 0;
    const net = income - expense;
    // Savings rate treats money moved into Investments as saved, not spent —
    // so it's added back on top of the plain net (income − all expenses).
    const investAmt = (data.categories || []).find(c => c.name === "Investments")?.total || 0;
    const savedNet = net + investAmt;
    const savingsRate = income > 0 ? ((savedNet / income) * 100).toFixed(1) : "0.0";

    const months = [...new Set(data.monthly.map(r => r.month))];
    const expMonths = months.filter(m => data.monthly.some(r => r.month === m && r.type === "expense"));
    const avgExp = expMonths.length ? expense / expMonths.length : 0;
    const txCount = (data.transaction_count?.expense || 0) + (data.transaction_count?.income || 0);

    container.innerHTML = `
        <div class="summary-card"><div class="label">Total Income</div><div class="value income">+${fmt(income)}</div></div>
        <div class="summary-card"><div class="label">Total Expenses</div><div class="value expense">${fmt(expense)}</div></div>
        <div class="summary-card"><div class="label">Net</div><div class="value net ${net >= 0 ? "positive" : "negative"}">${net >= 0 ? "+" : ""}${fmt(net)}</div></div>
        <div class="summary-card"><div class="label">Savings Rate</div><div class="value ${savedNet >= 0 ? "positive" : "negative"}" title="Net savings incl. money invested, as a share of income">${savingsRate}%</div></div>
        <div class="summary-card"><div class="label">Avg. Monthly Expense</div><div class="value expense">${fmt(avgExp)}</div></div>
        <div class="summary-card"><div class="label">Transactions</div><div class="value">${txCount}</div></div>`;
}

// Every year-on-year figure on this page covers only the months the chosen
// year actually has. Once that is fewer than twelve, say so on the label —
// an unqualified "vs 2025" over seven months would read as a real fall.
function compareRangeLabel(data) {
    const mm = data.compare_months || [];
    if (!mm.length || mm.length >= 12) return "";
    const name = m => new Date(2000, parseInt(m, 10) - 1, 1)
        .toLocaleDateString("en-US", { month: "short" });
    const first = name(mm[0]), last = name(mm[mm.length - 1]);
    return first === last ? ` (${first})` : ` (${first}–${last})`;
}

function renderReportYoY(data) {
    const container = document.getElementById("report-yoy");
    const curInc  = data.totals.income  || 0;
    const curExp  = data.totals.expense || 0;
    const prevInc = data.prev_totals.income  || 0;
    const prevExp = data.prev_totals.expense || 0;

    // Badge sign comes from the direction of change (diff), never from the
    // raw ratio — a negative previous-year base used to produce "+-137.7%".
    // Color encodes good/bad, not up/down: more income and a higher net are
    // good; higher expenses are bad.
    function yoyBadge(cur, prev, goodWhenUp) {
        if (!prev) return "";
        const diff = cur - prev;
        const pct  = Math.abs((diff / Math.abs(prev)) * 100).toFixed(1);
        const good = goodWhenUp ? diff >= 0 : diff <= 0;
        const cls  = good ? "yoy-positive" : "yoy-negative";
        return `<span class="${cls}">${diff >= 0 ? "+" : "−"}${pct}%</span>`;
    }

    const range = compareRangeLabel(data);
    const rows = [
        { label: `Income vs ${data.year - 1}${range}`, cur: curInc, prev: prevInc, sign: "+", goodWhenUp: true },
        { label: `Expenses vs ${data.year - 1}${range}`, cur: curExp, prev: prevExp, sign: "", goodWhenUp: false },
        { label: `Net vs ${data.year - 1}${range}`, cur: curInc - curExp, prev: prevInc - prevExp, sign: "", goodWhenUp: true },
    ];

    container.innerHTML = rows.map(r => `
        <div class="report-stat-row">
            <span class="report-stat-label">${r.label}</span>
            <span class="report-stat-value">${r.sign}${fmt(r.cur)} ${yoyBadge(r.cur, r.prev, r.goodWhenUp)}</span>
        </div>`).join("");

    if (!prevInc && !prevExp) {
        container.innerHTML += `<p style="font-size:var(--text-subhead);color:var(--text-tertiary);margin-top:8px">No data for ${data.year - 1}</p>`;
    }
}

function renderReportMonths(data) {
    const container = document.getElementById("report-months");
    const months = [...new Set(data.monthly.map(r => r.month))].sort();

    const monthData = months.map(m => {
        const exp = data.monthly.filter(r => r.month === m && r.type === "expense").reduce((s, r) => s + r.total, 0);
        return { month: m, expense: exp };
    }).filter(d => d.expense > 0);

    if (!monthData.length) {
        container.innerHTML = '<p style="color:var(--text-tertiary);font-size:var(--text-subhead)">No expense data</p>';
        return;
    }

    const sorted = [...monthData].sort((a, b) => a.expense - b.expense);
    const best  = sorted[0];
    const worst = sorted[sorted.length - 1];
    const avg   = monthData.reduce((s, d) => s + d.expense, 0) / monthData.length;

    container.innerHTML = `
        <div class="report-stat-row">
            <span class="report-stat-label">Lowest spending month</span>
            <span class="report-stat-value" style="color:var(--green)">${monthLabel(best.month)} — ${fmt(best.expense)}</span>
        </div>
        <div class="report-stat-row">
            <span class="report-stat-label">Highest spending month</span>
            <span class="report-stat-value" style="color:var(--red)">${monthLabel(worst.month)} — ${fmt(worst.expense)}</span>
        </div>
        <div class="report-stat-row">
            <span class="report-stat-label">Monthly average</span>
            <span class="report-stat-value">${fmt(avg)}</span>
        </div>
        <div class="report-stat-row">
            <span class="report-stat-label">Months with data</span>
            <span class="report-stat-value">${monthData.length}</span>
        </div>`;
}

function renderReportCategoryBars(data) {
    const container = document.getElementById("report-category-bars");
    if (!data.categories.length) {
        container.innerHTML = '<div class="empty-state"><p>No expense data</p></div>';
        return;
    }
    const total = data.categories.reduce((s, c) => s + c.total, 0);
    const max   = data.categories[0].total;
    const prev  = data.prev_categories || {};
    container.innerHTML = data.categories.map(item => {
        const pct   = ((item.total / total) * 100).toFixed(1);
        const width = Math.max(2, (item.total / max) * 100).toFixed(1);
        const cat   = categories.find(c => c.name === item.name && c.type === "expense");
        // YoY delta vs last year — spending less is good (green).
        let delta = "";
        if (prev[item.name]) {
            const diff = item.total - prev[item.name];
            const dPct = Math.abs((diff / prev[item.name]) * 100).toFixed(0);
            delta = `<span class="${diff <= 0 ? "yoy-positive" : "yoy-negative"}" style="font-size:var(--text-caption)">${diff >= 0 ? "+" : "−"}${dPct}%</span>`;
        }
        return `<div class="cat-bar-row">
            <div class="cat-bar-label" title="${item.name}"><span class="cat-dot" style="background:${catDotColor(cat?.id)}"></span>${item.name}</div>
            <div class="cat-bar-track"><div class="cat-bar-fill" style="width:${width}%;background:${rgbaVar("--accent", 0.55)}"></div></div>
            <div class="cat-bar-amount">${fmt(item.total)} <span class="cat-bar-pct-inline">· ${pct}%</span> ${delta}</div>
        </div>`;
    }).join("");
}

function renderReportMonthlyChart(data) {
    const ctx = document.getElementById("report-monthly-chart");
    if (charts.reportMonthly) charts.reportMonthly.destroy();

    const months = [...new Set(data.monthly.map(r => r.month))].sort();
    if (!months.length) {
        charts.reportMonthly = new Chart(ctx, { type: "bar", data: { labels: ["No data"], datasets: [{ data: [0] }] }, options: chartOptions() });
        return;
    }

    const expData = months.map(m => {
        const r = data.monthly.find(x => x.month === m && x.type === "expense");
        return r ? r.total : 0;
    });
    const incData = months.map(m => {
        const r = data.monthly.find(x => x.month === m && x.type === "income");
        return r ? r.total : 0;
    });
    const netData = months.map((_, i) => incData[i] - expData[i]);

    // Pair the comparison line by calendar month, never by position in the
    // array: a previous year that starts later than this one would otherwise
    // slide the whole line sideways. A month it has no data for stays a gap.
    const prevExpData = months.map(m => {
        const prevM = `${data.year - 1}-${m.slice(5)}`;
        const r = (data.prev_monthly || []).find(x => x.month === prevM && x.type === "expense");
        return r ? r.total : null;
    });
    const hasPrev = prevExpData.some(v => v !== null);

    const datasets = [
        { label: "Income", data: incData, backgroundColor: rgbaVar("--accent", 0.7), borderRadius: 4, order: 2 },
        { label: "Expenses", data: expData, backgroundColor: rgbaVar("--red", 0.7), borderRadius: 4, order: 2 },
    ];

    if (hasPrev) {
        datasets.push({
            label: `${data.year - 1} Expenses`, type: "line", data: prevExpData,
            borderColor: rgbaVar("--red", 0.3), borderDash: [5, 4], borderWidth: 1.5,
            pointRadius: 0, tension: 0, fill: false, order: 1,
        });
    }

    const opts = chartOptions();
    opts.plugins.legend.display = true;

    charts.reportMonthly = new Chart(ctx, {
        type: "bar",
        data: { labels: months.map(monthLabel), datasets },
        options: opts,
    });
}

function renderReportMonthlyTable(data) {
    const tbody = document.getElementById("report-monthly-table");
    const months = [...new Set(data.monthly.map(r => r.month))].sort();

    if (!months.length) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;padding:24px;color:var(--text-tertiary)">No data</td></tr>`;
        return;
    }

    let totalInc = 0, totalExp = 0;
    const rows = months.map(m => {
        const inc = data.monthly.filter(r => r.month === m && r.type === "income").reduce((s, r) => s + r.total, 0);
        const exp = data.monthly.filter(r => r.month === m && r.type === "expense").reduce((s, r) => s + r.total, 0);
        const net = inc - exp;
        const rate = inc > 0 ? ((net / inc) * 100).toFixed(1) : "—";
        totalInc += inc;
        totalExp += exp;
        return `<tr>
            <td>${monthLabel(m)}</td>
            <td style="text-align:right" class="amount income">${inc ? "+" + fmt(inc) : "—"}</td>
            <td style="text-align:right" class="amount expense">${exp ? fmt(exp) : "—"}</td>
            <td style="text-align:right;font-weight:600;color:${net >= 0 ? "var(--green)" : "var(--red)"}">${net >= 0 ? "+" : ""}${fmt(net)}</td>
            <td style="text-align:right;color:${parseFloat(rate) >= 0 ? "var(--green)" : "var(--red)"}">${typeof rate === "string" && rate === "—" ? "—" : rate + "%"}</td>
        </tr>`;
    });

    const totalNet = totalInc - totalExp;
    const totalRate = totalInc > 0 ? ((totalNet / totalInc) * 100).toFixed(1) + "%" : "—";
    rows.push(`<tr style="font-weight:700;border-top:2px solid var(--separator)">
        <td>Total</td>
        <td style="text-align:right" class="amount income">+${fmt(totalInc)}</td>
        <td style="text-align:right" class="amount expense">${fmt(totalExp)}</td>
        <td style="text-align:right;color:${totalNet >= 0 ? "var(--green)" : "var(--red)"}">${totalNet >= 0 ? "+" : ""}${fmt(totalNet)}</td>
        <td style="text-align:right;color:${parseFloat(totalRate) >= 0 ? "var(--green)" : "var(--red)"}">${totalRate}</td>
    </tr>`);

    tbody.innerHTML = rows.join("");
}

function renderReportIncomeBars(data) {
    const container = document.getElementById("report-income-bars");
    if (!data.income_categories || !data.income_categories.length) {
        container.innerHTML = '<div class="empty-state"><p>No income data</p></div>';
        return;
    }
    const total = data.income_categories.reduce((s, c) => s + c.total, 0);
    const max = data.income_categories[0].total;
    container.innerHTML = data.income_categories.map(item => {
        const pct = ((item.total / total) * 100).toFixed(1);
        const width = Math.max(2, (item.total / max) * 100).toFixed(1);
        const cat = categories.find(c => c.name === item.name && c.type === "income");
        return `<div class="cat-bar-row">
            <div class="cat-bar-label" title="${item.name}"><span class="cat-dot" style="background:${catDotColor(cat?.id)}"></span>${item.name}</div>
            <div class="cat-bar-track"><div class="cat-bar-fill" style="width:${width}%;background:${rgbaVar("--green", 0.55)}"></div></div>
            <div class="cat-bar-amount">${fmt(item.total)} <span class="cat-bar-pct-inline">· ${pct}%</span></div>
        </div>`;
    }).join("");
}

function renderReportTopIncome(data) {
    const tbody = document.getElementById("report-top-income");
    if (!data.top_income || !data.top_income.length) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;padding:24px;color:var(--text-tertiary)">No income</td></tr>`;
        return;
    }
    tbody.innerHTML = data.top_income.map((t, i) => `<tr>
        <td style="color:var(--text-tertiary);font-weight:600">${i + 1}</td>
        <td>${fmtDate(t.date)}</td>
        <td>${t.store || "—"}</td>
        <td><span class="category-tag">${escapeHtml(t.category_name)}</span></td>
        <td style="text-align:right" class="amount income">+${fmt2(t.amount)}</td>
    </tr>`).join("");
}

function renderReportTopTransactions(data) {
    const tbody = document.getElementById("report-top-transactions");
    if (!data.top_transactions.length) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;padding:24px;color:var(--text-tertiary)">No transactions</td></tr>`;
        return;
    }
    tbody.innerHTML = data.top_transactions.map((t, i) => `<tr>
        <td style="color:var(--text-tertiary);font-weight:600">${i + 1}</td>
        <td>${fmtDate(t.date)}</td>
        <td>${t.store || "—"}</td>
        <td><span class="category-tag">${escapeHtml(t.category_name)}</span></td>
        <td style="text-align:right" class="amount expense">${fmt2(t.amount)}</td>
    </tr>`).join("");
}

function chartOptions() {
    const t = chartTheme();
    return {
        responsive: true,
        maintainAspectRatio: false,
        // Gentle fade/grow only. Disabling the x-axis animation stops line and
        // bar charts from sweeping in left-to-right every time a tab is opened
        // (charts are re-created on each tab switch, so the intro replays).
        animation: { duration: 400, easing: "easeOutQuart" },
        animations: { x: { duration: 0 } },
        interaction: { mode: "index", intersect: false },
        plugins: {
            legend: {
                labels: {
                    padding: 16, usePointStyle: true, pointStyle: "circle",
                    font: { size: 11, family: "-apple-system, BlinkMacSystemFont, sans-serif" },
                    color: t.text,
                },
            },
            tooltip: {
                backgroundColor: t.tooltipBg,
                titleColor: t.tooltipText, bodyColor: t.tooltipText,
                titleFont: { size: 12, family: "-apple-system, BlinkMacSystemFont, sans-serif", weight: 600 },
                bodyFont:  { size: 12, family: "-apple-system, BlinkMacSystemFont, sans-serif" },
                padding: 10, cornerRadius: 8,
                borderColor: t.tooltipBorder, borderWidth: 0.5,
            },
        },
        scales: {
            x: {
                grid: { display: false },
                ticks: { font: { size: 10, family: "-apple-system, BlinkMacSystemFont, sans-serif" }, color: t.tick },
                border: { display: false },
            },
            y: {
                grid: { color: t.grid, lineWidth: 0.5 },
                ticks: { font: { size: 10, family: "-apple-system, BlinkMacSystemFont, sans-serif" }, color: t.tick, callback: v => fmt(v) },
                border: { display: false },
            },
        },
    };
}

