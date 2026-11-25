// Balance — planning.js
//
// The Planning tab: assumptions, life events, the two charts and the
// year-by-year table. The projection itself is planning-math.js, the same
// rules as services/planning.py. This file only draws and saves.
//
// One of the ordered classic scripts index.html loads. They share one global
// scope on purpose: index.html wires inline handlers straight to these
// names, so these are plain scripts and not modules.

// Slider ranges from the approved mockup. A figure past the end (a large
// net worth, an age already outside the range) extends the range so the
// thumb can still reach it. Typing is not clamped.
const PLANNING_FIELDS = [
    { group: "You", items: [
        ["age", "Current age", 18, 70, 1, "yrs"],
        ["retAge", "Retirement age", 40, 75, 1, "yrs"],
        ["endAge", "Plan until age", 70, 100, 1, "yrs"],
    ]},
    { group: "Today", tag: "From Balance", items: [
        ["income", "Net income / month", 0, 15000, 50, "€"],
        ["expenses", "Spending / month", 0, 10000, 50, "€"],
        ["rent", "…of which rent", 0, 4000, 25, "€"],
        ["startInv", "Investments & savings now", 0, 500000, 1000, "€"],
    ]},
    { group: "Growth per year", items: [
        ["incG", "Income growth", -2, 8, 0.1, "%"],
        ["expG", "Spending growth", -2, 8, 0.1, "%"],
        ["infl", "Inflation", 0, 6, 0.1, "%"],
    ]},
    { group: "Investing", items: [
        ["retPre", "Return before retirement", 0, 12, 0.1, "%"],
        ["retPost", "Return in retirement", 0, 10, 0.1, "%"],
    ]},
    { group: "Retirement", items: [
        ["retPct", "Spending vs. before", 40, 120, 1, "%"],
        ["pension", "Pension / month (today's €)", 0, 6000, 50, "€"],
        ["pensionAge", "Pension starts at", 60, 72, 1, "yrs"],
        ["swr", "Safe withdrawal rate", 2, 6, 0.1, "%"],
    ]},
];

const PLANNING_ICONS = {
    house: '<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>',
    car: '<path d="M5 16l1.5-5h11L19 16"/><rect x="3" y="16" width="18" height="4" rx="1"/><circle cx="7" cy="20" r="1.5"/><circle cx="17" cy="20" r="1.5"/>',
    child: '<circle cx="12" cy="6" r="3"/><path d="M7 21v-6a5 5 0 0110 0v6"/>',
    sabbatical: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    windfall: '<rect x="3" y="8" width="18" height="13" rx="1"/><path d="M12 8v13M3 12h18M12 8c-2-4-6-4-6-1s6 1 6 1zM12 8c2-4 6-4 6-1s-6 1-6 1z"/>',
    extra: '<path d="M12 3v18M17 7H9.5a3 3 0 000 6h5a3 3 0 010 6H6"/>',
    oneoff: '<path d="M6 2h12l-1 20H7z"/><path d="M9 7h6"/>',
    custom: '<path d="M12 3l2.6 5.6 6.1.7-4.5 4.2 1.2 6L12 16.6 6.6 19.5l1.2-6L3.3 9.3l6.1-.7z"/>',
    shock: '<polyline points="2 6 9 13 13 9 22 18"/><polyline points="16 18 22 18 22 12"/>',
};

const PLANNING_TYPES = {
    house: { label: "Buy a home", color: "#AF52DE", token: "--purple", fields: [
        ["name", "Name", "text"], ["age", "Purchase age"], ["price", "Price (today's €)"],
        ["downPct", "Down payment %"], ["tax", "Transfer tax %"], ["rate", "Mortgage rate %"],
        ["term", "Loan term (years)"], ["costPct", "Upkeep & fees, % of price / yr"],
        ["appr", "Home value growth % / yr"], ["replacesRent", "Stops paying rent", "check"],
    ]},
    car: { label: "Car", color: "#FF9500", token: "--orange", fields: [
        ["name", "Name", "text"], ["age", "At age"], ["amount", "Cost (today's €)"],
        ["every", "Replace every N yrs (0 = once)"], ["until", "Last replacement by age"],
    ]},
    child: { label: "Child", color: "#FF2D55", token: "--pink", fields: [
        ["name", "Name", "text"], ["age", "Born when I'm"],
        ["amount", "Cost per year (today's €)"], ["years", "For how many years"],
    ]},
    sabbatical: { label: "Time off", color: "#5AC8FA", token: "--teal", fields: [
        ["name", "Name", "text"], ["age", "Starts at age"], ["years", "Years"], ["pct", "Income drop %"],
    ]},
    windfall: { label: "Inheritance", color: "#00a06b", token: "--green", fields: [
        ["name", "Name", "text"], ["age", "At age"], ["amount", "Amount (today's €)"],
    ]},
    extra: { label: "Extra income", color: "#00a06b", token: "--green", fields: [
        ["name", "Name", "text"], ["age", "From age"],
        ["amount", "Per year (today's €)"], ["years", "For how many years"],
    ]},
    oneoff: { label: "Other cost", color: "#FF9500", token: "--orange", fields: [
        ["name", "Name", "text"], ["age", "From age"],
        ["amount", "Per year (today's €)"], ["years", "For how many years"],
    ]},
    shock: { label: "Market crash", color: "#e0445b", token: "--red", fields: [
        ["name", "Name", "text"], ["age", "At age"], ["pct", "Investments fall %"],
    ]},
    custom: { label: "Custom", color: "#5856D6", token: "--indigo", fields: [
        ["name", "Name", "text"],
        ["dir", "Kind", "select", null, [["cost", "Cost / purchase"], ["income", "Income / money in"]]],
        ["age", "Start age"],
        ["amount", d => d.dir === "income" ? "Amount (money in)" : "Amount / price"],
        ["repeat", "How often", "select", null, [["once", "One-off"], ["yearly", "Every year, for N years"], ["every", "Every N years"]]],
        ["years", "For how many years", "number", d => d.repeat === "yearly"],
        ["every", "Every N years", "number", d => d.repeat === "every"],
        ["until", "Last time at age", "number", d => d.repeat === "every"],
        ["real", "Amounts in today's € (rise with inflation)", "check"],
        ["loan", "Pay part of it with a loan", "check", d => d.dir !== "income"],
        ["downPct", "Own share / down payment %", "number", d => d.dir !== "income" && d.loan],
        ["rate", "Loan interest %", "number", d => d.dir !== "income" && d.loan],
        ["term", "Payoff time (years)", "number", d => d.dir !== "income" && d.loan],
        ["asset", "Keep it as an asset (counts in net worth)", "check", d => d.dir !== "income"],
        ["assetPct", "Asset value, % of price", "number", d => d.dir !== "income" && d.asset],
        ["assetG", "Value change % / yr (minus = loses value)", "number", d => d.dir !== "income" && d.asset],
        ["running", "Running costs / yr while owned", "number", d => d.dir !== "income" && d.asset],
    ]},
};

const PLANNING_EVENT_DEFAULTS = {
    house: { name: "Buy an apartment", age: 32, price: 320000, downPct: 15, tax: 1.5, rate: 3.5, term: 25, costPct: 1.5, appr: 2, replacesRent: true },
    car: { name: "Car", age: 30, amount: 25000, every: 8, until: 70 },
    child: { name: "First child", age: 33, amount: 9000, years: 18 },
    sabbatical: { name: "Sabbatical", age: 40, years: 1, pct: 50 },
    windfall: { name: "Inheritance", age: 55, amount: 60000 },
    extra: { name: "Side income", age: 30, amount: 6000, years: 10 },
    oneoff: { name: "Wedding", age: 30, amount: 20000, years: 1 },
    shock: { name: "Market crash", age: 45, pct: 30 },
    custom: {
        name: "My event", dir: "cost", age: 35, amount: 10000, repeat: "once",
        years: 5, every: 10, until: 70, real: true, loan: false, downPct: 20,
        rate: 5, term: 5, asset: false, assetPct: 100, assetG: 0, running: 0,
    },
};

let planningState = null;
let planningLoading = false;
let planningBound = false;
let planningNextId = 1;
let planningDirty = false;
let planningSaveTimer = null;
let planningSaveChain = Promise.resolve();
let planningCharts = { cf: null, nw: null };
let planningModal = { editing: null, type: "house", draft: null };

function planningScenario() {
    return planningState.scenarios[planningState.current];
}

function planningOtherScenario() {
    const index = planningState.current === 0 ? 1 : 0;
    return planningState.scenarios[index] || null;
}

function planningPayload() {
    return {
        scenarios: planningState.scenarios.map(s => ({
            name: s.name,
            assumptions: s.assumptions,
            events: s.events,
        })),
        current: planningState.current,
        unit: planningState.unit,
        compare: planningState.compare,
    };
}

function schedulePlanningSave() {
    planningDirty = true;
    if (planningSaveTimer) clearTimeout(planningSaveTimer);
    planningSaveTimer = setTimeout(flushPlanningSave, 250);
}

function flushPlanningSave() {
    if (planningSaveTimer) { clearTimeout(planningSaveTimer); planningSaveTimer = null; }
    if (!planningDirty || !planningState || !planningState.ready) return;
    planningDirty = false;
    const snapshot = JSON.parse(JSON.stringify(planningPayload()));
    planningSaveChain = planningSaveChain.then(() => api("/api/planning", { method: "PUT", body: snapshot }))
        .then(saved => planningNoteSaved(saved))
        .catch(err => {
            planningDirty = true;
            toast((err && err.message) || "Could not save the plan");
        });
}

function planningNoteSaved(saved) {
    if (!saved || planningDirty || !planningState) return;
    saved.scenarios.forEach((scenario, i) => {
        const local = planningState.scenarios[i];
        if (!local || local.events.length !== scenario.events.length) return;
        scenario.events.forEach((event, j) => { local.events[j].id = event.id; });
    });
    let maxId = 0;
    planningState.scenarios.forEach(s => s.events.forEach(e => { if (e.id > maxId) maxId = e.id; }));
    if (maxId + 1 > planningNextId) planningNextId = maxId + 1;
}

function planningTodayHint() {
    const baseline = planningState.baseline || {};
    if (baseline.coverage === "full") return "Average of the last 12 months, after tax. Editable.";
    if (baseline.coverage === "partial") {
        const n = baseline.months_averaged;
        return `Average of the ${n} month${n === 1 ? "" : "s"} on record, after tax. Editable.`;
    }
    return "No transactions yet, so these start at 0. Editable.";
}

function planningBanner() {
    const el = document.getElementById("pl-banner-text");
    if (!el || !planningState) return;
    const b = planningState.baseline || {};
    const bits = [];
    if (!b.has_transactions && !b.has_accounts) {
        bits.push("Nothing in Balance yet, so income, spending and net worth start at 0.");
        bits.push("Type the figures, or add transactions and accounts and press Reset.");
    } else {
        if (b.coverage === "none") bits.push("No transactions yet, so income and spending start at 0.");
        else if (b.coverage === "partial") {
            const n = b.months_averaged;
            bits.push(`Income, spending and rent are the average of the ${n} month${n === 1 ? "" : "s"} on record.`);
        } else bits.push("Income, spending and rent are the average of the last 12 months.");
        if (b.transfers_excluded) {
            bits.push(`${fmt(b.transfers_excluded)} a month moved to Investments is left out of spending, because the plan invests whatever is left over.`);
        }
        if (b.has_accounts) bits.push(`Starting net worth is ${fmt(b.start_inv)}, the same figure as the Net Worth tab.`);
        else bits.push("No accounts yet, so the starting net worth is 0.");
        bits.push("Every figure is editable. Reset puts these back.");
    }
    el.textContent = bits.join(" ");
}

function planningRange(min, max, value) {
    if (value > max) max = value;
    if (value < min) min = value;
    return { min, max };
}

function renderPlanningFields() {
    const box = document.getElementById("pl-fields");
    if (!box) return;
    const assumptions = planningScenario().assumptions;
    box.innerHTML = PLANNING_FIELDS.map(group => {
        const hint = group.group === "Today" ? planningTodayHint() : "";
        return `<div class="pl-group">
            <div class="pl-group-title">${group.group}${group.tag ? `<span class="pl-tag">${group.tag}</span>` : ""}</div>
            ${hint ? `<div class="pl-hint" style="margin:-4px 0 8px">${escapeHtml(hint)}</div>` : ""}
            ${group.items.map(([key, label, min, max, step, unit]) => {
                const bounds = planningRange(min, max, Number(assumptions[key]) || 0);
                const value = assumptions[key];
                return `<div class="pl-field">
                    <div class="pl-field-top"><label for="pl-f-${key}">${label}</label>
                        <div class="pl-num-wrap"><input class="form-input pl-num" id="pl-f-${key}" data-k="${key}" type="number" inputmode="decimal" min="${bounds.min}" max="${bounds.max}" step="${step}" value="${value}"><span class="pl-unit">${unit}</span></div></div>
                    <input class="pl-range" type="range" data-k="${key}" min="${bounds.min}" max="${bounds.max}" step="${step}" value="${value}">
                </div>`;
            }).join("")}
        </div>`;
    }).join("");
}

function planningOnField(e) {
    const input = e.target.closest("input[data-k]");
    if (!input || !planningState) return;
    const key = input.dataset.k;
    const value = parseFloat(input.value);
    if (Number.isNaN(value)) return;
    planningScenario().assumptions[key] = value;
    document.querySelectorAll(`#pl-fields input[data-k="${key}"]`).forEach(other => {
        if (other !== input) other.value = value;
        if (other.max !== "" && value > parseFloat(other.max)) other.max = String(value);
        if (other.min !== "" && value < parseFloat(other.min)) other.min = String(value);
    });
    planningUpdate();
    schedulePlanningSave();
}

function planningUnitValue(row, value) {
    const shown = planningState.unit === "real" ? value / row.defl : value;
    return isFinite(shown) ? shown : null;
}

function planningFmtK(value) {
    const abs = Math.abs(value);
    const sign = value < 0 ? "−" : "";
    if (abs >= 1e6) return `${sign}€${(abs / 1e6).toFixed(abs >= 1e7 ? 0 : 1)}M`;
    if (abs >= 1e3) return `${sign}€${Math.round(abs / 1e3)}k`;
    return `${sign}€${Math.round(abs)}`;
}

function renderPlanningSummary(result) {
    const box = document.getElementById("pl-summary");
    if (!box) return;
    const assumptions = planningScenario().assumptions;
    const cards = planningSummarize(result, assumptions, planningState.unit);
    const unitLabel = planningState.unit === "real" ? "in today's €" : "in future €";
    const nw = cards.net_worth_at_retirement;
    const pensionBit = cards.pension_already
        ? " + pension"
        : `, + ${fmt(assumptions.pension)} pension from ${assumptions.pensionAge}`;
    const fiYears = cards.fi_age == null ? "" : `${cards.fi_age - assumptions.age} years from now`;
    box.innerHTML = `
        <div class="summary-card"><div class="label">Net worth at ${assumptions.retAge}</div><div class="value ${nw >= 0 ? "positive" : "negative"}">${fmt(nw)}</div><div class="sub">${unitLabel} · year ${cards.retirement_year}</div></div>
        <div class="summary-card"><div class="label">Financially independent</div><div class="value">${cards.fi_age != null ? "at " + cards.fi_age : "—"}</div><div class="sub">${cards.fi_age != null ? fiYears : "not before retirement"}</div></div>
        <div class="summary-card"><div class="label">Money lasts</div><div class="value ${cards.ran_out != null ? "negative" : "positive"}">${cards.ran_out != null ? "until " + cards.ran_out : "past " + assumptions.endAge}</div><div class="sub">${cards.ran_out != null ? "investments run out" : "with money left over"}</div></div>
        <div class="summary-card"><div class="label">Monthly spend at ${assumptions.retAge}</div><div class="value">${fmt(cards.sustainable_monthly)}</div><div class="sub">${assumptions.swr}% of investments${pensionBit}</div></div>
        <div class="summary-card"><div class="label">Savings rate now</div><div class="value ${cards.savings_rate >= 0 ? "positive" : "negative"}">${cards.savings_rate.toFixed(0)}%</div><div class="sub">${fmt(cards.monthly_surplus)} / month</div></div>
        <div class="summary-card"><div class="label">Peak net worth</div><div class="value">${planningFmtK(cards.peak_nw)}</div><div class="sub">at age ${cards.peak_age}</div></div>`;
}

function planningMarkerPlugin(rows, retAge) {
    return { id: "plMarkers", afterDatasetsDraw(chart) {
        const area = chart.chartArea;
        const x = chart.scales && chart.scales.x;
        if (!area || !x) return;
        const ctx = chart.ctx;
        ctx.save();
        ctx.font = "600 10px -apple-system, BlinkMacSystemFont, sans-serif";
        const placed = [];
        rows.forEach((r, i) => {
            const px = x.getPixelForValue(i);
            if (r.age === retAge) {
                ctx.strokeStyle = cssVar("--green"); ctx.lineWidth = 1.5; ctx.setLineDash([]);
                ctx.beginPath(); ctx.moveTo(px, area.top); ctx.lineTo(px, area.bottom); ctx.stroke();
                ctx.fillStyle = cssVar("--green"); ctx.textAlign = "left";
                ctx.fillText("Retire", px + 4, area.bottom - 6);
            }
            if (!r.marks.length && !r.minor.length) return;
            ctx.strokeStyle = cssVar("--text-tertiary"); ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
            ctx.beginPath();
            ctx.moveTo(px, r.marks.length ? area.top + 14 : area.top + 40);
            ctx.lineTo(px, area.bottom); ctx.stroke();
            if (!r.marks.length) return;
            const text = r.marks.join(" · ");
            const width = ctx.measureText(text).width;
            const cx = Math.min(Math.max(px, area.left + width / 2 + 4), area.right - width / 2 - 4);
            let labelY = area.top + 6;
            for (let row = 0; row < 4; row++) {
                const y = area.top + 6 + row * 12;
                const left = cx - width / 2;
                const hits = placed.some(item => Math.abs(item.y - y) < 10 && left < item.right + 8 && left + width > item.left - 8);
                if (!hits) { labelY = y; break; }
            }
            placed.push({ y: labelY, left: cx - width / 2, right: cx + width / 2 });
            ctx.fillStyle = cssVar("--text-secondary"); ctx.textAlign = "center";
            ctx.fillText(text, cx, labelY);
        });
        ctx.restore();
    }};
}

function planningChartOptions(rows) {
    const theme = chartTheme();
    const dark = document.documentElement.getAttribute("data-theme") === "dark";
    return {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: "index", intersect: false },
        layout: { padding: { top: 22 } },
        plugins: { legend: { display: false },
            tooltip: Object.assign({
                backgroundColor: theme.tooltipBg,
                padding: 10, cornerRadius: 8,
            }, dark ? {
                titleColor: theme.tooltipText,
                bodyColor: theme.tooltipText,
                borderColor: theme.tooltipBorder,
                borderWidth: 0.5,
            } : {}, { callbacks: {
                    title: items => {
                        const r = rows[items[0].dataIndex];
                        const marks = [...r.marks, ...r.minor];
                        return `Age ${r.age} · ${r.year}${marks.length ? " · " + marks.join(", ") : ""}`;
                    },
                    label: c => ` ${c.dataset.label}: ${fmt(c.raw)}`,
                } }),
        },
        scales: {
            x: { grid: { display: false }, ticks: { color: cssVar("--text-tertiary"), maxRotation: 0, autoSkip: true, autoSkipPadding: 12 } },
            y: {
                grid: { color: theme.grid },
                border: { display: false },
                ticks: { color: cssVar("--text-tertiary"), callback: v => planningFmtK(v) },
            },
        },
    };
}

function renderPlanningCashflow(result) {
    if (typeof Chart === "undefined") return;
    const canvas = document.getElementById("chart-planning-cashflow");
    if (!canvas) return;
    const rows = result.rows;
    if (planningCharts.cf) planningCharts.cf.destroy();
    const opts = planningChartOptions(rows);
    opts.scales.x.stacked = true;
    opts.scales.y.stacked = true;
    planningCharts.cf = new Chart(canvas, {
        data: { labels: rows.map(r => r.age), datasets: [
            { type: "line", label: "Net cash flow", data: rows.map(r => planningUnitValue(r, r.net)),
              borderColor: cssVar("--text-primary"), backgroundColor: cssVar("--text-primary"),
              borderWidth: 2, pointRadius: 0, tension: 0.25, stack: "net", order: 0 },
            { type: "bar", label: "Income", data: rows.map(r => planningUnitValue(r, r.income)),
              backgroundColor: cssVar("--green"), stack: "in", borderRadius: 2, order: 1 },
            { type: "bar", label: "Living costs", data: rows.map(r => -planningUnitValue(r, r.living)),
              backgroundColor: cssVar("--chart-muted"), stack: "out", order: 1 },
            { type: "bar", label: "Home & loan", data: rows.map(r => -planningUnitValue(r, r.housing)),
              backgroundColor: cssVar("--purple"), stack: "out", order: 1 },
            { type: "bar", label: "Big-ticket items", data: rows.map(r => -planningUnitValue(r, r.big)),
              backgroundColor: cssVar("--orange"), stack: "out", order: 1 },
        ]},
        options: Object.assign({}, opts, { datasets: { bar: { categoryPercentage: 0.9, barPercentage: 0.95 } } }),
        plugins: [planningMarkerPlugin(rows, planningScenario().assumptions.retAge)],
    });
}

function renderPlanningNetworth(result) {
    if (typeof Chart === "undefined") return;
    const canvas = document.getElementById("chart-planning-networth");
    if (!canvas) return;
    const rows = result.rows;
    if (planningCharts.nw) planningCharts.nw.destroy();
    const opts = planningChartOptions(rows);
    const accent = cssVar("--accent");
    const teal = cssVar("--teal");
    const datasets = [
        { label: "Investments", data: rows.map(r => planningUnitValue(r, r.inv)),
          borderColor: accent, backgroundColor: accent + "33", fill: "origin",
          borderWidth: 2, pointRadius: 0, tension: 0.25, stack: "nw" },
        { label: "Home equity", data: rows.map(r => planningUnitValue(r, r.equity)),
          borderColor: teal, backgroundColor: teal + "40", fill: "-1",
          borderWidth: 2, pointRadius: 0, tension: 0.25, stack: "nw" },
        { label: "Other assets − loans", data: rows.map(r => planningUnitValue(r, r.other_net)),
          borderColor: cssVar("--orange"), backgroundColor: cssVar("--orange") + "40", fill: "-1",
          borderWidth: 2, pointRadius: 0, tension: 0.25, stack: "nw" },
        { label: "FI target", data: rows.map(r => r.retired ? null : planningUnitValue(r, r.fi_target)),
          borderColor: cssVar("--chart-muted"), borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0, fill: false, stack: "fi" },
    ];
    const other = planningOtherScenario();
    const show = planningState.compare && other;
    const legend = document.getElementById("pl-legend-compare");
    if (legend) legend.style.display = show ? "" : "none";
    if (show) {
        const otherRows = planningProject(other.assumptions, other.events, planningState.startYear).rows;
        const byAge = {};
        otherRows.forEach(row => { byAge[row.age] = row; });
        const name = document.getElementById("pl-legend-compare-name");
        if (name) name.textContent = other.name + " (net worth)";
        datasets.push({
            label: other.name,
            data: rows.map(row => {
                const match = byAge[row.age];
                return match ? planningUnitValue(match, match.nw) : null;
            }),
            borderColor: cssVar("--indigo"), borderDash: [6, 4], borderWidth: 2,
            pointRadius: 0, fill: false, tension: 0.25, stack: "cmp",
        });
    }
    opts.scales.y.stacked = true;
    opts.plugins.tooltip.callbacks.footer = items => {
        const row = rows[items[0].dataIndex];
        return `Net worth: ${fmt(planningUnitValue(row, row.nw))}`;
    };
    planningCharts.nw = new Chart(canvas, {
        type: "line",
        data: { labels: rows.map(r => r.age), datasets },
        options: opts,
        plugins: [planningMarkerPlugin(rows, planningScenario().assumptions.retAge)],
    });
}

function planningLoanInfo(amount, downPct, rate, term) {
    const principal = amount * (1 - downPct / 100);
    const r = rate / 100;
    const years = Math.max(1, term);
    const payment = r > 0 ? principal * r / (1 - Math.pow(1 + r, -years)) : principal / years;
    return { principal, payment, interest: payment * years - principal };
}

function planningEventMeta(event) {
    switch (event.type) {
        case "house":
            return `Age ${event.age} · ${fmt(event.price)} · ${event.downPct}% down · ${event.rate}% for ${event.term} yrs${event.replacesRent ? " · no more rent" : ""}`;
        case "car":
            return `Age ${event.age} · ${fmt(event.amount)}${+event.every ? ` · every ${event.every} yrs until ${event.until}` : " · once"}`;
        case "child":
            return `Age ${event.age} · ${fmt(event.amount)} / yr for ${event.years} yrs`;
        case "sabbatical":
            return `Age ${event.age} · ${event.years} yr${event.years > 1 ? "s" : ""} · income −${event.pct}%`;
        case "windfall":
            return `Age ${event.age} · one-off`;
        case "extra":
        case "oneoff":
            return `Age ${event.age} · ${fmt(event.amount)} / yr for ${event.years} yr${event.years > 1 ? "s" : ""}`;
        case "shock":
            return `Age ${event.age} · investments −${event.pct}% in one year`;
        case "custom": {
            const how = event.repeat === "yearly" ? `every year for ${event.years} yrs`
                : event.repeat === "every" ? `every ${event.every} yrs until ${event.until}` : "once";
            const parts = [`Age ${event.age}`, `${event.dir === "income" ? "+" : ""}${fmt(event.amount)}`, how];
            if (event.dir !== "income" && event.loan) parts.push(`${event.downPct}% own share, loan ${event.rate}% over ${event.term} yrs`);
            if (event.dir !== "income" && event.asset) parts.push(`asset ${event.assetG >= 0 ? "+" : ""}${event.assetG}%/yr`);
            if (!event.real) parts.push("fixed €");
            return parts.join(" · ");
        }
        default:
            return "";
    }
}

function planningEventImpact(event) {
    switch (event.type) {
        case "house": {
            const loan = planningLoanInfo(event.price, event.downPct, event.rate, event.term);
            const upfront = event.price * (event.downPct + event.tax) / 100;
            return `<span style="color:var(--red)">−${fmt(upfront)}</span><small>upfront · ${fmt(loan.payment / 12)}/mo loan</small>`;
        }
        case "car":
        case "oneoff":
            return `<span style="color:var(--red)">−${fmt(event.amount)}</span><small>${event.type === "car" && +event.every ? "each time" : event.years > 1 ? "per year" : "one-off"}</small>`;
        case "child":
            return `<span style="color:var(--red)">−${fmt(event.amount * event.years)}</span><small>in total</small>`;
        case "sabbatical":
            return `<span style="color:var(--red)">−${event.pct}%</span><small>income</small>`;
        case "windfall":
            return `<span style="color:var(--green)">+${fmt(event.amount)}</span><small>one-off</small>`;
        case "extra":
            return `<span style="color:var(--green)">+${fmt(event.amount * event.years)}</span><small>in total</small>`;
        case "shock":
            return `<span style="color:var(--red)">−${event.pct}%</span><small>portfolio</small>`;
        case "custom": {
            const per = event.repeat === "yearly" ? "per year" : event.repeat === "every" ? "each time" : "one-off";
            if (event.dir === "income") return `<span style="color:var(--green)">+${fmt(event.amount)}</span><small>${per}</small>`;
            if (event.loan) {
                const loan = planningLoanInfo(event.amount, event.downPct, event.rate, event.term);
                return `<span style="color:var(--red)">−${fmt(event.amount * event.downPct / 100)}</span><small>own share · ${fmt(loan.payment / 12)}/mo loan</small>`;
            }
            return `<span style="color:var(--red)">−${fmt(event.amount)}</span><small>${per}</small>`;
        }
        default:
            return "";
    }
}

function planningIcon(type) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${PLANNING_ICONS[type] || ""}</svg>`;
}

// Light mode keeps the hex the icon was designed with. Dark mode follows the
// theme token, which is the same hue turned down so it doesn't glow.
function planningTypePaint(kind) {
    if (document.documentElement.getAttribute("data-theme") === "dark" && kind.token) {
        const color = `var(${kind.token})`;
        return { color, bg: `color-mix(in srgb, ${color} 12%, transparent)` };
    }
    return { color: kind.color, bg: `${kind.color}1f` };
}

function renderPlanningEvents() {
    const box = document.getElementById("pl-events");
    const quick = document.getElementById("pl-quick");
    if (!box) return;
    const events = planningScenario().events.slice().sort((a, b) => a.age - b.age);
    box.innerHTML = events.map(event => {
        const kind = PLANNING_TYPES[event.type];
        if (!kind) return "";
        const paint = planningTypePaint(kind);
        const off = event.on ? "" : ' <span class="pl-tag muted">off</span>';
        return `<div class="pl-event ${event.on ? "" : "off"}">
            <div class="pl-event-icon" style="background:${paint.bg};color:${paint.color}">${planningIcon(event.type)}</div>
            <div style="min-width:0"><div class="pl-event-name">${escapeHtml(event.name)}${off}</div><div class="pl-event-meta">${escapeHtml(planningEventMeta(event))}</div></div>
            <div class="pl-event-impact">${planningEventImpact(event)}</div>
            <div class="pl-event-actions">
                <button type="button" class="pl-switch ${event.on ? "on" : ""}" data-toggle="${event.id}" title="Include in the plan" aria-label="Include in the plan"></button>
                <button type="button" class="btn btn-ghost btn-sm" data-edit="${event.id}">Edit</button>
            </div>
        </div>`;
    }).join("") || '<div class="pl-hint">No events yet. Add one, or pick a kind below.</div>';
    if (quick) {
        quick.innerHTML = '<span class="pl-hint" style="margin-right:4px">Quick add:</span>' +
            Object.keys(PLANNING_TYPES).map(key =>
                `<button type="button" class="btn btn-secondary btn-sm" data-quick="${key}">${PLANNING_TYPES[key].label}</button>`
            ).join("");
    }
}

function renderPlanningTable(result) {
    const table = document.getElementById("pl-table");
    const hint = document.getElementById("pl-table-hint");
    if (!table) return;
    const retAge = planningScenario().assumptions.retAge;
    if (hint) hint.textContent = planningState.unit === "real"
        ? "Amounts in today's € (inflation-adjusted)"
        : "Amounts in future €";
    const cell = (row, value, signed) => {
        const shown = planningUnitValue(row, value);
        const amount = shown == null ? 0 : shown;
        let cls = "";
        if (signed) cls = amount < -0.5 ? "neg" : amount > 0.5 ? "pos" : "";
        else if (amount < -0.5) cls = "neg";
        const text = (signed && amount >= 0 ? "+" : "") + fmt(amount);
        return `<td class="${cls}">${text}</td>`;
    };
    table.innerHTML = `<thead><tr>
        <th>Age</th><th>Year</th><th>Income</th><th>Living</th><th>Home &amp; loan</th>
        <th>Big items</th><th>Net cash flow</th><th>Investments</th><th>Home value</th>
        <th>Other assets</th><th>Loans left</th><th>Net worth</th>
    </tr></thead><tbody>` + result.rows.map(row => {
        const marks = [...row.marks, ...row.minor].map(name => ` <span class="pl-tag muted">${escapeHtml(name)}</span>`).join("");
        const retire = row.age === retAge ? ' <span class="pl-tag">Retire</span>' : "";
        return `<tr class="${row.age === retAge ? "ret" : ""}">
            <td>${row.age}${retire}</td><td>${row.year}${marks}</td>
            ${cell(row, row.income, false)}${cell(row, row.living, false)}${cell(row, row.housing, false)}${cell(row, row.big, false)}
            ${cell(row, row.net, true)}
            ${cell(row, row.inv, false)}${cell(row, row.home_value, false)}${cell(row, row.other_assets, false)}${cell(row, row.loans, false)}
            <td style="font-weight:600" class="${row.nw < 0 ? "neg" : ""}">${fmt(planningUnitValue(row, row.nw))}</td>
        </tr>`;
    }).join("") + "</tbody>";
}

function renderPlanningHeader() {
    const tabs = document.getElementById("pl-scenarios");
    if (tabs) {
        tabs.innerHTML = planningState.scenarios.map((scenario, i) =>
            `<button type="button" class="btn btn-secondary btn-sm ${i === planningState.current ? "active" : ""}" data-scn="${i}">${escapeHtml(scenario.name)}</button>`
        ).join("") + '<button type="button" class="btn btn-secondary btn-sm" data-scn="new" title="Copy the current scenario">+ Scenario</button>';
    }
    document.querySelectorAll("#pl-unit .btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.unit === planningState.unit);
    });
    const compare = document.getElementById("pl-compare");
    const other = planningOtherScenario();
    if (compare) {
        compare.classList.toggle("active", planningState.compare && !!other);
        compare.textContent = planningState.compare && other ? `Comparing with ${other.name}` : "Compare";
        compare.disabled = !other && !planningState.compare;
    }
    const real = document.querySelector('#pl-unit [data-unit="real"]');
    if (real) real.title = `Inflation-adjusted: all amounts in ${planningState.startYear} euros`;
}

function planningUpdate() {
    if (!planningState || !planningState.ready) return;
    if (typeof Chart !== "undefined") {
        Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, sans-serif";
        Chart.defaults.color = cssVar("--text-secondary");
    }
    const scenario = planningScenario();
    const result = planningProject(scenario.assumptions, scenario.events, planningState.startYear);
    renderPlanningHeader();
    renderPlanningSummary(result);
    renderPlanningEvents();
    renderPlanningTable(result);
    renderPlanningCashflow(result);
    renderPlanningNetworth(result);
}

function planningCopyScenario() {
    if (planningState.scenarios.length >= 12) {
        toast("A plan can hold up to 12 scenarios");
        return;
    }
    const copy = JSON.parse(JSON.stringify(planningScenario()));
    copy.name = "Scenario " + (planningState.scenarios.length + 1);
    copy.events.forEach(event => { event.id = planningNextId++; });
    planningState.scenarios.push(copy);
    planningState.current = planningState.scenarios.length - 1;
    renderPlanningFields();
    planningUpdate();
    schedulePlanningSave();
    toast("Copied as " + copy.name);
}

function planningPick(source, keys) {
    const out = {};
    keys.forEach(key => { if (source && Object.prototype.hasOwnProperty.call(source, key)) out[key] = source[key]; });
    return out;
}

function openPlanningEvent(event, type) {
    planningModal.editing = event || null;
    planningModal.type = event ? event.type : (type || "house");
    planningModal.draft = event
        ? Object.assign({}, event)
        : Object.assign({}, PLANNING_EVENT_DEFAULTS[planningModal.type]);
    renderPlanningModal();
    const overlay = document.getElementById("planning-event-overlay");
    if (overlay) overlay.style.display = "flex";
}

function closePlanningEvent() {
    const overlay = document.getElementById("planning-event-overlay");
    if (overlay) overlay.style.display = "none";
    planningModal.editing = null;
}

function renderPlanningModal() {
    const title = document.getElementById("pl-modal-title");
    const types = document.getElementById("pl-modal-types");
    const fields = document.getElementById("pl-modal-fields");
    const del = document.getElementById("pl-modal-delete");
    if (!fields) return;
    const draft = planningModal.draft;
    if (title) title.textContent = planningModal.editing ? `Edit: ${planningModal.editing.name}` : "Add life event";
    if (del) del.style.visibility = planningModal.editing ? "visible" : "hidden";
    if (types) {
        types.innerHTML = Object.keys(PLANNING_TYPES).map(key => {
            const kind = PLANNING_TYPES[key];
            const active = key === planningModal.type ? "active" : "";
            const disabled = planningModal.editing ? "disabled" : "";
            return `<button type="button" class="pl-type ${active}" data-type="${key}" ${disabled}>${planningIcon(key)}${kind.label}</button>`;
        }).join("");
    }
    const spec = PLANNING_TYPES[planningModal.type];
    fields.innerHTML = spec.fields.filter(field => !field[3] || field[3](draft)).map(field => {
        const key = field[0];
        const label = typeof field[1] === "function" ? field[1](draft) : field[1];
        const kind = field[2] || "number";
        if (kind === "check") {
            return `<label class="full pl-check"><input type="checkbox" data-mk="${key}" ${draft[key] ? "checked" : ""}> ${escapeHtml(label)}</label>`;
        }
        if (kind === "select") {
            const options = (field[4] || []).map(([value, text]) =>
                `<option value="${value}" ${draft[key] === value ? "selected" : ""}>${escapeHtml(text)}</option>`
            ).join("");
            return `<div><label class="form-label">${escapeHtml(label)}</label><select class="form-select" data-mk="${key}">${options}</select></div>`;
        }
        const wide = kind === "text" ? "full" : "";
        const inputType = kind === "text" ? "text" : "number";
        const shown = draft[key] == null ? "" : draft[key];
        return `<div class="${wide}"><label class="form-label">${escapeHtml(label)}</label><input class="form-input" data-mk="${key}" type="${inputType}" value="${escapeHtml(shown)}"></div>`;
    }).join("")
        + (planningModal.type === "custom" ? `<div class="full pl-hint" id="pl-custom-preview"></div>` : "")
        + (planningModal.type === "house" ? `<div class="full pl-hint">Down payment and transfer tax come out of your investments in the purchase year. The loan is a fixed annuity (the same payment every year). Upkeep grows with inflation; the home's value grows at its own rate.</div>` : "");
    if (planningModal.type === "custom") planningCustomPreview();
}

function planningCustomPreview() {
    const box = document.getElementById("pl-custom-preview");
    if (!box) return;
    const draft = planningModal.draft;
    if (draft.dir === "income") {
        box.textContent = "Money in is added to that year's income and invested like any other surplus.";
        return;
    }
    const bits = [];
    if (draft.loan) {
        const loan = planningLoanInfo(+draft.amount || 0, +draft.downPct || 0, +draft.rate || 0, +draft.term || 1);
        bits.push(`You pay <b>${fmt((+draft.amount || 0) * (+draft.downPct || 0) / 100)}</b> yourself and borrow <b>${fmt(loan.principal)}</b>: <b>${fmt(loan.payment)}</b> a year (${fmt(loan.payment / 12)}/mo) for ${draft.term} years, ${fmt(loan.interest)} interest in total. Debt left counts against net worth.`);
    } else bits.push("Paid in full from your investments in the year it happens.");
    if (draft.asset) {
        const sign = draft.assetG >= 0 ? "+" : "";
        const trade = draft.repeat === "every" ? "; the old one is traded in when you replace it" : "";
        bits.push(`Its value (${draft.assetPct}% of price, ${sign}${draft.assetG}% a year) counts in net worth${trade}.`);
    }
    if (draft.real) bits.push("Amounts are in today's € and rise with inflation.");
    box.innerHTML = bits.join(" ");
}

function planningSyncDraft() {
    document.querySelectorAll("#pl-modal-fields [data-mk]").forEach(input => {
        if (input.type === "checkbox") planningModal.draft[input.dataset.mk] = input.checked;
        else if (input.type === "number") planningModal.draft[input.dataset.mk] = parseFloat(input.value) || 0;
        else planningModal.draft[input.dataset.mk] = input.value;
    });
}

function planningSaveEvent() {
    planningSyncDraft();
    const draft = planningModal.draft;
    const name = String(draft.name || "").trim();
    if (!name) { toast("An event needs a name"); return; }
    if (name.length > 80) { toast("An event name can be at most 80 characters"); return; }
    draft.name = name;
    if (planningModal.editing) {
        const events = planningScenario().events;
        const index = events.indexOf(planningModal.editing);
        const kept = { id: planningModal.editing.id, type: planningModal.editing.type, on: planningModal.editing.on };
        const next = Object.assign({}, draft, kept);
        if (index >= 0) events[index] = next;
        toast("Event updated");
    } else {
        if (planningScenario().events.length >= 80) {
            toast("A scenario can hold up to 80 events");
            return;
        }
        planningScenario().events.push(Object.assign(
            { id: planningNextId++, type: planningModal.type, on: true },
            PLANNING_EVENT_DEFAULTS[planningModal.type],
            draft,
        ));
        toast("Added “" + name + "”");
    }
    closePlanningEvent();
    planningUpdate();
    schedulePlanningSave();
}

async function planningDeleteEvent() {
    const event = planningModal.editing;
    if (!event) return;
    const ok = await confirmDialog({
        title: "Delete this event?",
        body: event.name,
        confirmLabel: "Delete",
        danger: true,
    });
    if (!ok) return;
    planningScenario().events = planningScenario().events.filter(item => item !== event && item.id !== event.id);
    closePlanningEvent();
    planningUpdate();
    schedulePlanningSave();
    toast("Event deleted");
}

function planningOnPageClick(e) {
    const target = e.target.closest("[data-toggle],[data-edit],[data-quick],[data-scn],[data-unit],[data-pl]");
    if (!target || !planningState) return;
    if (target.dataset.toggle) {
        const event = planningScenario().events.find(item => String(item.id) === target.dataset.toggle);
        if (event) event.on = !event.on;
        planningUpdate();
        schedulePlanningSave();
    } else if (target.dataset.edit) {
        const event = planningScenario().events.find(item => String(item.id) === target.dataset.edit);
        if (event) openPlanningEvent(event);
    } else if (target.dataset.quick) {
        openPlanningEvent(null, target.dataset.quick);
    } else if (target.dataset.scn !== undefined) {
        if (target.dataset.scn === "new") planningCopyScenario();
        else {
            planningState.current = +target.dataset.scn;
            renderPlanningFields();
            planningUpdate();
            schedulePlanningSave();
        }
    } else if (target.dataset.unit) {
        planningState.unit = target.dataset.unit;
        planningUpdate();
        schedulePlanningSave();
    } else if (target.dataset.pl === "compare") {
        if (!planningOtherScenario()) {
            toast("Add a second scenario to compare");
            return;
        }
        planningState.compare = !planningState.compare;
        planningUpdate();
        schedulePlanningSave();
    } else if (target.dataset.pl === "reset") {
        planningReset();
    } else if (target.dataset.pl === "add") {
        openPlanningEvent(null, "house");
    }
}

function planningOnModalClick(e) {
    if (e.target.id === "planning-event-overlay") { closePlanningEvent(); return; }
    const target = e.target.closest("[data-type],[data-pl]");
    if (!target || target.disabled) return;
    if (target.dataset.type && !planningModal.editing) {
        planningSyncDraft();
        planningModal.type = target.dataset.type;
        planningModal.draft = Object.assign(
            {}, PLANNING_EVENT_DEFAULTS[planningModal.type], planningPick(planningModal.draft, ["age"]),
        );
        renderPlanningModal();
    } else if (target.dataset.pl === "cancel") closePlanningEvent();
    else if (target.dataset.pl === "save") planningSaveEvent();
    else if (target.dataset.pl === "delete") planningDeleteEvent();
}

function planningOnModalChange(e) {
    if (!e.target.matches("select, input[type=checkbox]")) return;
    planningSyncDraft();
    renderPlanningModal();
}

function planningOnModalInput() {
    if (!document.getElementById("pl-custom-preview")) return;
    planningSyncDraft();
    planningCustomPreview();
}

function planningBind() {
    if (planningBound) return;
    planningBound = true;
    const page = document.getElementById("page-planning");
    const fields = document.getElementById("pl-fields");
    const overlay = document.getElementById("planning-event-overlay");
    const modalFields = document.getElementById("pl-modal-fields");
    if (page) page.addEventListener("click", planningOnPageClick);
    if (fields) fields.addEventListener("input", planningOnField);
    if (overlay) overlay.addEventListener("click", planningOnModalClick);
    if (modalFields) {
        modalFields.addEventListener("change", planningOnModalChange);
        modalFields.addEventListener("input", planningOnModalInput);
    }
    window.addEventListener("beforeunload", flushPlanningSave);
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden") flushPlanningSave();
    });
}

function planningApply(data) {
    const plan = data.plan;
    planningState = {
        ready: true,
        startYear: data.start_year,
        defaults: data.defaults,
        baseline: data.baseline,
        scenarios: plan ? plan.scenarios : [{
            name: "Base plan",
            assumptions: Object.assign({}, data.defaults),
            events: [],
        }],
        current: plan ? plan.current : 0,
        unit: plan ? plan.unit : "real",
        compare: plan ? !!plan.compare : false,
    };
    planningNextId = 1;
    planningState.scenarios.forEach(scenario => {
        scenario.events.forEach(event => {
            if (event.id >= planningNextId) planningNextId = event.id + 1;
        });
    });
    planningBind();
    planningBanner();
    renderPlanningFields();
    planningUpdate();
}

async function planningReset() {
    const data = await api("/api/planning");
    planningState.defaults = data.defaults;
    planningState.baseline = data.baseline;
    planningState.startYear = data.start_year;
    planningScenario().assumptions = Object.assign({}, data.defaults);
    planningBanner();
    renderPlanningFields();
    planningUpdate();
    schedulePlanningSave();
    toast("Assumptions reset to your Balance figures");
}

function loadPlanning() {
    planningBind();
    if (planningState && planningState.ready) {
        planningUpdate();
        return;
    }
    if (planningLoading) return;
    planningLoading = true;
    api("/api/planning").then(data => {
        planningLoading = false;
        planningApply(data);
    }).catch(err => {
        planningLoading = false;
        toast((err && err.message) || "Could not load the plan");
    });
}
