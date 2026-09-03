#!/usr/bin/env python3
"""Ask the assistant a question from the terminal.

There is no chat panel yet, and this is how the model gets tried before one is
built. It runs the real loop against the real database, so what you see here is
what the panel would say.

    python3 scripts/ask.py "what did I spend on groceries last month?"
    python3 scripts/ask.py --check
    python3 scripts/ask.py --backend anthropic "how was last year?"

Every question prints the tools it called underneath the answer. That is the
thing to watch: a wrong answer with the right tool is a prompt problem, and a
wrong answer with the wrong tool is a model problem. They have different fixes.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(description="Ask Balance. a question.")
    parser.add_argument("question", nargs="*", help="What to ask.")
    parser.add_argument("--check", action="store_true",
                        help="Report whether a model is reachable, then exit.")
    parser.add_argument("--backend", help="Override AI_BACKEND (local | anthropic).")
    parser.add_argument("--model", help="Override the model name.")
    args = parser.parse_args()

    # Overrides go in the environment before config is imported: config reads
    # its settings once, on first import, exactly like the test suite relies on.
    if args.backend:
        os.environ["AI_BACKEND"] = args.backend
    if args.model:
        os.environ["OLLAMA_MODEL" if os.environ.get("AI_BACKEND", "local") == "local"
                   else "AI_MODEL"] = args.model

    from ai import chat as ai_chat
    from ai.backends import BackendUnavailable

    state = ai_chat.status()
    print(f"backend: {state['backend']}  model: {state['model']}")
    if not state["reachable"]:
        print(f"  ✗ {state['detail']}")
        print("  Start it with:  ollama serve")
        return 1
    if not state["model_installed"]:
        print(f"  ✗ {state['detail']}")
        if state["installed_models"]:
            print("  Installed: " + ", ".join(state["installed_models"]))
        return 1
    print("  ✓ ready")

    if args.check:
        return 0
    if not args.question:
        parser.error("give a question, or --check")

    question = " ".join(args.question)
    print(f"\n> {question}\n")

    started = time.monotonic()
    try:
        result = ai_chat.chat([{"role": "user", "content": question}])
    except BackendUnavailable as exc:
        print(f"✗ {exc}")
        return 1
    elapsed = time.monotonic() - started

    print(result["reply"] or "(the model said nothing)")

    print("\n" + "─" * 60)
    if result["tool_calls"]:
        for call in result["tool_calls"]:
            mark = "✓" if call["ok"] else "✗"
            print(f"  {mark} {call['tool']}({_short(call['arguments'])})")
    else:
        # The single most useful thing this script can tell you. An answer with
        # no tool call is an answer the model made up.
        print("  ! no tools called — this answer was not read from your data")
    usage = result["usage"]
    print(f"  {elapsed:.1f}s · {usage['input_tokens']} in / "
          f"{usage['output_tokens']} out")
    return 0


def _short(arguments):
    return ", ".join(f"{k}={v!r}" for k, v in (arguments or {}).items())


if __name__ == "__main__":
    sys.exit(main())
