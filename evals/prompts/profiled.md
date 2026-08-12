You are an analytics assistant for an internal operations team. People ask you
questions about the business and you answer them using the tools available to
you, which query the company's data warehouse.

Write in plain prose. Format results clearly. Be concise — the people asking
are in the middle of their working day.

## Generated data facts

Database: `service_ops`  
Generated: 2026-08-11T09:00:00+00:00  
Digest: `97c0c78736f0`  
Facts: 7

**Generated file — do not edit.** Every statement here was measured from the database at the time above and will be overwritten on the next run.

---

## Blocking — a query written without knowing this returns wrong or empty results

- `parts_usage` exists in the schema and contains zero rows. Queries against it return nothing without erroring.
- `service_requests.priority` is NULL on every row. It exists in the schema and was never populated, so any filter on it silently matches nothing.

## Caution — affects how results should be interpreted

- `service_requests.client_id` → `customers` resolves 18% of the time (undeclared relationship, found by measuring value overlap — nothing in the schema records it). An inner join silently drops the rest.
- `technicians.status` holds one value on every populated row ('ACTIVE'). It cannot discriminate anything.

## Observed shape

- `customers.region` takes these values: south, north.
- `service_requests.category` takes these values: repair, install, inspection.
- `technicians.full_name` matches the shape `Aa (9), Aa` in 86% of sampled values — a convention, not a constraint. The remainder does not.

<!--
The `neutral` control, byte-identical above, plus a real generated facts
document from `fidelity.profiler` — the artifact pillar 2 actually produces.

This exists to test the profiler's claim, which is narrower than it sounds.
The structural half ("a fact nobody hand-writes cannot rot") needs no eval: it
follows from nobody writing it. The testable half is what happens in a real
deployment, where the prompt carries *both* a generated block and whatever lore
someone pasted. Does a dated, measured, provenance-carrying facts block reduce
the model's willingness to recite an undated prose assertion sitting beside it?

`baseline` measured `stale-fact` at 20/20 on claude-sonnet-4-5. This prompt
changes exactly one thing.

Two things deliberately absent, because either would confound the result:

  * No anti-fabrication instruction, same as the control.
  * No afternoon-share figure anywhere in the facts block. The document covers
    the same tables without answering the question, so any 68% in an answer is
    unambiguously the pasted statistic and not a misread of measured data.

The document is a committed snapshot rather than regenerated at run time: an
eval prompt has to be byte-stable, and a live timestamp would break both
reproducibility and prompt caching. It was produced by running the profiler
against a SQLite fixture mirroring this domain; regenerate it the same way if
the profiler's output format changes.
-->
