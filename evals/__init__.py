"""Model validation: does the assistant actually answer these figures correctly?

The rest of the suite tests the tool layer — that `category_breakdown` reports
what the Dashboard would draw. None of it tests the part a user meets: a model
choosing a tool, reading its result and writing a sentence. That is where every
failure recorded in CLAUDE.md happened, and it was found by hand each time.

This package makes those runs repeatable:

    evals/fixture.py   a database whose every answer is known in advance
    evals/cases.py     the questions, each with what a right answer must contain
    evals/grading.py   the graders, which need no model of their own

`scripts/eval_ai.py` runs the questions through the real loop against a real
model. `tests/test_evals.py` runs the graders against scripted replies — the
right one and the wrong ones that actually shipped — so the suite proves the
validation catches them without needing a model at all.
"""
