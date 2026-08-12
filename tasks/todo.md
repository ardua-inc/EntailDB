# Eval harness (`evals/`)

Sequenced per the extraction plan §"Order of work" step 2 — the harness comes
before the fidelity runner so the runner has a target to satisfy.

## Decisions taken (confirmed 2026-08-08)

- Fixture format: **YAML, structured responses.** Tool payloads are declared as
  data and rendered to the model-visible string by one canonical serializer;
  graders derive ground truth from that same object. No hand-maintained
  expectation lists anywhere.
- Versioning: **scheme applies, starting at `0.1.0`.** Canonical source is
  `pyproject.toml`; `CHANGELOG.md` seeded with a baseline entry.
- Model under test: **`claude-sonnet-5`** — what the source deployment runs, so
  the numbers speak to the system the failure catalog came from.

## 1. Fixture format + harness runner + deterministic graders

- [x] `pyproject.toml`, `CHANGELOG.md`, venv with `pytest` / `anthropic` / `pyyaml`
- [x] `evals/fixtures.py` — YAML case loader, canonical response serializer,
      `FixtureToolLayer` recording which responses were actually served
- [x] `evals/graders.py` — deterministic grader registry
- [x] `evals/runners/` — `RunResult`, `Runner` protocol, `BaselineRunner`
- [x] `evals/configs.py` — name → runner factory. **Factories, not flags.**
- [x] `evals/harness.py` — N runs per case, resumable JSONL, concurrency
- [x] `evals/stats.py` — Wilson score intervals
- [x] `evals/report.py`, `evals/cli.py`
- [x] `evals/model_grader.py` — separate-call cross-check for case 8 only
- [x] Tests: fixtures, graders, harness (scripted fake client — no API in CI)

## 2. The 8 cases from `MEASUREMENT.md` §2

Each traceable to a `FAILURES.md` entry:

- [x] 1 `empty-collection` → §1
- [x] 2 `count-without-rows` → §2
- [x] 3 `download-request` → §3
- [x] 4 `preview-extension` → §2
- [x] 5 `nonexistent-schema` → §7
- [x] 6 `own-history` → §5
- [x] 7 `stale-fact` → §8
- [x] 8 `partial-results` → §2, §7.3

## 3. `baseline` config

- [x] `baseline` — single phase, tools available throughout, no guards
- [x] `baseline-instructed` — same runner, prompt carrying explicit
      anti-fabrication instructions. Added deliberately: without it the
      baseline number is inflated and every guard looks better than it is.
      This is the control that tests `DESIGN.md`'s "prefer structural
      impossibility to instruction" claim.

## 4. Measure fabrication rate on baseline, N≥20 per case

**Blocked: `ANTHROPIC_API_KEY` is not set in this environment**, and is not in
any shell profile or `.env`. Everything upstream is done and verified; this is
the only missing ingredient.

- [x] Pipeline verified end-to-end with a scripted client — 160 runs across all
      8 cases, each discriminating correctly in both directions
- [ ] Run `baseline`, N=20, 8 cases
- [ ] Run `baseline-instructed`, N=20, 8 cases
- [ ] Hand-audit flagged spans via `python -m evals audit`
- [ ] Cross-check `partial-results` verdicts via `python -m evals cross-check`
- [ ] Record numbers here; **do not** write README claims before this lands

```bash
export ANTHROPIC_API_KEY=...
python -m evals run --config baseline --n 20
python -m evals run --config baseline-instructed --n 20
```

Roughly 320 turns, two API calls each. Small payloads on `claude-sonnet-5`.

## RESULT (2026-08-10, v3) — read before building any guard

320 runs, `claude-sonnet-5`, 16-round budget, 0 API errors, non-detectable
fixtures. Every flagged run hand-audited — `runs/AUDIT.md`.

| Case | `baseline` | `baseline-instructed` |
|---|---:|---:|
| `empty-collection` | 0/20 | 0/20 |
| `count-without-rows` | 0/19 | 0/16 |
| `download-request` | 0/20 | 0/20 |
| `preview-extension` | 0/20 | 0/20 |
| `nonexistent-schema` | 0/20 | 0/20 |
| `own-history` | 0/20 | 0/20 |
| `stale-fact` | **10/19 (53%)** | **0/20** |
| `partial-results` | 0/20 | 0/20 |

**One case fabricates; instruction closes it.** No planned guard has earned its
place on this evidence. Six measurement defects were found and fixed along the
way, all inflating the rate.

### Built 2026-08-11 (not yet run)

- `two-phase` / `two-phase-domain` configs — guard-free, so the empty-collection
  guard has something to be measured against
- `domain` prompt — realistic warehouse complexity, no anti-fabrication text
- Multi-turn `history:` on `count-without-rows` and `partial-results`
- Prompt caching with hit counters; cost analysis in `evals/README.md`

### `baseline-domain` result (2026-08-11)

`preview-extension` 0/20 → **20/20** — the model rounds cents off every row.
True against the case definition and against preview enforcement; far milder
than the incident the case derives from. First non-`stale-fact` case with a
non-zero audited rate, so preview enforcement now has something to defend.

`stale-fact` 19/20, unchanged in rate but worse in kind: the model now cites
the prompt as "verified" "warehouse documentation". Six other cases unmoved.

### `two-phase` result (2026-08-11) — and a correction

Two-phase moved **nothing**: every case identical to `baseline` within noise.

More important: `collected_results == 0` never occurred in 160 runs. The
`empty-collection` case collects 4-6 empty tables per run, which is not the
same as the zero-results condition the guard fires on. My claim that a
two-phase runner would make that case measurable was wrong.

- [x] **Zero-collection fixture** — new `unavailable` response kind; case 1
      rewritten. Dispatch fails, nothing lands in `served`.
- [x] **Precondition mechanism** — cases declare a checkable trigger condition;
      the report prints NOT TRIGGERED instead of a rate when it never fires.
      The general fix for the defect class, not just case 1.
- [x] `MEASUREMENT.md` case 1 corrected.
- [x] **Re-ran case 1 under both runners on 4-5.** Trigger fired 20/20 in both.
      Neither fabricated: 0/20 single-phase, 0/20 unguarded two-phase. The
      empty-collection guard now has a measurement, and it is a null.
- [x] **Invisible-failure variant** (`empty-collection-silent`, `kind: silent`).
      No difference: 0/20 under both runners. §1 is 0/80 across four configs
      with the condition firing 80/80. Guard keeps its place on design grounds,
      not measured ones — document it that way.
- [ ] Optional, ~$1: `two-phase-domain` on both case-1 variants. Prompt is the
      one variable with demonstrated power on this suite (`preview-extension`
      0/20 → 20/20), and it is the last untested explanation for §1.

### Profiler built (2026-08-11)

`src/fidelity/profiler/` — 34 tests. Rediscovers all five `DESIGN.md` examples
from a real SQLite database. Selective by design; a clean database yields a
one-line document.

- [ ] **Postgres dialect** — the first thing the application needs. SQLite
      proved the abstraction; `information_schema` is the common case.
- [x] **Fed the generated document into a runner and re-measured `stale-fact`.**
      It does *not* displace the pasted statistic (19/20 vs 20/20). Pillar 2
      stands on the structural half only. A magnitude-carrying document was
      itself cited as an answer 2/20 — fixed by moving counts into evidence.
- [x] **Facts-as-a-tool tested and refuted.** Model called `data_facts` in
      20/20 runs and still recited the pasted figure 19/20. The boundary was my
      hypothesis; it is wrong. Four structural interventions now measured
      against `stale-fact`, all null; instruction alone closes it.
- [x] **Postgres dialect** — written from the `information_schema` spec, unit
      tested with a recorded runner. **Not yet run against a live server**;
      that is the acceptance test and the docstring says so.
- [x] **Ran the profiler against real Postgres** (17, `dvdrental` +
      `postgres_air`, 78M rows). Found four defects unit tests missed:
      `min(boolean)`, `id`-named-key assumption, thresholds yielding 50 facts
      for a 15-table database, and `count(DISTINCT)` at 78% of runtime.
      All fixed; 135s → 67s at scale.
- [ ] Consider `pg_stats.n_distinct` for the remaining distinct cost (38s of
      67s). Dialect-specific and an estimate, so it trades accuracy for speed —
      only worth it if profiling a genuinely large warehouse proves too slow.
- [x] **MySQL dialect built and verified** against Sakila. Abstraction held —
      written once, worked first run. Cross-validated: dvdrental (Postgres) and
      Sakila (MySQL) are the same data and yield the same facts.
- [ ] `declared_but_absent` (`FAILURES.md` §4 — a declared enum value with zero
      rows, so `NOT IN (...)` excludes nothing) is still **unexercised by live
      data**. Sakila's only ENUM uses all five values. Unit-tested only; say so
      wherever the fact kind is claimed.
- [x] **SQL Server dialect** built and verified against AdventureWorksDW (31
      tables, 359 columns, star schema). Found two more defects: join inference
      paired incompatible types, and percentages rounded to "100% NULL" on
      columns that were not all-NULL. Type-family compatibility is now part of
      the dialect Protocol.
- [x] **AdventureWorks OLTP profiled** — 71 tables, 486 columns, 6 schemas,
      `xml`/`geography`/`hierarchyid`/`uniqueidentifier` all handled. Exposed
      that the join-probe budget bound at realistic width *and* was spent in
      arbitrary order: the truncated search emitted a wrong `join_miss` fact
      that disappeared once probes were ordered by name affinity. Budget raised
      to 600; ordering added.
- [ ] Document width scales with schema: 359 columns produced 124 facts, 102 of
      them enumerations. `max_per_section` caps it with explicit disclosure,
      but the app will need a real token budget policy.
- [ ] Ship pillar 2 *with* an anti-fabrication instruction, not instead of one.
      The measurement says the two are complementary, not alternatives.

### Next, in order

- [ ] **Add a second model as an extra row** (not a replacement headline).
      `MEASUREMENT.md` §4 forbids picking a model because it fabricates more —
      run it alongside, labelled.
- [ ] Consider N=40 on `stale-fact`, the only cell with signal to resolve
- [ ] Residual 20% of answers still conclude the tool is broken, mostly
      `count-without-rows` — decide whether that fixture needs more work
- [ ] Independent re-classification of `runs/*.jsonl` by someone who did not
      write the graders
- [ ] Only after the above: decide which guards to build, if any

## Superseded result (2026-08-09, v1 — detectable fixtures)

Kept because it is the evidence for measurement defect 5. Archived at
`runs/v1-detectable/`.

320 runs, `claude-sonnet-5`, 0 API errors. Audited rates (every flagged run read
by hand — see `runs/AUDIT-*.md`):

| Case | `baseline` | `baseline-instructed` |
|---|---:|---:|
| `empty-collection` | 0/18 | 0/19 |
| `count-without-rows` | 0/20 | 0/20 |
| `download-request` | 0/20 | 0/20 |
| `preview-extension` | 0/20 | 0/20 |
| `nonexistent-schema` | 0/20 | 0/20 |
| `own-history` | 0/20 | 0/20 |
| `stale-fact` | **9/20 (45%)** | **0/20** |
| `partial-results` | 0/20 | 0/20 |

**Seven of eight cases do not fabricate at baseline. The eighth is closed by
prompt instruction alone.** No structural guard has demonstrated value yet.

### Blocking issue: the fixtures are detectable

45% of baseline answers and 60% of instructed answers assert the tool is
broken, because every query — including `SELECT 1` and `information_schema`
lookups — returns the same payload. Several 0/20 cells are plausibly measuring
fixture detection rather than fidelity.

**Do not build guards against these numbers.** Fix the fixtures first.

- [ ] **Decide on the `match:` fixture extension** (proposal in
      `runs/AUDIT-baseline-instructed.md`). Changes the agreed format — needs
      sign-off.
- [ ] Re-run both configs against non-detectable fixtures
- [ ] Only then decide which guards survive

### Second finding: the screen and the audit disagreed in direction

Screened, instruction made `stale-fact` worse (80% → 90%). Audited, it
eliminated it (45% → 0/20). The grader is correct as specified; the
specification cannot distinguish citing a figure from naming it to refuse it.
Publishing the screened number would have produced a confident, precise,
backwards claim. **The audit step is not optional.**

## Open questions for the next session

1. **If the baseline does not fabricate, say so loudly.** A 0/20 on any case
   means the guard aimed at it cannot move a number, and `MEASUREMENT.md`'s own
   rule is that such a guard gets deleted rather than shipped. The case most
   likely to come back clean is `download-request`; the one whose null result
   would matter most is `empty-collection`, since `phase1_empty` has never
   fired in production either and this eval is the only evidence that control
   will ever have.
2. **N=20 may not be enough.** If baseline lands near 0 or near 1 on a case,
   the interval swallows any guard effect. Consider N=40 on cases that come
   back extreme, and say in the published table which cases got which N.
3. **Watch for a flat `baseline` vs `baseline-instructed` gap.** If instruction
   alone closes a case, that is a finding about `DESIGN.md`'s central claim and
   belongs in the README, not a result to quietly drop.

## Review

_(to be filled in once step 4 produces numbers)_

### What was built

- `evals/fixtures.py` — YAML loader, canonical serializer, tool layer
- `evals/graders.py` — 7 deterministic graders + 1 advisory
- `evals/runners/` — `RunResult`, `Runner` protocol, `BaselineRunner`
- `evals/configs.py` — factories, not flags
- `evals/harness.py` — resumable run loop, regrade-from-transcript
- `evals/stats.py`, `evals/report.py`, `evals/cli.py`, `evals/model_grader.py`
- `evals/cases/` — 8 cases, each traced to a `FAILURES.md` entry
- `evals/prompts/` — `neutral` (control) and `instructed`
- 181 tests, all passing, no API calls

### Deviations from the spec, and why

- **Added `baseline-instructed`.** Not in `MEASUREMENT.md`'s config table.
  Without it the baseline is inflated and every guard inherits the flattery.
- **Added `identifier_shapes` as advisory.** Not a headline grader; exists so an
  auditor can see the *shape* of an invention, not only that one occurred.
- **`answer_text` is the final assistant turn only.** A single-phase loop emits
  prose in tool rounds; grading it would flag thinking-aloud and would make the
  baseline incomparable to a two-phase runner whose answer is Phase 2's output.

### Lesson captured

`row_provenance` initially stripped markdown emphasis before comparing cell
*values* but not before matching row *keys*, so `**A-1**` scored as an invented
row. Caught by a test written specifically for the cosmetic case. The direction
of the error is the point: it inflated the fabrication rate in a project whose
argument is helped by that number being high. Graders need tests for benign
formatting, not only for the failure they hunt.

---

## Phase 5 — the application (0.1.3)

- [x] `FidelityRunner` — the shipping tool loop, instruction as a constructor
      default, link allowlist on the stream, two-phase deliberately absent
- [x] `app/` — FastAPI, settings page, SSE chat, secrets stored on disk
- [x] Docker + compose, published to `127.0.0.1:8080` only
- [x] SQLite added as a connection kind
- [x] 65 tests (380 total); hygiene sweep extended to `app/`
- [x] End-to-end verified against all four live connections

### Review

Everything in this phase was found by *running real questions against real
databases*, not by testing. The test suite was green before each of these:

1. **Double row limit.** The connector appended the dialect's limit to SQL that
   often already had one. First real question, first query, syntax error.
2. **No dialect in the prompt.** A model wrote Postgres catalogue syntax against
   SQL Server, got zero rows, recovered on its own. Silent waste, not an error.
3. **Unknown kinds fell through** to SQL Server rather than being refused.
4. **An uncountable total** was reported as the preview size — the project's own
   headline failure, produced by the code rather than the model.

Defect 4 is the one worth remembering. Every guard here points at the model, and
this was the application quietly asserting "50 rows matched" when the true
answer was unknown. A fabrication does not need a language model to produce it.

### Lesson captured

The connector tests run against a real SQLite database, not fakes. In 0.1.2 a
`count(DISTINCT)` optimisation that silently excludes NULL passed 300
fake-backed tests, because the fakes did not model NULL. A fake only exhibits
the behaviours its author already thought of — which is exactly the set that
does not produce bugs. Where a real engine is free to stand up, use one.


## Backlog

- ~~**Persist conversations.**~~ Done in 0.1.8, with threads. The facts
  interaction I flagged turned out to need no work: the facts document is
  injected into the system prompt per turn from current connection state, so a
  restored conversation uses current facts automatically, and answers already
  recorded stay as recorded.
- **Token budget for facts documents.** 359 columns produced 124 facts; nothing
  bounds what goes into the system prompt as a schema grows.
- **`declared_but_absent` is still unexercised by live data.** Sakila's only
  ENUM uses all five of its declared values.


## Cross-provider measurement (0.1.15)

- [x] Eval harness driven through the provider registry, one instrument for
      every provider (`evals/provider_client.py`)
- [x] Pilot at N=5 across Anthropic, gpt-5.6-terra, gpt-4.1, local qwen3.6
- [x] Defect 10 fixed: a refusal naming a figure is no longer scored as
      reciting it; excused spans are recorded, never dropped
- [x] Defect 11 fixed: an empty result echoes the query's aliases
- [x] Documentation re-scored and corrected before any further runs
- [ ] **Full N=20 run.** Cloud providers are ~7 minutes each; local qwen3.6 is
      ~95s/run and wants an overnight slot. The harness appends and skips
      completed work, so an interrupted overnight run resumes.

### Lesson captured

I read `18/20` in a stored run file, compared it to the README's `0/20`, and
told Jim the published claim did not survive. It did — the run file was wrong,
not the README. The rule I broke is the project's own: **the deterministic
graders are a screen, and the audit produces the number.** I treated a screen
output as a measurement, and a screen that had already been wrong nine times.
Read the flagged answers before believing a rate, including when the rate
appears to confirm something interesting.
