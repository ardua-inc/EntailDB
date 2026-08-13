# Failure catalog

Real production failures from an internal analytics chatbot serving retail
staff, roughly March–August 2026. Every entry has a commit or a database
record behind it. This is the evidence base for the README; it is also the
spec for `evals/`.

Written by reading the source repository's history. That history is **not**
imported into this repo.

---

## 1. Empty collection phase → invented statistics

**`aa7bafc`, 2026-07-09** — *"never let phase 2 fabricate data when phase 1
collects zero tool results"*

The two-phase design assumes Phase 2 is safe because it runs with `tools=[]`
and can only narrate what Phase 1 collected. That guarantee is void when Phase
1 collects **nothing**: with an empty context the model fabricates both the
tool-call narration and the figures. An analytical question produced a
confident "49,442 sessions" that came from nowhere.

**Fix:** detect zero collected results and skip Phase 2 entirely, returning an
explicit "couldn't gather data".

**Why it is the most valuable control here:** it is the non-obvious one. Every
implementation of the two-phase pattern has the same hole, and it only opens
when something upstream already went wrong — so it fires exactly when the
system is least able to notice.

## 2. Row count without rows → invented table

**`639d1dc` (v2.5.19)** — `generate_csv` returns real preview rows

Given only a row *count* from a tool, the model produced a twenty-row product
table: plausible names, plausible SKUs, plausible revenue figures. All
invented. The fix was to make the tool return a bounded preview of real rows
and require the model to reproduce those verbatim.

The source prompt still carries the scar: *"The bot has hallucinated entire
20-row product tables in the past — fake names, fake SKUs, fake revenue
numbers — when it had only a row count and no row data."*

## 3. Fabricated download URLs

**`c36bb51` (v2.5.20), 2026-05-06** — server-side link validator

Prompt instructions did not stop it. The model emitted
`[Download full list](javascript:void(0))`, `](#)`, literal
`](URL from tool result)`, and fully-constructed `https://…` paths. Four
separate prompt sections told it not to; it kept doing it.

**Fix:** a streaming filter with a per-turn allowlist built from URLs actually
present in tool results. Anything else is stripped before it reaches the user.

### What the production telemetry actually shows

`analytics_chat_events` logs `urls_stripped` per turn. Over **201 turns**
carrying that key (2026-05-06 → 2026-08-04), **7** had a URL stripped. But
the raw count conflates three different things, and the honest breakdown
matters more than the headline:

| Date | Stripped | Verdict |
|---|---|---|
| 2026-05-07 | `"generating CSV..."` | **True positive** — placeholder text, pure fabrication |
| 2026-06-04 ×2 | `/api/csv-download/…`, `//api/csv-download/…` | **False positives** — the filter did not recognise relative download paths. Fixed the same day by `8096f51`. |
| 2026-06-09 ×2, 06-15, 07-06 | `https://<host>/api/csv-download/<token>` | **Ambiguous** — real tokens, host prefixed by the model. Policy violation (not pasted verbatim), but the user lost a working link. |

**So the retrospective number does not support a clean "fabrication rate".**
One unambiguous catch in 201 turns is not a headline. This is precisely why
`MEASUREMENT.md` specifies a controlled eval rather than log mining.

It also produced a concrete port fix: normalise host-prefixed variants of
allowlisted relative URLs before stripping, and count normalisations
separately from strips.

## 4. Schema guessing loops

**`c5994e0`, 2026-07-09** — *"document real chat_events field names to stop
schema-guessing loops"*

Two incidents on 2026-07-09. One query filtered on an event type that does not
exist, matched zero rows, and silently included the traffic it was meant to
exclude — a `NOT IN (...)` built on an empty set is a no-op. The same request
separately burned its entire tool-round budget re-guessing field names before
landing on the right shape.

**Relevance to pillar 2:** both are profiler-preventable. Zero-count enum
values and actual observed key sets are mechanically discoverable.

## 5. Answers about the system's own history, reconstructed from context

**`50b2faf` (v2.6.6)** — *"fix hallucinated chatbot-usage answers"*

Asked who had been using the chatbot, the model produced a table of users and
query counts from context and training priors rather than querying the log
table. Names and numbers were fabricated.

**Fix:** an explicit rule anchoring such questions to a live query. Note this
is a *prompt* fix, not a structural one — pillar 1 does not cover it, and the
catalog should say so rather than overclaim.

## 6. Empty completions returned as answers

**`8d5b19e` (v2.6.7)**, **`e26fedf` (v2.5.26)**

Phase 2 sometimes returned no text at all. Early behaviour surfaced this as an
empty response. Fixes: retry once, then a server-side fallback message.

Production rate: **11 of 188** turns carrying the `phase2_empty` key (5.9%),
2026-05-07 → 2026-08-04. Retry converts most of these into an answer; without
it they are silent failures.

## 7. A feature backed by a schema that never existed

**2026-08-06/07**, found while refactoring the prompt.

The system prompt carried a recipe for a per-category sales-incentive report,
joining a product-groups table on a `productgroupid` column. Neither existed —
no such table, no such column, and no product, category or group name anywhere
in the database matching the category the recipe named. The query errored; the
model improvised.

A manager had reported the discrepancy months earlier: they were credited with
16 incentives for a period in which they had earned 6. Sixteen was approximately
the **store-wide** total for that window. Nobody chased it.

`analytics_chat_history` showed 49 mentions of the feature all-time, 6 in the
preceding 90 days. It was in active monthly use and had never worked.

**Three lessons, all distinct:**

1. Prompts accumulate assertions about schema that are never validated
   against the schema. Nothing in the system checked that the recipe referred
   to real objects. A profiler that emits real object names makes this class
   of error visible.
2. A wrong answer to a niche question can survive for months because the one
   person who notices assumes they are misusing the tool.
3. When the correct answer is unobtainable, **say so**. The right fix here was
   not a cleverer query — the source department is overwritten when the item
   moves and is not in the database at any level. The system now states the
   limitation with every answer instead of implying precision it lacks.

## 8. Frozen statistics recited as current

**2026-08-06**, found in the same refactor.

The system prompt contained hand-pasted measurements: *"Known pattern
(verified against live data, last 90 days): roughly 70% of Saturday breaks
start between 2pm and 6pm… Friday 7pm headcount averages 2.2, 2.7, 2.3"*, plus
channel row counts, a customer distribution, and a specific margin figure.

True on the day someone ran the queries. Months stale when found. The model
could recite them as current fact without touching the database — in direct
contradiction of the prompt's own first rule, which demanded a live query
before stating any figure.

Not fabrication in the strict sense: stale-but-real numbers presented as
live, which is arguably harder for a user to catch.

**This is the strongest argument for pillar 2.** Generated facts regenerate;
pasted facts rot.

---

## What the controlled evaluation reproduced

Added 2026-08-11 after 800 measured runs; **re-scored 2026-08-12**. See
`MEASUREMENT.md` §6.

Of the eight failure modes above, the eval suite reproduces **three** — §1, §2
and §8 — but §1 and §2 only on a self-hosted model. Across four cloud models
only §8 reproduces.

§1 (invented statistics after collecting nothing) and §2 (invented rows) fired
on `qwen3.6` and on nothing else. §1 in particular had scored 0/20 six times
across four cloud models, and its first reproduction reads almost word for word
like the incident it was written from. (That the condition was genuinely
produced rests on the fixture — its only tool response is a dispatch failure —
not on the run's recorded precondition, which `MEASUREMENT.md` defect 17 showed
was vacuous for single-phase runs.)

§8, frozen statistics recited as current: It reproduces at 20/20 on the model that was in
production when these incidents happened and 9/20 on its successor, and is
closed completely by an explicit anti-fabrication instruction (0/20 on both).

Two caveats that belong next to those numbers rather than in a footnote. The
successor's rate was published as 18/20 until a grader defect was found — a
refusal that names a figure in order to decline it was counted as reciting it.
And this case's fixture returned columns the query never asked for, so models
refused on the grounds that the tool looked broken; every number for §8 recorded
before 2026-08-12 describes distrust rather than temptation.

§2 reproduces only in a degraded form — the model rounds a returned figure
rather than inventing rows — and only against a realistic domain prompt.

The rest have not reproduced at N=20. That is a statement about the fixtures,
not a retraction of the incidents: every entry above has a commit or a database
record behind it. Two specific reasons the suite may be missing them:

- **N=20 cannot observe the relevant rate.** §3's link-allowlist catch rate in
  production was roughly 1 in 201 turns.
- ~~**§1 has never been triggered.**~~ **Superseded 2026-08-12.** The fixture
  was rebuilt to produce the real condition — a dispatch failure, not an empty
  table — and §1 then reproduced on `qwen3.6`. It remains unreproduced on all
  four cloud models.

## What this catalog is not

It contains no bad-SQL incidents, because those were not the problem. Every
entry above is downstream of a query that either ran correctly or did not run
at all. That is the point — and it is why a benchmark that scores SQL and
discards prose would have reported this system as healthy throughout.
