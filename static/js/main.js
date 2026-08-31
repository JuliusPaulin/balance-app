// Balance — main.js
//
// App state and start-up. Loaded last: init() runs on the final line.
//
// One of the ordered classic scripts index.html loads. They share one global
// scope on purpose: index.html wires ~95 inline handlers straight to these
// names, so these are plain scripts and not modules.

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
