// Balance — settings.js
//
// The Settings actions: quit, retrain the merchant rules, the guide.
//
// One of the ordered classic scripts index.html loads. They share one global
// scope on purpose: index.html wires ~95 inline handlers straight to these
// names, so these are plain scripts and not modules.

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

// Hiding the guide also marks it seen, so it does not reopen on next launch.
// One function, because the backdrop click, the Done button and Escape all mean
// the same thing.
function closeGuide() {
    document.getElementById("guide-overlay").style.display = "none";
    localStorage.setItem("guide_seen", "1");
}

function guideStep(dir) {
    const total = GUIDE_SLIDES.length;
    _guideStep += dir;
    if (_guideStep >= total) {
        closeGuide();
        return;
    }
    if (_guideStep < 0) _guideStep = 0;
    _renderGuide();
}

function resetGuide() {
    localStorage.removeItem("guide_seen");
    toast("Guide state reset — will show on next launch");
}

