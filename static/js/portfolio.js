// Balance — portfolio.js
// Portfolio analysis stays a separate surface so its assumptions and stale
// states cannot be confused with the ordinary net-worth ledger.

let portfolioData = null;
let portfolioRequest = 0;
let portfolioSettings = null;
let portfolioPreviewRequest = 0;
let portfolioLastPreview = null;

const portfolioColors = ["#0a84ff", "#34c759", "#ff9f0a", "#af52de", "#ff375f", "#64d2ff", "#30d158", "#ffd60a"];
const portfolioAllocationColor = (name, i) => {
    const mandates = {World: "#0a84ff", US: "#34c759", EM: "#ff9f0a", Norway: "#af52de", Europe: "#ff375f", Finland: "#64d2ff", Sweden: "#ffd60a", Apple: "#5856d6", Bonds: "#30d158", Unknown: "#8e8e93"};
    const hash = [...name].reduce((n, c) => (n * 31 + c.charCodeAt(0)) >>> 0, 0);
    return mandates[name] || portfolioColors[hash % portfolioColors.length];
};
const portfolioEsc = value => typeof escapeHtml === "function" ? escapeHtml(value) : String(value ?? "");
const portfolioMoney = value => value == null ? "—" : fmt2(Number(value));
const portfolioPercent = value => value == null ? "—" : `${(Number(value) * 100).toFixed(1)}%`;
const portfolioRows = rows => Array.isArray(rows) ? rows : [];

function portfolioDestroyChart(key) {
    if (charts[key]) { charts[key].destroy(); delete charts[key]; }
}

async function loadPortfolio() {
    const token = ++portfolioRequest;
    ++portfolioPreviewRequest; portfolioLastPreview = null;
    document.getElementById("portfolio-status").textContent = "Loading portfolio…";
    const error = document.getElementById("portfolio-error");
    if (!error) return;
    error.hidden = true;
    try {
        const data = await api("/api/portfolio");
        if (token !== portfolioRequest) return;
        portfolioData = data || {};
        portfolioSettings = portfolioData.settings || {};
        renderPortfolio();
    } catch (e) {
        if (token !== portfolioRequest) return;
        error.textContent = e.message || "Portfolio could not be loaded.";
        error.hidden = false;
        document.getElementById("portfolio-summary").innerHTML = "";
    }
}

function renderPortfolio() {
    const d = portfolioData || {};
    const total = d.total ?? d.listed_value;
    document.getElementById("portfolio-status").textContent = d.valuation_date ? `Valued ${fmtDate(d.valuation_date)}` : "Allocation, costs and risk";
    document.getElementById("portfolio-valuation-date").textContent = d.valuation_date ? `As of ${fmtDate(d.valuation_date)}` : "No valuation date";
    if (d.invalid_reason) { document.getElementById("portfolio-error").textContent=d.invalid_reason; document.getElementById("portfolio-error").hidden=false; }
    const cost = d.annual_cost_eur;
    document.getElementById("portfolio-summary").innerHTML = [
        ["Portfolio value", portfolioMoney(total), "accent"],
        ["Known annual fund costs", cost == null ? "Unknown" : portfolioMoney(cost), ""],
        ["Holdings", portfolioRows(d.holdings).length, ""],
        ["Holdings coverage", portfolioPercent(d.coverage?.holdings), ""],
    ].map(([label, value, tone]) => `<div class="summary-card"><div class="label">${label}</div><div class="value ${tone}">${value}</div></div>`).join("");
    const dates = portfolioRows(d.accounts).filter(a => a.included && a.balance).map(a => a.as_of).filter(Boolean).sort();
    document.getElementById("portfolio-dates-note").textContent = total > 0 ? `Listed investments ${portfolioMoney(d.listed_value)}; unclassified ${portfolioMoney(total - d.listed_value)}. Valuations ${dates[0]} to ${dates[dates.length-1]}. Emergency cash excluded.` + (d.stale_accounts?.length ? ` ${d.stale_accounts.length} account valuation(s) older than 45 days.` : "") : "No included investments yet. Import broker holdings through Net Worth, or include an investment account below.";
    document.getElementById("portfolio-preview-result").textContent = "Preview only; existing holdings are retained and no trades are placed.";
    renderPortfolioValueChart();
    renderPortfolioAllocation();
    renderPortfolioTargets();
    renderPortfolioHistory();
    renderPortfolioQuality();
    renderPortfolioHoldings();
    renderPortfolioAccounts();
    renderPortfolioAssumptions();
    renderPortfolioCurrentDrift();
    renderPortfolioRisk();
    renderPortfolioPerformance();
    renderPortfolioFlows();
    loadPortfolioModel();
}

function renderPortfolioValueChart() {
    const rows = portfolioRows(portfolioData?.history);
    portfolioDestroyChart("portfolioValue");
    if (!rows.length || typeof Chart === "undefined") return;
    charts.portfolioValue = new Chart(document.getElementById("chart-portfolio-value"), {
        type: "line", data: { labels: rows.map(r => r.date), datasets: [{ label: "Value", data: rows.map(r => r.value), borderColor: chartTheme().accent, backgroundColor: "rgba(10,132,255,.12)", fill: true, tension: .28, pointRadius: 2 }] },
        options: { maintainAspectRatio: false, scales: { y: { ticks: { callback: v => fmt(v) }, grid: { color: chartTheme().grid } }, x: { grid: { display: false } } }, plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => fmt2(c.raw) } } } }
    });
}

function renderPortfolioAllocation() {
    const basis = document.getElementById("portfolio-allocation-basis")?.value || "mandate";
    const rows = portfolioRows(portfolioData?.allocation?.[basis]);
    const list = document.getElementById("portfolio-allocation-list");
    const legendRow = (r, i) => `<div class="portfolio-legend-row"><span><i style="background:${portfolioAllocationColor(r.name, i)}"></i>${portfolioEsc(r.name)}</span><strong>${portfolioPercent(r.weight)}</strong></div>`;
    list.innerHTML = rows.length ? rows.slice(0, 9).map(legendRow).join("") + (rows.length > 9 ? `<details><summary>${rows.length - 9} smaller country exposures</summary>${rows.slice(9).map((r, i) => legendRow(r, i + 9)).join("")}</details>` : "") : `<div class="portfolio-muted">No verified ${portfolioEsc(basis.replace("_", " "))} breakdown.</div>`;
    portfolioDestroyChart("portfolioAllocation");
    if (!rows.length || typeof Chart === "undefined") return;
    charts.portfolioAllocation = new Chart(document.getElementById("chart-portfolio-allocation"), {
        type: "doughnut", data: { labels: rows.map(r => r.name), datasets: [{ data: rows.map(r => r.value), backgroundColor: rows.map((r, i) => portfolioAllocationColor(r.name, i)), borderWidth: 0 }] },
        options: { maintainAspectRatio: false, cutout: "68%", plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => `${c.label}: ${fmt2(c.raw)}` } } } }
    });
}

function renderPortfolioTargets() {
    const basis = document.getElementById("portfolio-allocation-basis").value;
    const rows = portfolioRows(portfolioData?.allocation?.[basis]);
    const targets = portfolioSettings?.targets?.[basis] || {};
    const names = [...new Set([...rows.map(r=>r.name),...Object.keys(targets)])];
    const extra = portfolioRows(portfolioData?.target_options?.[basis]).filter(name=>!names.includes(name));
    document.getElementById("portfolio-targets").innerHTML = `<div class="portfolio-target-head"><span>Targets must total 100%</span><button class="btn btn-secondary btn-sm" onclick="savePortfolioTargets()">Save</button><button class="btn btn-secondary btn-sm" onclick="clearPortfolioTargets()">Clear</button></div>` + names.map(name=>`<label>${portfolioEsc(name)}<input aria-label="${portfolioEsc(name)} target percent" class="form-input portfolio-target-input" data-target-name="${portfolioEsc(name)}" value="${targets[name]==null?"":(targets[name]*100).toFixed(2)}" type="number" min="0" max="100" step=".01" placeholder="—"><span>%</span></label>`).join("") + `<label><select class="form-input" id="portfolio-target-new" aria-label="Additional target allocation"><option value="">Additional allocation</option>${extra.map(name=>`<option>${portfolioEsc(name)}</option>`).join("")}</select><input class="form-input" id="portfolio-target-new-value" type="number" min="0" max="100" step=".01" placeholder="%"><span>%</span></label>`;
}
async function savePortfolioTargets() {
    const basis = document.getElementById("portfolio-allocation-basis").value, targets = {};
    document.querySelectorAll(".portfolio-target-input").forEach(i=>{if(i.value!=="") targets[i.dataset.targetName]=Number(i.value)/100;});
    const key=document.getElementById("portfolio-target-new").value.trim(), val=document.getElementById("portfolio-target-new-value").value;
    if(key && val!=="") targets[key]=Number(val)/100;
    if(Object.values(targets).some(v=>!Number.isFinite(v)||v<0) || (Object.keys(targets).length && Math.abs(Object.values(targets).reduce((a,b)=>a+b,0)-1)>1e-6)) {toast("Targets must add up to 100%.");return;}
    try {await api("/api/portfolio/settings",{method:"PUT",body:{targets:{...(portfolioSettings?.targets||{}),[basis]:targets}}});toast("Targets saved");await loadPortfolio();}catch(e){toast(e.message);}
}
async function clearPortfolioTargets() {
    const basis=document.getElementById("portfolio-allocation-basis").value;
    try{await api("/api/portfolio/settings",{method:"PUT",body:{targets:{...(portfolioSettings?.targets||{}),[basis]:{}}}});await loadPortfolio();}catch(e){toast(e.message);}
}

function renderPortfolioHistory() {
    const rows = portfolioRows(portfolioData?.history), basis = document.getElementById("portfolio-history-basis")?.value || "mandate";
    portfolioDestroyChart("portfolioHistory");
    if (!rows.length || typeof Chart === "undefined") return;
    const names = [...new Set(rows.flatMap(r => portfolioRows(r.allocation?.[basis]).map(x => x.name)))];
    const datasets = names.map((name, i) => ({ label: name, data: rows.map(r => (portfolioRows(r.allocation?.[basis]).find(x => x.name === name)?.weight || 0) * 100), backgroundColor: portfolioAllocationColor(name, i), borderWidth: 0, fill: true, pointRadius: 0 }));
    charts.portfolioHistory = new Chart(document.getElementById("chart-portfolio-history"), { type: "line", data: { labels: rows.map(r => r.date), datasets }, options: { maintainAspectRatio: false, scales: { y: { stacked: true, min: 0, max: 100, ticks: { callback: v => `${v}%` } }, x: { grid: { display: false } } }, plugins: { legend: { position: "bottom" } }, elements: { line: { tension: .2 } } } });
}

function renderPortfolioQuality() {
    const d = portfolioData || {}, c = d.coverage || {}, concentration = d.concentration || {};
    const coverage = [["Country data", c.country], ["Cost data", c.cost], ["Risk data", c.risk]];
    const dates = portfolioRows(d.valuation_dates);
    document.getElementById("portfolio-quality").innerHTML = coverage.map(([label, value]) => `<div class="portfolio-meter"><div><span>${label}</span><b>${portfolioPercent(value)}</b></div><span class="portfolio-meter-track"><i style="width:${Math.max(0, Math.min(100, Number(value || 0) * 100))}%"></i></span></div>`).join("") + `<div class="portfolio-concentration"><span>Largest holding</span><b>${portfolioPercent(concentration.largest)}</b></div><div class="portfolio-concentration"><span>Holding HHI</span><b>${concentration.holding_hhi == null ? "—" : Number(concentration.holding_hhi).toFixed(3)}</b></div><div class="portfolio-concentration"><span>Valuation history</span><b>${dates.length ? `${portfolioEsc(dates[0])} → ${portfolioEsc(dates[dates.length - 1])}` : "None"}</b></div>` + (portfolioRows(concentration.overlap).length ? `<p class="portfolio-note">${concentration.overlap.map(portfolioEsc).join(" ")}</p>` : "") + `<p class="portfolio-note">Unknown exposure remains in the portfolio denominator. Fund mandate and underlying country exposure use different bases.</p>`;
    document.getElementById("portfolio-cost-total").textContent = d.cost_known_value == null ? "Costs incomplete" : `${portfolioMoney(d.cost_known_value)} with known cost`;
}

function renderPortfolioHoldings() {
    const rows = [...portfolioRows(portfolioData?.holdings)].sort((a,b)=>b.value-a.value);
    const body = document.getElementById("portfolio-holdings");
    body.innerHTML = rows.length ? rows.map(h => `<tr><td><strong>${portfolioEsc(h.name)}</strong>${h.estimated ? `<small class="portfolio-tag">Estimated</small>` : ""}${h.source ? `<details class="portfolio-source-details"><summary>source details</summary><a class="portfolio-source" href="${portfolioEsc(h.source)}" target="_blank" rel="noopener">public source</a><small>${portfolioEsc(h.note || "Source metadata available")}${h.composition_date ? ` Composition: ${portfolioEsc(h.composition_date)}.` : ""}</small></details>` : ""}</td><td>${portfolioEsc(h.mandate || "—")}</td><td style="text-align:right">${portfolioMoney(h.value)}</td><td style="text-align:right">${portfolioPercent(h.weight)}</td><td>${h.as_of ? fmtDate(h.as_of) : "—"}</td><td style="text-align:right">${h.annual_cost == null ? "—" : `${(Number(h.annual_cost) * 100).toFixed(2)}%`}</td></tr>`).join("") : `<tr><td colspan="6" class="portfolio-empty">No holdings recorded.</td></tr>`;
    const select = document.getElementById("portfolio-preview-holding");
    const preferred = rows.find(h => /iShares Core MSCI World/i.test(h.name || ""));
    select.innerHTML = rows.filter(h => h.classified).map(h => `<option value="${portfolioEsc(h.key || h.name)}" ${preferred && h === preferred ? "selected" : ""}>${portfolioEsc(h.name)}</option>`).join("");
}

function renderPortfolioAccounts() {
    const rows = portfolioRows(portfolioData?.accounts);
    document.getElementById("portfolio-accounts").innerHTML = rows.length ? rows.map(a => `<div class="portfolio-account-row"><span><strong>${portfolioEsc(a.name)}</strong><small>${a.as_of ? `As of ${fmtDate(a.as_of)}` : "No balance date"}</small></span><span>${portfolioMoney(a.balance)}</span><select class="form-input portfolio-account-mode" data-account-id="${a.id}"><option value="investment" ${a.included ? "selected" : ""}>Included</option><option value="excluded" ${!a.included && !a.emergency ? "selected" : ""}>Excluded</option><option value="emergency" ${a.emergency ? "selected" : ""}>Emergency</option></select></div>`).join("") + `<button class="btn btn-primary btn-sm" onclick="savePortfolioAccountModes()">Save account settings</button>` : `<div class="portfolio-muted">No accounts configured.</div>`;
}

function togglePortfolioAccounts() { const el = document.getElementById("portfolio-accounts"); el.hidden = !el.hidden; }
function togglePortfolioAssumptions() { const el = document.getElementById("portfolio-assumptions"); el.hidden = !el.hidden; }

async function savePortfolioAccountModes() {
    const account_modes = {};
    document.querySelectorAll(".portfolio-account-mode").forEach(select => { account_modes[select.dataset.accountId] = select.value; });
    try { await api("/api/portfolio/settings", { method: "PUT", body: { ...(portfolioSettings || {}), account_modes } }); toast("Account settings saved"); await loadPortfolio(); } catch (e) { toast(e.message); }
}

function renderPortfolioRisk() {
    const risk = portfolioData?.risk || {}, el = document.getElementById("portfolio-risk-state");
    if (!risk.available) { el.textContent = risk.reason || "Risk model unavailable until enough dated history is available."; portfolioDestroyChart("portfolioModel"); return; }
    el.innerHTML = `Proxy backtest volatility <b>${portfolioPercent(risk.volatility)}</b> · max drawdown <b>${portfolioPercent(risk.max_drawdown)}</b> · Sharpe <b>${risk.sharpe == null ? "—" : Number(risk.sharpe).toFixed(2)}</b> · ${risk.n || 0} observations (${portfolioEsc(risk.start || "?")} → ${portfolioEsc(risk.end || "?")}).<p class="portfolio-note">Public market data refreshed ${portfolioEsc(portfolioData?.market?.refreshed_at?.slice(0,10) || "never")}.</p>`;
    renderPortfolioModel(portfolioData.model || risk);
}

async function loadPortfolioModel() {
    const token = portfolioRequest;
    try {
        const model = await api("/api/portfolio/model");
        if (token !== portfolioRequest || !model) return;
        portfolioData.model = model;
        renderPortfolioModel(model);
        if (model.available === false) document.getElementById("portfolio-risk-state").textContent = model.reason || "Risk model unavailable.";
    } catch (e) { if (token === portfolioRequest) document.getElementById("portfolio-risk-state").textContent = e.message; }
}

function renderPortfolioModel(model) {
    const points = portfolioRows(model?.points);
    portfolioDestroyChart("portfolioModel");
    const container = document.getElementById("portfolio-model-comparison");
    container.innerHTML = "";
    if (!model?.available || !points.length || typeof Chart === "undefined") { renderPortfolioMethods(model); return; }
    const point = (items, color, label) => ({label, data:items.filter(Boolean).map(p=>({x:p.volatility*100,y:p.return*100})), backgroundColor:color, borderColor:color, pointRadius:5});
    const datasets = [ {...point(points,chartTheme().accent,"Efficient frontier"),showLine:true,pointRadius:2},
        point([model.current],chartTheme().red,"Current covered investments"), point([model.benchmark],chartTheme().green,"World benchmark"),
        point([model.hypothetical_80_20],chartTheme().orange,"Hypothetical 80/20") ].filter(d=>d.data.length);
    charts.portfolioModel = new Chart(document.getElementById("chart-portfolio-model"),{type:"scatter",data:{datasets},options:{maintainAspectRatio:false,scales:{x:{title:{display:true,text:"Annual volatility %"}},y:{title:{display:true,text:"Assumed arithmetic return %"}}},plugins:{tooltip:{callbacks:{label:c=>`${c.dataset.label}: ${c.parsed.x.toFixed(2)}% risk / ${c.parsed.y.toFixed(2)}% assumed return`}}}}});
    container.innerHTML = `<p class="portfolio-note">Current point covers ${portfolioPercent(model.coverage)} of investments, normalised to 100%. Bonds are a hypothetical alternative. Assumptions exclude costs and taxes.</p>` + (model.same_return?.available ? `<p>At the same assumed return (${portfolioPercent(model.same_return.return)}), estimated volatility changes by <b>${(model.same_return.risk_reduction*100).toFixed(2)} percentage points</b>.</p>` : `<p class="portfolio-note">${portfolioEsc(model.same_return?.reason || "Same-return comparison unavailable.")}</p>`) + `<p class="portfolio-note">Approximate compound growth at current model weights: ${portfolioPercent(model.current.compound_approx)}. This is an assumption-based approximation, not a forecast.</p>`;
    renderPortfolioMethods(model);
}

function renderPortfolioMethods(model) {
    const risk = portfolioData?.risk || {}, market = portfolioData?.market || {}, sensitivity = model?.sensitivity || {};
    const cases = Object.entries(sensitivity).map(([name,p]) => {
        if (!p) return `<li>${portfolioEsc(name.replaceAll("_"," "))}: unavailable.</li>`;
        if (name === "last36_months") return `<li>Last 36 months: current volatility ${portfolioPercent(p.current_volatility)}, same-return frontier ${portfolioPercent(p.frontier_volatility)}.</li>`;
        return `<li>${portfolioEsc(name.replaceAll("_"," "))}: same-return frontier volatility ${portfolioPercent(p.volatility)} under the changed assumptions.</li>`;
    }).join("");
    const snapshots = portfolioRows(portfolioData?.history).map(h=>`<tr><td>${portfolioEsc(h.date)}</td><td>${h.risk?.available?portfolioPercent(h.risk.volatility):"Unavailable"}</td><td>${portfolioPercent(h.risk?.coverage)}</td></tr>`).join("");
    const weights = model?.same_return?.alternative || {};
    document.getElementById("portfolio-methods").innerHTML = `<p>Historical risk is a hypothetical monthly rebalanced backtest of the covered proxy mix, not your realised performance. Sample ${portfolioEsc(risk.start||"unavailable")} to ${portfolioEsc(risk.end||"unavailable")}, ${risk.n||0} common monthly returns. Monthly sample covariance × 12 captures correlations. Monthly drawdown can miss losses within a month.</p><p>Prices are adjusted for distributions and splits as supplied by Yahoo. USD prices are divided by matching EUR/USD rates. Cached public data refreshed ${portfolioEsc(market.refreshed_at||"never")}. No private balances or account identifiers leave Balance.</p><ul>${portfolioRows(market.proxies).map(p=>`<li>${portfolioEsc(p.group)} → ${portfolioEsc(p.symbol)} (${portfolioEsc(p.currency)}): Yahoo adjusted close.</li>`).join("")}</ul><p>Nordic country ETFs differ from Nordnet indices; broad EM includes China and differs from ex-China. World approximates Nordea's active global fund. Proxy coverage does not establish exact holdings performance.</p><p>The model uses explicit annual arithmetic assumptions, long-only weights summing to 100%, no borrowing and the edited caps. Approximate compound growth subtracts half the variance from arithmetic return. Optimiser weights are diagnostics and may change with the assumptions.</p><ul>${cases}</ul><p class="portfolio-model-weights">Same-return model weights: ${Object.entries(weights).map(([k,v])=>`${portfolioEsc(k)} ${portfolioPercent(v)}`).join(" · ") || "unavailable"}</p><p>Snapshot mix risk uses the same ${risk.n||0}-month sample for each recorded allocation. It estimates composition changes retrospectively, not what was known on each date.</p><table class="portfolio-drift-table"><thead><tr><th>Snapshot</th><th>Proxy volatility</th><th>Coverage</th></tr></thead><tbody>${snapshots}</tbody></table><p>Historical look-through uses current cached fund composition. Other undisclosed countries, fund cash and unclassified investments remain unknown. Fund costs exclude platform fees, trading costs, taxes and personal discounts. HHI and effective holdings describe instrument concentration, not independent underlying companies.</p>` + (market.warnings?.length?`<p>${market.warnings.map(portfolioEsc).join("<br>")}</p>`:"");
}

function renderPortfolioAssumptions() {
    const m = portfolioSettings?.model || {}, returns = m.returns || {}, max = m.max_weights || {};
    document.getElementById("portfolio-assumptions").innerHTML = `<div class="portfolio-assumption-grid"><label>Risk-free %<input class="form-input" data-model="risk_free" value="${Number((Number(m.risk_free || 0) * 100).toFixed(3))}" type="number" step=".1"></label><label>History months<select class="form-input" data-model="months"><option ${m.months === 36 ? "selected" : ""}>36</option><option ${m.months === 60 || !m.months ? "selected" : ""}>60</option></select></label><label class="portfolio-check"><input data-model="include_bonds" type="checkbox" ${m.include_bonds ? "checked" : ""}> Include bonds</label>${Object.entries(returns).map(([k, v]) => `<label>${portfolioEsc(k)} expected return %<input class="form-input portfolio-return" data-key="${portfolioEsc(k)}" value="${Number((Number(v) * 100).toFixed(3))}" type="number" step=".1"></label><label>${portfolioEsc(k)} maximum %<input class="form-input portfolio-cap" data-key="${portfolioEsc(k)}" value="${Number((Number((m.max_weights || {})[k] ?? 1) * 100).toFixed(3))}" type="number" min="0" max="100" step=".1"></label>`).join("")}</div><button class="btn btn-primary btn-sm" onclick="savePortfolioSettings()">Save assumptions</button>`;
}

function renderPortfolioPerformance() {
    const p = portfolioData?.performance || {}, el = document.getElementById("portfolio-performance");
    if (!p.available) { el.innerHTML = `<div class="portfolio-muted">${portfolioEsc(p.reason || "Performance is unavailable.")}</div>`; return; }
    el.innerHTML = `<div class="portfolio-concentration"><span>Annual money-weighted return</span><b>${portfolioPercent(p.mwr)}</b></div><div class="portfolio-concentration"><span>Net contributions</span><b>${portfolioMoney(p.net_flows)}</b></div><div class="portfolio-concentration"><span>Investment gains</span><b>${portfolioMoney(p.gains)}</b></div><div class="portfolio-concentration"><span>Period</span><b>${portfolioEsc(p.start)} → ${portfolioEsc(p.end)}</b></div>`;
}

function renderPortfolioFlows() {
    const el = document.getElementById("portfolio-flows"), flows = portfolioRows(portfolioData?.flows), accounts = portfolioRows(portfolioData?.accounts);
    const s = portfolioSettings || {};
    el.innerHTML = `<div class="portfolio-flow-form"><select class="form-input" id="portfolio-flow-account">${accounts.filter(a => a.included !== false && !a.emergency).map(a => `<option value="${a.id}">${portfolioEsc(a.name)}</option>`).join("")}</select><input class="form-input" id="portfolio-flow-date" type="date"><input class="form-input" id="portfolio-flow-amount" type="number" step=".01" placeholder="+ contribution / − withdrawal"><button class="btn btn-primary btn-sm" onclick="addPortfolioFlow()">Add flow</button></div><div class="portfolio-flow-complete"><span>Use this only after checking the ledger is complete for the whole period.</span><input class="form-input" id="portfolio-flow-from" type="date" value="${portfolioEsc(s.flow_complete_from || "")}"><span>to</span><input class="form-input" id="portfolio-flow-to" type="date" value="${portfolioEsc(s.flow_complete_to || "")}"><label class="portfolio-confirm"><input id="portfolio-flow-confirm" type="checkbox"> I confirm all contributions and withdrawals in this period are recorded</label><button class="btn btn-secondary btn-sm" onclick="savePortfolioFlowPeriod()">Save period</button></div>` + (flows.length ? flows.map(f => `<div class="portfolio-flow-row"><span>${portfolioEsc(f.date)} · ${portfolioEsc(accounts.find(a => String(a.id) === String(f.account_id))?.name || "Account")}</span><b>${portfolioMoney(f.amount)}</b><button class="btn btn-secondary btn-sm" onclick="deletePortfolioFlow(${f.id})">Delete</button></div>`).join("") : `<div class="portfolio-muted">No flows recorded.</div>`);
}
function togglePortfolioFlows() { const el = document.getElementById("portfolio-flows"); el.hidden = !el.hidden; }
async function addPortfolioFlow() { try { await api("/api/portfolio/flows", { method: "POST", body: { account_id: Number(document.getElementById("portfolio-flow-account").value), date: document.getElementById("portfolio-flow-date").value, amount: Number(document.getElementById("portfolio-flow-amount").value) } }); toast("Flow added"); await loadPortfolio(); } catch (e) { toast(e.message); } }
async function deletePortfolioFlow(id) { try { await api(`/api/portfolio/flows/${id}`, { method: "DELETE" }); toast("Flow deleted"); await loadPortfolio(); } catch (e) { toast(e.message); } }
async function savePortfolioFlowPeriod() { if (!document.getElementById("portfolio-flow-confirm")?.checked) { toast("Confirm that the ledger is complete first."); return; } try { await api("/api/portfolio/settings", { method: "PUT", body: { flow_complete_from: document.getElementById("portfolio-flow-from").value || null, flow_complete_to: document.getElementById("portfolio-flow-to").value || null } }); toast("Performance period saved"); await loadPortfolio(); } catch (e) { toast(e.message); } }

async function savePortfolioSettings() {
    const model = { ...(portfolioSettings?.model || {}), risk_free: Number(document.querySelector('[data-model="risk_free"]')?.value || 0) / 100, months: Number(document.querySelector('[data-model="months"]')?.value || 60), include_bonds: !!document.querySelector('[data-model="include_bonds"]')?.checked, returns: {}, max_weights: {} };
    document.querySelectorAll(".portfolio-return").forEach(i => { model.returns[i.dataset.key] = Number(i.value) / 100; });
    document.querySelectorAll(".portfolio-cap").forEach(i => { model.max_weights[i.dataset.key] = Number(i.value) / 100; });
    try { await api("/api/portfolio/settings", { method: "PUT", body: { model } }); toast("Portfolio assumptions saved"); await loadPortfolio(); } catch(e) { toast(e.message); }
}

function portfolioDriftTable(rows) {
    return `<table class="portfolio-drift-table"><thead><tr><th>Allocation</th><th>Current</th><th>After</th><th>Target</th><th>Gap</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${portfolioEsc(r.name)}</td><td>${portfolioPercent(r.current)}</td><td>${portfolioPercent(r.after)}</td><td>${portfolioPercent(r.target)}</td><td>${r.gap==null?"—":Math.abs(r.gap)<.001?"On target":`${(Math.abs(r.gap)*100).toFixed(1)} pp ${r.gap>0?"overweight":"underweight"}`}</td></tr>`).join("")}</tbody></table>`;
}
function renderPortfolioCurrentDrift() {
    const basis = document.getElementById("portfolio-allocation-basis").value;
    const rows = portfolioRows(portfolioData?.drift?.[basis]);
    document.getElementById("portfolio-current-drift").innerHTML = portfolioSettings?.targets?.[basis] && Object.keys(portfolioSettings.targets[basis]).length ? portfolioDriftTable(rows) : `<p class="portfolio-note">No targets set for this basis. Set your own goals below.</p>`;
}
function changePortfolioBasis() {
    renderPortfolioAllocation(); renderPortfolioTargets(); renderPortfolioCurrentDrift();
    if (portfolioLastPreview) renderPortfolioPreview(portfolioLastPreview);
}
function renderPortfolioPreview(data) {
    const basis = document.getElementById("portfolio-allocation-basis").value;
    document.getElementById("portfolio-preview-result").innerHTML = `<strong>${portfolioMoney(data.after.total)}</strong> after ${data.steps} contribution(s) of ${portfolioMoney(data.amount)}.${portfolioDriftTable(portfolioRows(data.drift?.[basis]))}<p class="portfolio-note">${portfolioEsc(data.assumptions)}</p>`;
}
async function previewPortfolioContribution() {
    const token = ++portfolioPreviewRequest, revision = portfolioRequest;
    const result = document.getElementById("portfolio-preview-result");
    result.textContent = "Calculating preview…";
    try {
        const data = await api("/api/portfolio/preview",{method:"POST",body:{amount:Number(document.getElementById("portfolio-preview-amount").value),key:document.getElementById("portfolio-preview-holding").value,steps:Number(document.getElementById("portfolio-preview-steps").value)}});
        if (token!==portfolioPreviewRequest || revision!==portfolioRequest) return;
        portfolioLastPreview=data; renderPortfolioPreview(data);
    } catch(e) { if (token===portfolioPreviewRequest) result.textContent=e.message; }
}

async function refreshPortfolioMarket() {
    try { const result = await api("/api/portfolio/refresh", { method: "POST" }); if (result?.status === "error") { toast(result.warning || "Market refresh failed"); } else { toast(result?.warnings?.length ? "Market data refreshed with warnings" : "Market data refreshed"); } await loadPortfolio(); } catch (e) { toast(e.message); }
}
