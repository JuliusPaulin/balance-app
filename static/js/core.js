// Balance — core.js
//
// The plumbing every area shares: state, the fetch wrapper, api(),
// toasts, confirmDialog(), navigation, theme, chart theming, the category
// palette, fmt() and the card-fullscreen affordance.
//
// One of the ordered classic scripts index.html loads. They share one global
// scope on purpose: index.html wires ~95 inline handlers straight to these
// names, so these are plain scripts and not modules.

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

// ── Closing the modal a save just finished with ─────────────────────
// A modal built per open used to close itself with
// `document.querySelector(".modal-overlay").remove()`, which takes the FIRST
// overlay in the document — not the open one. `#invest-overlay` is declared in
// index.html and so sits ahead of everything appended to the body, and it is a
// `.modal-overlay` whether or not it is showing. So every one of those saves
// deleted the hidden investment-import overlay and left the modal the user was
// looking at on screen, with a toast saying the save had landed: the "it does
// not close, you have to click outside" bug. (Clicking outside then worked
// because the backdrop handler is bound to the element itself.) It also took
// the investment overlay out of the DOM for good, so Import investments did
// nothing until the app was reloaded.
//
// The last VISIBLE overlay is the one on top and the one a save means, the
// same rule closeTopModal() uses for Escape.
function closeTopOverlay() {
    const open = [...document.querySelectorAll(".modal-overlay")]
        .filter(o => getComputedStyle(o).display !== "none");
    const top = open[open.length - 1];
    if (!top) return;
    const closer = window[top.dataset.close];
    if (typeof closer === "function") closer();
    else top.remove();
}

// ── Escape closes the top modal ─────────────────────────────────────
// Every modal closes on a click outside it, and confirmDialog() has always
// taken Escape too. None of the others did, so a drilldown opened by clicking
// a bar could only be dismissed by aiming at the backdrop behind it — and in a
// pywebview window there is no browser chrome to fall back on.
//
// The last overlay in the DOM is the one on top, and the one Escape means. The
// modals built per open are removed; an overlay declared in index.html is
// reused rather than rebuilt, so it names its own closer in `data-close` —
// which is also how a surface that is not a `.modal-overlay` at all (the guide)
// joins in.
function closeTopModal() {
    const open = [...document.querySelectorAll(".modal-overlay, [data-close]")]
        .filter(o => getComputedStyle(o).display !== "none");
    const top = open[open.length - 1];
    if (!top) return false;
    const closer = window[top.dataset.close];
    if (typeof closer === "function") closer();
    else top.remove();
    return true;
}

document.addEventListener("keydown", e => {
    if (e.key !== "Escape" || e.defaultPrevented) return;
    // Whatever is above the modal gets the key first: confirmDialog() and the
    // import category picker mark it handled, and these two say so by class.
    if (document.body.classList.contains("fs-active")) return;
    if (document.body.classList.contains("nav-open")) return;
    if (closeTopModal()) e.preventDefault();
});

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

