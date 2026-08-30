"""The agent loop behind the chat assistant.

Deliberately a hand-written loop rather than the SDK's tool runner. The plan
for this feature is to point the same tool layer at a local model once it is
proven (see ``docs/LOCAL_AI_RESEARCH.md``), and the loop is the only part that
has to change when that happens. Forty lines we own beats a helper we would
have to unpick, and it keeps the tool schemas in ``ai_tools`` provider-neutral.

What lives here: the system prompt, the request, and the tool-call cycle.
What does not: any knowledge of the database, or of what a category is.
"""

import json

import config
from ai_tools import TOOL_SCHEMAS, context_block, run_tool

# How many times the model may call tools before we stop and answer with what
# we have. Every question this assistant can be asked is answerable in one or
# two calls; more than that means it is going in circles, and a loop with no
# ceiling is a loop that can bill forever.
MAX_TOOL_ROUNDS = 5

SYSTEM_PROMPT = """You are the assistant inside Balance., a personal expense \
tracker that runs entirely on the user's own Mac. You answer questions about \
their spending, income, subscriptions and net worth.

You have tools that read the app's own figures. Use them. The rules below are \
not style preferences — the app exists to tell this person the truth about \
their money, and a number you invented is worse than no answer at all.

**Never state a figure that did not come from a tool result.** No totals you \
worked out yourself, no percentages you estimated, no "roughly". If you need a \
number you do not have, call a tool for it. If a tool cannot give it, say so.

**Quote the preformatted strings.** Every amount comes back twice: a raw number \
and an `_eur` string like "612 €". Put the `_eur` string in your answer. Use the \
raw numbers only to decide what to say — which category is largest, whether \
something rose or fell.

**Never work out dates.** You have no reliable sense of what "last month" is. \
Pass the `period` name and let the tool resolve it. Only use an explicit `month` \
when the user named a specific one.

**Say when something is unusual.** A single month's category breakdown carries \
`usual_month` — what that category normally costs. A figure next to its usual \
is worth far more than the figure alone, so mention it when it is interesting, \
and skip it for anything marked `is_fixed_cost` (rent does not have news in it).

Be brief. Two or three sentences for most questions. This is a side panel in a \
desktop app, not a report. Amounts are euros. You are read-only: you cannot add, \
edit or delete anything, so if asked, say that and point at the relevant screen."""


def _client():
    """The Anthropic client, imported lazily.

    The package is optional: a build without it should still run the rest of the
    app with the chat feature hidden, exactly as the bank card hides itself when
    Enable Banking is not configured.
    """
    import anthropic

    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)


def _text_of(content):
    """The plain text in a response, ignoring thinking and tool_use blocks."""
    return "".join(b.text for b in content if b.type == "text").strip()


def chat(messages):
    """Answer the conversation in ``messages``; return the reply and a trace.

    ``messages`` is the Anthropic message list — ``[{"role", "content"}, ...]``
    with the user's turn last. Returns ``{"reply", "tool_calls", "usage"}``.
    ``tool_calls`` is what the assistant actually looked at, which the UI can
    show so an answer is never just an assertion.
    """
    client = _client()
    # The context goes in the system prompt rather than a tool, because a model
    # that has to ask what today is will sometimes forget to.
    system = [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "Context for this conversation:\n"
                                 + json.dumps(context_block(), indent=2)},
    ]

    history = list(messages)
    trace = []
    usage = {"input_tokens": 0, "output_tokens": 0}

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(
            model=config.AI_MODEL,
            # Chat answers are two or three sentences. The ceiling is here to
            # stop a runaway, not to leave room for an essay.
            max_tokens=4096,
            system=system,
            messages=history,
            tools=[dict(t) for t in TOOL_SCHEMAS],
            thinking={"type": "adaptive"},
            # A chat panel is latency-sensitive and this is mostly a routing
            # task — the thinking that matters is "which tool", not a proof.
            # Raise AI_EFFORT if the answers start feeling shallow.
            output_config={"effort": config.AI_EFFORT},
        )

        usage["input_tokens"] += response.usage.input_tokens
        usage["output_tokens"] += response.usage.output_tokens

        # A safety decline stops the turn with no content worth reading.
        if response.stop_reason == "refusal":
            detail = getattr(response.stop_details, "explanation", None)
            return {"reply": detail or "I can't answer that one.",
                    "tool_calls": trace, "usage": usage}

        history.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return {"reply": _text_of(response.content), "tool_calls": trace,
                    "usage": usage}

        # Every tool_use block in the turn gets a tool_result, in ONE user
        # message. Splitting them across messages teaches the model to stop
        # calling tools in parallel.
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            output = run_tool(block.name, block.input)
            trace.append({"tool": block.name, "arguments": block.input,
                          "ok": "error" not in output})
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(output, default=str),
                "is_error": "error" in output,
            })
        history.append({"role": "user", "content": results})

    # Out of rounds. Ask for the answer with the tools withdrawn, so the turn
    # ends with a sentence rather than a seventh identical lookup.
    final = client.messages.create(
        model=config.AI_MODEL,
        max_tokens=4096,
        system=system,
        messages=history + [{
            "role": "user",
            "content": "Answer now using only what the tools already returned. "
                       "If that is not enough, say what you could not find out.",
        }],
        thinking={"type": "adaptive"},
        output_config={"effort": config.AI_EFFORT},
    )
    usage["input_tokens"] += final.usage.input_tokens
    usage["output_tokens"] += final.usage.output_tokens
    return {"reply": _text_of(final.content), "tool_calls": trace, "usage": usage}
