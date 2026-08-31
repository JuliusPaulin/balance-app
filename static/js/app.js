// ── State ────────────────────────────────────────────────────────────
let categories = [];
let currentPage = 1;
let stagingBatchId = null;
let stagingItems = [];   // may contain split virtual items
let stagingMeta = { filename: "" };   // set by whichever path enters review
let charts = {};
let merchantRules = [];
let monthsWithNotes = new Set();

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
        // silent keep-alive pings so the bar doesn't flash every 10 minutes, and
        // the assistant, which waits on a local model for ten or twenty seconds
        // and shows that in its own panel — blurring out the figures you are
        // asking about is the one thing that must not happen).
        if (!url || !SELF_TIMED_PATHS.some(p => url.pathname.startsWith(p))) {
            beginLoading();
            promise.then(endLoading, endLoading);
        }
        return promise;
    };
})();

// Requests that carry their own waiting state, and so must not raise the
// app-wide one.
const SELF_TIMED_PATHS = ["/healthz", "/api/chat"];

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
// Nearly every call the app makes runs through here, so this is the one place
// that decides what a failed request looks like. It throws, and that is the
// point: it used to hand the error body back as if it were data, so the caller
// read on, closed the modal over what the user had typed, and toasted
// "Transaction added" for a row the server had refused. Throwing stops the
// caller at the failed line, and the listener below says what went wrong.
class ApiError extends Error {
    constructor(message, status, code) {
        super(message);
        this.name   = "ApiError";
        this.status = status;
        this.code   = code;   // the server's short machine code, when it sent one
    }
}

// What to say when the server refuses without prose of its own — these are the
// failures that happen before the route, so no route writes a message for them.
const API_STATUS_MESSAGES = {
    403: "That request was refused — reload the app and try again.",
    429: "Too many requests just now — wait a moment and try again.",
    500: "The app hit an error. Nothing was saved.",
};

async function apiError(res) {
    // A Flask error page is HTML, so reading the body has to be allowed to fail.
    let body = null;
    try { body = await res.json(); } catch (e) { /* not JSON — go on the status */ }
    const detail = body && typeof body.error === "string" ? body.error : null;
    return new ApiError(
        detail || API_STATUS_MESSAGES[res.status] || `The server said ${res.status}.`,
        res.status,
        detail,
    );
}

async function api(url, options = {}) {
    if (options.body && typeof options.body === "object" && !(options.body instanceof FormData)) {
        options.headers = { "Content-Type": "application/json", ...options.headers };
        options.body = JSON.stringify(options.body);
    }
    let res;
    try {
        res = await fetch(url, options);
    } catch (e) {
        // fetch rejects only when the request never reached the server at all.
        throw new ApiError("Could not reach the server.", 0, "unreachable");
    }
    if (!res.ok) throw await apiError(res);
    if (res.status === 204) return null;
    return res.json();
}

// api() throws instead of making all its call sites check, so this is where the
// failures land. Only ApiError is claimed here: a plain bug in a handler must
// still reach the console as a bug, not as a message about the server. A caller
// that wants to handle its own catches first and never gets here — see
// loadBankStatus(), which keeps its card hidden and stays quiet.
window.addEventListener("unhandledrejection", (ev) => {
    if (!(ev.reason instanceof ApiError)) return;
    console.error("Request failed:", ev.reason.status, ev.reason.message);
    ev.preventDefault();
    toast(ev.reason.message);
});

// ── Toast ───────────────────────────────────────────────────────────
function toast(message) {
    const container = document.getElementById("toast-container");
    const el = document.createElement("div");
    el.className = "toast";
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => el.remove(), 3000);
}

// ── Confirm dialog ──────────────────────────────────────────────────
// The app asked its "are you sure?" questions through the browser's own
// confirm(), which inside a pywebview window is a system alert box: another
// app's typeface, no way to mark the destructive answer as destructive, and
// paragraphs faked with "\n\n". This asks the same question in the app's own
// modal, and resolves true or false so a caller reads as it always did:
//     if (!await confirmDialog({ title: "Delete this?", danger: true })) return;
function confirmDialog({ title, body = "", confirmLabel = "Confirm", danger = false }) {
    return new Promise(resolve => {
        const overlay = document.createElement("div");
        overlay.className = "modal-overlay";
        const paragraphs = String(body).split(/\n{2,}/).map(t => t.trim()).filter(Boolean)
            .map(t => `<p class="confirm-text">${escapeHtml(t).replace(/\n/g, "<br>")}</p>`)
            .join("");
        overlay.innerHTML = `
            <div class="modal confirm-modal" role="alertdialog" aria-modal="true">
                <div class="modal-title">${escapeHtml(title)}</div>
                ${paragraphs}
                <div class="modal-actions">
                    <button class="btn btn-secondary" data-confirm="no">Cancel</button>
                    <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-confirm="yes">${escapeHtml(confirmLabel)}</button>
                </div>
            </div>`;

        const close = answer => {
            document.removeEventListener("keydown", onKey, true);
            overlay.remove();
            resolve(answer);
        };
        // Escape cancels, the way it did in the box this replaces. Captured, so
        // an input underneath the overlay cannot swallow the key first.
        const onKey = e => { if (e.key === "Escape") { e.preventDefault(); close(false); } };
        overlay.addEventListener("click", e => {
            if (e.target === overlay) return close(false);
            const btn = e.target.closest("[data-confirm]");
            if (btn) close(btn.dataset.confirm === "yes");
        });
        document.addEventListener("keydown", onKey, true);
        document.body.appendChild(overlay);
        overlay.querySelector('[data-confirm="yes"]').focus({ preventScroll: true });
    });
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
            case "import": loadBankStatus(); loadImportHistory(); break;
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
    // The rail's category list comes from the facet counts, not from this
    // array, so it is refreshed by loadFacets() rather than from here.
    renderRailCategories();
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

// ── Search & the filter rail ────────────────────────────────────────
// The filters live in a rail beside the table rather than a drawer above it,
// so nothing opens, nothing closes, and the table never moves under you. Each
// value carries the count it would give — see /api/transactions/facets.
let searchDebounce = null;
let selectedCatIds = new Set();
let selectedMonths = new Set();
let selectedType   = "";           // "" | "expense" | "income"
let periodGrain    = "month";      // "month" | "year"
let facets         = { categories: [], types: [], months: [] };
let railMore       = { period: false, cat: false };
// Sorting used to live in two dropdowns inside the drawer while the table
// headers already sorted on click. The headers won; this is where the state
// they were duplicating now lives.
let txSort = { col: "date", dir: "desc" };

const RAIL_COLLAPSED = 6;          // values shown before "Show N more"

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

// Any rail control: reset to page 1 and reload. The counts move too, because
// a count that ignored the other filters would be telling you about a list
// you are not looking at.
function onRailFilterChange() {
    currentPage = 1;
    loadTransactions();
}

// On a narrow window the rail sits above the table and starts shut, so the
// Filters button still has a job there. On a wide one the rail is always
// visible and the button is hidden by CSS.
function toggleRail() {
    document.getElementById("tx-rail").classList.toggle("open");
}

function toggleRailMore(which) {
    railMore[which] = !railMore[which];
    if (which === "cat") renderRailCategories(); else renderRailPeriods();
}

function setPeriodGrain(grain) {
    periodGrain = grain;
    selectedMonths.clear();
    document.querySelectorAll(".rail-grain-btn").forEach(b =>
        b.classList.toggle("active", b.dataset.grain === grain));
    onRailFilterChange();
}

function toggleMonthFilter(key) {
    if (selectedMonths.has(key)) selectedMonths.delete(key);
    else selectedMonths.add(key);
    onRailFilterChange();
}

function setTypeFilter(type) {
    selectedType = selectedType === type ? "" : type;
    onRailFilterChange();
}

function toggleCatFilter(id) {
    if (selectedCatIds.has(id)) selectedCatIds.delete(id);
    else selectedCatIds.add(id);
    onRailFilterChange();
}

function clearPeriodFilter() {
    selectedMonths.clear();
    document.getElementById("search-date-from").value = "";
    document.getElementById("search-date-to").value = "";
    readDateFilter("search-date-from");
    readDateFilter("search-date-to");
    onRailFilterChange();
}

function clearCatFilter() {
    selectedCatIds.clear();
    onRailFilterChange();
}

function clearAmountFilter() {
    document.getElementById("search-amt-min").value = "";
    document.getElementById("search-amt-max").value = "";
    onRailFilterChange();
}

function resetSearch() {
    document.getElementById("search-q").value = "";
    document.getElementById("search-clear").style.display = "none";
    document.getElementById("search-date-from").value = "";
    document.getElementById("search-date-to").value = "";
    document.getElementById("search-amt-min").value = "";
    document.getElementById("search-amt-max").value = "";
    document.getElementById("rail-cat-search").value = "";
    readDateFilter("search-date-from");
    readDateFilter("search-date-to");
    selectedCatIds.clear();
    selectedMonths.clear();
    selectedType = "";
    onRailFilterChange();
}

// ── The rail's own rendering ────────────────────────────────────────
// A month key is "2026-07" and a year key is "2026"; the same set holds both
// because the grain decides which the rail is offering.
function monthsToPeriods() {
    if (periodGrain === "month") {
        const rows = withSelected(facets.months, selectedMonths, m => m.month,
                                  key => ({ month: key, n: 0 }));
        return rows.sort((a, b) => b.month.localeCompare(a.month))
            .map(m => ({ key: m.month, label: fmtMonthLabel(m.month), n: m.n }));
    }
    const byYear = new Map();
    facets.months.forEach(m => {
        const y = m.month.slice(0, 4);
        byYear.set(y, (byYear.get(y) || 0) + m.n);
    });
    selectedMonths.forEach(y => { if (!byYear.has(y)) byYear.set(y, 0); });
    return [...byYear.entries()].sort((a, b) => b[0].localeCompare(a[0]))
        .map(([y, n]) => ({ key: y, label: y, n }));
}

function fmtMonthLabel(key) {
    const [y, m] = key.split("-");
    const names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return `${names[parseInt(m, 10) - 1]} ${y}`;
}

// One row of the rail. `sel` drives both the tick and the weight, so a chosen
// value reads as chosen without relying on the tick alone.
function railItem({ key, label, n, sel, max, onclick, box = true }) {
    const share = max > 0 ? Math.max(3, Math.round((n / max) * 34)) : 0;
    // A value that would return nothing under the filters already on is a
    // dead click. It stays on the list — vanishing options are worse — but
    // says so rather than looking like the others.
    const dead = n === 0 && !sel ? " empty" : "";
    return `<button class="rail-item ${sel ? "sel" : ""}${dead}" onclick="${onclick}" title="${escapeHtml(label)} — ${n} transaction${n !== 1 ? "s" : ""}">
        ${box ? `<span class="rail-box"></span>` : ""}
        <span class="rail-name">${escapeHtml(label)}</span>
        <span class="rail-bar" style="width:${share}px"></span>
        <span class="rail-n">${n.toLocaleString()}</span>
    </button>`;
}

function renderRailPeriods() {
    const el = document.getElementById("rail-periods");
    if (!el) return;
    const all  = monthsToPeriods();
    const max  = Math.max(1, ...all.map(p => p.n));
    const show = railMore.period
        ? all
        : collapseKeeping(all, p => selectedMonths.has(p.key), RAIL_COLLAPSED);
    el.innerHTML = show.map(p => railItem({
        ...p, sel: selectedMonths.has(p.key), max,
        onclick: `toggleMonthFilter('${p.key}')`,
    })).join("") || `<div class="rail-empty">Nothing in range</div>`;
    renderRailMoreBtn("rail-period-more", all.length - show.length, "period");
}

// The collapsed rail shows the first few values — but never at the cost of
// hiding one you have selected. Original order is kept rather than floating
// the selected ones to the top, so the list does not reshuffle under the
// cursor every time you tick something.
function collapseKeeping(all, isSel, limit) {
    const sel  = all.filter(isSel);
    const rest = all.filter(x => !isSel(x)).slice(0, Math.max(0, limit - sel.length));
    const keep = new Set([...sel, ...rest]);
    return all.filter(x => keep.has(x));
}

// A value you have selected has to stay on its list even when the other
// filters have taken it to zero. The facet query groups over rows that exist,
// so a category with nothing left simply is not in the result — and the rail
// would then show no sign of a filter it is applying. Put it back at zero.
function withSelected(rows, selected, keyOf, make) {
    const present = new Set(rows.map(keyOf));
    const missing = [...selected].filter(k => !present.has(k)).map(make).filter(Boolean);
    return [...rows, ...missing];
}

function renderRailCategories() {
    const el = document.getElementById("rail-categories");
    if (!el) return;
    const term = (document.getElementById("rail-cat-search")?.value || "").toLowerCase().trim();
    const rows = withSelected(facets.categories, selectedCatIds, c => c.id, id => {
        const c = categories.find(x => x.id === id);
        return c ? { id: c.id, name: c.name, type: c.type, n: 0 } : null;
    });
    // A category you have picked also stays visible through a search that no
    // longer matches it — otherwise the filter vanishes mid-typing.
    const all = rows.filter(c =>
        selectedCatIds.has(c.id) || !term || c.name.toLowerCase().includes(term));
    const max  = Math.max(1, ...all.map(c => c.n));
    const show = (railMore.cat || term)
        ? all
        : collapseKeeping(all, c => selectedCatIds.has(c.id), RAIL_COLLAPSED);
    el.innerHTML = show.map(c => railItem({
        key: c.id, label: c.name, n: c.n, sel: selectedCatIds.has(c.id), max,
        onclick: `toggleCatFilter(${c.id})`,
    })).join("") || `<div class="rail-empty">No category matches</div>`;
    renderRailMoreBtn("rail-cat-more", term ? 0 : all.length - show.length, "cat");
}

function renderRailTypes() {
    const el = document.getElementById("rail-types");
    if (!el) return;
    const max = Math.max(1, ...facets.types.map(t => t.n));
    const label = { expense: "Expenses", income: "Income" };
    el.innerHTML = ["expense", "income"].map(t => {
        const row = facets.types.find(x => x.type === t) || { n: 0 };
        return railItem({
            key: t, label: label[t], n: row.n, sel: selectedType === t, max,
            onclick: `setTypeFilter('${t}')`,
        });
    }).join("");
}

function renderRailMoreBtn(id, hidden, which) {
    const btn = document.getElementById(id);
    if (!btn) return;
    if (hidden <= 0 && !railMore[which]) { btn.style.display = "none"; return; }
    btn.style.display = "block";
    btn.textContent = railMore[which] ? "Show fewer" : `Show ${hidden} more…`;
}

function renderRail() {
    renderRailPeriods();
    renderRailTypes();
    renderRailCategories();
}

// The tokens say what is narrowing the list in words, above the table. The
// rail already shows it, but on a narrow window the rail is shut — and a
// filter you cannot see is the fault this whole page is fixing.
function renderFilterTokens(active) {
    const el = document.getElementById("tx-tokens");
    if (!el) return;
    el.innerHTML = active.map(t =>
        `<span class="tx-token">${escapeHtml(t.label)}<button class="tx-token-x" onclick="${t.clear}" title="Remove this filter">×</button></span>`
    ).join("");
    el.style.display = active.length ? "flex" : "none";
    const clearBtn = document.getElementById("tx-clear-all");
    if (clearBtn) clearBtn.style.display = active.length ? "block" : "none";
    const count = document.getElementById("filter-count");
    if (count) count.textContent = active.length ? ` · ${active.length}` : "";
}

// ── Transactions ────────────────────────────────────────────────────

// A date the parser can't read is dropped from the query rather than refused,
// so the field has to say so: otherwise the box keeps showing what was typed
// while the list quietly goes back to every transaction. Half-typed dates hit
// this on the way to a valid one, which is why it marks rather than blocks.
function readDateFilter(id) {
    const el = document.getElementById(id);
    if (!el) return "";
    const raw = (el.value || "").trim();
    const iso = fiToIso(raw) || "";
    const bad = raw !== "" && !iso;
    el.classList.toggle("input-invalid", bad);
    el.title = bad ? "Not a date, so this filter is being ignored. Try 31.7.2026." : "";
    return iso;
}

// Every filter on the page, as query params. One builder, used for the list
// and for the facet counts, so the counts can never describe a different
// filter than the table under them.
function txFilterParams() {
    const p = new URLSearchParams();
    const q = document.getElementById("search-q")?.value.trim() || "";
    const dateFrom = readDateFilter("search-date-from");
    const dateTo   = readDateFilter("search-date-to");
    const amtMin   = document.getElementById("search-amt-min")?.value || "";
    const amtMax   = document.getElementById("search-amt-max")?.value || "";

    if (selectedType) p.set("type", selectedType);
    if (q) p.set("q", q);
    if (dateFrom) p.set("date_from", dateFrom);
    if (dateTo) p.set("date_to", dateTo);
    if (amtMin) p.set("amount_min", amtMin);
    if (amtMax) p.set("amount_max", amtMax);
    if (selectedCatIds.size) p.set("category_ids", [...selectedCatIds].join(","));
    if (selectedMonths.size) {
        // A year in the rail is every month it covers — the endpoint only
        // knows about months, and teaching it about years would put the same
        // calendar logic in two places.
        const months = periodGrain === "year"
            ? facets.months.map(m => m.month).filter(m => selectedMonths.has(m.slice(0, 4)))
            : [...selectedMonths];
        if (months.length) p.set("months", months.join(","));
    }
    return p;
}

// What is narrowing the list right now, in words, each with the call that
// takes it off again.
function activeFilterTokens() {
    const out = [];
    const dateFrom = document.getElementById("search-date-from")?.value.trim();
    const dateTo   = document.getElementById("search-date-to")?.value.trim();
    const amtMin   = document.getElementById("search-amt-min")?.value;
    const amtMax   = document.getElementById("search-amt-max")?.value;

    if (selectedType) {
        out.push({ label: selectedType === "income" ? "Income" : "Expenses",
                   clear: `setTypeFilter('${selectedType}')` });
    }
    if (selectedMonths.size) {
        const list = [...selectedMonths].sort();
        const label = list.length === 1
            ? (periodGrain === "year" ? list[0] : fmtMonthLabel(list[0]))
            : `${list.length} periods`;
        out.push({ label, clear: "clearPeriodFilter()" });
    }
    if (dateFrom || dateTo) {
        out.push({ label: `${dateFrom || "…"} – ${dateTo || "…"}`, clear: "clearPeriodFilter()" });
    }
    selectedCatIds.forEach(id => {
        const c = categories.find(x => x.id === id)
               || facets.categories.find(x => x.id === id);
        if (c) out.push({ label: c.name, clear: `toggleCatFilter(${id})` });
    });
    if (amtMin || amtMax) {
        out.push({ label: `${amtMin ? fmt(amtMin) : "0 €"} – ${amtMax ? fmt(amtMax) : "any"}`,
                   clear: "clearAmountFilter()" });
    }
    return out;
}

// The counts move with the filters, so they are refetched alongside the list.
// A failure here must not take the table down with it: the rail keeps its last
// counts and the list still loads.
async function loadFacets() {
    try {
        facets = await api(`/api/transactions/facets?${txFilterParams()}`);
    } catch (e) {
        return;
    }
    renderRail();
}

async function loadTransactions() {
    const params = txFilterParams();
    params.set("page", currentPage);
    params.set("per_page", 50);
    params.set("sort", txSort.col);
    params.set("dir", txSort.dir);

    renderFilterTokens(activeFilterTokens());

    const data = await api(`/api/transactions?${params}`);
    const tbody = document.getElementById("transactions-body");
    loadFacets();

    const countEl = document.getElementById("search-count");
    if (countEl) {
        // Answer "how much?" for the current filter, not just "how many" —
        // and at a size that matches how much that is worth knowing.
        countEl.innerHTML = data.total
            ? `<span class="tx-result-n">${data.total.toLocaleString()} transaction${data.total !== 1 ? "s" : ""}</span>
               <span class="tx-result-money"><span class="out">−${fmt(data.sum_expense)}</span> out · <span class="in">+${fmt(data.sum_income)}</span> in</span>`
            : `<span class="tx-result-n">Nothing matches</span>`;
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
    if (!await confirmDialog({
        title: "Delete this transaction?",
        confirmLabel: "Delete",
        danger: true,
    })) return;
    await api(`/api/transactions/${id}`, { method: "DELETE" });
    await loadTransactions();
    toast("Transaction deleted");
}

function sortTxCol(col) {
    if (txSort.col === col) {
        txSort.dir = txSort.dir === "asc" ? "desc" : "asc";
    } else {
        txSort = { col, dir: "desc" };
    }
    currentPage = 1;
    loadTransactions();
}

function updateTxSortIcons() {
    const { col, dir } = txSort;
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
    stagingHalved = false;
    syncHalveButton();
    const history = document.getElementById("import-history");
    if (history) history.style.display = "none";
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
    if (!await confirmDialog({
        title: "Disconnect your bank?",
        body: "You'll have to connect again before the next bank import. Transactions you already imported stay.",
        confirmLabel: "Disconnect",
        danger: true,
    })) return;
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
        } else if (data.error === "bank_auth") {
            toast(BANK_RETURN_MESSAGES.auth_error);
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

// The row's amount box, read the way fiToIso reads its date box. A comma is a
// decimal separator here: the app prints amounts as "16,05" and the CSVs it
// imports are written "-25,00", so a comma is what a Finnish hand types. The
// old type="number" input threw one away before any of our code saw it.
// Returns null when the value cannot stand as an amount.
function parseAmountInput(raw) {
    const s = String(raw ?? "").trim().replace(",", ".");
    if (s === "") return null;
    const n = Number(s);
    return Number.isFinite(n) && n > 0 ? Math.round(n * 100) / 100 : null;
}

function syncStagingFromDom() {
    stagingItems.forEach(item => {
        const typeSel   = document.querySelector(`[data-staging-type="${item.id}"]`);
        const storeInp  = document.querySelector(`[data-staging-store="${item.id}"]`);
        const dateInp   = document.querySelector(`[data-staging-date="${item.id}"]`);
        const amountInp = document.querySelector(`[data-staging-amount="${item.id}"]`);
        if (typeSel)   item._selectedType  = typeSel.value;
        if (storeInp)  item._editedStore   = storeInp.value;
        if (dateInp)   item._editedDate    = fiToIso(dateInp.value) || item._editedDate || item.date;
        if (amountInp) item._editedAmount  = parseAmountInput(amountInp.value)
                                             ?? item._editedAmount ?? item.amount;
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
// ── Category picker ─────────────────────────────────────────────────
// Replaces the per-row <select>. Shows every category at once in a colour-dotted
// grid and filters as you type: with 34 categories, scrolling a native dropdown
// was the slowest part of reviewing an import.
let _catPop = null;

function closeCatPicker() {
    if (!_catPop) return;
    document.removeEventListener("mousedown", _catPop.onDocDown, true);
    window.removeEventListener("scroll", _catPop.onScroll, true);
    _catPop.el.remove();
    _catPop = null;
}

function positionCatPop(el, anchor) {
    const r = anchor.getBoundingClientRect();
    const w = el.offsetWidth, h = el.offsetHeight;
    el.style.left = Math.max(8, Math.min(r.left + window.scrollX,
                                         window.scrollX + window.innerWidth - w - 12)) + "px";
    const fitsBelow = r.bottom + h + 12 < window.innerHeight;
    el.style.top = (fitsBelow ? r.bottom + window.scrollY + 6
                              : Math.max(window.scrollY + 8, r.top + window.scrollY - h - 6)) + "px";
}

function openCatPicker(anchor, currentId, onPick) {
    const sameChip = _catPop && _catPop.anchor === anchor;
    closeCatPicker();
    if (sameChip) return;   // clicking the open chip again closes it

    const el = document.createElement("div");
    el.className = "cat-pop";
    el.innerHTML = `<input class="cat-pop-search" placeholder="Search categories…" autocomplete="off">
        <div class="cat-pop-body"></div>
        <div class="cat-pop-foot"><span><kbd>↑</kbd><kbd>↓</kbd> move</span>
            <span><kbd>⏎</kbd> pick</span><span><kbd>esc</kbd> close</span></div>`;
    document.body.appendChild(el);

    const input = el.querySelector(".cat-pop-search");
    const body  = el.querySelector(".cat-pop-body");
    let hits = [], cursor = 0;

    function pick(id) {
        const cat = catById(id);
        closeCatPicker();
        if (cat) onPick(cat);
    }

    function draw() {
        const q = input.value.trim().toLowerCase();
        hits = categories.filter(c => c.name.toLowerCase().includes(q));
        if (cursor >= hits.length) cursor = Math.max(0, hits.length - 1);
        if (!hits.length) {
            body.innerHTML = `<div class="cat-pop-empty">Nothing matches “${escapeHtml(input.value)}”</div>`;
            return;
        }
        const section = (type, label) => {
            const cells = hits.map((c, i) => c.type !== type ? "" :
                `<button type="button" class="cat-pop-item ${i === cursor ? "on" : ""}" data-i="${i}">
                    <span class="dot" style="background:${catDotColor(c.id)}"></span>
                    <span class="n">${escapeHtml(c.name)}</span>
                    ${c.id === currentId ? `<span class="tick">✓</span>` : ""}
                </button>`).join("");
            return cells ? `<div class="cat-pop-group">${label}</div>
                            <div class="cat-pop-grid">${cells}</div>` : "";
        };
        body.innerHTML = section("expense", "Expense") + section("income", "Income");
        body.querySelectorAll(".cat-pop-item").forEach(b => {
            b.onmousedown = ev => { ev.preventDefault(); pick(hits[parseInt(b.dataset.i)].id); };
        });
        // Scroll the panel's own list, not the page: scrollIntoView() walks up to
        // the window and would jump the import list out from under the cursor.
        const on = body.querySelector(".cat-pop-item.on");
        if (on) {
            const top = on.offsetTop, bottom = top + on.offsetHeight;
            if (top < body.scrollTop) body.scrollTop = top;
            else if (bottom > body.scrollTop + body.clientHeight) body.scrollTop = bottom - body.clientHeight;
        }
    }

    input.oninput = () => { cursor = 0; draw(); };
    input.onkeydown = e => {
        if (e.key === "ArrowDown")    { e.preventDefault(); cursor = Math.min(cursor + 1, hits.length - 1); draw(); }
        else if (e.key === "ArrowUp") { e.preventDefault(); cursor = Math.max(cursor - 1, 0); draw(); }
        else if (e.key === "Enter")   { e.preventDefault(); if (hits[cursor]) pick(hits[cursor].id); }
        else if (e.key === "Escape")  { e.preventDefault(); closeCatPicker(); anchor.focus({ preventScroll: true }); }
    };

    el.style.visibility = "hidden";
    draw();
    positionCatPop(el, anchor);
    el.style.visibility = "";
    input.focus({ preventScroll: true });

    const onDocDown = ev => {
        if (!el.contains(ev.target) && ev.target !== anchor && !anchor.contains(ev.target)) closeCatPicker();
    };
    // Follow the chip when the page scrolls rather than closing: a stray trackpad
    // nudge should not throw away a half-typed search.
    const onScroll = () => positionCatPop(el, anchor);
    document.addEventListener("mousedown", onDocDown, true);
    window.addEventListener("scroll", onScroll, true);
    _catPop = { el, anchor, onDocDown, onScroll };
}

function catChipInner(catId, placeholder) {
    const cat = catById(catId);
    return `<span class="dot" style="background:${catDotColor(catId)}"></span>
        <span class="cat-chip-label">${cat ? escapeHtml(cat.name) : placeholder}</span>
        <span class="cat-chip-caret">⌄</span>`;
}

function openStagingCatPicker(btn, itemId) {
    const item = stagingItems.find(i => String(i.id) === String(itemId));
    if (!item) return;
    openCatPicker(btn, effCatId(item), cat => {
        item._selectedCatId = cat.id;
        item._selectedType  = cat.type;
        renderStaging();
    });
}

function weekdayLabel(iso) {
    const d = new Date(iso + "T00:00:00");
    return isNaN(d) ? "" : d.toLocaleDateString("en-US", { weekday: "long" });
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
    const v = parseAmountInput(inp.value);
    // Snapping the box back with nothing said is how an edit disappears. The
    // date box beside this one has always explained itself; so does this now.
    if (v === null) {
        toast("Amount must be more than 0 — e.g. 12,50");
        inp.value = effAmount(item);
        return;
    }
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
            <button type="button" class="cat-chip-btn" data-staging-cat="${item.id}"
                    onclick="openStagingCatPicker(this, '${item.id}')">
                ${catChipInner(catId, "Pick category")}
            </button>
        </span>
        <span class="cell-amount ${type}">
            <span class="sign">${type === "income" ? "+" : "−"}</span>
            <input type="text" inputmode="decimal" class="cell-input" data-staging-amount="${item.id}"
                   value="${effAmount(item)}" title="Amount — comma or dot, e.g. 12,50"
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

// Category chosen in the toolbar, waiting to be applied to the ticked rows.
let bulkCatId = null;

function populateBulkCategorySelect() {
    bulkCatId = null;
    renderBulkCatBtn();
}

function renderBulkCatBtn() {
    const btn = document.getElementById("bulk-category-btn");
    if (btn) btn.innerHTML = catChipInner(bulkCatId, "Assign category…");
}

function openBulkCatPicker(btn) {
    openCatPicker(btn, bulkCatId, cat => { bulkCatId = cat.id; renderBulkCatBtn(); });
}

function applyBulkCategory() {
    const cat = catById(bulkCatId);
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

// "÷2 Split costs" is for a statement you share with someone: your half of the
// costs. It used to halve every row, salary included, which is not a cost and
// is not shared. It also compounded — a second click quartered the import with
// nothing on screen to say the first had landed — so it is a toggle now, and
// undoing restores the amounts exactly rather than doubling a rounded half.
let stagingHalved = false;

function halveAllAmounts() {
    syncStagingFromDom();
    const shared = stagingItems.filter(i => effType(i) === "expense");
    if (!shared.length) { toast("Nothing to split — no expenses in this import"); return; }

    if (stagingHalved) {
        shared.forEach(item => {
            if (item._preHalveAmount != null) item._editedAmount = item._preHalveAmount;
            delete item._preHalveAmount;
        });
        stagingHalved = false;
    } else {
        shared.forEach(item => {
            item._preHalveAmount = effAmount(item);
            item._editedAmount = Math.round((effAmount(item) / 2) * 100) / 100;
        });
        stagingHalved = true;
    }
    renderStaging();
    syncHalveButton();
    const kept = stagingItems.length - shared.length;
    toast(stagingHalved
        ? `Expenses halved${kept ? ` · ${kept} income row${kept === 1 ? "" : "s"} left alone` : ""}`
        : "Amounts restored");
}

function syncHalveButton() {
    const btn = document.getElementById("halve-btn");
    if (!btn) return;
    btn.classList.toggle("active-filter", stagingHalved);
    btn.textContent = stagingHalved ? "÷2 Halved — undo" : "÷2 Split costs";
}

// ── Import history ──────────────────────────────────────────────────
// import_batches was written by three code paths and read by none: an
// abandoned review disappeared with nowhere to resume from, and a finished one
// left no record of what it brought in. This is the reader.
async function loadImportHistory() {
    const card = document.getElementById("import-history");
    const list = document.getElementById("import-history-list");
    if (!card || !list) return;
    let batches;
    try {
        batches = await api("/api/import/batches");
    } catch (e) {
        card.style.display = "none";
        return;
    }
    if (!batches.length) { card.style.display = "none"; return; }
    card.style.display = "block";
    list.innerHTML = batches.map(renderImportHistoryRow).join("");
}

function renderImportHistoryRow(b) {
    const when = b.imported_at ? fmtDate(b.imported_at.slice(0, 10)) : "";
    let state, action = "", note;
    if (b.status === "pending") {
        state = `<span class="recurring-badge recurring-due">Unfinished</span>`;
        note  = `${b.staged} row${b.staged === 1 ? "" : "s"} waiting`;
        action = `<button class="import-link-btn" onclick="resumeImport(${b.id})">Resume</button>
                  <button class="import-link-btn danger" onclick="discardBatch(${b.id})">Discard</button>`;
    } else if (b.status === "undone") {
        state = `<span class="recurring-badge recurring-stopped">Undone</span>`;
        note  = "its transactions were removed";
    } else {
        state = `<span class="recurring-badge recurring-active">Imported</span>`;
        note  = b.imported
            ? `${b.imported} transaction${b.imported === 1 ? "" : "s"} · −${fmt(b.sum_expense)} / +${fmt(b.sum_income)}`
            : "too old to undo — not linked to its transactions";
        // Only offer to take back an import we can actually identify.
        if (b.imported) {
            action = `<button class="import-link-btn danger" onclick="undoImport(${b.id}, ${b.imported})">Undo</button>`;
        }
    }
    return `<div class="import-history-row">
        <span class="ih-file" title="${escapeHtml(b.filename)}">${escapeHtml(b.filename)}</span>
        <span class="ih-when">${when}</span>
        <span class="ih-state">${state}</span>
        <span class="ih-note">${note}</span>
        <span class="ih-actions">${action}</span>
    </div>`;
}

// Pick an unfinished review back up where it was left.
async function resumeImport(batchId) {
    const data = await api(`/api/import/staging/${batchId}`);
    if (!data.items || !data.items.length) {
        toast("Nothing left to review in that import");
        loadImportHistory();
        return;
    }
    stagingMeta.filename = "Unfinished import";
    enterReview(data);
}

async function discardBatch(batchId) {
    if (!await confirmDialog({
        title: "Discard this unfinished import?",
        body: "The rows still waiting for review are thrown away. Nothing was added to your transactions.",
        confirmLabel: "Discard",
        danger: true,
    })) return;
    await api(`/api/import/batch/${batchId}`, { method: "DELETE" });
    toast("Unfinished import discarded");
    loadImportHistory();
}

async function undoImport(batchId, count) {
    if (!await confirmDialog({
        title: `Remove ${count} transaction${count === 1 ? "" : "s"}?`,
        body: "This import added them. Removing them cannot be undone.",
        confirmLabel: "Remove",
        danger: true,
    })) return;
    const res = await api(`/api/import/batch/${batchId}/undo`, { method: "POST" });
    toast(`${res.removed} transaction${res.removed === 1 ? "" : "s"} removed`);
    loadImportHistory();
}

// Cancel means cancel. Without this the batch and its staged rows outlived the
// screen that made them: nothing reads them back, nothing cleans them up, and
// they pile up unseen. confirmAllImports() deliberately does NOT come through
// here — a confirmed batch is the record of what was imported.
async function discardImport() {
    const id = stagingBatchId;
    cancelImport();
    if (id == null) return;
    try {
        await api(`/api/import/batch/${id}`, { method: "DELETE" });
    } catch (e) {
        // The rows are already off the screen; a failed cleanup is not worth
        // dragging the user back into a review they asked to leave.
        console.warn("Could not discard import batch", id, e);
    }
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
    // Leaving a review always changes the history: a batch was just confirmed,
    // discarded, or left unfinished.
    loadImportHistory();
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
    const catBreakdown = await api(expenseBreakdownUrl());
    renderCategoryBars(catBreakdown);
    await loadIncomeBreakdown(catBreakdown);
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
    if (!await confirmDialog({
        title: `Remove "${name}" from the ${fmtDate(asOf)} snapshot?`,
        body: `The account total for that date drops by ${cells[2].textContent.trim()}, ` +
              `and every month reading from that snapshot changes with it.\n\n` +
              `Sold it? Import a newer statement instead — the new snapshot won't list it, ` +
              `and your history stays true to what you held.`,
        confirmLabel: "Remove",
        danger: true,
    })) return;
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
    if (!await confirmDialog({
        title: `Close "${name}" as of ${isoToFi(asOf)}?`,
        body: `Its balance goes to 0 from that date. Months before it keep the old value, ` +
              `so your history stays intact. You can reopen it later.`,
        confirmLabel: "Close account",
    })) return;
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
    if (!await confirmDialog({
        title: `Delete "${name}" and every balance recorded for it?`,
        body: `This rewrites your net-worth history as if you never held it. ` +
              `If you sold it, close it instead (⊘) — that keeps the past.`,
        confirmLabel: "Delete",
        danger: true,
    })) return;
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
        try {
            await api(`/api/accounts/${row.dataset.accountId}/balances`,
                { method: "POST", body: { as_of: asOf, balance: parseFloat(input.value) } });
        } catch (e) {
            // One account at a time, so a refusal halfway leaves the earlier
            // ones saved. Say so — a silent stop looks like nothing happened.
            if (!(e instanceof ApiError)) throw e;
            toast(saved ? `Saved ${saved}, then stopped: ${e.message}` : e.message);
            await loadNetWorth();
            return;
        }
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

// ── Card fullscreen ─────────────────────────────────────────────────
const FS_ICON_OPEN = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>';
const FS_ICON_CLOSE = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 4H4v5M15 4h5v5M9 20H4v-5M15 20h5v-5"/></svg>';

function _isVisualCard(card) {
    if (card.classList.contains("no-fs")) return false;
    return !!(
        card.querySelector("canvas") ||
        card.querySelector(".heatmap-grid") ||
        card.querySelector("#category-bars") ||
        card.querySelector("#income-bars") ||
        card.querySelector("#report-category-bars") ||
        card.querySelector("#report-income-bars")
    );
}

function _findCardHeader(card) {
    const flex = card.querySelector(":scope > .flex.items-center");
    if (flex) return flex;
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
            <p class="confirm-text" style="margin-bottom:16px">
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
// (a fallback for the fetch wrapper, which normally reads it from the cookie).
async function loadAppState() {
    try {
        const res = await fetch("/api/me");
        if (!res.ok) return;
        const me = await res.json();
        if (me && me.csrf_token) csrfTokenCache = me.csrf_token;
        // Shown in Settings → Help, so you can tell at a glance whether an
        // update actually landed.
        const vEl = document.getElementById("app-version");
        if (vEl && me && me.version) vEl.textContent = me.version;
    } catch (e) {
        // network hiccup — don't block the rest of init
    }
}

// ── Ask: the assistant panel ─────────────────────────────────────────
// The loop, the tools and the model all live on the server; this is the panel
// around them. Two things here are not decoration:
//
//   1. Every answer shows which of the app's own screens it read. The whole
//      claim of this feature is that its figures are the Dashboard's figures,
//      and a number with nothing behind it is one you have to take on trust.
//   2. Errors land in the transcript, not in a toast. api() throws and the
//      global handler would turn that into a message floating over a chat that
//      still showed your question hanging unanswered.

let chatMessages = [];      // the conversation, as the API wants it
let chatBusy = false;
let chatReady = null;       // null = not probed yet; else the status payload

// The six tools, said in English. The point is not the function name — it is
// which screen of the app the number came off.
const CHAT_TOOL_NAMES = {
    search_transactions: "Read your transactions",
    category_breakdown: "Read the category breakdown",
    monthly_summary: "Read the monthly summary",
    list_subscriptions: "Read your subscriptions",
    annual_report: "Read the annual report",
    net_worth_summary: "Read your net worth",
};

const CHAT_SUGGESTIONS = [
    "What did I spend on groceries last month?",
    "Did I spend more in July than in June?",
    "What are my subscriptions costing me?",
    "How is this year going compared to last year?",
];

function toggleChat() {
    document.getElementById("chat-panel").classList.contains("open")
        ? closeChat() : openChat();
}

function openChat() {
    const panel = document.getElementById("chat-panel");
    panel.classList.add("open");
    panel.setAttribute("aria-hidden", "false");
    // The floating button steps aside rather than sitting over its own panel.
    document.body.classList.add("chat-open");
    document.getElementById("chat-open-btn").classList.add("active");
    // Probe once per session. The model runs on this machine and can simply be
    // off, so what matters is whether it is answering *now*.
    if (chatReady === null) loadChatStatus();
    setTimeout(() => document.getElementById("chat-input").focus(), 220);
}

function closeChat() {
    const panel = document.getElementById("chat-panel");
    panel.classList.remove("open");
    panel.setAttribute("aria-hidden", "true");
    document.body.classList.remove("chat-open");
    document.getElementById("chat-open-btn").classList.remove("active");
}

async function loadChatStatus() {
    try {
        chatReady = await api("/api/chat/status");
    } catch (e) {
        // Caught rather than thrown on: the panel says what is wrong itself,
        // which is more use than a toast over an empty transcript.
        chatReady = { configured: false, detail: "Could not reach the app's own server." };
    }
    const label = document.getElementById("chat-model");
    label.textContent = chatReady.configured && chatReady.model
        ? `${chatReady.model} · on this Mac` : "on this Mac";
    renderChatLog();
}

function resetChat() {
    chatMessages = [];
    renderChatLog();
    document.getElementById("chat-input").focus();
}

// ── Rendering ────────────────────────────────────────────────────────

function renderChatLog(pending = false) {
    const log = document.getElementById("chat-log");

    if (!chatMessages.length && !pending) {
        log.innerHTML = chatReady && !chatReady.configured
            ? chatSetupHtml() : chatEmptyHtml();
        updateChatLengthNote();
        return;
    }

    log.innerHTML = chatMessages.map(chatMessageHtml).join("")
        + (pending ? `<div class="chat-thinking">
               <span class="chat-dots"><span></span><span></span><span></span></span>
               Reading your figures…
           </div>` : "");
    log.scrollTop = log.scrollHeight;
    updateChatLengthNote();
}

function chatEmptyHtml() {
    return `<div class="chat-empty">
        <div class="chat-empty-title">Ask about your money</div>
        <div class="chat-empty-sub">Runs on a model on this Mac. Nothing leaves the
            machine, and every answer says which screen it read.</div>
        <div class="chat-suggestions">
            ${CHAT_SUGGESTIONS.map(q => `
                <button class="chat-suggestion" onclick="askChat(this.textContent.trim())">${escapeHtml(q)}</button>
            `).join("")}
        </div>
    </div>`;
}

// "Unavailable" tells someone with Ollama running and a different model pulled
// exactly nothing. The status endpoint knows which of the two it is, so say it,
// and give the command.
function chatSetupHtml() {
    const s = chatReady || {};
    const model = s.model || "qwen3.5:4b";
    let what, how;
    if (s.reachable === false) {
        what = "Ollama isn't running on this Mac.";
        how = "ollama serve";
    } else if (s.model_installed === false) {
        what = `Ollama is running, but <strong>${escapeHtml(model)}</strong> isn't installed.`;
        how = `ollama pull ${model}`;
        if (s.installed_models && s.installed_models.length) {
            what += ` You have: ${s.installed_models.map(escapeHtml).join(", ")}.`;
        }
    } else {
        what = escapeHtml(s.detail || "The assistant isn't set up yet.");
        how = `ollama pull ${model}`;
    }
    return `<div class="chat-setup">
        <h4>Not ready yet</h4>
        <p>${what}</p>
        <code>${escapeHtml(how)}</code>
        <p>The assistant answers from your own database using a model on this
           machine, so it needs one installed.</p>
        <button class="btn btn-secondary btn-sm" onclick="chatReady=null;loadChatStatus()">Check again</button>
    </div>`;
}

function chatMessageHtml(m) {
    if (m.role === "user") {
        return `<div class="chat-msg chat-msg-user">
            <div class="chat-bubble">${escapeHtml(m.content)}</div>
        </div>`;
    }
    const cls = m.isError ? "chat-msg chat-msg-assistant chat-msg-error"
                          : "chat-msg chat-msg-assistant";
    return `<div class="${cls}">
        <div class="chat-bubble">${chatFormat(m.content)}</div>
        ${m.isError ? "" : chatSourcesHtml(m)}
    </div>`;
}

// The working. A tool that failed is shown as one: the model was handed the
// error and may have answered around it, and that is worth seeing.
function chatSourcesHtml(m) {
    const calls = m.toolCalls || [];
    if (!calls.length) {
        // A refusal ("I can't delete things") legitimately reads nothing, and
        // flagging it would be noise. An unsourced *figure* is the real fault,
        // so the warning follows the digits.
        return /\d/.test(m.content || "")
            ? `<div class="chat-unsourced">
                   <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
                        style="width:12px;height:12px"><path d="M12 9v4m0 4h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>
                   No lookup behind this answer — check it before trusting it
               </div>`
            : "";
    }
    const rows = calls.map(c => {
        const name = CHAT_TOOL_NAMES[c.tool] || c.tool;
        const args = chatArgSummary(c.arguments);
        return `<div class="${c.ok ? "" : "chat-source-failed"}">
            ${c.ok ? "" : "couldn't read — "}${escapeHtml(name)}
            ${args ? `<span class="chat-source-args">${escapeHtml(args)}</span>` : ""}
        </div>`;
    }).join("");
    // The months read, on the summary line rather than inside the fold. The
    // model is asked to name them and mostly does, but this is the one fact
    // that decides whether an answer is right, so it should not depend on that.
    const periods = [...new Set(calls.map(c => c.period).filter(Boolean))];
    const when = periods.length === 1 ? chatPeriodLabel(periods[0]) : null;
    const count = calls.length === 1 ? "1 lookup" : `${calls.length} lookups`;
    const label = when ? `${escapeHtml(when)} · ${count}` : count;
    return `<details class="chat-sources">
        <summary>
            <svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M9 18l6-6-6-6"/></svg>
            ${label}
        </summary>
        <div class="chat-source-list">${rows}</div>
    </details>`;
}

// "2026-07" → "Jul 2026"; "2026-06 to 2026-08" → "Jun 2026 to Aug 2026". A
// label the tools already computed, said the way the rest of the app says it.
function chatPeriodLabel(period) {
    if (!period) return "";
    return String(period).replace(/\d{4}-\d{2}/g, m => fmtMonthLabel(m));
}

function chatArgSummary(args) {
    if (!args || typeof args !== "object") return "";
    return Object.entries(args)
        .filter(([k]) => k !== "period" && k !== "months")
        .filter(([, v]) => v !== null && v !== undefined && v !== "")
        .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : v}`)
        .join(" · ");
}

// Escape first, then the little the model actually emits: **bold**, and
// amounts. Everything else is left as typed — `white-space: pre-wrap` keeps the
// line breaks, so there is no need to build HTML out of them.
function chatFormat(text) {
    return escapeHtml(text || "")
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/(-?\d[\d\s .,]*\s?€)/g, '<span class="amt">$1</span>');
}

function updateChatLengthNote() {
    // The whole history is resent every turn, so a long conversation is slower
    // and dearer than a short one. The server refuses at 20 turns; warn first.
    const note = document.getElementById("chat-length-note");
    const turns = chatMessages.filter(m => m.role === "user").length;
    if (turns >= 15) {
        note.hidden = false;
        note.textContent = turns >= 19
            ? "This conversation is full — start a new one."
            : "Getting long. A new conversation will be quicker.";
    } else {
        note.hidden = true;
    }
}

// ── Sending ──────────────────────────────────────────────────────────

function askChat(question) {
    const input = document.getElementById("chat-input");
    input.value = question;
    sendChat();
}

async function sendChat(event) {
    if (event) event.preventDefault();
    if (chatBusy) return false;

    const input = document.getElementById("chat-input");
    const question = input.value.trim();
    if (!question) return false;

    chatMessages.push({ role: "user", content: question });
    input.value = "";
    autoGrowChatInput();
    setChatBusy(true);
    renderChatLog(true);

    try {
        // Only role and content go up; the tool trace is ours to display and
        // the server rejects anything else in a message.
        const payload = chatMessages.map(m => ({ role: m.role, content: m.content }));
        const result = await api("/api/chat", { method: "POST", body: { messages: payload } });
        chatMessages.push({
            role: "assistant",
            content: result.reply || "The model replied with nothing.",
            toolCalls: result.tool_calls || [],
        });
    } catch (e) {
        if (!(e instanceof ApiError)) throw e;
        // In the transcript, beneath the question it failed to answer.
        chatMessages.push({ role: "assistant", content: e.message, isError: true });
        // A model that is off now was probably on when the panel opened.
        if (e.status === 503 || e.status === 400) chatReady = null;
    } finally {
        setChatBusy(false);
        renderChatLog();
        input.focus();
    }
    return false;
}

function setChatBusy(busy) {
    chatBusy = busy;
    document.getElementById("chat-input").disabled = busy;
    document.getElementById("chat-send").disabled = busy
        || !document.getElementById("chat-input").value.trim();
}

function autoGrowChatInput() {
    const input = document.getElementById("chat-input");
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 132) + "px";
}

function initChat() {
    const input = document.getElementById("chat-input");

    input.addEventListener("input", () => {
        autoGrowChatInput();
        document.getElementById("chat-send").disabled = chatBusy || !input.value.trim();
    });

    // Enter sends, Shift+Enter breaks the line — what every chat box does, and
    // the opposite would surprise everyone.
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendChat();
        }
    });

    document.addEventListener("keydown", (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
            e.preventDefault();
            toggleChat();
        } else if (e.key === "Escape"
                   && document.getElementById("chat-panel").classList.contains("open")) {
            closeChat();
        }
    });

    renderChatLog();
}

async function init() {
    initTheme();
    loadAppState();
    renderPaletteOptions();
    applyChartDefaults();
    startKeepAlive();
    // A first load that fails must not take the rest of the shell with it: the
    // toast says what happened and the app is still there to try again from.
    try {
        await loadCategories();
        await loadDashboard();
    } catch (e) {
        if (!(e instanceof ApiError)) throw e;
        toast(e.message);
    }
    injectFullscreenButtons();
    initDashboardFloatPill();
    initChat();
    handleBankReturn();
}

// What each /#import?bank=<reason> code from the server means to the user.
// auth_error is the app's own Enable Banking credentials being refused: there is
// no consent yet at that point, so "reconnect" would send them round the same
// loop. Only the server owner can fix it.
const BANK_RETURN_MESSAGES = {
    connected: "Bank connected",
    cancelled: "Bank connection was cancelled",
    error: "Couldn't reach your bank — try again",
    auth_error: "This app's bank credentials were refused. Check the Enable Banking app id and key.",
    not_configured: "Bank import isn't set up on this server",
};

// After the bank consent round-trip the server redirects to /#import?bank=<reason>.
// Detect that, open the Import tab, toast the outcome, and refresh the status card.
function handleBankReturn() {
    const hash = window.location.hash || "";
    const m = hash.match(/bank=(\w+)/);
    if (!m) return;
    const importNav = document.querySelector('.nav-item[data-page="import"]');
    if (importNav) importNav.click();  // activates page + runs loadBankStatus()
    toast(BANK_RETURN_MESSAGES[m[1]] || "Bank connection failed");
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
