// Balance — net-worth.js
//
// Accounts, balances, holdings and the investment import.
//
// One of the ordered classic scripts index.html loads. They share one global
// scope on purpose: index.html wires ~95 inline handlers straight to these
// names, so these are plain scripts and not modules.

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

