// ── Balance AI ───────────────────────────────────────────────────────
// The panel around the assistant. The loop, the tools and the model all live
// on the server; this is what you talk to them through.
//
// Two things here are not decoration:
//
//   1. Every answer shows which of the app's own screens it read, and over
//      which months. The whole claim of this feature is that its figures are
//      the Dashboard's figures, and a number with nothing behind it is one you
//      have to take on trust.
//   2. Errors land in the transcript, not in a toast. api() throws, and the
//      global handler would float the message over a panel still showing the
//      question it failed to answer.
let chatMessages = [];      // the conversation, as the API wants it
let chatBusy = false;
let chatReady = null;       // null = not probed yet; else the status payload
// The answer as it arrives: what it is doing now, and what it has written so
// far. Null when nothing is in flight.
let chatPending = null;

// The six tools, said in English. The point is not the function name — it is
// which screen of the app the number came off.
const CHAT_TOOL_NAMES = {
    search_transactions: "Read your transactions",
    category_breakdown: "Read the category breakdown",
    monthly_summary: "Read the monthly summary",
    list_subscriptions: "Read your subscriptions",
    annual_report: "Read the annual report",
    net_worth_summary: "Read your net worth",
    analyse_month: "Read the whole month",
};

// The same six, said as something happening rather than something done.
const CHAT_TOOL_DOING = {
    search_transactions: "Reading your transactions",
    category_breakdown: "Reading the category breakdown",
    monthly_summary: "Reading the monthly summary",
    list_subscriptions: "Reading your subscriptions",
    annual_report: "Reading the annual report",
    net_worth_summary: "Reading your net worth",
    analyse_month: "Going through the whole month",
};

// All about the month just gone, because that is the one a person can still do
// something about. The analysis is first: it is the question you would not
// think to ask, and the only one worth waiting twenty seconds for.
const CHAT_SUGGESTIONS = [
    "Make a trend analysis of my latest month",
    "What was my largest spending category last month, and the top 3 items in it?",
    "How did last month compare to my usual?",
    "What are my subscriptions costing me?",
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
    // Which model is answering is a setting, not a brand. It stays on the
    // set-up card, where it is the thing you have to type, and nowhere else.
    renderChatLog();
}

function resetChat() {
    chatMessages = [];
    renderChatLog();
    document.getElementById("chat-input").focus();
}

// ── Rendering ────────────────────────────────────────────────────────

function renderChatLog() {
    const log = document.getElementById("chat-log");

    if (!chatMessages.length && !chatPending) {
        log.innerHTML = chatReady && !chatReady.configured
            ? chatSetupHtml() : chatEmptyHtml();
        updateChatLengthNote();
        return;
    }

    log.innerHTML = chatMessages.map(chatMessageHtml).join("")
        + (chatPending ? chatPendingHtml() : "");
    scrollChatToEnd();
    updateChatLengthNote();
}

function chatPendingHtml() {
    // The lookups it has already done, then what it is doing now, then what it
    // has written. The lookups stay up because most of the wait is the model
    // reading the result back — a second or two of "Reading the category
    // breakdown · Jul 2026" is the answer's provenance arriving before the
    // answer, which is the part worth waiting for.
    const done = chatPending.toolCalls.map(c => {
        const name = CHAT_TOOL_NAMES[c.tool] || c.tool;
        const when = c.period ? ` · ${escapeHtml(chatPeriodLabel(c.period))}` : "";
        return `<div class="chat-step${c.ok === false ? " chat-source-failed" : ""}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6"><path d="M20 6L9 17l-5-5"/></svg>
            ${escapeHtml(name)}${when}
        </div>`;
    }).join("");

    const status = `<div class="chat-thinking">
        <span class="chat-dots"><span></span><span></span><span></span></span>
        ${escapeHtml(chatPending.status)}
    </div>`;
    const text = chatPending.text
        ? `<div class="chat-msg chat-msg-assistant">
               <div class="chat-bubble" id="chat-live">${chatFormat(chatPending.text)}</div>
           </div>`
        : "";
    // Once words are coming they are the news, so the status drops below them.
    return done + (chatPending.text ? text + status : status);
}

function scrollChatToEnd() {
    const log = document.getElementById("chat-log");
    log.scrollTop = log.scrollHeight;
}

function chatEmptyHtml() {
    return `<div class="chat-empty">
        <div class="chat-empty-title">Ask about your money</div>
        <div class="chat-empty-sub">Runs on this Mac. Nothing leaves the machine,
            and every answer says which screen it read.</div>
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
    // The server always reports the model it is configured for, reachable or
    // not, so this never has to guess a name — and a name is the whole use of
    // this card. It is the only place in the panel a model is mentioned: which
    // one answers is a setting, not the brand.
    const model = s.model;
    let what, how;
    if (s.reachable === false) {
        what = "Ollama isn't running on this Mac.";
        how = "ollama serve";
    } else if (s.model_installed === false && model) {
        what = `Ollama is running, but <strong>${escapeHtml(model)}</strong> isn't installed.`;
        how = `ollama pull ${model}`;
        if (s.installed_models && s.installed_models.length) {
            what += ` You have: ${s.installed_models.map(escapeHtml).join(", ")}.`;
        }
    } else {
        what = escapeHtml(s.detail || "Balance AI isn't set up yet.");
        how = model ? `ollama pull ${model}` : "ollama serve";
    }
    return `<div class="chat-setup">
        <h4>Not ready yet</h4>
        <p>${what}</p>
        <code>${escapeHtml(how)}</code>
        <p>Balance AI answers from your own database using a model on this
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
    chatPending = { status: "Thinking…", text: "", toolCalls: [] };
    renderChatLog();

    try {
        // Only role and content go up; the tool trace is ours to display and
        // the server rejects anything else in a message.
        const payload = chatMessages.map(m => ({ role: m.role, content: m.content }));
        const result = await streamChat(payload);
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
        chatPending = null;
        setChatBusy(false);
        renderChatLog();
        input.focus();
    }
    return false;
}

// Reads /api/chat/stream and moves the panel along as the events land.
// Resolves with the same payload the plain endpoint returns, because the last
// event carries it: the final state is rendered from one authoritative object
// rather than from whatever the stream was caught mid-way through.
async function streamChat(messages) {
    const res = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages }),
    });
    // A refused conversation is still an ordinary JSON error with a status.
    if (!res.ok) throw await apiError(res);

    // Every browser this ships in reads a response as a stream, but the app
    // runs inside whatever WebKit the Mac happens to have. Without it the whole
    // answer still arrives — just all at once, as it did before.
    if (!res.body || typeof res.body.getReader !== "function") {
        const events = (await res.text()).split("\n\n")
            .map(part => part.split("\n").find(l => l.startsWith("data: ")))
            .filter(Boolean)
            .map(line => { try { return JSON.parse(line.slice(6)); } catch (e) { return null; } })
            .filter(Boolean);
        const last = events[events.length - 1];
        if (!last || last.type === "error") {
            throw new ApiError((last && last.error) || "The answer stopped halfway.",
                               503, last && last.code);
        }
        return last;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let done = null;

    for (;;) {
        const { value, done: finished } = await reader.read();
        if (finished) break;
        buffer += decoder.decode(value, { stream: true });

        // Events are separated by a blank line; the last piece may be partial.
        const parts = buffer.split("\n\n");
        buffer = parts.pop();
        for (const part of parts) {
            const line = part.split("\n").find(l => l.startsWith("data: "));
            if (!line) continue;
            let event;
            try { event = JSON.parse(line.slice(6)); } catch (e) { continue; }
            if (event.type === "done" || event.type === "error") done = event;
            else applyChatEvent(event);
        }
    }

    if (!done) throw new ApiError("The answer stopped halfway.", 0, "truncated");
    if (done.type === "error") {
        throw new ApiError(done.error || "The assistant could not be reached",
                           done.code === "backend_unavailable" ? 503 : 502, done.code);
    }
    return done;
}

function applyChatEvent(event) {
    if (!chatPending) return;

    if (event.type === "tool") {
        // Anything written before a lookup was a preamble, not the answer.
        chatPending.text = "";
        chatPending.status = (CHAT_TOOL_DOING[event.tool] || "Looking that up") + "…";
        renderChatLog();
    } else if (event.type === "looked_up") {
        chatPending.toolCalls.push(event);
        chatPending.status = "Writing the answer…";
        renderChatLog();
    } else if (event.type === "token") {
        const first = !chatPending.text;
        chatPending.text += event.text;
        const live = document.getElementById("chat-live");
        if (live && !first) {
            // Patch the one bubble rather than rebuilding the transcript on
            // every token — that would drop any text the user had selected.
            live.innerHTML = chatFormat(chatPending.text);
            scrollChatToEnd();
        } else {
            renderChatLog();
        }
    }
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

