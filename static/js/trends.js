// Balance — trends.js
//
// The Trends page: the category picker, its charts and its drilldowns.
//
// One of the ordered classic scripts index.html loads. They share one global
// scope on purpose: index.html wires ~95 inline handlers straight to these
// names, so these are plain scripts and not modules.

// ── Trends ───────────────────────────────────────────────────────────
let trendsSelectedCatIds  = [];
let trendsPeriod          = 12;
let trendsTopTxPeriod     = 12;
let trendsCachedData      = null;
let trendsForecastAllData = null;
let trendsForecastKey     = null;
let trendsTopTxItems      = [];
let trendsTopTxSort       = { col: "amount", dir: "desc" };

// ── Trends category multi-select ─────────────────────────────────────
function toggleTrendsCatDropdown() {
    const dd   = document.getElementById("trends-cat-dropdown");
    const open = dd.style.display !== "none";
    dd.style.display = open ? "none" : "block";
    if (!open) {
        document.getElementById("trends-cat-search").focus();
        setTimeout(() => document.addEventListener("click", _closeTrendsCatDd, true), 0);
    }
}
function _closeTrendsCatDd(e) {
    if (!document.getElementById("trends-cat-selector").contains(e.target)) {
        document.getElementById("trends-cat-dropdown").style.display = "none";
        document.removeEventListener("click", _closeTrendsCatDd, true);
    }
}
function filterTrendsCatOptions() {
    const q = document.getElementById("trends-cat-search").value.toLowerCase();
    document.querySelectorAll(".trends-cat-opt").forEach(el => {
        el.style.display = el.dataset.name.toLowerCase().includes(q) ? "" : "none";
    });
}
function clearTrendsCats() {
    document.querySelectorAll(".trends-cat-opt input").forEach(cb => cb.checked = false);
}
async function applyTrendsCats() {
    const checked = [...document.querySelectorAll(".trends-cat-opt input:checked")];
    trendsSelectedCatIds = checked.map(cb => parseInt(cb.value));
    document.getElementById("trends-cat-dropdown").style.display = "none";
    const btnSpan = document.getElementById("trends-cat-btn").querySelector("span");
    if (!trendsSelectedCatIds.length) {
        btnSpan.textContent = "Select categories…";
    } else if (trendsSelectedCatIds.length === 1) {
        btnSpan.textContent = checked[0].closest(".trends-cat-opt").dataset.name;
    } else {
        btnSpan.textContent = `${trendsSelectedCatIds.length} categories`;
    }
    await loadTrendsData();
}

async function loadTrends() {
    const opts = document.getElementById("trends-cat-options");
    if (!opts.children.length) {
        const cats    = categories.length ? categories : await api("/api/categories");
        const expense = cats.filter(c => c.type === "expense");
        const income  = cats.filter(c => c.type === "income");
        const makeSection = (label, list) => {
            if (!list.length) return "";
            return `<div style="padding:5px 10px 2px;font-size:10px;font-weight:700;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.6px">${label}</div>` +
                list.map(c => `<label class="trend-option trends-cat-opt" data-name="${c.name.replace(/"/g,"&quot;")}" style="display:flex;align-items:center;gap:8px;cursor:pointer;padding:6px 10px">
                    <input type="checkbox" value="${c.id}" style="width:14px;height:14px;flex-shrink:0;cursor:pointer">
                    <span style="font-size:13px">${escapeHtml(c.name)}</span>
                </label>`).join("");
        };
        opts.innerHTML = makeSection("Expenses", expense) + makeSection("Income", income);
    }
    loadTrendsData();
}

// ── Shared drilldown modal ────────────────────────────────────────────
function openTrendsDrilldownModal(title, rows) {
    const total     = rows.reduce((s, r) => s + r.amount, 0);
    const tableRows = rows.map(r => `
        <tr>
            <td style="font-size:13px;white-space:nowrap">${fmtDate(r.date)}</td>
            <td style="font-size:13px">${r.store || "—"}</td>
            <td style="font-size:13px"><span class="category-tag">${r.category_name || ""}</span></td>
            <td class="amount" style="font-size:13px;text-align:right;white-space:nowrap">${fmt2(r.amount)}</td>
        </tr>`).join("");
    document.body.insertAdjacentHTML("beforeend", `<div class="modal-overlay" onclick="if(event.target===this)this.remove()">
        <div class="modal" style="max-width:640px">
            <div class="modal-title">${title}</div>
            <div style="max-height:460px;overflow-y:auto;margin:0 -4px">
                <table style="width:100%">
                    <thead><tr>
                        <th style="font-size:12px">Date</th><th style="font-size:12px">Store</th>
                        <th style="font-size:12px">Category</th><th style="font-size:12px;text-align:right">Amount</th>
                    </tr></thead>
                    <tbody>${tableRows}</tbody>
                    <tfoot><tr style="border-top:1px solid var(--border)">
                        <td colspan="3" style="font-size:13px;font-weight:600;padding-top:8px">Total (${rows.length} transactions)</td>
                        <td style="font-size:13px;font-weight:600;padding-top:8px;text-align:right" class="amount">${fmt(total)}</td>
                    </tr></tfoot>
                </table>
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
            </div>
        </div>
    </div>`);
}

async function openTrendsMonthDrilldown(month) {
    const res     = await api(`/api/transactions?category_ids=${trendsSelectedCatIds.join(",")}&month=${month}&sort=amount&dir=desc&per_page=500`);
    const catName = trendsCachedData ? trendsCachedData.category.name : "Category";
    openTrendsDrilldownModal(`${catName} — ${monthLabelFull(month)}`, res.items || []);
}

async function openTrendsMerchantDrilldown(store) {
    let url = `/api/transactions?category_ids=${trendsSelectedCatIds.join(",")}&q=${encodeURIComponent(store)}&sort=amount&dir=desc&per_page=500`;
    if (trendsPeriod > 0) {
        const d = new Date();
        d.setMonth(d.getMonth() - trendsPeriod);
        url += `&date_from=${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-01`;
    }
    const res = await api(url);
    openTrendsDrilldownModal(store, res.items || []);
}

// ── Utilities ─────────────────────────────────────────────────────────
function trimTrailingZeros(monthly) {
    let end = monthly.length - 1;
    while (end > 0 && monthly[end].count === 0) end--;
    return monthly.slice(0, end + 1);
}

// ── Render functions ──────────────────────────────────────────────────
function renderTrendsStatCards(data) {
    const s         = data.stats;
    const isIncome  = data.category.type === "income";
    const container = document.getElementById("trends-stat-cards");
    container.innerHTML = `
        <div class="summary-card"><div class="label">${isIncome ? "Total Earned" : "Total Spent"}</div><div class="value ${isIncome ? "income" : "expense"}">${fmt(s.total)}</div></div>
        <div class="summary-card"><div class="label">Avg / Month</div><div class="value">${fmt(s.avg_monthly)}</div></div>
        <div class="summary-card"><div class="label">Transactions</div><div class="value">${s.tx_count}</div></div>
        <div class="summary-card"><div class="label">Avg / Transaction</div><div class="value">${fmt(s.avg_per_tx)}</div></div>`;
}

function renderTrendsMonthlyChart(data) {
    // The stat card above already says "Total Earned" for an income category;
    // this title said "Spending by Month" underneath it whatever was selected.
    const title = document.getElementById("trends-monthly-title");
    if (title) {
        title.textContent = data.category && data.category.type === "income"
            ? "Income by Month" : "Spending by Month";
    }
    if (charts.trendsMonthly) charts.trendsMonthly.destroy();
    const ctx      = document.getElementById("chart-trends-monthly");
    const monthly = trimTrailingZeros(data.monthly);
    const months  = monthly.map(r => r.month);
    const totals   = monthly.map(r => r.total);
    const counts   = monthly.map(r => r.count);
    const nonZero  = totals.filter(v => v > 0);
    const avg      = nonZero.length ? nonZero.reduce((a, b) => a + b, 0) / nonZero.length : 0;
    charts.trendsMonthly = new Chart(ctx, {
        type: "bar",
        data: {
            labels: months.map(monthLabel),
            datasets: [
                {
                    label: data.category.name, data: totals,
                    backgroundColor: totals.map(v => v === 0 ? "rgba(142,142,147,0.2)" : rgbaVar("--accent", 0.8)),
                    borderRadius: 6, borderSkipped: false,
                },
                {
                    label: "Average", data: months.map(() => avg), type: "line",
                    borderColor: rgbaVar("--accent", 1), backgroundColor: "transparent",
                    borderWidth: 2, borderDash: [5, 4], pointRadius: 0, tension: 0,
                },
            ],
        },
        options: {
            ...chartOptions(),
            onClick(event, elements) {
                if (!elements.length || elements[0].datasetIndex !== 0) return;
                const idx = elements[0].index;
                if (counts[idx] > 0) openTrendsMonthDrilldown(months[idx]);
            },
            onHover(event, elements) {
                const t = event.native?.target;
                if (t) t.style.cursor = (elements.length && elements[0].datasetIndex === 0 && counts[elements[0].index] > 0) ? "pointer" : "default";
            },
            plugins: {
                ...chartOptions().plugins,
                tooltip: {
                    ...chartOptions().plugins.tooltip,
                    callbacks: {
                        label: ctx => ` ${fmt(ctx.parsed.y)}`,
                        afterLabel: ctx => ctx.datasetIndex === 0 && counts[ctx.dataIndex] > 0 ? `  ${counts[ctx.dataIndex]} transactions — click to view` : "",
                    },
                },
            },
        },
    });
}

function renderTrendsMomChart(data) {
    if (charts.trendsMom) charts.trendsMom.destroy();
    const container = document.getElementById("chart-trends-mom").parentElement;
    if (!container.querySelector("canvas")) container.innerHTML = '<canvas id="chart-trends-mom"></canvas>';
    const ctx     = document.getElementById("chart-trends-mom");
    const monthly = trimTrailingZeros(data.monthly);
    if (monthly.length < 2) {
        container.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-tertiary);font-size:13px">Need at least 2 months of data</div>';
        return;
    }
    const labels = [], months = [], changes = [], prevAmts = [], currAmts = [];
    for (let i = 1; i < monthly.length; i++) {
        const prev = monthly[i - 1].total, curr = monthly[i].total;
        labels.push(monthLabel(monthly[i].month));
        months.push(monthly[i].month);
        changes.push(prev > 0 ? ((curr - prev) / prev * 100) : (curr > 0 ? 100 : 0));
        prevAmts.push(prev); currAmts.push(curr);
    }
    const rolling = changes.map((_, i) => {
        const win = changes.slice(Math.max(0, i - 1), i + 2);
        return win.reduce((a, b) => a + b, 0) / win.length;
    });
    // Up is not always bad. On an income category a rise is the good outcome,
    // so the colours follow the category's type instead of assuming spending.
    // The endpoint already resolves the type for a multi-category selection.
    const upIsGood  = data.category && data.category.type === "income";
    const upColor   = upIsGood ? "--green" : "--red";
    const downColor = upIsGood ? "--red"   : "--green";

    const labelPlugin = {
        id: "momPctLabels",
        afterDatasetsDraw(chart) {
            const meta = chart.getDatasetMeta(0);
            const { ctx: c } = chart;
            c.save();
            c.font = "bold 10px -apple-system, BlinkMacSystemFont, sans-serif";
            c.textAlign = "center";
            meta.data.forEach((bar, i) => {
                const v = changes[i];
                c.fillStyle = v >= 0 ? rgbaVar(upColor, 1) : rgbaVar(downColor, 1);
                c.fillText((v >= 0 ? "+" : "") + v.toFixed(1) + "%", bar.x, bar.y + (v >= 0 ? -7 : 13));
            });
            c.restore();
        },
    };
    charts.trendsMom = new Chart(ctx, {
        type: "bar", plugins: [labelPlugin],
        data: {
            labels,
            datasets: [
                {
                    label: "MoM Change", data: changes,
                    backgroundColor: changes.map(v => v >= 0 ? rgbaVar(upColor, 0.7) : rgbaVar(downColor, 0.7)),
                    borderColor:     changes.map(v => v >= 0 ? rgbaVar(upColor, 1)   : rgbaVar(downColor, 1)),
                    borderWidth: 1, borderRadius: 5, borderSkipped: false, maxBarThickness: 28,
                },
                {
                    label: "3M Avg", data: rolling, type: "line",
                    borderColor: rgbaVar("--accent", 0.9), backgroundColor: "transparent",
                    borderWidth: 2, pointRadius: 3, pointBackgroundColor: rgbaVar("--accent", 1), tension: 0.4, order: -1,
                },
            ],
        },
        options: {
            ...chartOptions(),
            layout: { padding: { top: 18 } },
            onClick(event, elements) {
                if (!elements.length || elements[0].datasetIndex !== 0) return;
                openTrendsMonthDrilldown(months[elements[0].index]);
            },
            onHover(event, elements) {
                const t = event.native?.target;
                if (t) t.style.cursor = (elements.length && elements[0].datasetIndex === 0) ? "pointer" : "default";
            },
            plugins: {
                ...chartOptions().plugins,
                tooltip: {
                    ...chartOptions().plugins.tooltip,
                    callbacks: {
                        title: ctxArr => ctxArr[0].label,
                        label: ctx => {
                            if (ctx.datasetIndex === 1) return ` 3M avg: ${ctx.parsed.y.toFixed(1)}%`;
                            const i   = ctx.dataIndex;
                            const sgn = changes[i] >= 0 ? "+" : "";
                            return [` Change: ${sgn}${changes[i].toFixed(1)}%`, ` Previous: ${fmt(prevAmts[i])}`, ` Current:  ${fmt(currAmts[i])}`];
                        },
                    },
                },
            },
            scales: {
                ...chartOptions().scales,
                y: {
                    ...chartOptions().scales.y,
                    grid: {
                        color:     ctx => ctx.tick.value === 0 ? chartTheme().gridZero : chartTheme().grid,
                        lineWidth: ctx => ctx.tick.value === 0 ? 1.5 : 0.5,
                    },
                    ticks: { ...chartOptions().scales.y.ticks, callback: v => v.toFixed(0) + "%" },
                },
            },
        },
    });
}

function renderTrendsMerchants(data) {
    const container = document.getElementById("trends-merchants");
    if (!data.top_merchants.length) {
        container.innerHTML = '<div style="text-align:center;padding:24px;color:var(--text-tertiary);font-size:13px">No data</div>';
        return;
    }
    const max = data.top_merchants[0].total;
    // Merchants have no identity color, so one muted bar color reads better
    // than a rainbow (same reasoning as design #8).
    container.innerHTML = data.top_merchants.map(m => {
        const width = Math.max(2, (m.total / max) * 100).toFixed(1);
        const safe  = m.store.replace(/'/g, "\\'");
        return `<div class="cat-bar-row" style="cursor:pointer" title="Click to view transactions" onclick="openTrendsMerchantDrilldown('${safe}')">
            <div class="cat-bar-label" title="${escapeHtml(m.store)}">${escapeHtml(m.store)}</div>
            <div class="cat-bar-track"><div class="cat-bar-fill" style="width:${width}%;background:${rgbaVar("--accent", 0.55)}"></div></div>
            <div class="cat-bar-amount">${fmt(m.total)} <span class="cat-bar-pct-inline">· ${m.count}×</span></div>
        </div>`;
    }).join("");
}

function renderTrendsFreqChart(data) {
    if (charts.trendsFreq) charts.trendsFreq.destroy();
    const ctx     = document.getElementById("chart-trends-freq");
    const monthly = trimTrailingZeros(data.monthly);
    const months  = monthly.map(r => r.month);
    const counts  = monthly.map(r => r.count);
    const avg     = counts.reduce((a, b) => a + b, 0) / (counts.length || 1);
    charts.trendsFreq = new Chart(ctx, {
        type: "line",
        data: {
            labels: months.map(monthLabel),
            datasets: [
                {
                    label: "Transactions", data: counts,
                    borderColor: rgbaVar("--purple", 1), backgroundColor: rgbaVar("--purple", 0.1),
                    borderWidth: 2, pointRadius: 4, tension: 0.3, fill: true,
                },
                {
                    label: "Average", data: months.map(() => avg),
                    borderColor: rgbaVar("--accent", 1), backgroundColor: "transparent",
                    borderWidth: 1.5, borderDash: [5, 4], pointRadius: 0, tension: 0,
                },
            ],
        },
        options: {
            ...chartOptions(),
            onClick(event, elements) {
                if (!elements.length || elements[0].datasetIndex !== 0) return;
                const idx = elements[0].index;
                if (counts[idx] > 0) openTrendsMonthDrilldown(months[idx]);
            },
            onHover(event, elements) {
                const t = event.native?.target;
                if (t) t.style.cursor = (elements.length && elements[0].datasetIndex === 0 && counts[elements[0].index] > 0) ? "pointer" : "default";
            },
            plugins: {
                ...chartOptions().plugins,
                tooltip: {
                    ...chartOptions().plugins.tooltip,
                    callbacks: {
                        label: ctx => ` ${ctx.parsed.y} transactions`,
                        afterLabel: ctx => ctx.datasetIndex === 0 && counts[ctx.dataIndex] > 0 ? "  click to view" : "",
                    },
                },
            },
            scales: {
                ...chartOptions().scales,
                y: { ...chartOptions().scales.y, ticks: { ...chartOptions().scales.y.ticks, callback: v => v % 1 === 0 ? v : "" } },
            },
        },
    });
}

function linReg(values) {
    const n = values.length;
    if (n < 2) return { slope: 0, intercept: values[0] || 0 };
    let sumX = 0, sumY = 0, sumXY = 0, sumX2 = 0;
    for (let i = 0; i < n; i++) { sumX += i; sumY += values[i]; sumXY += i * values[i]; sumX2 += i * i; }
    const slope = (n * sumXY - sumX * sumY) / (n * sumX2 - sumX * sumX);
    return { slope, intercept: (sumY - slope * sumX) / n };
}

function renderTrendsForecast(data) {
    if (charts.trendsForecast) charts.trendsForecast.destroy();
    const ctx  = document.getElementById("chart-trends-forecast");
    const hist = trimTrailingZeros(data.monthly);
    const totals = hist.map(r => r.total);
    const n = totals.length;
    const { slope, intercept } = linReg(totals);

    // Project 12 months forward from the last month with data
    const [lastY, lastM] = hist[n - 1].month.split("-").map(Number);
    const futureMonths = [], futureVals = [];
    for (let i = 1; i <= 12; i++) {
        let fm = lastM + i, fy = lastY;
        while (fm > 12) { fm -= 12; fy++; }
        futureMonths.push(`${fy}-${String(fm).padStart(2, "0")}`);
        futureVals.push(Math.max(0, slope * (n - 1 + i) + intercept));
    }

    const trendLine    = Array.from({ length: n + 12 }, (_, i) => Math.max(0, slope * i + intercept));
    const forecastTotal = futureVals.reduce((a, b) => a + b, 0);
    const histAvg      = totals.reduce((a, b) => a + b, 0) / (n || 1);
    const trendDir     = slope > histAvg * 0.01 ? "↑ Increasing" : slope < -histAvg * 0.01 ? "↓ Decreasing" : "→ Stable";
    // As on the month-over-month bars: rising income is good news, rising
    // spending is not, so the colour has to know which one it is looking at.
    const rising       = slope > histAvg * 0.01;
    const falling      = slope < -histAvg * 0.01;
    const goodUp       = data.category && data.category.type === "income";
    const trendColor   = rising  ? (goodUp ? "var(--green)" : "var(--red)")
                       : falling ? (goodUp ? "var(--red)" : "var(--green)")
                       : "var(--text-secondary)";

    document.getElementById("trends-forecast-summary").innerHTML = `
        <span>Next 12M: <strong>${fmt(forecastTotal)}</strong></span>
        <span>Avg/mo: <strong>${fmt(forecastTotal / 12)}</strong></span>
        <span style="color:${trendColor};font-weight:600">${trendDir}</span>`;

    charts.trendsForecast = new Chart(ctx, {
        type: "bar",
        data: {
            labels: [...hist.map(r => monthLabel(r.month)), ...futureMonths.map(monthLabel)],
            datasets: [
                {
                    label: "Historical", data: [...totals, ...Array(12).fill(null)],
                    backgroundColor: rgbaVar("--accent", 0.75), borderRadius: 5, borderSkipped: false, order: 2,
                },
                {
                    // Hollow, dashed bars mark these as projected (vs. solid historical).
                    label: "Forecast", data: [...Array(n).fill(null), ...futureVals],
                    backgroundColor: rgbaVar("--accent", 0.12), borderColor: rgbaVar("--accent", 0.9),
                    borderWidth: 1.5, borderDash: [5, 3], borderRadius: 5, borderSkipped: false, order: 2,
                },
                {
                    label: "Trend", data: trendLine, type: "line",
                    borderColor: rgbaVar("--purple", 1), backgroundColor: "transparent",
                    borderWidth: 2, borderDash: [6, 3], pointRadius: 0, tension: 0, order: 1,
                },
            ],
        },
        options: {
            ...chartOptions(),
            onClick(event, elements) {
                if (!elements.length || elements[0].datasetIndex !== 0) return;
                const idx = elements[0].index;
                if (hist[idx]?.count > 0) openTrendsMonthDrilldown(hist[idx].month);
            },
            onHover(event, elements) {
                const t = event.native?.target;
                if (t) t.style.cursor = (elements.length && elements[0].datasetIndex === 0 && hist[elements[0].index]?.count > 0) ? "pointer" : "default";
            },
            plugins: {
                ...chartOptions().plugins,
                tooltip: {
                    ...chartOptions().plugins.tooltip,
                    callbacks: { label: ctx => ctx.parsed.y != null ? ` ${fmt(ctx.parsed.y)}` : "" },
                },
            },
        },
    });
}

function renderTrendsTopTx(items) {
    trendsTopTxItems = items || [];
    applyTrendsTopTxSort();
}

function sortTrendsTopTx(col) {
    if (trendsTopTxSort.col === col) {
        trendsTopTxSort.dir = trendsTopTxSort.dir === "asc" ? "desc" : "asc";
    } else {
        trendsTopTxSort = { col, dir: col === "amount" ? "desc" : "asc" };
    }
    applyTrendsTopTxSort();
}

function applyTrendsTopTxSort() {
    const { col, dir } = trendsTopTxSort;
    const sorted = [...trendsTopTxItems].sort((a, b) => {
        let va = a[col] ?? "", vb = b[col] ?? "";
        if (col === "amount") { va = +va; vb = +vb; }
        const cmp = va < vb ? -1 : va > vb ? 1 : 0;
        return dir === "asc" ? cmp : -cmp;
    });

    // Update header icons
    ["date", "store", "amount"].forEach(c => {
        const th = document.getElementById(`toptx-th-${c}`);
        if (!th) return;
        const icon = th.querySelector(".sort-icon");
        icon.textContent = col === c ? (dir === "asc" ? " ↑" : " ↓") : "";
        th.style.color = col === c ? "var(--accent)" : "";
    });

    const tbody = document.getElementById("trends-top-tx-body");
    if (!sorted.length) {
        tbody.innerHTML = `<tr><td colspan="4" style="text-align:center;padding:24px;color:var(--text-tertiary)">No transactions</td></tr>`;
        return;
    }
    tbody.innerHTML = sorted.map((t, i) => `<tr>
        <td style="color:var(--text-tertiary);font-weight:600">${i + 1}</td>
        <td>${fmtDate(t.date)}</td>
        <td>${t.store || "—"}</td>
        <td style="text-align:right" class="amount expense">${fmt2(t.amount)}</td>
    </tr>`).join("");
}

