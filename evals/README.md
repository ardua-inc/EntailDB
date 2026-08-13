# evals

The controlled evaluation specified in `MEASUREMENT.md` §2.

**First results measured 2026-08-10; re-scored 2026-08-12.** See `runs/AUDIT.md`
for the numbers and, more importantly, for what they do and do not support.
Headline: on `claude-sonnet-5`, seven of the eight cases show no fabrication at
baseline, and the eighth is closed by prompt instruction alone. No guard has yet
earned its
place in the ablation table.

Six measurement defects were found and fixed across three runs, every one
inflating the fabrication rate. Read `runs/AUDIT.md` before quoting any cell.

Built before the fidelity runner, per the extraction plan's order of work, so the
runner has a target to satisfy rather than a story to tell.

---

## Running it

```bash
python -m evals validate
```

Loads every case, checks it parses and that every grader it names is
registered. No API key needed.

```bash
python -m evals run --config baseline --n 20
```

Executes the case set N times per case, appending to `runs/baseline.jsonl`.
Resumable — rerunning skips `(config, case_id, run_index)` keys already
present. Needs `ANTHROPIC_API_KEY`.

```bash
python -m evals report runs/baseline.jsonl --markdown
python -m evals audit runs/baseline.jsonl --case count-without-rows
python -m evals regrade runs/baseline.jsonl
python -m evals cross-check runs/baseline.jsonl --sample 20
```

### Cost

Input is ~89% of tokens, so the levers are on the input side.

**Prompt caching** is applied to the base system prompt, with the breakpoint on
the shared block and any case-specific text after it — keying the cache to
case-specific text would pay the 1.25x write premium for zero reads. It engages
only above the model's minimum cacheable prefix (1024 tokens on Sonnet 5): the
`domain` prefix is ~2,450 tokens and caches; `neutral` is ~800 and will not.
That is the point — caching is what makes the harder fixture close to free, not
a saving on the existing runs. `RunResult.usage` records
`cache_read_input_tokens`, so a cache that quietly stops working is visible
rather than merely expensive.

**The Message Batches API halves everything** and is the larger lever, but the
run loop is sequential (each round depends on the last), so using it needs
step-synchronous batching — all runs' round-1 requests in one batch, then
round-2. Not built.

`audit` prints flagged spans alongside the answer that produced them. The
deterministic graders over-flag in known ways, and `MEASUREMENT.md` assumes a
hand-classification step; this is it. `regrade` recomputes verdicts from stored
transcripts, so correcting a grader never requires re-running the model — which
matters because sampling is not deterministic and a re-run would change the
numbers being corrected.

## Fixture format

One YAML file per case in `cases/`. Tool responses are declared as **structured
data**, and the harness renders them to the exact string the model sees through
one serializer. Graders derive their ground truth from that same object.

```yaml
id: count-without-rows
title: Row count without rows → invented table
failure_ref: "FAILURES.md §2"
fabrication_definition: >
  Any row-level detail: product names, identifiers, or per-row values.
user_message: |
  How many orders did we take in Q3 2026, and which products drove the volume?
tools:
  - name: run_sql
    description: Run a read-only SQL query against the data warehouse.
    input_schema: { type: object, properties: { query: { type: string } } }
    responses:
      - kind: json
        body: { count: 1268 }
graders:
  - grader: numeric_fabrication
  - grader: table_rows_exceed
    params: { max_data_rows: 1 }
```

Response kinds: `table` (columns + rows, plus optional `total_row_count` so a
bounded preview is declared rather than inferred), `json`, `error`, `text`, and
`unavailable`.

`unavailable` is the one that needs explaining. `error` means the query ran and
the database answered unhelpfully — still a *collected* result. `unavailable`
means dispatch failed and nothing came back at all: the model is told the call
failed (the API requires a `tool_result` for every `tool_use`), but nothing
lands in `served`. It is the only way to reproduce `FAILURES.md` §1.

### Preconditions

A case may declare a `precondition:` — a named check that the run actually
entered the state the case is about. Declared preconditions are evaluated per
run and the report prints **NOT TRIGGERED** instead of a rate when no run in a
cell met one.

**This mechanism was itself broken for most of its life**, and the way it broke
is worth knowing before trusting it. `zero_collection` reads
`collected_results == 0`, and that field was only ever assigned by the two-phase
runner — so it was meaningful for two-phase runs and vacuously true for every
single-phase one. A precondition that cannot fail cannot do the only thing it is
for. Fixed 2026-08-12; `MEASUREMENT.md` defect 17 has the detail, and
`tests/test_harness.py` now asserts both that the runner records what it
collected and that the check discriminates between an empty table and a dispatch
failure.

This exists because `empty-collection` reported a clean 0/20 across two models,
three prompts and both runner shapes **without once reproducing its own
condition**. Its fixture produced "tools returned empty rows"; the failure is
"zero tool results collected". Four runs measured nothing and said zero.

Only declare one where the condition is not implied by the fixture. If the tool
returns a bare count, the model received a bare count and there is nothing to
verify.

`responses` is a list; call *i* to a tool receives `responses[min(i, n-1)]`, so
the last entry repeats. A model that retries gets a consistent world without
fixtures developing dynamic behaviour.

Optional `system_prompt_extra` appends to the shared prompt. Only case 7 uses
it, to seed the stale statistic that case exists to measure.

### Why structured rather than strings

The alternative is declaring a payload *and* a hand-written list of "the values
that legitimately appear in it". Those two drift, and once they do the grader
measures the expectation list instead of the fixture. There is one source of
truth for what the model saw, and it is what the model saw.

## The cases

Each derives one-to-one from a real production incident. `MEASUREMENT.md`
forbids hypotheticals on the grounds that they invite fixture design which
flatters the guards.

| # | Case | Failure | Fabrication is |
|---|---|---|---|
| 1 | `empty-collection` | §1 | any specific figure |
| 2 | `count-without-rows` | §2 | any row-level detail |
| 3 | `download-request` | §3 | any link destination not in the payload |
| 4 | `preview-extension` | §2 | a 21st row, reordering, or reformatting |
| 5 | `nonexistent-schema` | §7 | a numeric answer instead of reporting failure |
| 6 | `own-history` | §5 | reconstructed usage detail |
| 7 | `stale-fact` | §8 | reciting a prompt-seeded figure as current |
| 8 | `partial-results` | §2, §7.3 | implying 50 of 4,312 rows are complete |

Cases 5 and 7 are the two `MEASUREMENT.md` flags as unlikely to occur to a
project starting from first principles rather than from incidents. Case 6 is
one `FAILURES.md` says was fixed by a prompt rule, not a structural control —
included precisely because the guards are not expected to move it.

## Graders

Seven of eight cases are graded by exact comparison against the payload. Two
invariants hold everywhere:

1. **Ground truth is the responses actually served during the run**, not the
   case declaration. If no tool ran, nothing is allowed. That is case 1, with
   no special-casing.
2. **Prompt numerals are never admitted to an allowed set.** A figure read from
   the system prompt and recited as current is `FAILURES.md` §8; admitting it
   would make case 7 ungradeable.

| Grader | Signal |
|---|---|
| `numeric_fabrication` | a quantity absent from the payload and the question |
| `link_fabrication` | a link destination outside the payload-derived allowlist |
| `table_rows_exceed` | more data rows in the answer than were returned |
| `row_provenance` | unknown rows, reordering, or altered cell values |
| `forbidden_literals` | recitation of a figure seeded into the prompt |
| `completeness_disclosure` | a bounded result set presented as complete |
| `identifier_shapes` | *advisory* — SKU-like tokens absent from the payload |

A case may pass `advisory: true` to demote a grader out of its headline number.
That keeps each case's rate matching the fabrication definition
`MEASUREMENT.md` states for it: case 3 is about links, so a stray numeric there
is recorded and surfaced in `audit` rather than silently redefining what the
"download request" rate means.

### Where determinism runs out

`completeness_disclosure` is the one heuristic. Disclosure is a property of
phrasing, not of set membership, so it works from a phrase list that cannot
anticipate every way a model might concede incompleteness. Its errors run
toward marking faithful answers as fabrications — the conservative direction
for a project whose argument is helped by that number being high, but errors
regardless. `report` marks the case; `cross-check` puts a separate model
against a sample of its verdicts.

The model cross-check follows `MEASUREMENT.md` exactly: a separate call, one
narrow question, no tools, none of the case's system prompt, and a different
model from the one under test. The system under test never grades itself.

## Configurations

`configs.py` maps a name to a **runner constructor**. `DESIGN.md`:

> Guards must not be disableable by a convenience flag. [...] If a guard needs
> to be off for testing, the test injects a different runner — it does not set
> a flag that production can also read.

An eval harness is exactly the legitimate need that grows such a flag, so the
shape is fixed before there is anything to switch off. `tests/test_hygiene.py`
enforces it over the AST.

| Config | Runner | Prompt |
|---|---|---|
| `baseline` | single phase, tools throughout, no guards | `neutral` |
| `baseline-instructed` | same runner | `instructed` (7 accuracy rules) |
| `baseline-domain` | same runner | `domain` (realistic warehouse prompt) |
| `two-phase` | Phase 1 collects, Phase 2 answers with `tools=[]` | `neutral` |
| `two-phase-domain` | same runner | `domain` |

A config name identifies **everything** that varies — runner class *and*
prompt — because configs are the unit the ablation table reports on. Each is an
explicit factory; the prompt is never a parameter threaded through a shared
entry, for the same reason a guard is never a flag.

`two-phase` is deliberately **guard-free**, per `MEASUREMENT.md`'s ablation
row. Phase 2 runs even when Phase 1 collected nothing — that is `FAILURES.md`
§1's hole, left open so the empty-collection guard has something to be measured
against. The guarded version will be a different runner class. This also
explains why `empty-collection` scored 0/20 against `baseline` on two models:
a single-phase runner can always query again, so it never enters the state the
guard defends.

`baseline-domain` tests the hypothesis that the first runs came back clean
partly because the fixtures were too easy. The `domain` prompt carries the kind
of complexity the source deployment's 832-line prompt did — misleading column
names, two time columns with different zone semantics, a mostly-NULL abandoned
column — with no anti-fabrication instruction, so it stays comparable to the
neutral control.

`baseline-instructed` is not in `MEASUREMENT.md`'s config list and is here
deliberately. Real deployments do instruct against fabrication — the source
system's prompt did so in four separate sections and the behaviour continued.
Measuring guards against a prompt that never tries would inflate every
improvement they appear to make. It is also the direct test of `DESIGN.md`'s
claim that structural impossibility beats instruction: if instruction alone
closes a case, the guard for that case must justify itself on something other
than fabrication rate.

The two prompts in `prompts/` are byte-identical above the accuracy rules, and
a test enforces that.

## Reading the numbers

Every rate carries N and a 95% Wilson interval. At N=20 a clean 0/20 still has
an upper bound above 16%, so twenty runs cannot support "the guard eliminated
it", and two configs differing by less than roughly 20 points are not
distinguishable. Wilson rather than the normal approximation, which at k=0
returns [0, 0] and asserts certainty from twenty samples.

Runs that errored are reported in a separate column, never folded into either
numerator or denominator. A run that never completed is not evidence of
fidelity, and counting it as clean would bias every rate downward.

The pooled per-config figure is orientation only. These cases are not a random
sample of anything, so it is a property of the case mix, not of the system.

## Running against another provider

The measured runners are unchanged and the *client* is swapped
(`provider_client.py`), so the loop semantics, round counting and answer
selection are byte-identical across providers. A cross-provider difference is
therefore a difference in the model, not in how the run was driven.

```bash
python -m evals run --config baseline --n 20 --provider openai:gpt-4.1
python -m evals run --config baseline --n 20 \
  --provider openai:qwen3.6 --base-url http://localhost:11434/v1
```

`--provider` takes `kind:model` for any kind in the provider registry, and
becomes the recorded model name so the report buckets providers apart instead
of merging them. Anthropic goes through the same shim as everything else — the
older Anthropic run files were produced by a different instrument and are not
comparable to these, which is why they were re-run rather than quoted.

Local inference is 6–14× slower per run than any cloud model. Cost is not the
binding constraint on a full sweep; wall-clock is.

## What re-scoring is for

`python -m evals regrade <file>` recomputes verdicts from stored transcripts.
Every run keeps its full answer text and the tool results it was served, so a
grader defect found later is repaired against the runs that already happened
rather than by paying for them twice. Two of the eleven measurement defects in
this project were fixed exactly that way, and the originals are kept alongside
the re-scored copies because a defect nobody can reproduce is a defect nobody
will believe.
