# Local AI chat in Balance. — feasibility research

*Researched 2026-08-30. Nothing here is implemented; this is the decision
record for whether a chat assistant can run on-device and which model it
should use.*

## Verdict

Yes, it is possible, and this app is unusually well suited to it — but the
model choice is the *third* most important decision, behind the architecture
and the hardware floor. A 4–9B model on a Mac is entirely capable of the job
described below. The same model is entirely incapable of the job most people
build first.

## The architecture decides whether a small model works

The obvious build is text-to-SQL: hand the model the schema, let it query
`expenses.db`. Don't. That is the one shape where small models fall off a
cliff — an 8B writes plausible SQL against a ten-table schema and gets the
join or the sign wrong, and the app reports a number that is simply false.

Balance. already has the alternative sitting in `routes/`. Every question a
person would ask this app is already an endpoint that returns a *computed*
answer:

| Question | Endpoint that already answers it |
|---|---|
| "What did I spend on groceries in June?" | `/api/dashboard/category-breakdown?month=` |
| "Is that a lot?" | the same call — it carries `median` and `fixed` |
| "What are my subscriptions?" | `/api/recurring` |
| "Show me everything over 200 € since May" | `/api/transactions?amount_min=&date_from=` |
| "How is this year against last?" | `/api/reports/annual` |
| "How much am I worth?" | `/api/networth/summary` |

So the model's job is not arithmetic and not SQL. It is: pick one of about six
functions, fill two or three parameters, and phrase the result in a sentence.
That is a task small models do at near-parity with frontier models, because
it is mostly classification.

**The rule that makes it safe: every number in the answer comes from a tool
result, never from the model.** Amounts, dates, category totals, counts. The
model gets to choose the tool and write the prose around the figure. In an
app whose whole purpose is telling you the truth about your money, a
hallucinated euro is not a rough edge — it is the end of the feature.

### Proposed tool surface

Six read-only tools, each a thin wrapper over an existing route, reusing
`_filter_clauses()` and `current_user_id()` unchanged:

1. `search_transactions(month?, months?, category?, type?, q?, date_from?, date_to?, amount_min?, amount_max?, limit?)`
2. `category_breakdown(month | months, type)` — returns the baseline columns too
3. `monthly_summary(months)`
4. `list_subscriptions()`
5. `annual_report(year)`
6. `net_worth_summary()`

Read-only on purpose for v1. Letting a 4B model write to `transactions` is a
different risk conversation, and the app has no undo for a hand-edited row.

## Hardware is the real constraint

The app is a pywebview shell with a Flask server already resident, so the
model does not get the whole machine.

| Mac RAM | What fits (4-bit) | Verdict |
|---|---|---|
| 8 GB | ~4B, ≈2.5 GB weights | Tight. Workable, will swap under pressure. |
| 16 GB | 7–9B, ≈5–6 GB weights | The comfortable target. |
| 24 GB+ | 27–32B | Possible, unnecessary for a six-tool router. |

Below 16 GB the honest answer is "4B, and keep the tool surface small".

## Model recommendation

Ranked for *this* job — tool selection and short factual phrasing, not
open-ended reasoning:

1. **Qwen3.5-9B** (or Qwen3 8B where 3.5 isn't packaged yet) — 4-bit, ~6 GB,
   Apache-2.0, native tool calling, long context. The Qwen3 line has been the
   most consistently reliable small-model tool caller through 2026. First
   choice on a 16 GB machine.
2. **Qwen3.5-4B** — same family, ~2.5 GB. The 8 GB fallback, and fast enough
   that first-token latency stops being noticeable.
3. **Gemma 4 E4B / 26B-A4B** — the MoE shape keeps active parameters low, so
   the 26B runs faster than its size suggests on Apple Silicon. Worth
   benchmarking as the alternative if Qwen's phrasing feels stiff.

Avoid the Llama 3.x line here — it still tool-calls fine but it is the oldest
option on the list with nothing to recommend it over Qwen.

**Verify before committing.** The 2026 small-model landscape moves monthly and
the secondary sources covering it contradict each other on what has shipped.
Check `ollama show <model>` for a native `tools` capability rather than
trusting a blog table — including this one.

## Packaging: three options, one recommendation

**A. Require Ollama, detect it, hide the feature when absent.** ← recommended

The app talks to `http://localhost:11434`. No build changes, no growth in
`Balance.dmg`, no notarization problem, and the user can swap models without a
new release. It also has an exact precedent in this codebase:
`enable_banking_configured()` already hides the bank card when its
environment isn't there. An `ollama_configured()` beside it would read as
native.

The cost is a prerequisite install — acceptable for an app with one user.

**B. Bundle llama.cpp + a GGUF via PyInstaller.** Technically the cleanest
distribution (llama.cpp is the runtime designed to be statically linked) and
the only route to a true one-click install. But `Balance.dmg` goes from tens
of megabytes to ~3 GB, `Balance.spec` grows a universal2 binary dependency,
and every release re-uploads the weights. Not worth it for one user.

**C. Apple's Foundation Models framework.** The ~3B on-device model is already
on the Mac: nothing to download, nothing to bundle, no RAM budget of your own.
Two problems. It is a Swift API, so a Python app needs a helper binary or a
PyObjC bridge; and 3B is thin for multi-tool orchestration even with the
narrow surface above. Genuinely attractive later — worth revisiting once the
tool layer exists and can be pointed at a second backend.

## Local vs. the cloud API — the honest comparison

Cost is not the argument. A chat turn here is roughly 2 000 input tokens
(system prompt + tool schemas + one tool result) and ~200 out. On Claude Haiku
4.5 that is about ⅓ of a cent — call it €2/month at twenty turns a day, less
with prompt caching. That is not a number that decides anything.

The arguments that do decide it:

- **Privacy.** This is the app's stated design — one user, one file, no
  server, no network database. Posting a full transaction history to an API
  is the first thing the app has ever done that contradicts that. Local
  chat is the only version consistent with what Balance. already is.
- **Offline.** Works on a plane, works when the API is down.
- **No key management.** Nothing to store, rotate, or leak.

Against, honestly: a 9B will be noticeably worse at vague, multi-step
questions ("why was spring more expensive than usual?"), and the quality gap
is real. If it disappoints, the tool layer is the same either way — the
backend swaps and nothing else changes. Build the tools first, and the
local-vs-cloud choice stays reversible.

## Known risks

- **Date reasoning is where small models actually fail**, not tool choice.
  "Last month", "since summer", "compared to this time last year" all go
  wrong. Mitigate by resolving periods in Python: put today's date and the
  available month list in the system prompt, and expose an explicit
  `resolve_period` helper rather than letting the model do calendar
  arithmetic.
- **Finnish merchant strings.** Small models are weaker in Finnish. Keep the
  model out of interpreting `store` text — that job already belongs to
  `merchant_rules.py`, which does it better and deterministically.
- **Cold-start latency.** First token after a model load is seconds. Use
  Ollama's keep-alive and warm the model when the chat panel opens.
- **The frontend has no streaming path.** `api()` in `app.js` throws
  `ApiError` on any non-2xx and expects a JSON body. Chat needs SSE and a
  token-by-token render — a genuinely new path beside `api()`, not an
  extension of it. The import, bank and investment paths already use raw
  `fetch` for their own reasons; this would be a fourth.
- **Rate limiting and CSRF.** A chat endpoint is a POST and inherits the
  `core.py` guards. Long-lived SSE responses and Flask-Limiter need checking
  together.

## Suggested first step

Build the six tools and the agent loop against the cloud API first, where the
model is not the variable. Once the tool layer is right, point it at Ollama
and measure the drop. That order answers "is a local model good enough for
Balance.?" with evidence instead of a guess, and throws nothing away either
way.
