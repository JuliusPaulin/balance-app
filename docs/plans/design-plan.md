# Designing Balance — a plan for the next attempt

Written after the AirForm re-skin was built and thrown away. `main` is back to
where it started; nothing below is code yet.

---

## What went wrong, so it is not repeated

The re-skin was not rejected for one bad decision. It was rejected because
**everything changed at once**, and by the time it could be looked at, the only
verdict available was all or nothing.

Three specific causes:

1. **The unit of change was the whole app.** Nine work packages, every page,
   before a single screen was reviewed. When something felt wrong there was no
   way to say *which* thing — and no way to keep the good parts.

2. **A mockup is not the app.** The mockups were judged on their own, one card
   filling a screen. The same component dropped into a real page with six other
   cards and real density read completely differently — which is exactly what
   "expenses by category is way too large" was. The mockup was not wrong; the
   scale it was drawn at was.

3. **The checkpoint question was asked too early and too cheaply.** "Dashboard
   first, or the whole thing?" was asked before there was anything to look at.
   Answering "the whole thing" was reasonable, and it removed every chance to
   correct course. Next time that question is not asked — the checkpoints are
   simply there.

---

## What the evidence says about the taste

Only things actually said, not inferred:

| Liked | Disliked |
|---|---|
| AirForm's language, chosen from a live site | The re-skin as applied to the app |
| Ring gauges, hatched bars, tick meters, the label inside its own track, dashed lines with hollow markers, the Eco cards' roundness — all picked out by hand | The headline sentence card — "remove the text window" |
| Segmented tick meters over continuous bars, asked for by name | Category cards "way too large" |
| The blue palette, kept even when the design was dropped | Small grey captions under figures — noise |

The pattern: **components were liked, the wholesale application was not.** Every
rejection was about *size, density or an extra thing on the page*. None was
about the vocabulary itself.

---

## The rule for next time

> **One variable at a time, seen in the real app, revertible on its own.**

Design changes are independent. They can be shipped and judged separately:

| # | Change | Blast radius | Judgeable alone? |
|---|---|---|---|
| A | Accent colour | tokens only | yes |
| B | Corner radius scale | tokens only | yes |
| C | Density — padding, row height, font sizes | tokens + a few rules | yes |
| D | Card treatment — borders vs hairline rings vs shadows | one rule | yes |
| E | Typography — family, weight, tracking | tokens | yes |
| F | One component, e.g. the breakdown bars | one renderer | yes |

The failed attempt did A–F simultaneously. Each one on its own is an afternoon
and a one-line revert.

---

## The proposal: a live theme lab, before any code

The app is already fully tokenised — `--accent`, `--radius-*`, spacing, type
scale — and Settings already has a working palette picker. That machinery is
enough to build a **dev-only panel that changes those tokens live, on the real
app, with the real data**.

Then the loop is: open the app, drag a slider, look at the actual Dashboard
with the actual figures. Nothing is committed until something is chosen. Pick a
value, and it gets baked into `:root` as a one-line change.

Why this and not more mockups: every mockup round so far has been judged
positively and then failed on contact with the real page. A lab removes that
gap — what is judged *is* the app.

**Scope:** one panel, toggled by a key or a Settings row, never shipped in a
build. Controls for accent, radius, density, shadow strength, and font. It
prints a paste-ready token block, the way `docs/mockups/colorways-2026/`
already does for colour alone.

---

## If a lab is too much, the smaller version

Take the ladder above one rung at a time, straight on `main`, each as its own
commit:

1. **Density first.** It is the most likely real complaint — cards feeling
   large or loose — and it is pure token work.
2. **Then radius**, if rounder is still wanted.
3. **Then colour**, which is already proven to work: branch `airform-accent`
   holds the blue accent over the unchanged v1 design, ready to merge or drop.

Each one gets looked at before the next starts.

---

## What is kept

- `v2-airform` — the full re-skin, 14 commits. Not deleted. If any single part
  of it is ever wanted, it can be lifted out on its own.
- `airform-accent` — v1's design with the blue accent, and nothing else.
- `docs/mockups/airform-2026/` — the six mockup screens.
- `docs/v2-plan.md` — the work packages and the ten defects found, several of
  which were real bugs in v1 and are still worth fixing on their own merits:
  white on the green accent is 2.9:1 on every primary button, and the outlier
  flag is dead on income.

---

## Open question

"It's so broken" has not been diagnosed. It matters which of these it was,
because they lead to different next steps:

- **The app misbehaved** — something did not work, not just looked wrong. That
  is a bug hunt on the branch, and worth doing even though the branch is parked.
- **It looked wrong** — then the plan above is the answer.
- **It was disorienting** — everything moved at once and nothing was where it
  was expected. That is an argument for the ladder, and specifically for
  changing colour and density *before* anything structural.
