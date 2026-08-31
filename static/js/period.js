// Balance — period.js
//
// The period controls shared by the pages that show a range: the horizon
// buttons, the month picker, and the floating pill.
//
// One of the ordered classic scripts index.html loads. They share one global
// scope on purpose: index.html wires ~95 inline handlers straight to these
// names, so these are plain scripts and not modules.

// ── Horizon buttons ─────────────────────────────────────────────────
function setHorizon(value) {
    dashboardHorizon = String(value);
    selectedPeriods.clear();
    document.querySelectorAll(".horizon-btn").forEach(b => {
        b.classList.toggle("active", b.dataset.horizon === dashboardHorizon);
    });
    updatePeriodBtn();
    loadDashboard();
}

// ── Period dropdown ─────────────────────────────────────────────────
function togglePeriodDropdown() {
    const dd = document.getElementById("period-dropdown");
    const open = dd.style.display === "none";
    dd.style.display = open ? "block" : "none";
    if (open) buildPeriodDropdown();
}

// Close on outside click
document.addEventListener("click", e => {
    const sel = document.getElementById("period-selector");
    if (sel && !sel.contains(e.target)) {
        const dd = document.getElementById("period-dropdown");
        if (dd) dd.style.display = "none";
    }
});

function buildPeriodDropdown() {
    const container = document.getElementById("period-dropdown-items");
    if (!container) return;

    const monthSet = [...new Set(cachedMonthly.map(r => r.month))].sort().reverse();
    const yearMap = {};
    monthSet.forEach(m => {
        const y = m.slice(0, 4);
        if (!yearMap[y]) yearMap[y] = [];
        yearMap[y].push(m);
    });
    const years = Object.keys(yearMap).sort().reverse();

    let html = "";

    if (selectedPeriods.size > 0) {
        html += `<div class="period-clear-row" onclick="clearPeriods()">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M18 6L6 18M6 6l12 12"/></svg>
            Clear all <span style="color:var(--text-tertiary)">(${selectedPeriods.size} selected)</span>
        </div>`;
    }

    years.forEach(year => {
        const yearMonths   = yearMap[year];
        const selCount     = yearMonths.filter(m => selectedPeriods.has(m)).length;
        const allSel       = selCount === yearMonths.length;
        const someSel      = selCount > 0 && !allSel;
        const expanded     = periodYearExpanded[year] ?? false;
        const monthsJoined = yearMonths.join(",");

        html += `<div class="period-year-row">
            <label class="period-cb-label" onclick="event.stopPropagation()">
                <input type="checkbox" class="period-checkbox year-cb" data-year="${year}"
                       ${allSel ? "checked" : ""}
                       onchange="toggleYearSelection('${year}','${monthsJoined}',this.checked)">
            </label>
            <span class="period-year-text" onclick="toggleYearExpand('${year}')">${year}
                ${selCount > 0 ? `<span style="font-size:10px;color:var(--accent);margin-left:4px">${selCount}/${yearMonths.length}</span>` : ""}
            </span>
            <span class="period-year-chevron ${expanded ? "open" : ""}" onclick="toggleYearExpand('${year}')">&#9660;</span>
        </div>
        <div class="period-months" id="period-months-${year}" style="display:${expanded ? "block" : "none"}">
            ${yearMonths.map(m => `
                <div class="period-month-row ${selectedPeriods.has(m) ? "selected" : ""}">
                    <label class="period-cb-label" onclick="event.stopPropagation()">
                        <input type="checkbox" class="period-checkbox"
                               ${selectedPeriods.has(m) ? "checked" : ""}
                               onchange="toggleMonthSelection('${m}')">
                    </label>
                    <span onclick="toggleMonthSelection('${m}')">${monthLabelFull(m)}</span>
                </div>`).join("")}
        </div>`;
    });

    container.innerHTML = html || `<div style="padding:16px;text-align:center;color:var(--text-tertiary);font-size:var(--text-subhead)">No data yet</div>`;

    // Set indeterminate state (can't be done in HTML)
    document.querySelectorAll(".year-cb").forEach(cb => {
        const year      = cb.dataset.year;
        const yMonths   = (yearMap[year] || []);
        const selCount  = yMonths.filter(m => selectedPeriods.has(m)).length;
        cb.indeterminate = selCount > 0 && selCount < yMonths.length;
    });
}

function toggleYearExpand(year) {
    periodYearExpanded[year] = !periodYearExpanded[year];
    buildPeriodDropdown();
}

async function toggleYearSelection(year, monthsStr, checked) {
    const yearMonths = monthsStr.split(",").filter(Boolean);
    if (checked) {
        yearMonths.forEach(m => selectedPeriods.add(m));
    } else {
        yearMonths.forEach(m => selectedPeriods.delete(m));
    }
    _syncHorizonAfterPeriodChange();
    updatePeriodBtn();
    buildPeriodDropdown();
    await applyPeriodFilter();
}

async function toggleMonthSelection(month) {
    if (selectedPeriods.has(month)) {
        selectedPeriods.delete(month);
    } else {
        selectedPeriods.add(month);
    }
    _syncHorizonAfterPeriodChange();
    updatePeriodBtn();
    buildPeriodDropdown();
    await applyPeriodFilter();
}

function clearPeriods() {
    selectedPeriods.clear();
    dashboardHorizon = "ytd";
    document.querySelectorAll(".horizon-btn").forEach(b => {
        b.classList.toggle("active", b.dataset.horizon === "ytd");
    });
    updatePeriodBtn();
    buildPeriodDropdown();
    applyPeriodFilter();
}

function _syncHorizonAfterPeriodChange() {
    if (selectedPeriods.size > 0) {
        dashboardHorizon = null;
        document.querySelectorAll(".horizon-btn").forEach(b => b.classList.remove("active"));
    } else {
        dashboardHorizon = "ytd";
        document.querySelectorAll(".horizon-btn").forEach(b => {
            b.classList.toggle("active", b.dataset.horizon === "ytd");
        });
    }
}

function updatePeriodBtn() {
    const btn   = document.getElementById("period-selector-btn");
    const label = document.getElementById("period-label");
    if (!btn || !label) return;
    if (selectedPeriods.size === 0) {
        label.textContent = "Pick period";
        btn.classList.remove("active-period");
    } else if (selectedPeriods.size === 1) {
        label.textContent = monthLabelFull([...selectedPeriods][0]);
        btn.classList.add("active-period");
    } else {
        label.textContent = `${selectedPeriods.size} months`;
        btn.classList.add("active-period");
    }
}

// Apply period filter using cached monthly data (no full reload)
async function applyPeriodFilter() {
    if (!cachedMonthly.length) { loadDashboard(); return; }
    const filtered = filterData(cachedMonthly);
    renderSummaryCards(filtered);
    renderMonthlyChart(filtered);
    if (cachedTopExpenses) drawTrendsFromData(cachedTopExpenses);
    renderSummaryTable(cachedMonthly);
    // Category bars need fresh API call for correct aggregation
    const catBreakdown = await api(expenseBreakdownUrl());
    renderCategoryBars(catBreakdown);
    await loadIncomeBreakdown(catBreakdown);
}

// ── Dashboard: floating period pill ─────────────────────────────────
// Once the header has scrolled away, the horizon buttons and the period
// picker re-form as a pill over the content. The controls are MOVED, not
// copied, so there is only ever one period dropdown in the DOM.
function initDashboardFloatPill() {
    const page = document.getElementById("page-dashboard");
    const dock = document.getElementById("dash-float-dock");
    const pill = document.getElementById("dash-float-pill");
    const home = document.getElementById("dash-header-controls");
    if (!page || !dock || !pill || !home) return;

    const title = page.querySelector(".page-title");
    let floating = false;

    function setFloating(on) {
        if (on === floating) return;
        floating = on;
        const from = on ? home : pill;
        const to   = on ? pill : home;
        [...from.children].forEach(n => to.appendChild(n));
        dock.classList.toggle("shown", on);
    }

    // The title is the landmark: it never changes container, so moving the
    // controls can't feed back into the measurement. Float once it is gone.
    function update() {
        setFloating(page.classList.contains("active") &&
                    title.getBoundingClientRect().bottom < 0);
    }

    window.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    // Switching tabs doesn't reset the scroll position, so re-check after
    // every page change — the handler at the top of this file has already
    // moved `.active` by the time this one runs.
    document.querySelectorAll(".nav-item[data-page]")
            .forEach(btn => btn.addEventListener("click", update));
    update();
}

