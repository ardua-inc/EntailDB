# Lessons

Patterns worth not repeating. See `~/Developer/CLAUDE.md` §3.

## Graders need tests for the benign case, not only the failure

**2026-08-08, `evals/graders.py`.** `row_provenance` stripped markdown emphasis
before comparing cell *values* but not before matching row *keys*. A model
writing `**A-1**` instead of `A-1` scored as having invented a row.

Caught only because a test was written for the cosmetic case — every test aimed
at the failure mode passed.

**Why it matters more than an ordinary bug:** the error inflated the measured
fabrication rate, in a project whose central argument is helped by that number
being high. A grader that is wrong in the flattering direction will not feel
wrong when the results come in.

**Rule:** for every grader, write the false-positive test alongside the
true-positive one. Specifically: formatting variation, thousands separators,
markdown emphasis, and the faithful refusal that mentions a number ("returned 0
rows").

## Normalise once, at the boundary

The same bug in general form: two code paths compared strings, and only one
normalised them. Fixed by extracting `_bare_cell()` and calling it once, on
entry, so every downstream comparison sees the same representation.

Applies to the numeric graders too — `normalise_number()` is called at every
comparison site rather than each site inventing its own `.replace(",", "")`.

## The measurement apparatus fails more often than the system under test

**2026-08-09/10.** Across three measured runs, **six** defects were found —
every one in the harness, none in the model, and every one inflating the
fabrication rate. That is the direction that flatters the project, and none of
them felt wrong while the numbers were arriving.

Two were not grader bugs at all:

- **Detectable fixtures.** Every query returned the same payload, including
  `SELECT 1` and schema lookups, so 45-60% of answers concluded the tool was
  broken and refused on grounds unavailable in production.
- **A round budget too small.** Once fixtures answered schema queries, the model
  kept exploring and burned an 8-round budget without ever answering — 13 of 20
  runs on one case, scored as clean.

**Rule:** before believing any cell, read the raw answers behind it — not just
the flagged ones. Defect 5 was invisible in every flagged span and obvious in
the first paragraph of an unflagged answer.

**Rule:** archive superseded runs rather than deleting them. `runs/v1-detectable/`
is the evidence for why the fixture format changed; without it the change is an
unsupported assertion.

## A deterministic grader can be wrong in direction, not just magnitude

`forbidden_literals` tests whether a seeded figure appears in the answer. It
cannot distinguish citing the number from naming it in order to refuse it.
Under instruction the model named it *more* often — because refusing explicitly
requires saying what is being refused.

Screened, that reads as "instruction does not help". Audited, instruction took
the case from 53% to 0/20.

**Rule:** when a grader's verdict could be produced by two opposite behaviours,
it is a screen, not a metric. Report both numbers, and never publish the screen
alone.
