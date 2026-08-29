// ── State ────────────────────────────────────────────────────────────
let categories = [];
let currentPage = 1;
let stagingBatchId = null;
let stagingItems = [];   // may contain split virtual items
let stagingMeta = { filename: "" };   // set by whichever path enters review
let charts = {};
let merchantRules = [];
let monthsWithNotes = new Set();

// Calendar state
let calendarMonth = null;  // "YYYY-MM"
let calendarData = {};     // date -> {expense, income}

// ── CSRF: global fetch wrapper ───────────────────────────────────────
// Server enforces a double-submit CSRF token on POST/PUT/PATCH/DELETE. Rather
// than touch each of the ~21 mutating call sites, we wrap window.fetch once so
// every same-origin mutating request automatically carries the X-CSRF-Token
// header. The token comes from the non-httpOnly `csrf_token` cookie the server
// sets on each response (also cached from /api/me as a fallback). GET/HEAD and
// cross-origin requests are passed through untouched.
const CSRF_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
let csrfTokenCache = null;  // populated by /api/me (loadAppState) as a fallback

function readCsrfCookie() {
    const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : null;
}

function csrfToken() {
    return readCsrfCookie() || csrfTokenCache;
}

(function installCsrfFetch() {
    const nativeFetch = window.fetch.bind(window);
    window.fetch = function (input, init = {}) {
        const method = (init.method || (typeof input !== "string" && input.method) || "GET").toUpperCase();
        // Determine the request URL to confirm it's same-origin.
        let url;
        try {
            url = new URL(typeof input === "string" ? input : input.url, window.location.origin);
        } catch (e) {
            url = null;
        }
        const sameOrigin = !url || url.origin === window.location.origin;
        if (CSRF_METHODS.has(method) && sameOrigin) {
            const token = csrfToken();
            if (token) {
                const headers = new Headers(init.headers || (typeof input !== "string" && input.headers) || undefined);
                if (!headers.has("X-CSRF-Token")) headers.set("X-CSRF-Token", token);
                init = { ...init, headers };
            }
        }
        const promise = nativeFetch(input, init);
        // Show the top loading bar while any real request is in flight (skip the
        // silent keep-alive pings so the bar doesn't flash every 10 minutes).
        if (!url || !url.pathname.startsWith("/healthz")) {
            beginLoading();
            promise.then(endLoading, endLoading);
        }
        return promise;
    };
})();

// ── Global loading indicator ────────────────────────────────────────
// The overlay only appears when a request is actually slow (>400 ms). Fast
// page switches render without the full-screen blur flash; long work (first
// recurring scan, big imports) still gets covered (design #4).
let _loadingCount = 0;
let _loadingTimer = null;
const LOADING_DELAY_MS = 400;
function beginLoading() {
    _loadingCount++;
    if (_loadingCount === 1 && !_loadingTimer) {
        _loadingTimer = setTimeout(() => {
            _loadingTimer = null;
            if (_loadingCount > 0) document.documentElement.classList.add("loading");
        }, LOADING_DELAY_MS);
    }
}
function endLoading() {
    _loadingCount = Math.max(0, _loadingCount - 1);
    if (_loadingCount === 0) {
        if (_loadingTimer) { clearTimeout(_loadingTimer); _loadingTimer = null; }
        document.documentElement.classList.remove("loading");
    }
}

// ── API Helper ──────────────────────────────────────────────────────
async function api(url, options = {}) {
    if (options.body && typeof options.body === "object" && !(options.body instanceof FormData)) {
        options.headers = { "Content-Type": "application/json", ...options.headers };
        options.body = JSON.stringify(options.body);
    }
    const res = await fetch(url, options);
    if (res.status === 204) return null;
    return res.json();
}

// ── Toast ───────────────────────────────────────────────────────────
function toast(message) {
    const container = document.getElementById("toast-container");
    const el = document.createElement("div");
    el.className = "toast";
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => el.remove(), 3000);
}

// ── Navigation ──────────────────────────────────────────────────────
// Scope to page tabs only — sidebar action buttons (theme toggle, Sync, Quit)
// are also .nav-item but have no data-page, so they must not hijack the
// active tab or blank the page.
document.querySelectorAll(".nav-item[data-page]").forEach(btn => {
    btn.addEventListener("click", () => {
        document.querySelectorAll(".nav-item[data-page]").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
        document.getElementById("page-" + btn.dataset.page).classList.add("active");

        switch (btn.dataset.page) {
            case "dashboard": loadDashboard(); break;
            case "transactions": loadTransactions(); break;
            case "categories": loadCategories(); break;
            case "trends": loadTrends(); break;
            case "subscriptions": loadRecurring(); break;
            case "reports": loadReport(); break;
            case "networth": loadNetWorth(); break;
            case "import": loadBankStatus(); break;
            case "settings": break;
        }

        // Tapping a nav tab closes the mobile drawer (no-op on desktop).
        closeMobileNav();
    });
});

// ── Mobile navigation drawer ─────────────────────────────────────────
// Toggling body.nav-open drives the off-canvas .sidebar + .nav-backdrop
// purely via CSS @media rules; on desktop the class has no visual effect.
function toggleMobileNav() {
    if (document.body.classList.contains("nav-open")) closeMobileNav();
    else openMobileNav();
}

function openMobileNav() {
    document.body.classList.add("nav-open");
    const burger = document.getElementById("mobile-hamburger");
    if (burger) burger.setAttribute("aria-expanded", "true");
}

function closeMobileNav() {
    document.body.classList.remove("nav-open");
    const burger = document.getElementById("mobile-hamburger");
    if (burger) burger.setAttribute("aria-expanded", "false");
}

// Escape closes the drawer when it's open.
document.addEventListener("keydown", e => {
    if (e.key === "Escape" && document.body.classList.contains("nav-open")) closeMobileNav();
});

// ── Theme (light / dark / auto) ──────────────────────────────────────
const THEME_KEY = "theme";
const darkMql = window.matchMedia("(prefers-color-scheme: dark)");

// Resolve a stored preference ("light" | "dark" | "auto") to an actual theme.
function resolvedTheme(pref) {
    pref = pref || localStorage.getItem(THEME_KEY) || "light";
    return (pref === "dark" || (pref === "auto" && darkMql.matches)) ? "dark" : "light";
}

function applyThemeAttr(pref) {
    document.documentElement.setAttribute("data-theme", resolvedTheme(pref));
}

// Reflect the current preference in both the Settings segmented control
// and the sidebar quick-toggle (which shows the action it will perform).
function syncThemeControls(pref) {
    pref = pref || localStorage.getItem(THEME_KEY) || "light";
    document.querySelectorAll(".theme-seg-btn").forEach(b => {
        b.classList.toggle("active", b.dataset.themeChoice === pref);
    });
    const isDark = resolvedTheme(pref) === "dark";
    const sun = document.getElementById("theme-icon-sun");
    const moon = document.getElementById("theme-icon-moon");
    const label = document.getElementById("theme-quick-label");
    if (sun && moon && label) {
        sun.style.display  = isDark ? "" : "none";
        moon.style.display = isDark ? "none" : "";
        label.textContent  = isDark ? "Light mode" : "Dark mode";
    }
}

function setTheme(pref) {
    localStorage.setItem(THEME_KEY, pref);
    applyThemeAttr(pref);
    syncThemeControls(pref);
    refreshChartsForTheme();
}

// Sidebar quick toggle: flip between explicit light and dark.
function toggleTheme() {
    setTheme(resolvedTheme() === "dark" ? "light" : "dark");
}

// While "auto", track OS appearance changes live.
darkMql.addEventListener("change", () => {
    if ((localStorage.getItem(THEME_KEY) || "light") === "auto") {
        applyThemeAttr("auto");
        syncThemeControls("auto");
        refreshChartsForTheme();
    }
});

function initTheme() {
    const pref = localStorage.getItem(THEME_KEY) || "light";
    applyThemeAttr(pref);   // head script already set this; harmless re-apply
    syncThemeControls(pref);
}

// ── Chart theming ────────────────────────────────────────────────────
function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Convert a hex CSS variable (#rgb / #rrggbb) into an rgba() string.
// Consumed tokens are expected to be hex; the guards below just keep us
// safe (no NaN output) if one is ever pointed at a non-hex value.
function rgbaVar(name, alpha) {
    const raw = cssVar(name);
    // Already an rgb()/rgba() value: re-emit with the requested alpha.
    if (raw.startsWith("rgb")) {
        const nums = raw.match(/[\d.]+/g);
        if (nums && nums.length >= 3) {
            return `rgba(${nums[0]}, ${nums[1]}, ${nums[2]}, ${alpha})`;
        }
        return raw;
    }
    let h = raw.replace("#", "");
    if (h.length === 3) h = h.split("").map(c => c + c).join("");
    const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
    // Hex parse failed (non-hex token): fall back to the raw value.
    if (Number.isNaN(r) || Number.isNaN(g) || Number.isNaN(b)) return raw;
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

// Live, theme-aware palette read from the CSS variables.
function chartTheme() {
    const dark = resolvedTheme() === "dark";
    return {
        text:          cssVar("--text-secondary"),
        tick:          cssVar("--text-tertiary"),
        grid:          dark ? "rgba(255,255,255,0.07)" : "rgba(60,60,67,0.06)",
        gridZero:      dark ? "rgba(255,255,255,0.20)" : "rgba(60,60,67,0.22)",
        accent:        cssVar("--accent"),
        green:         cssVar("--green"),
        red:           cssVar("--red"),
        purple:        cssVar("--purple"),
        orange:        cssVar("--orange"),
        surface:       cssVar("--bg"),
        tooltipBg:     dark ? "rgba(10,13,20,0.94)"      : "rgba(28,28,30,0.92)",
        tooltipText:   dark ? "#e7ebf2"                  : "#ffffff",
        tooltipBorder: dark ? "rgba(255,255,255,0.10)"   : "rgba(255,255,255,0.08)",
    };
}

function applyChartDefaults() {
    if (typeof Chart === "undefined") return;
    const t = chartTheme();
    Chart.defaults.color = t.text;
    Chart.defaults.borderColor = t.grid;
    Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, sans-serif";
}

// Re-run the active page's loader so its charts/bars re-render with the
// current theme + category palette.
function rerenderActivePage() {
    const active = document.querySelector(".nav-item[data-page].active")?.dataset.page;
    switch (active) {
        case "dashboard":    loadDashboard();    break;
        case "transactions": loadTransactions(); break;
        case "categories":   loadCategories();   break;
        case "trends":       loadTrends();       break;
        case "subscriptions": loadRecurring();   break;
        case "reports":      loadReport();       break;
        case "networth":     loadNetWorth();     break;
    }
}

// Re-render the charts on the active page so they pick up the new theme.
function refreshChartsForTheme() {
    if (typeof Chart === "undefined") return;
    applyChartDefaults();
    rerenderActivePage();
}

// ── Category color palette (user-selectable in Settings) ─────────────
const CATEGORY_PALETTES = {
    vibrant: { name: "Vibrant", desc: "Lively & most distinct",
        colors: ["#10b981","#06b6d4","#3b82f6","#6366f1","#8b5cf6","#ec4899","#f43f5e","#f59e0b","#84cc16","#14b8a6","#a855f7","#ef4444"] },
    cool: { name: "Cool analogous", desc: "Green → blue → violet, no warm",
        colors: ["#00a06b","#0d9488","#0891b2","#0284c7","#2563eb","#4f46e5","#6d28d9","#7c3aed","#9333ea","#0e7490","#1d4ed8","#5b21b6"] },
    green: { name: "Green / teal", desc: "Greens & teals only — most on-brand",
        colors: ["#0b6e4f","#00a06b","#0d9488","#11998e","#16a34a","#15803d","#0e7c66","#2a9d8f","#1a936f","#2d6a4f","#40916c","#1e7a5e"] },
    muted: { name: "Muted jewel", desc: "Desaturated & sophisticated",
        colors: ["#3f8f7b","#4a7fa5","#6b6b9e","#8a6a9e","#a86a78","#b08968","#9e8a5e","#5e8a6b","#6a8caf","#7d7d92","#a47b8e","#8f9e6a"] },
    slate: { name: "Accent + slate", desc: "Green accent over cool grays — quiet",
        colors: ["#00a06b","#5b8a72","#0d9488","#4a6b8a","#64748b","#475569","#7c8da3","#33586a","#8a9ba8","#3d5a5a","#5f7d6e","#6b7f8c"] },
};
const PALETTE_KEY = "categoryPalette";
const DEFAULT_PALETTE = "vibrant";

function activePaletteKey() {
    const k = localStorage.getItem(PALETTE_KEY);
    return CATEGORY_PALETTES[k] ? k : DEFAULT_PALETTE;
}

// Mutable so existing CHART_COLORS[...] reads pick up the chosen palette.
let CHART_COLORS = CATEGORY_PALETTES[activePaletteKey()].colors;

function setCategoryPalette(key) {
    if (!CATEGORY_PALETTES[key]) return;
    localStorage.setItem(PALETTE_KEY, key);
    CHART_COLORS = CATEGORY_PALETTES[key].colors;
    syncPaletteControls(key);
    applyChartDefaults();
    rerenderActivePage();
}

// Build the palette picker rows in Settings (swatches come from the data).
function renderPaletteOptions() {
    const host = document.getElementById("palette-options");
    if (!host) return;
    host.innerHTML = Object.entries(CATEGORY_PALETTES).map(([key, p]) => `
        <button type="button" class="palette-opt" data-palette="${key}" onclick="setCategoryPalette('${key}')">
            <span class="palette-swatches">${p.colors.slice(0, 9).map(c => `<span style="background:${c}"></span>`).join("")}</span>
            <span class="palette-meta"><span class="palette-name">${p.name}</span><span class="palette-desc">${p.desc}</span></span>
            <span class="palette-check">✓</span>
        </button>`).join("");
    syncPaletteControls(activePaletteKey());
}

function syncPaletteControls(key) {
    document.querySelectorAll(".palette-opt").forEach(el => {
        el.classList.toggle("active", el.dataset.palette === key);
    });
}

// ── Format Helpers ──────────────────────────────────────────────────
function fmt(amount) {
    return new Intl.NumberFormat("fi-FI", { style: "currency", currency: "EUR", maximumFractionDigits: 0 }).format(amount);
}

// Row-level money keeps cents; fmt() (whole euros) stays for aggregates/charts.
function fmt2(amount) {
    return new Intl.NumberFormat("fi-FI", { style: "currency", currency: "EUR", minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(amount);
}

function fmtDate(dateStr) {
    const d = new Date(dateStr + "T00:00:00");
    return d.toLocaleDateString("fi-FI", { day: "numeric", month: "short", year: "numeric" });
}

function monthLabel(ym) {
    const [y, m] = ym.split("-");
    const d = new Date(parseInt(y), parseInt(m) - 1);
    return d.toLocaleDateString("en-US", { month: "short", year: "2-digit" });
}

function monthLabelFull(ym) {
    const [y, m] = ym.split("-");
    const d = new Date(parseInt(y), parseInt(m) - 1);
    return d.toLocaleDateString("en-US", { month: "long", year: "numeric" });
}

function escapeHtml(s) {
    return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

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
    buildCatFilterChips();
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
    document.querySelector(".modal-overlay").remove();
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
    document.querySelector(".modal-overlay").remove();
    await loadCategories();
    toast("Category deleted");
}

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
    if (!confirm(`Re-apply "${rule.pattern}" → ${rule.category_name} to all historical transactions?`)) return;
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
    if (!confirm("Delete this merchant rule?")) return;
    await api(`/api/merchant-rules/${id}`, { method: "DELETE" });
    merchantRules = merchantRules.filter(r => r.id !== id);
    renderMerchantRules();
    toast("Rule deleted");
}

// ── Search helpers ──────────────────────────────────────────────────
let searchDebounce = null;
let advancedOpen = false;
let selectedCatIds = new Set();

function debounceSearch() {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => { currentPage = 1; loadTransactions(); }, 320);
    const q = document.getElementById("search-q").value;
    document.getElementById("search-clear").style.display = q ? "flex" : "none";
}

function clearSearch() {
    document.getElementById("search-q").value = "";
    document.getElementById("search-clear").style.display = "none";
    currentPage = 1;
    loadTransactions();
}

function toggleAdvancedSearch() {
    advancedOpen = !advancedOpen;
    document.getElementById("advanced-search").style.display = advancedOpen ? "block" : "none";
    document.getElementById("toggle-advanced-btn").classList.toggle("active-filter", advancedOpen);
}

function buildCatFilterChips() {
    const wrap = document.getElementById("cat-filter-wrap");
    if (!wrap) return;
    const expenseCats = categories.filter(c => c.type === "expense");
    const incomeCats  = categories.filter(c => c.type === "income");
    const all = [...expenseCats, ...incomeCats];
    wrap.innerHTML = all.map(c => `
        <button class="cat-chip ${selectedCatIds.has(c.id) ? "selected" : ""}"
                onclick="toggleCatFilter(${c.id})">${escapeHtml(c.name)}</button>
    `).join("");
}

function toggleCatFilter(id) {
    if (selectedCatIds.has(id)) selectedCatIds.delete(id);
    else selectedCatIds.add(id);
    buildCatFilterChips();
    currentPage = 1;
    loadTransactions();
}

function resetSearch() {
    document.getElementById("search-q").value = "";
    document.getElementById("filter-type").value = "";
    document.getElementById("search-date-from").value = "";
    document.getElementById("search-date-to").value = "";
    document.getElementById("search-amt-min").value = "";
    document.getElementById("search-amt-max").value = "";
    document.getElementById("search-sort").value = "date";
    document.getElementById("search-dir").value = "desc";
    document.getElementById("search-clear").style.display = "none";
    selectedCatIds.clear();
    buildCatFilterChips();
    currentPage = 1;
    loadTransactions();
}

// ── Transactions ────────────────────────────────────────────────────
async function loadTransactions() {
    const type     = document.getElementById("filter-type")?.value || "";
    const q        = document.getElementById("search-q")?.value.trim() || "";
    const dateFrom = fiToIso(document.getElementById("search-date-from")?.value) || "";
    const dateTo   = fiToIso(document.getElementById("search-date-to")?.value) || "";
    const amtMin   = document.getElementById("search-amt-min")?.value || "";
    const amtMax   = document.getElementById("search-amt-max")?.value || "";
    const sort     = document.getElementById("search-sort")?.value || "date";
    const dir      = document.getElementById("search-dir")?.value || "desc";

    let url = `/api/transactions?page=${currentPage}&per_page=50`;
    if (type) url += `&type=${type}`;
    if (q) url += `&q=${encodeURIComponent(q)}`;
    if (dateFrom) url += `&date_from=${dateFrom}`;
    if (dateTo) url += `&date_to=${dateTo}`;
    if (amtMin) url += `&amount_min=${amtMin}`;
    if (amtMax) url += `&amount_max=${amtMax}`;
    if (selectedCatIds.size) url += `&category_ids=${[...selectedCatIds].join(",")}`;
    if (sort) url += `&sort=${sort}&dir=${dir}`;

    const data = await api(url);
    const tbody = document.getElementById("transactions-body");

    const countEl = document.getElementById("search-count");
    if (countEl) {
        // Answer "how much?" for the current filter, not just "how many".
        countEl.textContent = data.total
            ? `${data.total.toLocaleString()} result${data.total !== 1 ? "s" : ""} · −${fmt(data.sum_expense)} / +${fmt(data.sum_income)}`
            : "";
    }

    if (data.items.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;padding:32px;color:var(--text-tertiary);font-size:14px">No transactions found</td></tr>`;
    } else {
        tbody.innerHTML = data.items.map(t => `<tr class="tx-row" onclick="openEditTransaction(${t.id})">
            <td data-label="Date">${fmtDate(t.date)}</td>
            <td data-label="Store" class="tx-store-cell">${t.store ? escapeHtml(t.store) : "—"}</td>
            <td data-label="Category"><span class="category-tag"><span class="cat-dot" style="background:${catDotColor(t.category_id)}"></span>${escapeHtml(t.category_name)}</span></td>
            <td data-label="Amount" class="amount ${t.type}">${t.type === "income" ? "+" : "−"}${fmt2(t.amount)}</td>
            <td class="tx-actions-cell" onclick="event.stopPropagation()">
                <div class="btn-group">
                    <button class="btn-icon" onclick="openEditTransaction(${t.id})" title="Edit">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                    </button>
                    <button class="btn-icon" onclick="deleteTransaction(${t.id})" title="Delete">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
                    </button>
                </div>
            </td>
        </tr>`).join("");
    }

    const totalPages = Math.ceil(data.total / 50);
    const pag = document.getElementById("transactions-pagination");
    if (totalPages > 1) {
        pag.innerHTML = `
            <button class="btn btn-secondary btn-sm" ${currentPage <= 1 ? "disabled" : ""} onclick="currentPage--;loadTransactions()">Previous</button>
            <span class="page-info">Page ${currentPage} of ${totalPages}</span>
            <button class="btn btn-secondary btn-sm" ${currentPage >= totalPages ? "disabled" : ""} onclick="currentPage++;loadTransactions()">Next</button>`;
    } else {
        pag.innerHTML = "";
    }
    updateTxSortIcons();
}

function openTransactionModal(t = null) {
    const isEdit = t !== null;
    const expenseCats = categories.filter(c => c.type === "expense");
    const incomeCats = categories.filter(c => c.type === "income");

    const catOptions = (type) => {
        const list = type === "income" ? incomeCats : expenseCats;
        return list.map(c => `<option value="${c.id}" ${t && t.category_id === c.id ? "selected" : ""}>${escapeHtml(c.name)}</option>`).join("");
    };

    const html = `<div class="modal-overlay" onclick="if(event.target===this)this.remove()">
        <div class="modal">
            <div class="modal-title">${isEdit ? "Edit" : "Add"} Transaction</div>
            <div class="form-row">
                <div class="form-group">
                    <label class="form-label">Date</label>
                    <input class="form-input" type="text" inputmode="numeric" id="modal-t-date" placeholder="31.7.2026" title="Day.Month.Year" value="${isoToFi(t ? t.date : new Date().toISOString().slice(0, 10))}">
                </div>
                <div class="form-group">
                    <label class="form-label">Type</label>
                    <select class="form-select" id="modal-t-type" onchange="updateCategoryOptions()">
                        <option value="expense" ${!t || t.type === "expense" ? "selected" : ""}>Expense</option>
                        <option value="income" ${t && t.type === "income" ? "selected" : ""}>Income</option>
                    </select>
                </div>
            </div>
            <div class="form-group">
                <label class="form-label">Store / Description</label>
                <input class="form-input" id="modal-t-store" value="${t ? t.store : ""}" placeholder="e.g. K-Market">
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label class="form-label">Category</label>
                    <select class="form-select" id="modal-t-category">
                        ${catOptions(t ? t.type : "expense")}
                    </select>
                </div>
                <div class="form-group">
                    <label class="form-label">Amount (€)</label>
                    <input class="form-input" type="number" step="0.01" min="0" id="modal-t-amount" value="${t ? t.amount : ""}" placeholder="0.00">
                </div>
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
                <button class="btn btn-primary" onclick="saveTransaction(${t ? t.id : "null"})">${isEdit ? "Save" : "Add"}</button>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML("beforeend", html);
}

function updateCategoryOptions() {
    const type = document.getElementById("modal-t-type").value;
    const select = document.getElementById("modal-t-category");
    const list = categories.filter(c => c.type === type);
    select.innerHTML = list.map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join("");
}

async function openEditTransaction(id) {
    const data = await api(`/api/transactions?page=1&per_page=1000`);
    const t = data.items.find(x => x.id === id);
    if (t) openTransactionModal(t);
}

async function saveTransaction(id) {
    const data = {
        date: fiToIso(document.getElementById("modal-t-date").value),
        store: document.getElementById("modal-t-store").value,
        category_id: parseInt(document.getElementById("modal-t-category").value),
        amount: parseFloat(document.getElementById("modal-t-amount").value),
        type: document.getElementById("modal-t-type").value,
    };

    if (!data.date || !data.amount) {
        toast("Please fill in date and amount");
        return;
    }

    if (id) {
        await api(`/api/transactions/${id}`, { method: "PUT", body: data });
    } else {
        await api("/api/transactions", { method: "POST", body: data });
    }
    document.querySelector(".modal-overlay").remove();
    await loadTransactions();
    toast(id ? "Transaction updated" : "Transaction added");
}

async function deleteTransaction(id) {
    if (!confirm("Delete this transaction?")) return;
    await api(`/api/transactions/${id}`, { method: "DELETE" });
    await loadTransactions();
    toast("Transaction deleted");
}

function sortTxCol(col) {
    const sortEl = document.getElementById("search-sort");
    const dirEl  = document.getElementById("search-dir");
    if (!sortEl || !dirEl) return;
    if (sortEl.value === col) {
        dirEl.value = dirEl.value === "asc" ? "desc" : "asc";
    } else {
        sortEl.value = col;
        dirEl.value = "desc";
    }
    currentPage = 1;
    loadTransactions();
}

function updateTxSortIcons() {
    const col = document.getElementById("search-sort")?.value || "date";
    const dir = document.getElementById("search-dir")?.value || "desc";
    document.querySelectorAll(".tx-th[data-col]").forEach(th => {
        const c    = th.dataset.col;
        const icon = th.querySelector(".sort-icon");
        if (!icon) return;
        if (c === col) {
            icon.innerHTML = dir === "asc"
                ? `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 19V5M5 12l7-7 7 7"/></svg>`
                : `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 5v14M5 12l7 7 7-7"/></svg>`;
            th.classList.add("sort-active");
        } else {
            icon.innerHTML = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="opacity:0.25"><path d="M12 5v14M5 12l7 7 7-7"/></svg>`;
            th.classList.remove("sort-active");
        }
    });
}

// ── CSV Import ──────────────────────────────────────────────────────
const dropZone = document.getElementById("drop-zone");
const csvInput = document.getElementById("csv-input");

dropZone.addEventListener("click", () => csvInput.click());
dropZone.addEventListener("dragover", e => { e.preventDefault(); dropZone.classList.add("drag-over"); });
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
dropZone.addEventListener("drop", e => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    if (e.dataTransfer.files.length) uploadCSV(e.dataTransfer.files[0]);
});
csvInput.addEventListener("change", () => {
    if (csvInput.files.length) uploadCSV(csvInput.files[0]);
});

async function uploadCSV(file) {
    stagingMeta.filename = file.name;
    const formData = new FormData();
    formData.append("file", file);

    const res = await fetch("/api/import/upload", { method: "POST", body: formData });
    const data = await res.json();

    if (!res.ok) {
        toast(data.error || "Import failed");
        return;
    }

    // Unrecognized layout → let the user map the columns (and optionally
    // remember the format for next time).
    if (data.needs_mapping) {
        openColumnMappingModal(file, data);
        return;
    }

    showStagingFromResponse(data);
}

// Feed an upload/upload-mapped/bank-fetch response into the review table.
// The single entry point for the review pipeline, shared by the CSV upload,
// the column-mapping path, and the bank-import fetch.
function enterReview(data) {
    stagingBatchId = data.batch_id;
    stagingItems = data.items;
    renderStaging();
    document.getElementById("import-upload").style.display = "none";
    const bankCard = document.getElementById("import-bank");
    if (bankCard) bankCard.style.display = "none";
    document.getElementById("import-review").style.display = "block";
    syncBulkBar();
    populateBulkCategorySelect();
    toast(`${data.count} items ready for review`);
}

// Back-compat alias for the existing CSV callers.
function showStagingFromResponse(data) {
    enterReview(data);
}

// ── Import from bank (Enable Banking) ───────────────────────────────
// Reflect the server's bank-connection state into the Import page card.
async function loadBankStatus() {
    let status;
    try {
        status = await api("/api/import/bank/status");
    } catch (e) {
        return;  // leave the card hidden on transient errors
    }
    const card = document.getElementById("import-bank");
    if (!card) return;

    // Hide the whole card when the server has no Enable Banking credentials.
    if (status.configured === false) {
        card.style.display = "none";
        return;
    }
    card.style.display = "block";

    const disc = document.getElementById("bank-disconnected");
    const exp = document.getElementById("bank-expired");
    const conn = document.getElementById("bank-connected");
    disc.style.display = "none";
    exp.style.display = "none";
    conn.style.display = "none";

    if (status.connected) {
        conn.style.display = "block";
        const sel = document.getElementById("bank-account-select");
        sel.innerHTML = (status.accounts || []).map(a => {
            const label = [a.name, a.iban].filter(Boolean).join(" · ") || a.uid;
            return `<option value="${escapeHtml(a.uid)}">${escapeHtml(label)}</option>`;
        }).join("");
        // Default range: today-90d .. today.
        const today = new Date();
        const past = new Date(today.getTime() - 90 * 24 * 60 * 60 * 1000);
        document.getElementById("bank-date-from").value = isoToFi(isoDate(past));
        document.getElementById("bank-date-to").value = isoToFi(isoDate(today));
        const meta = document.getElementById("bank-conn-meta");
        const vu = status.valid_until ? new Date(status.valid_until) : null;
        meta.textContent = (status.aspsp_name || "Bank")
            + (vu && !isNaN(vu) ? ` · consent valid until ${vu.toLocaleDateString()}` : "");
    } else if (status.expired) {
        exp.style.display = "block";
    } else {
        disc.style.display = "block";
    }
}

function isoDate(d) {
    return d.toISOString().slice(0, 10);
}

// Full-page navigation to the consent flow; the server 302s to the bank and
// the callback returns to /#import?bank=connected.
function connectBank() {
    window.location = "/api/import/bank/connect";
}

async function disconnectBank() {
    if (!confirm("Disconnect your bank? You'll need to reconnect to import again.")) return;
    await api("/api/import/bank/disconnect", { method: "POST" });
    toast("Bank disconnected");
    loadBankStatus();
}

async function fetchBankTransactions() {
    const account_uid = document.getElementById("bank-account-select").value;
    const date_from = fiToIso(document.getElementById("bank-date-from").value);
    const date_to = fiToIso(document.getElementById("bank-date-to").value);
    if (!account_uid) { toast("Pick an account"); return; }
    if (!date_from || !date_to) { toast("Pick a date range"); return; }

    const res = await fetch("/api/import/bank/fetch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_uid, date_from, date_to }),
    });
    const data = await res.json();
    if (!res.ok) {
        if (data.error === "session_expired" || data.error === "not_connected") {
            toast("Bank connection expired — reconnect");
            loadBankStatus();
        } else {
            toast(data.error || "Fetch failed");
        }
        return;
    }
    if (!data.count) {
        toast("No new transactions in that range");
        return;
    }
    stagingMeta.filename = "Bank import (Nordea)";
    enterReview(data);
}

// Column-mapping modal shown when a CSV's columns weren't auto-detected.
// Lets the user pick Date / Merchant / Amount over a preview of the file, choose
// the amount-sign convention, and remember the format. Re-posts the same file to
// /api/import/upload-mapped (CSRF header auto-added by the global fetch wrapper).
function openColumnMappingModal(file, resp) {
    const headers = resp.headers || [];
    const guess = resp.guess || {};

    const colOptions = (selected, allowNone) => {
        let html = allowNone ? `<option value="">— none —</option>` : "";
        html += headers.map((h, i) =>
            `<option value="${i}" ${selected === i ? "selected" : ""}>${escapeHtml(h || ("Column " + (i + 1)))}</option>`
        ).join("");
        return html;
    };

    const previewHead = headers.map(h => `<th>${escapeHtml(h)}</th>`).join("");
    const previewBody = (resp.sample_rows || []).map(row =>
        `<tr>${headers.map((_, i) => `<td>${escapeHtml(String(row[i] == null ? "" : row[i]))}</td>`).join("")}</tr>`
    ).join("");

    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
        <div class="modal" style="max-width:620px">
            <div class="modal-title">Map your CSV columns</div>
            <p style="color:var(--text-secondary);font-size:13px;margin-bottom:14px">
                We didn't recognize this format. Tell us which columns to use — we'll
                remember it for next time.
            </p>
            <div style="overflow-x:auto;border:0.5px solid var(--separator);border-radius:var(--radius-sm);margin-bottom:16px">
                <table class="map-preview"><thead><tr>${previewHead}</tr></thead><tbody>${previewBody}</tbody></table>
            </div>
            <div class="map-fields">
                <label>Date column
                    <select id="map-date">${colOptions(guess.date, false)}</select>
                </label>
                <label>Merchant column <span style="color:var(--text-tertiary)">(optional)</span>
                    <select id="map-store">${colOptions(guess.store, true)}</select>
                </label>
                <label>Amount column
                    <select id="map-amount">${colOptions(guess.amount, false)}</select>
                </label>
                <label>Amount sign
                    <select id="map-sign">
                        <option value="neg_expense">Negative = expense (default)</option>
                        <option value="pos_expense">Positive = expense</option>
                    </select>
                </label>
            </div>
            <label style="display:flex;align-items:center;gap:8px;margin-top:14px;font-size:13px;cursor:pointer">
                <input type="checkbox" id="map-remember" checked> Remember this format
            </label>
            <div class="modal-actions">
                <button class="btn btn-secondary" id="map-cancel">Cancel</button>
                <button class="btn" id="map-import">Import</button>
            </div>
        </div>`;
    document.body.appendChild(overlay);

    const close = () => { overlay.remove(); if (csvInput) csvInput.value = ""; };
    overlay.addEventListener("click", e => { if (e.target === overlay) close(); });
    overlay.querySelector("#map-cancel").addEventListener("click", close);

    overlay.querySelector("#map-import").addEventListener("click", async () => {
        const dateCol = overlay.querySelector("#map-date").value;
        const amountCol = overlay.querySelector("#map-amount").value;
        if (dateCol === "" || amountCol === "") {
            toast("Pick the Date and Amount columns");
            return;
        }
        const fd = new FormData();
        fd.append("file", file);
        fd.append("date_col", dateCol);
        fd.append("amount_col", amountCol);
        fd.append("store_col", overlay.querySelector("#map-store").value);
        fd.append("amount_sign", overlay.querySelector("#map-sign").value);
        fd.append("remember", overlay.querySelector("#map-remember").checked ? "1" : "0");
        const r = await fetch("/api/import/upload-mapped", { method: "POST", body: fd });
        const d = await r.json();
        if (!r.ok) { toast(d.error || "Import failed"); return; }
        overlay.remove();
        stagingMeta.filename = file.name;
        showStagingFromResponse(d);
    });
}

// ── Import: day-first date helpers ──────────────────────────────────
// The review table shows dates as D.M.YYYY (Finnish, day-first) because the
// native <input type="date"> renders month-first under the en_FI WebKit
// locale, which reads as swapped day/month. Storage stays ISO YYYY-MM-DD.
function isoToFi(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
    if (!m) return iso || "";
    return `${+m[3]}.${+m[2]}.${m[1]}`;
}

function fiToIso(s) {
    s = (s || "").trim();
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;             // already ISO
    const m = /^(\d{1,2})\.(\d{1,2})\.(\d{4})$/.exec(s);      // D.M.YYYY
    if (m) {
        const d = +m[1], mo = +m[2];
        if (d >= 1 && d <= 31 && mo >= 1 && mo <= 12) {
            return `${m[3]}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
        }
    }
    return null;                                              // unparseable
}

function syncStagingFromDom() {
    stagingItems.forEach(item => {
        const catSel    = document.querySelector(`[data-staging-cat="${item.id}"]`);
        const typeSel   = document.querySelector(`[data-staging-type="${item.id}"]`);
        const storeInp  = document.querySelector(`[data-staging-store="${item.id}"]`);
        const dateInp   = document.querySelector(`[data-staging-date="${item.id}"]`);
        const amountInp = document.querySelector(`[data-staging-amount="${item.id}"]`);
        if (catSel && catSel.value) {
            item._selectedCatId = parseInt(catSel.value);
            item._selectedType  = catById(item._selectedCatId)?.type || item._selectedType;
        }
        if (typeSel)   item._selectedType  = typeSel.value;
        if (storeInp)  item._editedStore   = storeInp.value;
        if (dateInp)   item._editedDate    = fiToIso(dateInp.value) || item._editedDate || item.date;
        if (amountInp) item._editedAmount  = parseFloat(amountInp.value) || item.amount;
    });
}

// ── Import review: ledger renderer ──────────────────────────────────
// Effective (edited-else-original) accessors for a staging item. One category
// control per row: picking a category from the income optgroup makes the row
// income, and vice versa — there is no separate Type select.
function catById(id) { return categories.find(c => c.id === id) || null; }

function effDate(item)   { return item._editedDate   ?? item.date; }
function effStore(item)  { return item._editedStore  ?? item.store ?? ""; }
function effAmount(item) { return item._editedAmount ?? item.amount; }

function effCatId(item) {
    if (item._selectedCatId) return item._selectedCatId;
    if (item._isSplit && item._splitCategoryId) return item._splitCategoryId;
    return categories.find(c => c.name === item.suggested_category)?.id ?? null;
}

function effType(item) {
    const cat = catById(effCatId(item));
    return cat ? cat.type : (item._selectedType || item.type || "expense");
}

// One category = one color, everywhere. A color stored on the category wins;
// otherwise fall back to a stable id-keyed pick from the active palette.
function catDotColor(catId) {
    if (!catId) return "transparent";
    const stored = catById(catId)?.color;
    return stored || CHART_COLORS[catId % CHART_COLORS.length];
}

// Colors for several categories drawn together (multi-series charts). There are
// more categories than palette entries, so two ids can land on the same slot
// (any id-modulo scheme collides for ids differing by the palette length).
// Identity colors are kept where possible; a series that would repeat a color
// already used in this chart takes the nearest unused palette entry instead.
function distinctCatColors(catIds) {
    const used = new Set();
    return catIds.map(id => {
        let color = catDotColor(id);
        if (used.has(color)) {
            const free = CHART_COLORS.find(c => !used.has(c));
            if (free) color = free;
        }
        used.add(color);
        return color;
    });
}

// Category <select> with Expense/Income optgroups; empty selection allowed.
function categoryOptgroups(selectedId, placeholder) {
    const group = type => categories.filter(c => c.type === type).map(c =>
        `<option value="${c.id}" ${selectedId === c.id ? "selected" : ""}>${escapeHtml(c.name)}</option>`
    ).join("");
    const ph = `<option value="" disabled ${selectedId == null ? "selected" : ""}>${placeholder}</option>`;
    return `${ph}<optgroup label="Expense">${group("expense")}</optgroup><optgroup label="Income">${group("income")}</optgroup>`;
}

function weekdayLabel(iso) {
    const d = new Date(iso + "T00:00:00");
    return isNaN(d) ? "" : d.toLocaleDateString("en-US", { weekday: "long" });
}

function onStagingCatChange(sel, itemId) {
    const item = stagingItems.find(i => String(i.id) === String(itemId));
    if (!item) return;
    const cat = catById(parseInt(sel.value));
    if (!cat) return;
    item._selectedCatId = cat.id;
    item._selectedType  = cat.type;
    renderStaging();
}

function onStagingDateChange(inp, itemId) {
    const item = stagingItems.find(i => String(i.id) === String(itemId));
    if (!item) return;
    const iso = fiToIso(inp.value);
    if (!iso) { toast("Use day.month.year, e.g. 31.7.2026"); inp.value = isoToFi(effDate(item)); return; }
    item._editedDate = iso;
    renderStaging();
}

function onStagingStoreChange(inp, itemId) {
    const item = stagingItems.find(i => String(i.id) === String(itemId));
    if (item) item._editedStore = inp.value;
}

function onStagingAmountChange(inp, itemId) {
    const item = stagingItems.find(i => String(i.id) === String(itemId));
    if (!item) return;
    const v = parseFloat(inp.value);
    if (isNaN(v) || v <= 0) { inp.value = effAmount(item); return; }
    item._editedAmount = v;
    renderStaging();
}

function renderStagingRow(item) {
    const catId  = effCatId(item);
    const type   = effType(item);
    const isPart = item._isSplit;
    const needsCat = catId == null;

    const partBadge = isPart
        ? `<span class="split-part-badge">SPLIT</span>` : "";
    const splitBtn = isPart ? "" :
        `<button class="btn-icon" onclick="openSplitModal('${item.id}')" title="Split transaction">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 3h5v5M8 3H3v5M3 16v5h5m13-5v5h-5"/><path d="M21 3L3 21"/></svg>
        </button>`;

    return `<div class="staging-row ${isPart ? "split-child" : ""}">
        <input type="checkbox" class="staging-checkbox" data-id="${item.id}" onchange="syncBulkBar()">
        <input type="text" inputmode="numeric" class="cell-input cell-date" data-staging-date="${item.id}"
               value="${isoToFi(effDate(item))}" placeholder="31.7.2026" title="Day.Month.Year"
               onchange="onStagingDateChange(this, '${item.id}')">
        <span class="cell-store-wrap">
            <input type="text" class="cell-input" data-staging-store="${item.id}"
                   value="${effStore(item).replace(/"/g, "&quot;")}" placeholder="Store"
                   onchange="onStagingStoreChange(this, '${item.id}')">
            ${partBadge}
        </span>
        <span class="chip-cat ${needsCat ? "review" : ""}">
            <span class="dot" style="background:${catDotColor(catId)}"></span>
            <select data-staging-cat="${item.id}" onchange="onStagingCatChange(this, '${item.id}')">
                ${categoryOptgroups(catId, "Pick category")}
            </select>
        </span>
        <span class="cell-amount ${type}">
            <span class="sign">${type === "income" ? "+" : "−"}</span>
            <input type="number" class="cell-input" data-staging-amount="${item.id}"
                   value="${effAmount(item)}" step="0.01" min="0.01"
                   onchange="onStagingAmountChange(this, '${item.id}')">
            <span class="sign">€</span>
        </span>
        <span class="staging-actions">
            ${splitBtn}
            <button class="btn-icon" onclick="removeStagingItem('${item.id}')" title="Remove">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M18 6L6 18M6 6l12 12"/></svg>
            </button>
        </span>
    </div>`;
}

function renderStaging() {
    const wrap = document.getElementById("staging-groups");
    if (!wrap) return;

    // Preserve row selection across re-renders.
    const checkedIds = new Set([...document.querySelectorAll(".staging-checkbox:checked")].map(cb => cb.dataset.id));

    // Group by effective date, newest day first; keep item order inside a day.
    const byDay = new Map();
    for (const item of stagingItems) {
        const d = effDate(item);
        if (!byDay.has(d)) byDay.set(d, []);
        byDay.get(d).push(item);
    }
    const days = [...byDay.keys()].sort().reverse();

    wrap.innerHTML = days.map(day => {
        const items = byDay.get(day);
        const net = items.reduce((s, it) => s + (effType(it) === "income" ? 1 : -1) * effAmount(it), 0);
        const splits = items.filter(i => i._isSplit).length;
        const countLabel = `${items.length} transaction${items.length === 1 ? "" : "s"}${splits ? ` · ${splits} split` : ""}`;
        return `<div class="import-day">
            <div class="import-day-head">
                <b>${weekdayLabel(day)} ${isoToFi(day)}</b>
                <span>${countLabel}</span>
                <span class="day-total">${net >= 0 ? "+" : "−"}${fmt(Math.abs(net))}</span>
            </div>
            ${items.map(renderStagingRow).join("")}
        </div>`;
    }).join("");

    checkedIds.forEach(id => {
        const cb = document.querySelector(`.staging-checkbox[data-id="${id}"]`);
        if (cb) cb.checked = true;
    });
    updateImportSummary();
    syncBulkBar();
}

function updateImportSummary() {
    const n = stagingItems.length;
    let out = 0, inc = 0, needCat = 0;
    let minD = null, maxD = null;
    for (const item of stagingItems) {
        const a = effAmount(item);
        if (effType(item) === "income") inc += a; else out += a;
        if (effCatId(item) == null) needCat++;
        const d = effDate(item);
        if (!minD || d < minD) minD = d;
        if (!maxD || d > maxD) maxD = d;
    }
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    set("import-file-name", stagingMeta.filename || "Import");
    set("import-file-meta", n ? `${n} transaction${n === 1 ? "" : "s"} · ${isoToFi(minD)} – ${isoToFi(maxD)}` : "");
    set("import-sum-out", "−" + fmt(out));
    set("import-sum-in", "+" + fmt(inc));
    const rev = document.getElementById("import-sum-review");
    if (rev) {
        rev.textContent = needCat ? `${needCat} need review` : "All matched";
        rev.classList.toggle("warn", needCat > 0);
    }
    set("import-footer-note", needCat
        ? `${needCat} transaction${needCat === 1 ? "" : "s"} without a category will be saved as Other.`
        : "Confirming saves the transactions and retrains merchant rules.");
    const btn = document.getElementById("confirm-all-btn");
    if (btn) btn.textContent = `Confirm ${n} transaction${n === 1 ? "" : "s"}`;
}

// ── Import: Bulk select ─────────────────────────────────────────────
function syncBulkBar() {
    const checked = document.querySelectorAll(".staging-checkbox:checked").length;
    const total   = document.querySelectorAll(".staging-checkbox").length;
    const bar     = document.getElementById("bulk-bar");
    if (!bar) return;
    bar.classList.toggle("hidden", total === 0);
    document.getElementById("bulk-count").textContent = `${checked} of ${total} selected`;
    // keep header checkbox in sync
    const headerCb = document.getElementById("select-all-check-header");
    if (headerCb) headerCb.checked = total > 0 && checked === total;
    const topCb = document.getElementById("select-all-check");
    if (topCb) topCb.checked = total > 0 && checked === total;
}

function toggleSelectAll(checked) {
    document.querySelectorAll(".staging-checkbox").forEach(cb => cb.checked = checked);
    // keep both checkboxes in sync
    ["select-all-check", "select-all-check-header"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.checked = checked;
    });
    syncBulkBar();
}

function populateBulkCategorySelect() {
    const sel = document.getElementById("bulk-category-select");
    if (!sel) return;
    sel.innerHTML = categoryOptgroups(null, "Assign category…");
}

function applyBulkCategory() {
    const catId = parseInt(document.getElementById("bulk-category-select").value);
    const cat = catById(catId);
    if (!cat) { toast("Pick a category first"); return; }
    const checked = [...document.querySelectorAll(".staging-checkbox:checked")];
    if (!checked.length) { toast("Select rows first"); return; }
    checked.forEach(cb => {
        const item = stagingItems.find(i => String(i.id) === String(cb.dataset.id));
        if (item) { item._selectedCatId = cat.id; item._selectedType = cat.type; }
    });
    renderStaging();
    toast(`${cat.name} applied to ${checked.length} row${checked.length === 1 ? "" : "s"}`);
}

// ── Import: Split ───────────────────────────────────────────────────
let splitVirtualCounter = -1;
let _splitCatOpts = "";   // category <option> HTML for the open split modal

function openSplitModal(itemId) {
    syncStagingFromDom();   // pick up any inline edits so the split total matches the table
    const item = stagingItems.find(i => String(i.id) === String(itemId));
    if (!item) return;

    const expenseCats = categories.filter(c => c.type === "expense");
    const currentCatId = effCatId(item);
    const catOpts = expenseCats.map(c =>
        `<option value="${c.id}" ${c.id === currentCatId ? "selected" : ""}>${escapeHtml(c.name)}</option>`
    ).join("");
    _splitCatOpts = catOpts;
    const total = item._editedAmount ?? item.amount;
    const half = (total / 2).toFixed(2);

    const html = `<div class="modal-overlay" onclick="if(event.target===this)this.remove()">
        <div class="modal" style="max-width:520px">
            <div class="modal-title">Split Transaction — ${fmt(total)}</div>
            <p style="font-size:var(--text-subhead);color:var(--text-tertiary);margin-bottom:14px">${item.store || "—"} &nbsp;·&nbsp; ${fmtDate(item.date)}</p>
            <div id="split-rows-wrap">
                ${splitRowHtml(0, half, catOpts)}
                ${splitRowHtml(1, half, catOpts)}
            </div>
            <div style="margin:10px 0;display:flex;justify-content:space-between;align-items:center">
                <button class="btn btn-ghost btn-sm" onclick="addSplitRow()">+ Add part</button>
                <span id="split-remaining" style="font-size:var(--text-subhead);color:var(--text-tertiary)"></span>
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
                <button class="btn btn-primary" onclick="confirmSplit('${item.id}', ${total})">Split</button>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML("beforeend", html);
    updateSplitRemaining(total);
}

function splitRowHtml(idx, amount, catOpts) {
    return `<div class="split-row" data-split-idx="${idx}">
        <span style="font-size:var(--text-caption);color:var(--text-tertiary);width:20px;flex-shrink:0">${idx + 1}.</span>
        <input type="number" class="form-input split-amount-input" step="0.01" min="0.01" value="${amount}"
               style="width:110px;flex-shrink:0" oninput="onSplitAmountInput(this)">
        <select class="form-select split-cat-select" style="flex:1">${catOpts}</select>
        ${idx > 1 ? `<button class="btn-icon" onclick="this.closest('.split-row').remove();updateSplitRemaining()">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
        </button>` : ""}
    </div>`;
}

// All split-modal lookups are scoped to the top-most overlay so a stray or
// stacked modal can never leak its inputs into this one's sums.
function topModal() {
    return [...document.querySelectorAll(".modal-overlay")].pop() || document;
}

function addSplitRow() {
    const wrap = topModal().querySelector("#split-rows-wrap");
    const idx  = wrap.querySelectorAll(".split-row").length;
    wrap.insertAdjacentHTML("beforeend", splitRowHtml(idx, "0.00", _splitCatOpts));
    updateSplitRemaining();
}

function onSplitAmountInput(input) {
    // With exactly two parts, editing one auto-fills the other with the
    // remainder so changing the ratio is a single edit. With 3+ parts the
    // "Remaining" indicator guides the user instead.
    const modal = topModal();
    const inputs = [...modal.querySelectorAll(".split-amount-input")];
    const el = modal.querySelector("#split-remaining");
    const total = parseFloat(el?.dataset.total) || 0;
    if (inputs.length === 2 && total > 0) {
        const other = inputs.find(i => i !== input);
        const v = parseFloat(input.value);
        if (other && !isNaN(v)) {
            other.value = Math.max(0, Math.round((total - v) * 100) / 100).toFixed(2);
        }
    }
    updateSplitRemaining();
}

function updateSplitRemaining(total) {
    const modal = topModal();
    const inputs = modal.querySelectorAll(".split-amount-input");
    const sum = [...inputs].reduce((s, i) => s + (parseFloat(i.value) || 0), 0);
    const el  = modal.querySelector("#split-remaining");
    if (!el) return;
    if (total !== undefined) el.dataset.total = total;
    const t = parseFloat(el.dataset.total) || 0;
    const diff = Math.round((t - sum) * 100) / 100;
    el.textContent = diff === 0 ? "✓ Balanced" : `Remaining: ${fmt(diff)}`;
    el.style.color = diff === 0 ? "var(--green)" : "var(--red)";
}

function confirmSplit(itemId, totalAmount) {
    syncStagingFromDom();
    const modal = topModal();
    const inputs  = [...modal.querySelectorAll(".split-amount-input")];
    const catSels = [...modal.querySelectorAll(".split-cat-select")];

    const parts = inputs.map((inp, i) => ({
        amount: parseFloat(inp.value) || 0,
        category_id: parseInt(catSels[i].value),
    })).filter(p => p.amount > 0);

    const sum = parts.reduce((s, p) => s + p.amount, 0);
    if (Math.abs(sum - totalAmount) > 0.01) {
        toast(`Parts must sum to ${fmt(totalAmount)}`);
        return;
    }

    // Find the original item in stagingItems
    const origIdx = stagingItems.findIndex(i => String(i.id) === String(itemId));
    if (origIdx === -1) return;
    const orig = stagingItems[origIdx];

    // Replace original with virtual split items. syncStagingFromDom() above
    // stamped the table's full amount/category onto `orig` (_editedAmount,
    // _selectedCatId) — those must NOT leak into the parts, or every part
    // renders and confirms with the original's full amount and category.
    const splitItems = parts.map((p, i) => ({
        ...orig,
        id: `split_${itemId}_${i}`,
        _isSplit: true,
        _stagingId: orig.id,
        _splitCategoryId: p.category_id,
        _editedAmount: undefined,
        _selectedCatId: undefined,
        amount: p.amount,
        suggested_category: categories.find(c => c.id === p.category_id)?.name || "",
    }));

    stagingItems.splice(origIdx, 1, ...splitItems);
    if (modal !== document) modal.remove();
    renderStaging();
    syncBulkBar();
    toast(`Split into ${parts.length} transactions`);
}

async function confirmAllImports() {
    syncStagingFromDom();
    const items = stagingItems.map(item => {
        const type = effType(item);
        // Rows left without a category fall back to this type's "Other"
        // (the pre-redesign behaviour, now called out in the footer note).
        const catId = effCatId(item)
            || categories.find(c => c.name === "Other" && c.type === type)?.id
            || categories.find(c => c.type === type)?.id;
        const entry = {
            category_id: catId,
            type,
            store: effStore(item),
            date: effDate(item),
            amount: effAmount(item),
        };
        if (item._isSplit) entry.staging_id = item._stagingId;
        else entry.id = item.id;
        return entry;
    });

    const res = await api("/api/import/confirm", { method: "POST", body: { items, batch_id: stagingBatchId } });
    toast(res?.rules_retrained != null
        ? `All imports confirmed · ${res.rules_retrained} merchant rules retrained`
        : "All imports confirmed");
    cancelImport();
}

function halveAllAmounts() {
    syncStagingFromDom();
    stagingItems.forEach(item => {
        item._editedAmount = Math.round((effAmount(item) / 2) * 100) / 100;
    });
    renderStaging();
    toast("All amounts halved");
}

function cancelImport() {
    stagingBatchId = null;
    stagingItems = [];
    stagingMeta = { filename: "" };
    document.getElementById("import-upload").style.display = "block";
    document.getElementById("import-review").style.display = "none";
    const bar = document.getElementById("bulk-bar");
    if (bar) bar.classList.add("hidden");
    csvInput.value = "";
    // Restore the bank card (enterReview hid it) to its state-driven view.
    loadBankStatus();
}

async function removeStagingItem(id) {
    syncStagingFromDom();
    const item = stagingItems.find(i => String(i.id) === String(id));
    // only hit the API for real DB ids (not virtual split items)
    if (item && !item._isSplit) {
        await api(`/api/import/staging/${id}`, { method: "DELETE" });
    }
    stagingItems = stagingItems.filter(i => String(i.id) !== String(id));
    renderStaging();
    syncBulkBar();
}

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
    let catUrl = "/api/dashboard/category-breakdown";
    if (selectedPeriods.size > 0) catUrl += `?months=${[...selectedPeriods].join(",")}`;
    const catBreakdown = await api(catUrl);
    renderCategoryBars(catBreakdown);
}

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
    let catUrl = "/api/dashboard/category-breakdown";
    if (selectedPeriods.size > 0) catUrl += `?months=${[...selectedPeriods].join(",")}`;

    const [monthly, topExpenses, catBreakdown] = await Promise.all([
        api("/api/dashboard/monthly-summary"),
        api("/api/dashboard/top-expenses"),
        api(catUrl),
    ]);

    cachedMonthly = monthly;
    const filtered = filterData(monthly);

    await loadMonthsWithNotes();

    renderSummaryCards(filtered);
    renderMonthlyChart(filtered);
    renderCategoryBars(catBreakdown);
    renderTrendsChart(topExpenses);
    renderSummaryTable(monthly);

    if (!calendarMonth) {
        const allMonths = [...new Set(monthly.map(r => r.month))].sort();
        calendarMonth = allMonths[allMonths.length - 1] || new Date().toISOString().slice(0, 7);
    }
    await loadCalendar(calendarMonth);
    await loadHeatmap();
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
                { label: "Income",   data: incomeData,  backgroundColor: rgbaVar("--accent", 0.85), borderRadius: 6, borderSkipped: false },
                { label: "Expenses", data: expenseData, backgroundColor: rgbaVar("--red", 0.85), borderRadius: 6, borderSkipped: false },
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

function renderCategoryBars(breakdown) {
    // Capture which months this breakdown covers so drill-down can match
    breakdownMonths = breakdown.months || (breakdown.month ? [breakdown.month] : []);

    const container = document.getElementById("category-bars");
    if (!breakdown.items || breakdown.items.length === 0) {
        container.innerHTML = '<div class="empty-state"><p>No data for this period</p></div>';
        return;
    }
    const total = breakdown.items.reduce((s, i) => s + i.total, 0);
    const max   = breakdown.items[0].total;
    // One quiet bar color; the category's identity color lives in the label
    // dot (design #8). Bars keep a minimum width so tail rows stay visible.
    container.innerHTML = breakdown.items.map(item => {
        const pct   = ((item.total / total) * 100).toFixed(1);
        const width = Math.max(2, (item.total / max) * 100).toFixed(1);
        const catId = categories.find(c => c.name === item.name)?.id ?? "";
        return `<div class="cat-bar-row" style="cursor:pointer" onclick="openCategoryDrilldown(${catId},'${item.name.replace(/'/g,"\\'")}')">
            <div class="cat-bar-label"><span class="cat-dot" style="background:${catDotColor(catId)}"></span>${item.name}</div>
            <div class="cat-bar-track"><div class="cat-bar-fill" style="width:${width}%;background:${rgbaVar("--accent", 0.55)}"></div></div>
            <div class="cat-bar-amount">${fmt(item.total)} <span class="cat-bar-pct-inline">· ${pct}%</span></div>
        </div>`;
    }).join("");
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

    let months = [...new Set(data.trends.map(r => r.month))].sort();
    if (dashboardHorizon === "ytd") {
        const year = new Date().getFullYear().toString();
        months = months.filter(m => m.startsWith(year));
    } else if (dashboardHorizon !== "0") {
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

// ── Cash Flow Calendar ──────────────────────────────────────────────
async function loadCalendar(month) {
    calendarMonth = month;
    const data = await api(`/api/dashboard/daily-totals?month=${month}`);

    calendarData = {};
    (data.items || []).forEach(d => {
        const day = d.date;
        if (!calendarData[day]) calendarData[day] = { expense: 0, income: 0 };
        calendarData[day][d.type] += d.total;
    });

    const label = document.getElementById("calendar-month-label");
    if (label) label.textContent = monthLabelFull(month);

    const totalEl = document.getElementById("calendar-total");
    const totalExp = Object.values(calendarData).reduce((s, d) => s + (d.expense || 0), 0);
    if (totalEl) totalEl.textContent = totalExp > 0 ? `Total: ${fmt(totalExp)}` : "";

    renderCalendar(month);
}

function renderCalendar(month) {
    const grid = document.getElementById("calendar-grid");
    if (!grid) return;

    const [year, monthNum] = month.split("-").map(Number);
    const daysInMonth = new Date(year, monthNum, 0).getDate();
    // getDay() = 0 (Sun). We want Mon as first col (0=Mon..6=Sun)
    let firstDow = new Date(year, monthNum - 1, 1).getDay();
    firstDow = (firstDow + 6) % 7; // shift so Mon=0

    // Find max expense for color scaling
    const expenses = Object.values(calendarData).map(d => d.expense || 0);
    const maxExp = Math.max(...expenses, 1);

    const today = new Date().toISOString().slice(0, 10);
    const dayHeaders = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

    let html = dayHeaders.map(d =>
        `<div class="calendar-day-header">${d}</div>`
    ).join("");

    // Empty cells before first day
    for (let i = 0; i < firstDow; i++) {
        html += `<div class="calendar-day empty"></div>`;
    }

    for (let d = 1; d <= daysInMonth; d++) {
        const dateStr = `${month}-${String(d).padStart(2, "0")}`;
        const dayData = calendarData[dateStr];
        const expense = dayData?.expense || 0;
        const isToday = dateStr === today;

        let bg = "var(--bg-secondary)";
        let amountText = "";
        let hasExpense = false;

        if (expense > 0) {
            hasExpense = true;
            const intensity = expense / maxExp;
            // Single-hue accent intensity ramp: heavier spend = more opaque accent.
            // Theme-aware (works in light and dark) since --accent is read live.
            const alpha = 0.10 + intensity * 0.75;
            bg = rgbaVar("--accent", alpha);
            amountText = expense >= 1000
                ? `${(expense / 1000).toFixed(1)}k`
                : expense >= 100
                    ? Math.round(expense)
                    : expense.toFixed(0);
        }

        html += `<div class="calendar-day ${isToday ? "today" : ""} ${hasExpense ? "has-expense" : ""}"
                     style="background:${bg}"
                     title="${dateStr}${expense > 0 ? ": " + fmt(expense) : ""}">
            <div class="calendar-day-num">${d}</div>
            ${amountText ? `<div class="calendar-day-amount" style="color:${expense > maxExp * 0.6 ? cssVar("--on-accent") : "var(--text-primary)"}">${amountText}</div>` : ""}
        </div>`;
    }

    grid.innerHTML = html;
}

function calendarPrev() {
    if (!calendarMonth) return;
    const [y, m] = calendarMonth.split("-").map(Number);
    const d = new Date(y, m - 2, 1);
    calendarMonth = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
    loadCalendar(calendarMonth);
}

function calendarNext() {
    if (!calendarMonth) return;
    const [y, m] = calendarMonth.split("-").map(Number);
    const d = new Date(y, m, 1);
    calendarMonth = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
    loadCalendar(calendarMonth);
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
        return `<button class="note-btn ${hasNote ? "has-note" : ""}" onclick="openNoteModal('${month}')" title="${hasNote ? "View/edit note" : "Add note"}">
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
                html += `<tr class="row-month ${collapsed ? "hidden" : ""}" data-year="${year}">
                    <td style="padding-left:36px">${monthLabel(d.month)}</td>
                    <td style="text-align:right" class="amount income">+${fmt(d.income)}</td>
                    <td style="text-align:right" class="amount">${fmt(d.expense)}</td>
                    <td style="text-align:right" class="${d.diff >= 0 ? "diff-positive" : "diff-negative"}">${d.diff >= 0 ? "+" : ""}${fmt(d.diff)}</td>
                    <td>${noteBtn(d.month)}</td>
                </tr>`;
            });
        });

        tbody.innerHTML = html;
    } else {
        tbody.innerHTML = monthData.slice().reverse().map(d => `<tr>
            <td>${monthLabel(d.month)}</td>
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

    const rows = [
        { label: `Income vs ${data.year - 1}`, cur: curInc, prev: prevInc, sign: "+", goodWhenUp: true },
        { label: `Expenses vs ${data.year - 1}`, cur: curExp, prev: prevExp, sign: "", goodWhenUp: false },
        { label: "Net vs previous year", cur: curInc - curExp, prev: prevInc - prevExp, sign: "", goodWhenUp: true },
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

    const prevMonths = data.prev_monthly ? [...new Set(data.prev_monthly.map(r => r.month))].sort() : [];
    const prevExpData = months.map((m, i) => {
        const prevM = prevMonths[i];
        if (!prevM) return null;
        const r = data.prev_monthly.find(x => x.month === prevM && x.type === "expense");
        return r ? r.total : 0;
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

// ── Net Worth ────────────────────────────────────────────────────────
let netWorthMonths = 12;
let investPreview = null;  // last parsed investment-import preview (review modal)

async function loadNetWorth() {
    const asof = document.getElementById("nw-asof");
    if (asof && !asof.value) asof.value = isoToFi(new Date().toISOString().slice(0, 10));
    await Promise.all([loadNetWorthSummary(), loadNetWorthChart()]);
}

async function loadNetWorthSummary() {
    const data = await api("/api/networth/summary");
    renderNetWorthCards(data);
    renderNetWorthAccounts(data.accounts || []);
}

function renderNetWorthCards(d) {
    const chg = d.change_vs_prev;
    const chgSign = chg > 0 ? "+" : "";
    // Net worth is the headline — it gets the accent; assets/liabilities stay
    // neutral. Change is colored by its sign (design #18).
    document.getElementById("networth-cards").innerHTML = `
        <div class="summary-card"><div class="label">Net Worth</div><div class="value" style="color:var(--accent)">${fmt(d.net_worth)}</div></div>
        <div class="summary-card"><div class="label">Total Assets</div><div class="value">${fmt(d.assets)}</div></div>
        <div class="summary-card"><div class="label">Total Liabilities</div><div class="value">−${fmt(d.liabilities)}</div></div>
        <div class="summary-card"><div class="label">Change vs last month</div><div class="value" style="color:${chg >= 0 ? "var(--green)" : "var(--red)"}">${chgSign}${fmt(chg)}</div></div>`;
}

function nwChip(a) {
    if (a.group_name) return `<span class="nw-chip">${escapeHtml(a.group_name)}</span>`;
    if (a.external_id) return `<span class="nw-chip">Bank</span>`;
    return "";
}

function renderNetWorthAccounts(accounts) {
    const body = document.getElementById("networth-accounts-body");
    if (!accounts.length) {
        body.innerHTML = `<tr><td colspan="7" style="color:var(--text-tertiary);padding:16px">No accounts yet. Add one below to start tracking your net worth.</td></tr>`;
        return;
    }
    // Group by broker (group_name); ungrouped/manual accounts under "Other".
    const groups = new Map();
    for (const a of accounts) {
        const key = a.group_name || "__ungrouped__";
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(a);
    }
    // Named brokers first (alphabetical), ungrouped last.
    const keys = [...groups.keys()].sort((x, y) => {
        if (x === "__ungrouped__") return 1;
        if (y === "__ungrouped__") return -1;
        return x.localeCompare(y);
    });

    let html = "";
    for (const key of keys) {
        const rows = groups.get(key);
        const label = key === "__ungrouped__" ? "Other accounts" : key;
        // Broker subtotal = sum of shown accounts' latest balances (assets +,
        // liabilities −), matching how net worth nets them.
        const subtotal = rows.reduce((s, a) => {
            if (a.is_archived || a.latest_balance == null) return s;
            return s + (a.type === "liability" ? -a.latest_balance : a.latest_balance);
        }, 0);
        html += `<tr class="nw-group-row"><td colspan="6">${escapeHtml(label)}</td>
            <td style="text-align:right;font-weight:700">${fmt(subtotal)}</td></tr>`;
        for (const a of rows) html += nwAccountRow(a);
    }
    body.innerHTML = html;
    // Show the asset-level breakdown by default: expand every account that has
    // holdings so individual assets appear separately (not just account sums).
    // Closed accounts stay collapsed — their last snapshot no longer counts, and
    // showing those values under a zero balance only reads as a contradiction.
    for (const a of accounts) {
        if ((a.holdings_count || 0) > 0 && !a.is_archived) toggleHoldings(a.id);
    }
}

function nwAccountRow(a) {
    const closed = !!a.is_archived;
    const expandable = (a.holdings_count || 0) > 0;
    const caret = expandable
        ? `<span class="nw-caret" onclick="toggleHoldings(${a.id})" id="nw-caret-${a.id}" title="Show holdings">▸</span>`
        : `<span class="nw-caret-spacer"></span>`;
    // A closed account keeps its history; it just carries a zero from the day it
    // was closed, so it counts in past months and not in this one.
    const status = closed
        ? `<span class="nw-status nw-status-closed" title="Counts in months before it was closed">Closed</span>`
        : `<span class="nw-status">Open</span>`;
    // Blank means "no change" — spell out the value that carries forward so an
    // empty field never reads as zero.
    const carry = a.latest_balance != null
        ? `keep ${fmt(a.latest_balance)}`
        : "no balance yet";
    const balanceCell = closed
        ? `<td style="text-align:right;color:var(--text-tertiary);font-size:12px">closed</td>`
        : `<td style="text-align:right"><input type="number" step="0.01" class="form-input nw-balance-input"
                placeholder="${escapeHtml(carry)}" title="Leave blank to keep the current balance"
                style="width:150px;text-align:right;padding:6px 8px;font-size:13px"></td>`;
    const closeBtn = closed
        ? `<button class="btn btn-ghost btn-sm" onclick="reopenNetWorthAccount(${a.id})" title="Reopen this account">↩</button>`
        : `<button class="btn btn-ghost btn-sm" onclick="closeNetWorthAccount(${a.id})" title="Sold or paid off — set to zero and close, keeping history">⊘</button>`;
    return `
        <tr data-account-id="${a.id}" data-account-name="${escapeHtml(a.name)}"
            class="nw-account-row${closed ? " nw-account-closed" : ""}">
            <td style="font-weight:600">${caret}${escapeHtml(a.name)}${nwChip(a)}</td>
            <td style="text-transform:capitalize">${a.type}</td>
            <td style="text-align:right">${a.latest_balance != null ? (a.type === "liability" ? "−" : "") + fmt(a.latest_balance) : "—"}</td>
            <td>${a.latest_as_of ? fmtDate(a.latest_as_of) : "—"}</td>
            <td style="text-align:center">${status}</td>
            ${balanceCell}
            <td style="white-space:nowrap">${closeBtn}<button class="btn btn-ghost btn-sm"
                onclick="deleteNetWorthAccount(${a.id})" title="Delete for good, including history">✕</button></td>
        </tr>`;
}

async function toggleHoldings(accountId) {
    const existing = document.querySelector(`tr.nw-holdings-row[data-for="${accountId}"]`);
    const caret = document.getElementById(`nw-caret-${accountId}`);
    if (existing) {
        existing.remove();
        if (caret) caret.textContent = "▸";
        return;
    }
    if (caret) caret.textContent = "▾";
    const anchor = document.querySelector(`#networth-accounts-body tr[data-account-id="${accountId}"]`);
    if (!anchor) return;
    const tr = document.createElement("tr");
    tr.className = "nw-holdings-row";
    tr.dataset.for = accountId;
    tr.innerHTML = `<td colspan="7" style="padding:0">
        <div class="nw-holdings-loading">Loading holdings…</div></td>`;
    anchor.after(tr);
    let data;
    try {
        data = await api(`/api/networth/holdings?account_id=${accountId}`);
    } catch (e) {
        tr.querySelector("td").innerHTML = `<div class="nw-holdings-loading">Could not load holdings.</div>`;
        return;
    }
    const holdings = data.holdings || [];
    if (!holdings.length) {
        tr.querySelector("td").innerHTML = `<div class="nw-holdings-loading">No holdings recorded.</div>`;
        return;
    }
    const rows = holdings.map(h => {
        const pct = h.return_pct;
        const pctCls = pct == null ? "" : (pct >= 0 ? "income" : "expense");
        const pctTxt = pct == null ? "—" : `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`;
        const pcs = h.units == null ? "—" : (+h.units).toLocaleString("fi-FI", { maximumFractionDigits: 3 });
        return `<tr>
            <td>${escapeHtml(h.name)}</td>
            <td style="text-align:right">${pcs}</td>
            <td style="text-align:right">${fmt(h.value_eur)}</td>
            <td style="text-align:right" class="${pctCls}">${pctTxt}</td>
            <td style="text-align:right;width:34px"><button class="btn btn-ghost btn-sm"
                onclick="deleteHolding(${h.id}, '${data.as_of}', this)"
                title="Remove from this snapshot">✕</button></td>
        </tr>`;
    }).join("");
    tr.querySelector("td").innerHTML = `
        <table class="nw-holdings-table">
            <thead><tr><th>Holding</th><th style="text-align:right">pcs</th>
                <th style="text-align:right">Value</th><th style="text-align:right">Return %</th>
                <th></th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
        <div class="nw-holdings-note">Snapshot of ${fmtDate(data.as_of)}. Removing a holding
            corrects this snapshot, so the months reading from it change too. To record a
            sale, import a newer statement — the new snapshot simply won't list it.</div>`;
}

async function deleteHolding(holdingId, asOf, btn) {
    const cells = btn.closest("tr").querySelectorAll("td");
    const name = cells[0].textContent.trim();
    if (!confirm(`Remove "${name}" from the ${fmtDate(asOf)} snapshot?\n\n` +
                 `The account total for that date drops by ${cells[2].textContent.trim()}, ` +
                 `and every month reading from that snapshot changes with it.\n\n` +
                 `Sold it? Import a newer statement instead — the new snapshot won't list it, ` +
                 `and your history stays true to what you held.`)) return;
    await api(`/api/networth/holdings/${holdingId}`, { method: "DELETE" });
    await loadNetWorth();
}

function nwAccountName(id) {
    // Read the stored name, not the cell text — that also holds the caret and
    // the broker chip.
    const row = document.querySelector(`#networth-accounts-body tr[data-account-id="${id}"]`);
    return row?.dataset.accountName || "this account";
}

async function closeNetWorthAccount(id) {
    // Sold or paid off: record a zero on the closing date rather than deleting.
    // Carry-forward then drops the account from this month on and leaves every
    // earlier month exactly as it was.
    const asOf = fiToIso(document.getElementById("nw-asof").value);
    if (!asOf) { toast("Set the date first (day.month.year)"); return; }
    const name = nwAccountName(id);
    if (!confirm(`Close "${name}" as of ${isoToFi(asOf)}?\n\n` +
                 `Its balance goes to 0 from that date. Months before it keep the old value, ` +
                 `so your history stays intact. You can reopen it later.`)) return;
    await api(`/api/accounts/${id}/close`, { method: "POST", body: { as_of: asOf } });
    toast(`Closed ${name}`);
    await loadNetWorth();
}

async function reopenNetWorthAccount(id) {
    await api(`/api/accounts/${id}/reopen`, { method: "POST" });
    toast("Reopened — enter a balance to bring it back into the total");
    await loadNetWorth();
}

async function addNetWorthAccount() {
    const nameEl = document.getElementById("nw-new-name");
    const name = nameEl.value.trim();
    const type = document.getElementById("nw-new-type").value;
    if (!name) { toast("Enter an account name"); return; }
    await api("/api/accounts", { method: "POST", body: { name, type } });
    nameEl.value = "";
    await loadNetWorth();
}

async function deleteNetWorthAccount(id) {
    const name = nwAccountName(id);
    if (!confirm(`Delete "${name}" and every balance ever recorded for it?\n\n` +
                 `This rewrites your net-worth history as if you never held it. ` +
                 `If you sold it, close it instead (⊘) — that keeps the past.`)) return;
    await api(`/api/accounts/${id}`, { method: "DELETE" });
    await loadNetWorth();
}

async function saveNetWorthBalances() {
    const asOf = fiToIso(document.getElementById("nw-asof").value);
    if (!asOf) { toast("Pick a date first (day.month.year)"); return; }
    // Only the accounts you typed a figure into get a new balance row. The rest
    // are left alone on purpose: net worth carries the last balance forward, so
    // an untouched account keeps its old value instead of dropping to zero.
    const rows = document.querySelectorAll("#networth-accounts-body tr[data-account-id]");
    let saved = 0, kept = 0;
    for (const row of rows) {
        const input = row.querySelector(".nw-balance-input");
        if (!input) continue;              // closed account, no input
        if (input.value === "") { kept++; continue; }
        await api(`/api/accounts/${row.dataset.accountId}/balances`,
            { method: "POST", body: { as_of: asOf, balance: parseFloat(input.value) } });
        saved++;
    }
    if (!saved) { toast("Enter at least one new balance"); return; }
    toast(kept
        ? `Updated ${saved}, kept ${kept} unchanged`
        : `Updated ${saved} balance${saved > 1 ? "s" : ""}`);
    await loadNetWorth();
}

// ── Investment import (Nordnet CSV / Nordea xlsx → Net Worth) ─────────
function pickInvestmentFiles() {
    const input = document.getElementById("nw-invest-file");
    if (input) { input.value = ""; input.click(); }
}

async function onInvestmentFilesPicked(input) {
    if (!input.files || !input.files.length) return;
    openInvestModal();
    const body = document.getElementById("invest-body");
    body.innerHTML = `<div class="nw-holdings-loading">Parsing ${input.files.length} file(s)…</div>`;
    setInvestConfirmEnabled(false);

    const fd = new FormData();
    for (const f of input.files) fd.append("files", f);
    let res, data;
    try {
        res = await fetch("/api/networth/import-investments/preview", { method: "POST", body: fd });
        data = await res.json().catch(() => ({}));
    } catch (e) {
        body.innerHTML = `<div class="invest-error">Could not reach the server.</div>`;
        return;
    }
    if (!res.ok) {
        body.innerHTML = `<div class="invest-error">${escapeHtml(data.error || "Could not parse the file(s).")}</div>`;
        return;
    }
    investPreview = data;
    renderInvestPreview(data);
    setInvestConfirmEnabled(true);
}

function renderInvestPreview(data) {
    const body = document.getElementById("invest-body");
    const files = data.files || [];
    if (!files.length) {
        body.innerHTML = `<div class="invest-error">No accounts found in the file(s).</div>`;
        setInvestConfirmEnabled(false);
        return;
    }
    body.innerHTML = files.map((f, fi) => renderInvestFile(f, fi)).join("");
}

function renderInvestFile(f, fi) {
    const warns = (f.warnings || []).map(w =>
        `<div class="invest-warn">⚠ ${escapeHtml(w)}</div>`).join("");
    const dateRequired = !f.as_of;
    const dateField = `
        <label class="invest-asof">as of
            <input type="text" inputmode="numeric" placeholder="31.7.2026" title="Day.Month.Year" class="form-input invest-asof-input" data-file="${fi}"
                   value="${f.as_of ? isoToFi(f.as_of) : ""}" ${dateRequired ? 'data-required="1"' : ""}
                   style="width:auto;padding:4px 6px;font-size:12px">
            ${dateRequired ? '<span class="invest-warn-inline">date needed</span>' : ""}
        </label>`;
    const accounts = (f.accounts || []).map((a, ai) => renderInvestAccount(a, fi, ai)).join("");
    return `
        <div class="invest-file" data-file="${fi}">
            <div class="invest-file-head">
                <span class="nw-chip">${escapeHtml(sourceLabel(f.source))}</span>
                <span class="invest-file-name">${escapeHtml(f.filename || "")}</span>
                ${dateField}
            </div>
            ${warns}
            ${accounts || '<div class="invest-warn">No accounts parsed.</div>'}
        </div>`;
}

function sourceLabel(src) {
    return {
        nordnet_stocks: "Nordnet stocks",
        nordnet_funds: "Nordnet funds",
        nordea_xlsx: "Nordea",
    }[src] || (src || "Import");
}

function renderInvestAccount(a, fi, ai) {
    const match = a.match || {};
    const matched = match.existing_account_id != null;
    const matchHtml = matched ? `
        <div class="invest-match">
            <span>Matches an existing account (by ${escapeHtml(match.by || "id")}).</span>
            <label><input type="radio" name="map-${fi}-${ai}" class="invest-map" value="map"
                data-target="${match.existing_account_id}" checked> Update it</label>
            <label><input type="radio" name="map-${fi}-${ai}" class="invest-map" value="create"> Create new</label>
        </div>` : "";
    const holdings = a.kind === "cash"
        ? `<div class="invest-cash">Cash balance · ${fmt(a.total_eur)}</div>`
        : renderInvestHoldings(a.holdings || []);
    return `
        <div class="invest-account" data-file="${fi}" data-acct="${ai}">
            <div class="invest-account-head">
                <input type="checkbox" class="invest-include" checked title="Include in import">
                <input type="text" class="form-input invest-name" value="${escapeHtml(a.label || "")}"
                       style="flex:1;min-width:120px;padding:4px 8px;font-size:13px">
                <select class="form-input invest-type" style="width:auto;padding:4px 6px;font-size:12px">
                    <option value="asset" selected>Asset</option>
                    <option value="liability">Liability</option>
                </select>
                <span class="invest-total">${fmt(a.total_eur)}</span>
            </div>
            ${matchHtml}
            ${holdings}
        </div>`;
}

function renderInvestHoldings(holdings) {
    if (!holdings.length) return `<div class="invest-warn">No holdings.</div>`;
    const rows = holdings.map(h => {
        const pct = h.return_pct;
        const pctCls = pct == null ? "" : (pct >= 0 ? "income" : "expense");
        const pctTxt = pct == null ? "—" : `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`;
        const pcs = h.units == null ? "—" : (+h.units).toLocaleString("fi-FI", { maximumFractionDigits: 3 });
        return `<tr><td>${escapeHtml(h.name)}</td><td style="text-align:right">${pcs}</td>
            <td style="text-align:right">${fmt(h.value_eur)}</td>
            <td style="text-align:right" class="${pctCls}">${pctTxt}</td></tr>`;
    }).join("");
    return `<table class="nw-holdings-table invest-holdings"><thead><tr>
        <th>Holding</th><th style="text-align:right">pcs</th>
        <th style="text-align:right">Value</th><th style="text-align:right">Return %</th>
        </tr></thead><tbody>${rows}</tbody></table>`;
}

async function confirmInvestmentImport() {
    if (!investPreview) return;
    const out = [];
    let missingDate = false;
    (investPreview.files || []).forEach((f, fi) => {
        const dateEl = document.querySelector(`.invest-asof-input[data-file="${fi}"]`);
        const asOf = dateEl ? fiToIso(dateEl.value) : f.as_of;
        (f.accounts || []).forEach((a, ai) => {
            const el = document.querySelector(`.invest-account[data-file="${fi}"][data-acct="${ai}"]`);
            if (!el) return;
            const include = el.querySelector(".invest-include").checked;
            if (!include) return;
            if (!asOf) { missingDate = true; return; }
            const name = el.querySelector(".invest-name").value.trim() || a.label;
            const type = el.querySelector(".invest-type").value;
            const mapEl = el.querySelector('.invest-map[value="map"]:checked');
            const target = mapEl ? parseInt(mapEl.dataset.target, 10) : null;
            out.push({
                external_id: a.external_id,
                name, group_name: a.broker, type, kind: a.kind,
                as_of: asOf, target_account_id: target,
                include: true, total_eur: a.total_eur,
                holdings: a.holdings || [],
            });
        });
    });

    if (missingDate) { toast("Pick an 'as of' date for each file first"); return; }
    if (!out.length) { toast("Select at least one account to import"); return; }

    setInvestConfirmEnabled(false);
    let res, data;
    try {
        res = await fetch("/api/networth/import-investments/confirm", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ accounts: out }),
        });
        data = await res.json().catch(() => ({}));
    } catch (e) {
        toast("Could not reach the server"); setInvestConfirmEnabled(true); return;
    }
    if (!res.ok) { toast(data.error || "Import failed"); setInvestConfirmEnabled(true); return; }
    closeInvestModal();
    toast(`Updated ${data.updated} account${data.updated === 1 ? "" : "s"} · ${fmtDate(data.as_of)}`);
    await loadNetWorth();
}

function openInvestModal() {
    document.getElementById("invest-overlay").style.display = "flex";
}
function closeInvestModal() {
    document.getElementById("invest-overlay").style.display = "none";
    investPreview = null;
}
function setInvestConfirmEnabled(on) {
    const btn = document.getElementById("invest-confirm-btn");
    if (btn) btn.disabled = !on;
}

async function setNetWorthPeriod(m) {
    netWorthMonths = m;
    document.querySelectorAll(".nw-period-btn").forEach(b =>
        b.classList.toggle("active", parseInt(b.dataset.months) === m));
    await loadNetWorthChart();
}

async function loadNetWorthChart() {
    const { series } = await api(`/api/networth/history?months=${netWorthMonths}`);
    renderNetWorthChart(series);
}

function renderNetWorthChart(series) {
    const ctx = document.getElementById("chart-networth");
    if (!ctx) return;
    if (charts.networth) charts.networth.destroy();
    charts.networth = new Chart(ctx, {
        type: "line",
        data: {
            labels: series.map(p => monthLabel(p.month)),
            datasets: [{
                label: "Net Worth",
                data: series.map(p => p.net_worth),
                borderColor: chartTheme().accent,
                backgroundColor: (() => {
                    const g = ctx.getContext("2d").createLinearGradient(0, 0, 0, ctx.height || 240);
                    g.addColorStop(0, rgbaVar("--accent", 0.30));
                    g.addColorStop(1, rgbaVar("--accent", 0));
                    return g;
                })(),
                fill: true, tension: 0.3, pointRadius: 3, pointHoverRadius: 6,
                pointBackgroundColor: chartTheme().accent,
            }],
        },
        options: chartOptions(),
    });
}

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

// ── Recurring & subscriptions ───────────────────────────────────────
const RECURRING_STATUS = {
    active:        { label: "Active",     cls: "recurring-active" },
    due_soon:      { label: "Due soon",   cls: "recurring-due" },
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
    // in orange with a hint instead of a calm gray (design #14).
    const todayIso = new Date().toISOString().slice(0, 10);
    const duePast = i.next_date && i.next_date < todayIso;
    const nextDue = i.next_date
        ? (duePast
            ? `<span style="color:var(--orange)" title="Expected date has passed — the series may have lapsed">${fmtDate(i.next_date)}</span>`
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
          `<span>${data.summary.count} recurring</span>`
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
                c.fillStyle = v >= 0 ? rgbaVar("--red", 1) : rgbaVar("--green", 1);
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
                    backgroundColor: changes.map(v => v >= 0 ? rgbaVar("--red", 0.7) : rgbaVar("--green", 0.7)),
                    borderColor:     changes.map(v => v >= 0 ? rgbaVar("--red", 1)   : rgbaVar("--green", 1)),
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
    const trendColor   = slope > histAvg * 0.01 ? "var(--red)" : slope < -histAvg * 0.01 ? "var(--green)" : "var(--text-secondary)";

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

// ── Card fullscreen ─────────────────────────────────────────────────
const FS_ICON_OPEN = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>';
const FS_ICON_CLOSE = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 4H4v5M15 4h5v5M9 20H4v-5M15 20h5v-5"/></svg>';

function _isVisualCard(card) {
    if (card.classList.contains("no-fs")) return false;
    return !!(
        card.querySelector("canvas") ||
        card.querySelector(".heatmap-grid") ||
        card.querySelector(".calendar-grid") ||
        card.querySelector("#category-bars") ||
        card.querySelector("#report-category-bars") ||
        card.querySelector("#report-income-bars")
    );
}

function _findCardHeader(card) {
    const flex = card.querySelector(":scope > .flex.items-center");
    if (flex) return flex;
    const calNav = card.querySelector(":scope > .calendar-nav");
    if (calNav) return calNav;
    const title = card.querySelector(":scope > .card-title");
    if (!title) return null;
    // Wrap loose .card-title in a flex header so we can drop the button next to it
    const wrap = document.createElement("div");
    wrap.className = "flex items-center";
    wrap.style.cssText = "justify-content:space-between;margin-bottom:14px";
    title.parentNode.insertBefore(wrap, title);
    title.style.margin = "0";
    wrap.appendChild(title);
    return wrap;
}

function injectFullscreenButtons(root = document) {
    root.querySelectorAll(".card").forEach(card => {
        if (card.dataset.fsInjected) return;
        if (!_isVisualCard(card)) return;
        const header = _findCardHeader(card);
        if (!header) return;
        const btn = document.createElement("button");
        btn.className = "card-fs-btn";
        btn.title = "Fullscreen";
        btn.innerHTML = FS_ICON_OPEN;
        btn.addEventListener("click", e => { e.stopPropagation(); toggleCardFullscreen(card); });
        header.appendChild(btn);
        card.dataset.fsInjected = "1";
    });
}

let _fsBackdrop = null;
let _fsKeyHandler = null;

function toggleCardFullscreen(card) {
    if (card.classList.contains("is-fullscreen")) {
        exitFullscreen(card);
    } else {
        enterFullscreen(card);
    }
}

function enterFullscreen(card) {
    document.querySelectorAll(".card.is-fullscreen").forEach(c => exitFullscreen(c));
    card.classList.add("is-fullscreen");
    document.body.classList.add("fs-active");
    _fsBackdrop = document.createElement("div");
    _fsBackdrop.className = "fs-backdrop";
    _fsBackdrop.addEventListener("click", () => exitFullscreen(card));
    document.body.appendChild(_fsBackdrop);
    const btn = card.querySelector(":scope .card-fs-btn");
    if (btn) { btn.innerHTML = FS_ICON_CLOSE; btn.title = "Exit fullscreen"; }
    _fsKeyHandler = e => { if (e.key === "Escape") exitFullscreen(card); };
    document.addEventListener("keydown", _fsKeyHandler);
    // Resize all chart.js charts so they fit the new container
    requestAnimationFrame(() => {
        Object.values(charts).forEach(c => c && c.resize && c.resize());
    });
}

function exitFullscreen(card) {
    card.classList.remove("is-fullscreen");
    document.body.classList.remove("fs-active");
    if (_fsBackdrop) { _fsBackdrop.remove(); _fsBackdrop = null; }
    if (_fsKeyHandler) { document.removeEventListener("keydown", _fsKeyHandler); _fsKeyHandler = null; }
    const btn = card.querySelector(":scope .card-fs-btn");
    if (btn) { btn.innerHTML = FS_ICON_OPEN; btn.title = "Fullscreen"; }
    requestAnimationFrame(() => {
        Object.values(charts).forEach(c => c && c.resize && c.resize());
    });
}

// ── Quit ─────────────────────────────────────────────────────────────
function quitApp() {
    fetch("/api/quit", { method: "POST" });
}

// ── Train Merchant Rules ─────────────────────────────────────────────
async function trainMerchantRules() {
    const html = `<div class="modal-overlay" onclick="if(event.target===this)this.remove()">
        <div class="modal">
            <div class="modal-title">Rebuild rules from history?</div>
            <p style="font-size:var(--text-subhead);color:var(--text-secondary);margin:0 0 16px">
                This will analyse all your transactions and rebuild the merchant rules from scratch.
                Any rules you added manually will be removed.
            </p>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
                <button class="btn btn-primary" onclick="runTraining(this)">Rebuild rules</button>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML("beforeend", html);
}

async function runTraining(btn) {
    btn.disabled = true;
    btn.textContent = "Rebuilding…";
    try {
        const r = await fetch("/api/merchant-rules/train", { method: "POST" });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
        btn.closest(".modal-overlay").remove();
        toast(`Training complete — ${j.inserted} rules generated from ${j.total_stores} stores`);
        loadMerchantRules();
    } catch (e) {
        btn.disabled = false;
        btn.textContent = "Rebuild rules";
        toast(`Training failed${e.message ? ": " + e.message : ""}`);
    }
}

// ── App Guide ────────────────────────────────────────────────────────
const GUIDE_SLIDES = [
    {
        title: "Welcome to Balance.",
        icon: `<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="1.5"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>`,
        body: "Balance. gives you the full picture of your money — income, spending, and net worth in one place. Import your bank CSV, categorise transactions, and track trends over time.",
    },
    {
        title: "Import Your Transactions",
        icon: `<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="1.5"><path d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1M12 4v12m0 0l-4-4m4 4l4-4"/></svg>`,
        body: "Go to <strong>Import</strong> and drop a CSV from your bank. Supported formats: Finnair credit card, Finnish bank statement (EtuTili), and Nordea Platinum. Review each transaction before confirming — you can edit the category, amount, or delete rows.",
    },
    {
        title: "Categories & Merchant Rules",
        icon: `<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="1.5"><path d="M7 7h.01M7 3h5a1.99 1.99 0 011.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.99 1.99 0 013 12V7a4 4 0 014-4z"/></svg>`,
        body: "Go to <strong>Categories</strong> to manage your expense and income groups. <em>Merchant Rules</em> automatically assign a category to a store — so \"K-Market\" always maps to Groceries. Press <strong>Rebuild rules</strong> to regenerate them from your transaction history (this also happens automatically after each import).",
    },
    {
        title: "Dashboard & Trends",
        icon: `<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="1.5"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>`,
        body: "The <strong>Dashboard</strong> gives you a monthly overview — expenses vs income, top categories, and daily totals. Click any bar in the category chart to drill into individual transactions. <strong>Trends</strong> lets you compare categories across months.",
    },
    {
        title: "Reports",
        icon: `<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="1.5"><path d="M3 3v18h18"/><path d="M7 14l4-4 3 3 5-6"/></svg>`,
        body: "<strong>Reports</strong> shows your full annual summary — every category, month over month — so you can see where your money goes across the whole year.",
    },
];

let _guideStep = 0;

function showGuide() {
    _guideStep = 0;
    _renderGuide();
    document.getElementById("guide-overlay").style.display = "flex";
    localStorage.setItem("guide_seen", "1");
}

function _renderGuide() {
    const slide = GUIDE_SLIDES[_guideStep];
    const total = GUIDE_SLIDES.length;

    document.getElementById("guide-slides").innerHTML = `
        <div style="padding:40px 40px 32px;text-align:center">
            <div style="margin-bottom:20px">${slide.icon}</div>
            <div style="font-size:22px;font-weight:600;color:var(--text-primary);margin-bottom:12px">${slide.title}</div>
            <p style="font-size:15px;color:var(--text-secondary);line-height:1.6;margin:0">${slide.body}</p>
        </div>`;

    const dots = document.getElementById("guide-dots");
    dots.innerHTML = Array.from({ length: total }, (_, i) =>
        `<span style="width:7px;height:7px;border-radius:50%;background:${i === _guideStep ? "var(--accent)" : "var(--border)"};display:inline-block;transition:background 0.2s"></span>`
    ).join("");

    document.getElementById("guide-prev").style.display = _guideStep === 0 ? "none" : "";
    const nextBtn = document.getElementById("guide-next");
    nextBtn.textContent = _guideStep === total - 1 ? "Done" : "Next";
}

function guideStep(dir) {
    const total = GUIDE_SLIDES.length;
    _guideStep += dir;
    if (_guideStep >= total) {
        document.getElementById("guide-overlay").style.display = "none";
        localStorage.setItem("guide_seen", "1");
        return;
    }
    if (_guideStep < 0) _guideStep = 0;
    _renderGuide();
}

function resetGuide() {
    localStorage.removeItem("guide_seen");
    toast("Guide state reset — will show on next launch");
}

// ── Init ─────────────────────────────────────────────────────────────
// ── App state ────────────────────────────────────────────────────────
// Single-user local app: no login. Fetch /api/me only to grab the CSRF token
// (fallback for the fetch wrapper) and, defensively, hide any .desktop-only
// actions if somehow running hosted.
async function loadAppState() {
    try {
        const res = await fetch("/api/me");
        if (!res.ok) return;
        const me = await res.json();
        if (me && me.csrf_token) csrfTokenCache = me.csrf_token;
        if (me.is_hosted) {
            document.querySelectorAll(".desktop-only").forEach(el => {
                el.style.display = "none";
            });
        }
    } catch (e) {
        // network hiccup — don't block the rest of init
    }
}

async function init() {
    initTheme();
    loadAppState();
    renderPaletteOptions();
    applyChartDefaults();
    startKeepAlive();
    await loadCategories();
    await loadDashboard();
    injectFullscreenButtons();
    handleBankReturn();
}

// After the bank consent round-trip the callback redirects to
// /#import?bank=connected (or ?bank=error). Detect that, open the Import tab,
// toast the outcome, and refresh the bank status card.
function handleBankReturn() {
    const hash = window.location.hash || "";
    const m = hash.match(/bank=(connected|error)/);
    if (!m) return;
    const importNav = document.querySelector('.nav-item[data-page="import"]');
    if (importNav) importNav.click();  // activates page + runs loadBankStatus()
    if (m[1] === "connected") toast("Bank connected");
    else toast("Bank connection was cancelled");
    // Clean the hash so a refresh doesn't re-toast.
    history.replaceState(null, "", window.location.pathname);
}

// While the app is open, ping the DB-touching health endpoint every few minutes
// so Render's free instance stays warm and Supabase doesn't pause. Only fires
// when the tab is visible (no point warming a server nobody's looking at), and
// also pings immediately when the user returns to the tab. We accept a slow
// first load after a long idle — this just keeps it fast for the rest of the
// session. /healthz/db is public + GET, so no auth/CSRF needed.
function startKeepAlive() {
    const ping = () => {
        if (document.visibilityState !== "visible") return;
        fetch("/healthz/db", { cache: "no-store" }).catch(() => {});
    };
    setInterval(ping, 10 * 60 * 1000); // every 10 min (< Render's ~15 min sleep)
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") ping();
    });
}

// Re-inject buttons whenever the user navigates to a different page,
// because Trends/Reports cards aren't rendered until visited.
document.querySelectorAll(".nav-item[data-page]").forEach(btn => {
    btn.addEventListener("click", () => {
        // wait for page-specific load to populate cards
        setTimeout(() => injectFullscreenButtons(), 200);
    });
});

init();
