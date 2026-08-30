// Balance — dashboard.js
//
// The Dashboard: month notes, the cards, the category breakdowns and
// their baselines, the summary table and the heatmap.
//
// One of the ordered classic scripts index.html loads. They share one global
// scope on purpose: index.html wires ~95 inline handlers straight to these
// names, so these are plain scripts and not modules.

// ── Month Notes ─────────────────────────────────────────────────────
async function loadMonthsWithNotes() {
    const months = await api("/api/notes");
    monthsWithNotes = new Set(months);
}

function openNoteModal(month) {
    api(`/api/notes/${month}`).then(data => {
        const label = monthLabelFull(month);
        const note  = data.note || "";
        const html = `<div class="modal-overlay" onclick="if(event.target===this)this.remove()">
            <div class="modal">
                <div class="modal-title">Note for ${label}</div>
                <div class="form-group">
                    <textarea class="note-textarea" id="modal-note-text" placeholder="Add a note for this month…">${note}</textarea>
                </div>
                <div class="modal-actions">
                    <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
                    <button class="btn btn-primary" onclick="saveNote('${month}')">Save</button>
                </div>
            </div>
        </div>`;
        document.body.insertAdjacentHTML("beforeend", html);
        document.getElementById("modal-note-text").focus();
    });
}

async function saveNote(month) {
    const note = document.getElementById("modal-note-text").value;
    await api(`/api/notes/${month}`, { method: "PUT", body: { note } });
    if (note.trim()) monthsWithNotes.add(month);
    else monthsWithNotes.delete(month);
    document.querySelector(".modal-overlay").remove();
    renderSummaryTable(cachedMonthly);
    toast("Note saved");
}

// ── Dashboard ───────────────────────────────────────────────────────
// CHART_COLORS (the active category palette) is defined with the palette
// registry near the top of the file and is user-selectable in Settings.

let dashboardHorizon = "ytd";
let selectedPeriods = new Set(); // Set of "YYYY-MM" strings
let breakdownMonths = [];        // active months shown in the category bar chart
let periodYearExpanded = {};     // which years are expanded in the dropdown
let yearViewCollapsed = {};
let showYearView = false;
let cachedMonthly = [];

// ── Data filter ─────────────────────────────────────────────────────
function filterData(monthly) {
    if (selectedPeriods.size > 0) {
        return monthly.filter(r => selectedPeriods.has(r.month));
    }
    const allMonths = [...new Set(monthly.map(r => r.month))].sort();
    if (!dashboardHorizon || dashboardHorizon === "0") return monthly;
    if (dashboardHorizon === "ytd") {
        const year = new Date().getFullYear().toString();
        return monthly.filter(r => r.month.startsWith(year));
    }
    const n = parseInt(dashboardHorizon);
    const cutoff = allMonths.slice(-n);
    return monthly.filter(r => cutoff.includes(r.month));
}

function filterByHorizon(monthly) { return filterData(monthly); }

async function loadDashboard() {
    const [monthly, topExpenses] = await Promise.all([
        api("/api/dashboard/monthly-summary"),
        api("/api/dashboard/top-expenses"),
    ]);

    // The breakdown waits for the monthly rows: it is asked for the months the
    // period controls currently cover, and those months come from this data.
    cachedMonthly = monthly;
    const filtered = filterData(monthly);
    const catBreakdown = await api(expenseBreakdownUrl());

    await loadMonthsWithNotes();

    renderSummaryCards(filtered);
    renderMonthlyChart(filtered);
    renderCategoryBars(catBreakdown);
    renderTrendsChart(topExpenses);
    renderSummaryTable(monthly);

    await loadIncomeBreakdown(catBreakdown);
    await loadHeatmap();
}

// Both breakdown cards open on the latest month alone, and while they do they
// ignore the period controls at the top of the page. Switch either to Period
// and both follow those controls like every other card. One scope for the two
// of them: they answer the same question about the same months, and a pair that
// could disagree is the bug this whole function exists to prevent.
let breakdownScope = "latest";   // "latest" | "period"

async function setBreakdownScope(scope) {
    breakdownScope = scope;
    const catBreakdown = await api(expenseBreakdownUrl());
    renderCategoryBars(catBreakdown);
    await loadIncomeBreakdown(catBreakdown);
}

// Which months the two cards currently describe. On "latest", the latest month
// there is data for — not the latest month the period covers. Otherwise the
// explicit month picks if there are any, else every month the horizon covers.
// Empty only before the monthly rows have loaded, where the endpoint's own
// fallback (the latest month with data) is the best guess available.
function breakdownPeriodMonths() {
    if (breakdownScope === "latest") {
        return [...new Set(cachedMonthly.map(r => r.month))].sort().slice(-1);
    }
    if (selectedPeriods.size > 0) return [...selectedPeriods].sort();
    if (!cachedMonthly.length) return [];
    return [...new Set(filterData(cachedMonthly).map(r => r.month))].sort();
}

// The expense card decides the period; the income card is pinned to it.
function expenseBreakdownUrl() {
    const months = breakdownPeriodMonths();
    let url = "/api/dashboard/category-breakdown";
    if (months.length) url += `?months=${months.join(",")}`;
    return url;
}

// Income is pinned to whatever period the expense card resolved to, so the
// two cards can never end up describing different months.
async function loadIncomeBreakdown(expenseBreakdown) {
    const params = new URLSearchParams({ type: "income" });
    const months = breakdownPeriodMonths();
    if (months.length)               params.set("months", months.join(","));
    else if (expenseBreakdown.month) params.set("month", expenseBreakdown.month);
    renderIncomeBars(await api(`/api/dashboard/category-breakdown?${params}`));
}

function renderSummaryCards(monthly) {
    const container = document.getElementById("summary-cards");
    const months = [...new Set(monthly.map(r => r.month))].sort();
    const latest = months[months.length - 1];

    if (!latest) {
        container.innerHTML = `
            <div class="summary-card"><div class="label">Income</div><div class="value income">${fmt(0)}</div></div>
            <div class="summary-card"><div class="label">Expenses</div><div class="value expense">${fmt(0)}</div></div>
            <div class="summary-card"><div class="label">Net</div><div class="value">${fmt(0)}</div></div>`;
        return;
    }

    const totalIncome  = monthly.filter(r => r.type === "income").reduce((s, r) => s + r.total, 0);
    const totalExpense = monthly.filter(r => r.type === "expense").reduce((s, r) => s + r.total, 0);
    const totalInvest  = monthly.filter(r => r.type === "investment").reduce((s, r) => s + r.total, 0);
    const net = totalIncome - totalExpense;
    const avgExpense = totalExpense / months.length;
    // Savings rate treats money moved into Investments as saved, not spent —
    // so it's added back on top of the plain net (income − all expenses).
    const savedNet = net + totalInvest;
    const savingsRate = totalIncome > 0 ? ((savedNet / totalIncome) * 100).toFixed(1) : "0.0";

    container.innerHTML = `
        <div class="summary-card"><div class="label">Total Income</div><div class="value income">+${fmt(totalIncome)}</div></div>
        <div class="summary-card"><div class="label">Total Expenses</div><div class="value expense">${fmt(totalExpense)}</div></div>
        <div class="summary-card"><div class="label">Net</div><div class="value net ${net >= 0 ? "positive" : "negative"}">${net >= 0 ? "+" : ""}${fmt(net)}</div></div>
        <div class="summary-card"><div class="label">Savings Rate</div><div class="value ${savedNet >= 0 ? "positive" : "negative"}" title="Net savings incl. money invested, as a share of income">${savingsRate}%</div></div>
        <div class="summary-card"><div class="label">Avg. Monthly Expense</div><div class="value expense">${fmt(avgExpense)}</div></div>`;
}

function renderMonthlyChart(monthly) {
    const ctx = document.getElementById("chart-monthly");
    if (charts.monthly) charts.monthly.destroy();
    const theme = chartTheme();
    const onAccent = cssVar("--on-accent");

    const months = [...new Set(monthly.map(r => r.month))].sort();
    const incomeData  = months.map(m => monthly.filter(r => r.month === m && r.type === "income").reduce((s, r) => s + r.total, 0));
    const expenseData = months.map(m => monthly.filter(r => r.month === m && r.type === "expense").reduce((s, r) => s + r.total, 0));
    const diffData    = months.map((_, i) => incomeData[i] - expenseData[i]);
    const yMax        = Math.max(...incomeData, ...expenseData) * 1.15;

    const fontSize   = months.length > 18 ? 9 : months.length > 12 ? 10 : 11;
    // Without a cap, one month's two bars stretch to half the card each. The cap
    // alone is not enough there: with a single category Chart.js measures the
    // slot from the gap between neighbours, has none to measure, and on the
    // first paint after load settles on a sliver. An explicit thickness skips
    // that measurement, so the bar is the width we asked for either way.
    const barSizing = months.length === 1
        ? { barThickness: 72 }
        : { maxBarThickness: 72 };
    const diffPlugin = {
        id: "monthlyDiffLabels",
        afterDatasetsDraw(chart) {
            const { ctx: c, scales } = chart;
            const m0 = chart.getDatasetMeta(0);
            const m1 = chart.getDatasetMeta(1);
            const bottom = chart.chartArea.bottom;
            c.save();
            c.textAlign = "center";
            c.textBaseline = "middle";

            // Inside-bar value labels — drawn only when the value fits INSIDE the
            // bar both vertically (tall enough) and horizontally (narrower than the
            // bar), so the number never spills outside a slim bar.
            const barFs = months.length > 18 ? 8 : months.length > 12 ? 9 : 10;
            c.font = `600 ${barFs}px -apple-system, BlinkMacSystemFont, sans-serif`;
            [[m0, incomeData, onAccent], [m1, expenseData, "rgba(255,255,255,0.92)"]].forEach(([meta, data, color]) => {
                data.forEach((val, i) => {
                    if (!val) return;
                    const bar = meta.data[i];
                    if (!bar) return;
                    const barH = bottom - bar.y;
                    const label = fmt(val);
                    const labelH = barFs + 4;
                    if (barH < labelH + 8) return;                      // bar too short
                    if (c.measureText(label).width > bar.width - 4) return; // too narrow
                    c.fillStyle = color;
                    c.fillText(label, bar.x, bar.y + labelH);
                });
            });

            // Net diff badge above each month — drawn only when the pill fits within
            // the month's column width, so badges never overlap or spill on dense
            // ranges (e.g. 18+ months). Exact values are always in the tooltip.
            const padX = 6, padY = 3, radius = 4, gap = 8;
            const slotW = (chart.chartArea.right - chart.chartArea.left) / Math.max(1, months.length);
            c.font = `600 ${fontSize}px -apple-system, BlinkMacSystemFont, sans-serif`;
            diffData.forEach((diff, i) => {
                const b0 = m0.data[i], b1 = m1.data[i];
                if (!b0 || !b1) return;
                const label  = (diff >= 0 ? "+" : "") + fmt(diff);
                const x      = scales.x.getPixelForValue(i);
                const bh     = fontSize + padY * 2;
                const topBar = Math.min(b0.y, b1.y);
                const cy     = Math.max(chart.chartArea.top + bh / 2 + 2, topBar - gap - bh / 2);
                const w      = c.measureText(label).width;
                if (w + padX * 2 > slotW - 2) return;                  // wouldn't fit the column
                const bx     = x - w / 2 - padX;
                const by     = cy - bh / 2;
                const bw     = w + padX * 2;
                const color  = diff >= 0 ? theme.green : theme.red;
                c.beginPath();
                c.roundRect(bx, by, bw, bh, radius);
                c.fillStyle   = diff >= 0 ? rgbaVar("--green", 0.14) : rgbaVar("--red", 0.12);
                c.fill();
                c.strokeStyle = diff >= 0 ? rgbaVar("--green", 0.45) : rgbaVar("--red", 0.40);
                c.lineWidth   = 1;
                c.stroke();
                c.fillStyle = color;
                c.fillText(label, x, cy);
            });
            c.restore();
        },
    };

    charts.monthly = new Chart(ctx, {
        type: "bar",
        plugins: [diffPlugin],
        data: {
            labels: months.map(monthLabel),
            datasets: [
                { label: "Income",   data: incomeData,  backgroundColor: rgbaVar("--accent", 0.85), borderRadius: 6, borderSkipped: false, ...barSizing },
                { label: "Expenses", data: expenseData, backgroundColor: rgbaVar("--red", 0.85), borderRadius: 6, borderSkipped: false, ...barSizing },
            ],
        },
        options: {
            ...chartOptions(),
            layout: { padding: { top: 44 } },
            scales: {
                x: { grid: { display: false }, ticks: { font: { size: 11, family: "-apple-system, BlinkMacSystemFont, sans-serif" }, color: theme.tick }, border: { display: false } },
                y: { display: false, max: yMax },
            },
            plugins: {
                ...chartOptions().plugins,
                tooltip: {
                    ...chartOptions().plugins.tooltip,
                    callbacks: {
                        afterBody(items) {
                            const i = items[0]?.dataIndex;
                            if (i === undefined) return "";
                            const diff = diffData[i];
                            return `\nDifference: ${diff >= 0 ? "+" : ""}${fmt(diff)}`;
                        },
                    },
                },
            },
        },
    });
}

// Expenses and Income share one renderer: same bars, same period, same
// drill-down. Only the data and the empty-state wording differ.
function renderCategoryBars(breakdown) { renderBreakdownBars("category-bars", breakdown, "expense"); }
function renderIncomeBars(breakdown)   { renderBreakdownBars("income-bars",   breakdown, "income"); }

function renderBreakdownBars(containerId, breakdown, type) {
    // Capture which months this breakdown covers so drill-down can match
    breakdownMonths = breakdown.months || (breakdown.month ? [breakdown.month] : []);
    document.querySelectorAll(".breakdown-scope-btn").forEach(b =>
        b.classList.toggle("active", b.dataset.scope === breakdownScope));

    const container = document.getElementById(containerId);
    if (!container) return;
    if (!breakdown.items || breakdown.items.length === 0) {
        container.innerHTML = `<div class="empty-state"><p>No ${type === "income" ? "income" : "expenses"} for this period</p></div>`;
        return;
    }
    const total = breakdown.items.reduce((s, i) => s + i.total, 0);
    // The track has to hold the baseline tick as well as the bar, so it is
    // scaled by whichever is larger. Scaling by the bars alone would push a
    // tick for an over-median month clean off the end of its own track.
    const max = Math.max(...breakdown.items.map(i => Math.max(i.total, i.median || 0)));
    // One quiet bar color; the category's identity color lives in the label
    // dot (design #8). Bars keep a minimum width so tail rows stay visible.
    const fill = rgbaVar(type === "income" ? "--green" : "--accent", 0.55);
    container.innerHTML = breakdown.items.map(item => {
        const pct   = ((item.total / total) * 100).toFixed(1);
        const width = Math.max(2, (item.total / max) * 100).toFixed(1);
        // Match the type as well as the name — "Other" and "Investments" exist
        // on both sides, and the wrong id would drill into the wrong category.
        const catId = categories.find(c => c.name === item.name && c.type === type)?.id ?? "";
        const { tick, delta, hot } = baselineMarks(item, max, type);
        return `<div class="cat-bar-row" style="cursor:pointer" onclick="openCategoryDrilldown(${catId},'${item.name.replace(/'/g,"\\'")}')">
            <div class="cat-bar-label"><span class="cat-dot" style="background:${catDotColor(catId)}"></span>${item.name}</div>
            <div class="cat-bar-track"><div class="cat-bar-fill${hot ? " over" : ""}" style="width:${width}%${hot ? "" : `;background:${fill}`}"></div>${tick}</div>
            <div class="cat-bar-amount">${fmt(item.total)} <span class="cat-bar-pct-inline">· ${pct}%</span>${delta}</div>
        </div>`;
    }).join("");
}

// How far off its median a category has to land before the card says anything
// (QUIET_BAND), and before the bar itself takes the warning colour
// (OUTLIER_BAND). Real spending swings; the bands are wide on purpose.
const QUIET_BAND = 0.25;
const OUTLIER_BAND = 0.50;

// "Normal" for a category, drawn on the same track as the month itself: a tick
// where the median sits, and how far this month lands from it. The endpoint
// sends a median only for a single month — over a longer period `median` is
// null and every one of these comes back empty, so the bars draw as they did
// before any of this existed.
function baselineMarks(item, max, type) {
    const empty = { tick: "", delta: "", hot: false };
    if (item.median == null) return empty;

    // A category that never moves has no news in it; saying "0%" beside rent
    // every month teaches the eye to skip the column that carries the news.
    if (item.fixed) return { ...empty, delta: `<span class="cat-bar-delta flat">fixed</span>` };

    // No usual to be off. Reporting a share of zero would be a divide by
    // nothing dressed up as a percentage.
    if (item.median === 0) {
        return { ...empty, delta: `<span class="cat-bar-delta flat" title="Nothing in this category in the six months before">unusual</span>` };
    }

    const ratio  = item.total / item.median;
    const change = ratio - 1;
    // A quarter either way is ordinary month-to-month movement, not a story.
    // It still gets a tick — seeing the month land near its own median is the
    // point — but no number. A tenth, which this used to be, painted most of
    // a real card red and taught the eye to ignore the colour.
    const quiet = Math.abs(change) < QUIET_BAND;
    // Up is bad on spending and good on income. The Trends page already knows
    // this; a rising salary painted red reads as a warning about earning more.
    const good = type === "income" ? change > 0 : change < 0;
    const tone = quiet ? "flat" : good ? "down" : "up";
    // Past a few times over, a percentage stops being readable — "+3726%" is
    // a category you barely ever buy, not a number anyone reads. Say it as a
    // multiple, which is both shorter and how you'd say it out loud.
    const text = quiet ? "as usual"
        : ratio >= 4 ? `${Math.round(ratio)}× usual`
        : `${change > 0 ? "+" : ""}${Math.round(change * 100)}%`;

    return {
        tick: `<span class="cat-bar-tick" style="left:${Math.min(100, (item.median / max) * 100).toFixed(1)}%" title="Usual: ${fmt(item.median)}"></span>`,
        delta: `<span class="cat-bar-delta ${tone}" title="Usual: ${fmt(item.median)} a month">${text}</span>`,
        // The bar itself only turns colour for a real outlier. Half the card
        // in red says nothing; two rows in red is the card doing its job.
        hot: !good && change >= OUTLIER_BAND,
    };
}

async function openCategoryDrilldown(catId, catName) {
    if (!catId) return;
    let url = `/api/transactions?category_ids=${catId}&sort=amount&dir=desc&per_page=500`;
    if (breakdownMonths.length > 0) url += `&months=${breakdownMonths.join(",")}`;
    const data = await api(url);
    const rows = data.items || [];
    const total = rows.reduce((s, r) => s + r.amount, 0);

    const tableRows = rows.map(r => `
        <tr>
            <td style="font-size:13px;white-space:nowrap">${fmtDate(r.date)}</td>
            <td style="font-size:13px">${r.store || "—"}</td>
            <td class="amount" style="font-size:13px;white-space:nowrap">${fmt2(r.amount)}</td>
        </tr>`).join("");

    const html = `<div class="modal-overlay" onclick="if(event.target===this)this.remove()">
        <div class="modal" style="max-width:560px">
            <div class="modal-title">${catName}</div>
            <div style="max-height:420px;overflow-y:auto;margin:0 -4px">
                <table style="width:100%">
                    <thead><tr>
                        <th style="font-size:12px">Date</th>
                        <th style="font-size:12px">Store</th>
                        <th style="font-size:12px;text-align:right">Amount</th>
                    </tr></thead>
                    <tbody>${tableRows}</tbody>
                    <tfoot><tr style="border-top:2px solid var(--border)">
                        <td colspan="2" style="font-size:13px;font-weight:600;padding-top:8px">Total (${rows.length} transactions)</td>
                        <td class="amount" style="font-size:13px;font-weight:600;padding-top:8px">${fmt(total)}</td>
                    </tr></tfoot>
                </table>
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML("beforeend", html);
}

// Clicking a Monthly Summary row lists everything in that month.
async function openMonthDrilldown(month) {
    const data = await api(`/api/transactions?months=${month}&sort=date&dir=desc&per_page=1000`);
    const rows = data.items || [];

    const tableRows = rows.length ? rows.map(r => `
        <tr>
            <td style="font-size:13px;white-space:nowrap">${fmtDate(r.date)}</td>
            <td style="font-size:13px">${r.store || "—"}</td>
            <td style="font-size:13px;color:var(--text-secondary)">${r.category_name || "—"}</td>
            <td class="amount ${r.type === "income" ? "income" : ""}" style="font-size:13px;white-space:nowrap;text-align:right">${r.type === "income" ? "+" : "−"}${fmt2(r.amount)}</td>
        </tr>`).join("")
        : `<tr><td colspan="4" style="text-align:center;padding:24px;color:var(--text-tertiary)">No transactions this month</td></tr>`;

    const net = data.sum_income - data.sum_expense;
    const html = `<div class="modal-overlay" onclick="if(event.target===this)this.remove()">
        <div class="modal" style="max-width:680px">
            <div class="modal-title">${monthLabelFull(month)}</div>
            <div style="font-size:13px;color:var(--text-secondary);margin:-8px 0 12px">
                ${fmt(data.sum_income)} in · ${fmt(data.sum_expense)} out ·
                <span class="${net >= 0 ? "diff-positive" : "diff-negative"}">${net >= 0 ? "+" : ""}${fmt(net)} net</span>
            </div>
            <div style="max-height:440px;overflow-y:auto;margin:0 -4px">
                <table style="width:100%">
                    <thead><tr>
                        <th style="font-size:12px">Date</th>
                        <th style="font-size:12px">Store</th>
                        <th style="font-size:12px">Category</th>
                        <th style="font-size:12px;text-align:right">Amount</th>
                    </tr></thead>
                    <tbody>${tableRows}</tbody>
                </table>
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML("beforeend", html);
}

// cached top-5 data and selected category IDs
let cachedTopExpenses = null;
let selectedTrendCatIds = null;

function renderTrendsChart(data) {
    cachedTopExpenses = data;
    drawTrendsFromData(data);
    populateTrendDropdown();
}

function drawTrendsFromData(data) {
    const ctx = document.getElementById("chart-trends");
    if (charts.trends) charts.trends.destroy();

    if (!data.trends || data.trends.length === 0) {
        charts.trends = new Chart(ctx, { type: "line", data: { labels: ["No data"], datasets: [{ data: [0] }] }, options: chartOptions() });
        return;
    }

    // Same precedence as filterData(): an explicit month pick beats the horizon
    // buttons. A null horizon means the picker is driving, and must not fall
    // through to the slice — parseInt(null) is NaN, and slice(-NaN) quietly
    // hands back every month there has ever been while the rest of the page
    // shows the two the user picked.
    let months = [...new Set(data.trends.map(r => r.month))].sort();
    if (selectedPeriods.size > 0) {
        months = months.filter(m => selectedPeriods.has(m));
    } else if (dashboardHorizon === "ytd") {
        const year = new Date().getFullYear().toString();
        months = months.filter(m => m.startsWith(year));
    } else if (dashboardHorizon && dashboardHorizon !== "0") {
        months = months.slice(-parseInt(dashboardHorizon));
    }

    // Lines only — overlapping area fills turned to mud where series cross
    // (design #9). Line color = the category's stable identity color (#2),
    // de-duplicated so two series never share one color.
    const lineColors = distinctCatColors(data.categories.map(c => c.id));
    const datasets = data.categories.map((cat, i) => ({
        label: cat.name,
        data: months.map(m => {
            const match = data.trends.find(r => r.month === m && r.category_id === cat.id);
            return match ? match.total : 0;
        }),
        borderColor: lineColors[i],
        backgroundColor: lineColors[i],
        fill: false, tension: 0, pointRadius: 3, pointHoverRadius: 6,
    }));

    charts.trends = new Chart(ctx, {
        type: "line",
        data: { labels: months.map(monthLabel), datasets },
        options: chartOptions(),
    });
}

// ── Trend Dropdown ───────────────────────────────────────────────────
function populateTrendDropdown() {
    const container = document.getElementById("trend-options");
    const expenseCats = categories.filter(c => c.type === "expense");
    container.innerHTML = expenseCats.map(c => {
        const checked = selectedTrendCatIds === null
            ? (cachedTopExpenses?.categories?.some(t => t.id === c.id) ?? false)
            : selectedTrendCatIds.includes(c.id);
        return `<label class="trend-option">
            <input type="checkbox" value="${c.id}" ${checked ? "checked" : ""}>
            <span>${escapeHtml(c.name)}</span>
        </label>`;
    }).join("");
}

function filterTrendCategories() {
    const q = document.getElementById("trend-search").value.toLowerCase();
    document.querySelectorAll(".trend-option").forEach(el => {
        el.style.display = el.querySelector("span").textContent.toLowerCase().includes(q) ? "" : "none";
    });
}

function toggleTrendDropdown() {
    const dd = document.getElementById("trend-dropdown");
    const open = dd.style.display === "none";
    dd.style.display = open ? "block" : "none";
    if (open) { populateTrendDropdown(); document.getElementById("trend-search").focus(); }
}

document.addEventListener("click", e => {
    const sel = document.getElementById("trend-selector");
    if (sel && !sel.contains(e.target)) {
        document.getElementById("trend-dropdown").style.display = "none";
    }
});

async function applyTrendSelection() {
    const checked = [...document.querySelectorAll("#trend-options input:checked")];
    if (checked.length === 0) { toast("Select at least one category"); return; }
    const ids = checked.map(el => parseInt(el.value));
    selectedTrendCatIds = ids;
    const expenseCats = categories.filter(c => c.type === "expense");
    const isTop5 = cachedTopExpenses?.categories?.every(c => ids.includes(c.id)) && ids.length === 5;
    const label = isTop5 ? "Top 5 categories"
        : ids.length === 1 ? expenseCats.find(c => c.id === ids[0])?.name
        : `${ids.length} categories`;
    document.getElementById("trend-selector-btn").textContent = label;
    document.getElementById("trend-dropdown").style.display = "none";
    const data = await api(`/api/dashboard/category-trends?ids=${ids.join(",")}`);
    drawTrendsFromData(data);
}

async function resetTrendToTop5() {
    selectedTrendCatIds = null;
    document.getElementById("trend-selector-btn").textContent = "Top 5 categories";
    document.getElementById("trend-dropdown").style.display = "none";
    if (cachedTopExpenses) drawTrendsFromData(cachedTopExpenses);
}

// ── Summary Table ───────────────────────────────────────────────────
function toggleYearView() {
    showYearView = !showYearView;
    document.getElementById("toggle-year-btn").textContent = showYearView ? "Show Monthly" : "Show Yearly";
    renderSummaryTable(cachedMonthly);
}

function renderSummaryTable(monthly) {
    const thead = document.getElementById("summary-table-head");
    const tbody = document.getElementById("summary-table-body");

    const months = [...new Set(monthly.map(r => r.month))].sort();

    const monthData = months.map(m => {
        const income  = monthly.filter(r => r.month === m && r.type === "income").reduce((s, r) => s + r.total, 0);
        const expense = monthly.filter(r => r.month === m && r.type === "expense").reduce((s, r) => s + r.total, 0);
        return { month: m, income, expense, diff: income - expense };
    });

    thead.innerHTML = `<tr>
        <th>Period</th>
        <th style="text-align:right">Income</th>
        <th style="text-align:right">Expenses</th>
        <th style="text-align:right">Difference</th>
        <th style="width:32px"></th>
    </tr>`;

    function noteBtn(month) {
        const hasNote = monthsWithNotes.has(month);
        // stopPropagation: the row itself now opens the month.
        return `<button class="note-btn ${hasNote ? "has-note" : ""}" onclick="event.stopPropagation();openNoteModal('${month}')" title="${hasNote ? "View/edit note" : "Add note"}">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <path d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5"/>
                <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/>
            </svg>
        </button>`;
    }

    if (showYearView) {
        const years = {};
        monthData.forEach(d => {
            const y = d.month.slice(0, 4);
            if (!years[y]) years[y] = { income: 0, expense: 0, diff: 0, months: [] };
            years[y].income += d.income;
            years[y].expense += d.expense;
            years[y].diff += d.diff;
            years[y].months.push(d);
        });

        let html = "";
        Object.keys(years).sort().reverse().forEach(year => {
            const y = years[year];
            const collapsed = yearViewCollapsed[year] ?? true;
            const chevron = collapsed ? "collapsed" : "";

            html += `<tr class="row-year" onclick="toggleYear('${year}')">
                <td><span class="year-chevron ${chevron}">&#9660;</span>${year}</td>
                <td style="text-align:right" class="amount income">+${fmt(y.income)}</td>
                <td style="text-align:right" class="amount">${fmt(y.expense)}</td>
                <td style="text-align:right" class="${y.diff >= 0 ? "diff-positive" : "diff-negative"}">${y.diff >= 0 ? "+" : ""}${fmt(y.diff)}</td>
                <td></td>
            </tr>`;

            y.months.reverse().forEach(d => {
                html += `<tr class="row-month row-clickable ${collapsed ? "hidden" : ""}" data-year="${year}" onclick="openMonthDrilldown('${d.month}')">
                    <td style="padding-left:36px">${monthLabel(d.month)}<span class="row-go">&#8250;</span></td>
                    <td style="text-align:right" class="amount income">+${fmt(d.income)}</td>
                    <td style="text-align:right" class="amount">${fmt(d.expense)}</td>
                    <td style="text-align:right" class="${d.diff >= 0 ? "diff-positive" : "diff-negative"}">${d.diff >= 0 ? "+" : ""}${fmt(d.diff)}</td>
                    <td>${noteBtn(d.month)}</td>
                </tr>`;
            });
        });

        tbody.innerHTML = html;
    } else {
        tbody.innerHTML = monthData.slice().reverse().map(d => `<tr class="row-clickable" onclick="openMonthDrilldown('${d.month}')">
            <td>${monthLabel(d.month)}<span class="row-go">&#8250;</span></td>
            <td style="text-align:right" class="amount income">+${fmt(d.income)}</td>
            <td style="text-align:right" class="amount">${fmt(d.expense)}</td>
            <td style="text-align:right" class="${d.diff >= 0 ? "diff-positive" : "diff-negative"}">${d.diff >= 0 ? "+" : ""}${fmt(d.diff)}</td>
            <td>${noteBtn(d.month)}</td>
        </tr>`).join("");
    }
}

function toggleYear(year) {
    yearViewCollapsed[year] = !(yearViewCollapsed[year] ?? true);
    const rows    = document.querySelectorAll(`.row-month[data-year="${year}"]`);
    const chevron = document.querySelector(`.row-year[onclick*="${year}"] .year-chevron`);
    rows.forEach(r => r.classList.toggle("hidden"));
    if (chevron) chevron.classList.toggle("collapsed");
}

// ── Spending Heatmap ────────────────────────────────────────────────
let heatmapYear = null;

async function loadHeatmap() {
    const sel = document.getElementById("heatmap-year-select");
    if (!sel) return;
    const yr = sel.value || "";
    const url = yr ? `/api/dashboard/heatmap?year=${yr}` : "/api/dashboard/heatmap";
    const data = await api(url);

    if (data.available_years?.length) {
        const cur = sel.value || data.year;
        sel.innerHTML = data.available_years.map(y =>
            `<option value="${y}" ${parseInt(cur) === y ? "selected" : ""}>${y}</option>`
        ).join("");
    }
    heatmapYear = data.year;
    renderHeatmap(data);
}

function renderHeatmap(data) {
    const grid = document.getElementById("heatmap-grid");
    const monthRow = document.getElementById("heatmap-month-labels");
    const summary = document.getElementById("heatmap-summary");
    if (!grid) return;

    const byDate = {};
    (data.items || []).forEach(it => { byDate[it.date] = it.total; });

    const totals = Object.values(byDate).filter(v => v > 0).sort((a, b) => a - b);
    const total = totals.reduce((s, v) => s + v, 0);
    const days = totals.length;

    if (summary) {
        summary.textContent = days
            ? `${fmt(total)} across ${days} days · max ${fmt(totals[totals.length - 1])}`
            : "No expense data";
    }

    // Quintile thresholds for 5 buckets
    const q = (p) => totals.length ? totals[Math.min(totals.length - 1, Math.floor(totals.length * p))] : 0;
    const t1 = q(0.20), t2 = q(0.40), t3 = q(0.60), t4 = q(0.80);
    function level(v) {
        if (v <= 0) return null;
        if (v <= t1) return 0;
        if (v <= t2) return 1;
        if (v <= t3) return 2;
        if (v <= t4) return 3;
        return 4;
    }

    const year = data.year;
    const jan1 = new Date(year, 0, 1);
    const dec31 = new Date(year, 11, 31);
    const isLeap = ((year % 4 === 0) && (year % 100 !== 0)) || (year % 400 === 0);
    const daysInYear = isLeap ? 366 : 365;
    // Mon=0..Sun=6
    const firstDow = (jan1.getDay() + 6) % 7;

    const cells = [];
    for (let i = 0; i < firstDow; i++) cells.push(`<div class="heatmap-cell empty"></div>`);

    const monthCols = {}; // colIdx -> short month name
    for (let i = 0; i < daysInYear; i++) {
        const d = new Date(year, 0, 1 + i);
        const ds = `${year}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
        const v = byDate[ds] || 0;
        const lvl = level(v);
        const cellCol = Math.floor((firstDow + i) / 7) + 1; // 1-based grid col
        if (d.getDate() === 1) {
            monthCols[cellCol] = d.toLocaleDateString("en-US", { month: "short" });
        }
        const cls = lvl == null ? "heatmap-cell" : "heatmap-cell has-data";
        const lvlAttr = lvl == null ? "" : `data-level="${lvl}"`;
        const titleAttr = v > 0
            ? `${ds}: ${fmt(v)}`
            : `${ds}: no spend`;
        cells.push(`<div class="${cls}" ${lvlAttr} title="${titleAttr}" data-date="${ds}"></div>`);
    }

    grid.innerHTML = cells.join("");

    const totalCols = Math.ceil((firstDow + daysInYear) / 7);
    if (monthRow) {
        const labelCells = [];
        for (let c = 1; c <= totalCols; c++) {
            labelCells.push(`<span style="grid-column:${c}">${monthCols[c] || ""}</span>`);
        }
        monthRow.innerHTML = labelCells.join("");
    }

    // Click → drill to that day's transactions
    grid.querySelectorAll(".heatmap-cell.has-data").forEach(el => {
        el.addEventListener("click", () => openDayDrilldown(el.dataset.date));
    });
}

async function openDayDrilldown(dateStr) {
    const data = await api(`/api/transactions?date_from=${dateStr}&date_to=${dateStr}&type=expense&per_page=200&sort=amount&dir=desc`);
    const rows = data.items || [];
    const total = rows.reduce((s, r) => s + r.amount, 0);
    const tableRows = rows.map(r => `
        <tr>
            <td style="font-size:13px">${r.store || "—"}</td>
            <td style="font-size:13px"><span class="category-tag">${r.category_name}</span></td>
            <td class="amount" style="font-size:13px;white-space:nowrap;text-align:right">${fmt2(r.amount)}</td>
        </tr>`).join("");

    const html = `<div class="modal-overlay" onclick="if(event.target===this)this.remove()">
        <div class="modal" style="max-width:540px">
            <div class="modal-title">${fmtDate(dateStr)}</div>
            <div style="max-height:400px;overflow-y:auto;margin:0 -4px">
                ${rows.length ? `<table style="width:100%">
                    <thead><tr>
                        <th style="font-size:12px">Store</th>
                        <th style="font-size:12px">Category</th>
                        <th style="font-size:12px;text-align:right">Amount</th>
                    </tr></thead>
                    <tbody>${tableRows}</tbody>
                    <tfoot><tr style="border-top:2px solid var(--border)">
                        <td colspan="2" style="font-size:13px;font-weight:600;padding-top:8px">Total (${rows.length})</td>
                        <td class="amount" style="font-size:13px;font-weight:600;padding-top:8px;text-align:right">${fmt(total)}</td>
                    </tr></tfoot>
                </table>` : `<p style="text-align:center;padding:24px;color:var(--text-tertiary)">No expenses on this day</p>`}
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML("beforeend", html);
}

