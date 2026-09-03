#!/usr/bin/env python3
"""Ask the model the questions in `evals/`, and score the answers.

`scripts/ask.py` asks one question and shows the working; this asks all of them
and says which held. It runs against a throwaway database built by
`evals/fixture.py`, so every right answer is a figure known in advance and a run
can be written down and compared with the last one.

    python3 scripts/eval_ai.py                       # the whole suite, once
    python3 scripts/eval_ai.py --repeat 3            # a pass rate, not a verdict
    python3 scripts/eval_ai.py --case subscriptions  # one question, verbosely
    python3 scripts/eval_ai.py --backend anthropic   # the control
    python3 scripts/eval_ai.py --json runs/4b.json   # for keeping

`--repeat` is the one to reach for when comparing two models. A small model is
not deterministic: a case that passes once has passed once, and the difference
between 3/3 and 1/3 is the difference between shipping it and not.
"""

import argparse
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The database goes somewhere throwaway BEFORE config is imported, the same rule
# the test suite runs under: config reads SQLITE_PATH once, on first import, and
# an eval that wrote its fixture into the real figures would be unforgivable.
_TMP = tempfile.mkdtemp(prefix="balance-evals-")
os.environ["SQLITE_PATH"] = os.path.join(_TMP, "expenses.db")


def _prepare(today=None):
    import config
    from data import schema as database
    from evals import fixture

    assert config.SQLITE_PATH.startswith(_TMP), (
        f"The evals must run against a scratch database, not {config.SQLITE_PATH}."
    )
    database.init_db()
    database.seed_local_user()
    return fixture.build(today)


def _outputs_for(trace):
    """Re-run each lookup to get back what the model was actually handed.

    The loop's trace records which tool ran with which arguments, not the result
    — the panel does not need it. The graders do: "is this figure one a tool
    returned" has no answer without the results. The tools are read-only and the
    database has not moved, so running them again returns the same thing.
    """
    from ai.tools import run_tool
    return [run_tool(call["tool"], call["arguments"]) for call in trace]


def run_case(case, on_event=None):
    """Ask one question; return the loop's result, the outputs and the grades."""
    from ai import chat as ai_chat
    from evals import grading

    started = time.monotonic()
    # A conversation, not a question: every turn is sent with the ones before
    # it, and the last answer is the one graded. Most cases are one turn, which
    # is the same thing said shortly.
    history = []
    for turn in case.conversation():
        history.append({"role": "user", "content": turn})
        result = ai_chat.chat(history, on_event=on_event)
        history.append({"role": "assistant", "content": result.get("reply") or ""})
    elapsed = time.monotonic() - started
    outputs = _outputs_for(result.get("tool_calls") or [])
    return {
        "case": case.id,
        "question": " → ".join(case.conversation()),
        "reply": result.get("reply") or "",
        "trace": [{"tool": c["tool"], "arguments": c["arguments"],
                   "period": c.get("period"), "ok": c.get("ok")}
                  for c in result.get("tool_calls") or []],
        "checks": [c._asdict() for c in grading.check(case, result, outputs)],
        "seconds": round(elapsed, 1),
        "usage": result.get("usage"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeat", type=int, default=1,
                        help="Ask each question N times and report a pass rate.")
    parser.add_argument("--case", action="append",
                        help="Only these case ids (repeatable).")
    parser.add_argument("--backend", help="Override AI_BACKEND.")
    parser.add_argument("--model", help="Override the model name.")
    parser.add_argument("--json", dest="json_path", help="Write the full run here.")
    parser.add_argument("--list", action="store_true",
                        help="List the cases and what each is for, then exit.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print every reply and every lookup.")
    args = parser.parse_args()

    if args.backend:
        os.environ["AI_BACKEND"] = args.backend
    if args.model:
        os.environ["OLLAMA_MODEL" if os.environ.get("AI_BACKEND", "local") == "local"
                   else "AI_MODEL"] = args.model

    fx = _prepare()
    from evals import cases as case_module

    cases = case_module.build(fx)
    if args.case:
        wanted = set(args.case)
        cases = [c for c in cases if c.id in wanted]
        missing = wanted - {c.id for c in cases}
        if missing:
            parser.error(f"no such case: {', '.join(sorted(missing))}")
    if args.list:
        for case in cases:
            print(f"\n\033[1m{case.id}\033[0m  {case.question}")
            print(f"  {case.why}")
        return 0

    from ai import chat as ai_chat
    state = ai_chat.status()
    print(f"backend: {state['backend']}  model: {state['model']}")
    if not (state["reachable"] and state["model_installed"]):
        print(f"  ✗ {state['detail']}")
        return 1
    print(f"  ✓ ready · {len(cases)} cases × {args.repeat}\n")

    runs, failed = [], []
    for case in cases:
        passes = 0
        for attempt in range(args.repeat):
            run = run_case(case)
            runs.append(run)
            bad = [c for c in run["checks"]
                   if not c["ok"] and not c["advisory"]]
            noted = [c for c in run["checks"] if not c["ok"] and c["advisory"]]
            passes += not bad
            if args.verbose or bad or noted:
                _report(case, run, bad, noted,
                        attempt if args.repeat > 1 else None)
        mark = "✓" if passes == args.repeat else ("·" if passes else "✗")
        print(f"{mark} {case.id:<20} {passes}/{args.repeat}")
        if passes < args.repeat:
            failed.append(case.id)

    total = len(cases) * args.repeat
    clean = sum(1 for r in runs
                if all(c["ok"] or c["advisory"] for c in r["checks"]))
    seconds = sum(r["seconds"] for r in runs)
    print(f"\n{clean}/{total} answers clean · {seconds / max(len(runs), 1):.1f}s "
          f"a question · {len(failed)} case(s) not perfect")
    if failed:
        print("  " + ", ".join(failed))

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump({"backend": state["backend"], "model": state["model"],
                       "repeat": args.repeat, "runs": runs}, fh,
                      indent=2, ensure_ascii=False)
        print(f"  written to {args.json_path}")
    return 1 if failed else 0


def _report(case, run, bad, noted, attempt):
    """The answer, what it looked up, and which checks it missed.

    All three together, because that is what tells a prompt problem from a model
    problem: a wrong answer off the right lookup is one fix, a wrong lookup is
    another.
    """
    label = f"{case.id}" + (f" #{attempt + 1}" if attempt is not None else "")
    print(f"\n\033[1m{label}\033[0m  {case.question}")
    print(f"  {run['reply'].strip() or '(nothing)'}")
    for call in run["trace"]:
        marker = "✓" if call["ok"] else "✗"
        shown = ", ".join(f"{k}={v!r}" for k, v in (call["arguments"] or {}).items())
        print(f"  {marker} {call['tool']}({shown}) → {call['period']}")
    if not run["trace"]:
        print("  ! no tools called")
    for check in bad:
        print(f"  \033[31m✗ {check['name']}\033[0m"
              + (f" — {check['detail']}" if check["detail"] else ""))
    for check in noted:
        # Shown, not counted. Worth knowing and not worth failing a run over.
        print(f"  \033[33m· {check['name']}\033[0m"
              + (f" — {check['detail']}" if check["detail"] else ""))
    if bad:
        print(f"  ({run['seconds']}s) — {case.why}")


if __name__ == "__main__":
    sys.exit(main())
