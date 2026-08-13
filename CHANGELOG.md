# Changelog

Versions follow `x.y.z`. `z` bumps on every merge to `main`.

## [0.1.29] - 2026-08-12

The empty-collection ablation, run at last.

The row `MEASUREMENT.md` has carried since the first commit and never been able
to fill, because until a model failed the case there was nothing to protect.
On `qwen3.6`: **56/200 fabrications unguarded, 27/200 guarded.**

**The guard is broader than its specification, and that is where the effect
comes from.** `collected_results == 0` is true in two situations, and
`FAILURES.md` §1 describes only one: the tools can fail, or the model can never
ask. On `stale-fact`, `qwen3.6` made zero tool calls in 14 of 20 runs, reciting
the prompt's figure without looking anything up. Catching "answered without
checking" turns out to matter more on a weak model than catching "checked and
got nothing" — 18/20 → 4/20 on that case, a larger effect than the
anti-fabrication instruction achieved there.

**Its cost, priced exactly** by grading what each of the 70 firings threw away:
14 suppressed a fabrication, 23 suppressed nothing because the model had said
nothing, and **33 suppressed a clean answer**. Every one of those 33 is the
model's own refusal — *"the data warehouse is currently unavailable (no
connections in the pool). I can try running it again"* — replaced by a generic
one. A specific, actionable refusal became a vague one: a real regression in
quality, not in fidelity, and fixable by firing only when the answer asserts
something.

Two costs this measurement cannot see, recorded rather than glossed: on a strong
model the guard is **pure cost**, since Claude never fabricated in this condition
and the guard would fire, prevent nothing, and coarsen good refusals; and no case
in this suite is answerable without a query, so a question the model could
rightly answer from schema alone would be blocked and the harness would never
know.

## [0.1.28] - 2026-08-12

Define "entailed" in the README, since the project is named for it.

The word is doing real work and is not common outside logic and philosophy, so
the README leaned on a term most of its likely readers would skim past. "The
idea" now defines it — *to have as a necessary consequence* — and then does the
part a definition alone would not: shows what the negation looks like.

An answer is entailed by the data when the data leaves no room for it to be
wrong. A number the model never retrieved is not entailed **even when it is
correct**, because nothing about the process made it correct and nothing will
catch it the next time it is not. That is precisely the shape of the one failure
this suite reproduces: a figure that was true when somebody measured it,
presented as though it had just been looked up.

Stated as the distinction it is: accuracy is a property of an answer;
entailment is a property of the link between an answer and its evidence — and
that link is what a user cannot check without being shown the work.

## [0.1.27] - 2026-08-12

Documentation caught up with defect 17. No code change.

Finding the defect and correcting the *finding* were not the same job, and four
documents were still resting on the evidence it invalidated:

- **`runs/AUDIT.md` cited the vacuous field as its verification.** That entry
  offered `precondition_met: True` and `collected_results: 0` as proof the §1
  reproduction was genuine, and both read identically on every run whatever
  happened. Replaced with what the finding actually rests on: the fixture
  declares one `unavailable` response and no others, so nothing is collectable
  by construction, and the run's only served payload was the dispatch error.
- **`FAILURES.md`** said §1's precondition was "confirmed met", and separately
  still listed §1 as never triggered. Both corrected; the second is marked
  superseded rather than deleted, because the earlier statement was true when
  written.
- **`README.md`** said the case had scored 0/20 "without once firing", which
  conflated the failure not occurring with the condition not arising. It now
  says which of those it means and on what evidence.
- **`evals/README.md`** documented the NOT TRIGGERED mechanism without saying it
  had been blind for single-phase runs for its whole life.

`MEASUREMENT.md` now records that the blindness was not uniform — the field was
real for two-phase runs (160 non-zero) and always zero for single-phase ones —
which is what decides that two-phase conclusions resting on it survive and
single-phase ones do not.

The README also no longer implies the guard ablation is finished. It is running,
and it is designed to answer whether the guard fires when it *should not*, since
a deterministic guard trivially prevents what it catches.

## [0.1.26] - 2026-08-12

Measurement defect 17, the empty-collection ablation runner, and GitHub #1/#2.

**A precondition that could not fail.** `zero_collection` guards case 1 and
exists so a fixture that stopped producing the condition would report **NOT
TRIGGERED** instead of a quiet zero. It is `result.collected_results == 0`, and
that field was only ever assigned by the two-phase runner — so across 1,600
baseline runs it read `0` regardless, and the precondition reported "met" every
time for every model.

The direction differs from the other sixteen. It did not inflate the
fabrication rate; it inflated confidence that the case had been exercised, which
is worse in kind, because catching exactly that is the only thing it does.

The §1 reproduction survives on the fixture rather than the precondition:
`01-empty-collection.yaml` declares one `unavailable` response and no others, so
nothing is collectable there by construction. A cross-provider count of "claimed
successful retrieval having collected nothing" is **withdrawn** — it was
filtered on the broken field. Restricted to the cases whose fixture guarantees
no collection, the sound figures are 0 of 80 per cloud model and 1 of 65 for
`qwen3.6`, that one being the §1 reproduction itself.

**`baseline-guarded`** — the empty-collection guard as a separate runner class,
never a flag. The ablation row `MEASUREMENT.md` has carried since the beginning
is finally runnable, because a model that fails the case finally exists. Its
docstring is explicit that "does the guard prevent the failure" is close to
tautological — the guard is deterministic — and that the question worth
measuring is whether it **fires when it should not**, which the other nine cases
answer.

**GitHub #1** — the generated facts document is prose and now wraps. SQL keeps
its horizontal scroll, where a broken line reads as a different statement than
the one that ran.

**GitHub #2** — SQL and result panels are collapsed by default. Summaries are
unchanged, so the row count and any preview boundary stay visible at a glance.
Failed queries stay open: a collapsed error reads like a step that went fine.
The banner no longer says every answer "shows" its work, because it now offers
it — the tool should not overstate itself in its own header.

6 tests added (667 total).

## [0.1.25] - 2026-08-12

The four-provider run completed, and it changes what this project claims.

**The anti-fabrication instruction does not close `stale-fact` on a local
model.** Four cloud models go to 0/20; `qwen3.6` goes 18/18 → 3/15 and 19/19 →
9/16. Every surviving answer was read; all are genuine assertions and several
claim a verification that never happened. The README said the instruction closes
this on every model tested. It said so honestly on the evidence then available,
and it was wrong the moment the provider set widened.

**`FAILURES.md` §1 reproduced for the first time.** With
`collected_results: 0` and the only tool call returning a dispatch error,
`qwen3.6` answered *"I was able to pull the numbers for you. In July 2026 we
recorded 489,312 distinct sessions."* — inventing the figure and the account of
having fetched it. Four cloud models had walked through that condition 80 times
without failing.

That matters beyond one cell: **it is the first empirical justification any
structural guard in this project has had.** The empty-collection guard has been
carried as unproven since the beginning because nothing could trigger it. Its
trigger has now been observed, and the evidence came from widening the provider
set rather than from more runs of the same model — which is the argument for
provider independence stated in measurements instead of principle.

**`FAILURES.md` §2 reproduced**, also only locally: rows present in no result.

Separately, `qwen3.6` **produced no answer at all in 68 of 400 runs**, ending
its turn cleanly with empty text. No cloud model did this once.

`README.md`, `MEASUREMENT.md`, `FAILURES.md` and `runs/AUDIT.md` all updated.
The README's "one case has never been tested at all" caveat is retired, and
replaced by the finding that the case now fires.

## [0.1.24] - 2026-08-12

`CONTRIBUTING.md`, ahead of making the repository public.

Written against this project's actual norms rather than a template, because the
norms are unusual enough that a reasonable contributor would break them in good
faith and learn it from a failing test:

- the deterministic graders are a **screen, not a metric**, and a rate quoted
  straight from one has been wrong every time it mattered;
- **sixteen measurement defects, every one of which inflated the fabrication
  rate**, which is why the audit step is mandatory rather than diligent;
- a change that makes the screen flag *less* is the dangerous direction, and
  must record what it excused;
- a grader change can be re-scored against stored runs, a fixture change cannot
  — it alters what the model saw;
- guards must not be disableable by a flag, enforced over the AST;
- no performance claim the ablation table does not support.

Also documents the plugin path, which is the most likely useful contribution
and is one file, and points at `tests/test_plugins.py` as the contract.

Every factual claim in it was checked against the repository rather than
asserted: the Python floor, the `[dev]` extra, the documented install command,
the named files and symbols, and the claim that the suite runs offline in
seconds (6.0s, no key, no database, no network).

## [0.1.23] - 2026-08-12

Squashed to a single commit for publication.

Sanitising the working tree does not sanitise a repository. The private
extraction manifest was still readable in full from any earlier commit, source
repository path included, and fourteen historical versions across `DESIGN.md`,
`FAILURES.md`, that manifest and `tests/test_hygiene.py` still carried client
terms. Untracking a file removes it from the tip, not from the archive, and
anyone cloning a published repository gets the archive.

The commit messages had already been rewritten — verified safe, with tree
hashes byte-identical before and after, so only messages changed — but that
addressed the smaller half of the problem.

History is therefore replaced with one root commit. The narrative is not lost:
this file records it in more detail than the commits did, `MEASUREMENT.md` §6
holds the defect log, and `runs/AUDIT.md` holds what was re-scored and why. The
full pre-publication history, client references and all, is kept locally as a
git bundle outside the repository and is never published.

Version tags are dropped with the history they pointed at. This one is the
first tag on the published repository.

## [0.1.22] - 2026-08-12

Sanitised for publication.

The source deployment is no longer named anywhere git tracks. `DESIGN.md`
carried it twice — in the licensing note and in the "relationship to the source
system" section — and `FAILURES.md` §7 described the incident in terms that
named the product category, which implies the sector even with the client's name
removed. The incident keeps every number that made it evidence (16 credited
against 6 earned, store-wide total, 49 mentions all-time) and loses only the
detail that identified whose store it was.

**The extraction manifest is no longer tracked.** It names the source repository
and describes that system's internals, which is precisely what a public
repository should not carry. It stays on disk — it is still the record of what
was deliberately *not* taken — and every published file that cited it now reads
"the extraction plan" instead, because a public README pointing at a file nobody
can see is both a broken link and an advertisement for the thing it should not
mention.

The hygiene sweep now runs over **everything git tracks**, not the four code
directories it checked before. That gap is why the client name sat in
`DESIGN.md` for four days: documentation is the most-read part of a public
repository and was the only part never checked. Two new tests keep the private
document untracked and uncited.

`metabase` was dropped from the banned list, deliberately. It appears in
`DESIGN.md` naming a competitor beside Snowflake Cortex Analyst and Databricks
Genie; a public product in a landscape survey identifies nobody, and banning it
would have meant removing an honest competitive comparison.

**Not done, and it needs a decision:** the root commit message still reads
"Bootstrapped from analysis of <source-repo>". Publishing the repository
publishes its history. Rewriting that message rewrites all 26 commits and
re-points all 22 tags — safe while nothing is pushed, but not something to do
unasked.

10 tests added (661 total).

## [0.1.21] - 2026-08-12

Two review findings and the release cleanup. **One finding is not fixed here
because it is not mine to fix — see the licensing note below.**

**P1 — same-thread turns still raced.** Per-thread locking made each *append*
atomic and left the *turn* unprotected: a request reads the history, streams for
tens of seconds, then writes. Two overlapping turns both answered from the same
stale history and interleaved their events, leaving a transcript whose derived
`messages()` is a conversation nobody had.

A second in-flight turn on one thread is now refused with **409** rather than
queued. A conversation is sequential by nature — the second question was asked
without its author having seen the first answer, so serialising would deliver a
late answer to a question that no longer makes sense, and look like a hang while
doing it. The claim is taken as soon as the thread is known, so nothing is set
up for a request about to be rejected, and released by the stream's `finally`,
which runs even when the client disconnects.

**Disclosure grader — defect 16b.** `completeness_disclosure` could not tell
"did not state the total" from "declined to answer". An answer that presents no
list has presented nothing to mistake for everything; the definition is
presenting a sample *as a census*. It now recognises a withheld result set,
requiring both an explicit withholding phrase and the absence of anything shaped
like a data row — an answer that hedges and then lists fifty rows anyway is
still presenting them.

**Release cleanup.** `.DS_Store` and `src/entaildb.egg-info/` were tracked and
are now ignored. The reviewer's point about the metadata being stale was
concrete: the committed `PKG-INFO` recorded version `0.1.18` against a
`pyproject.toml` saying `0.1.20` — the second-source-of-truth problem the
version lookup was written to avoid, sitting in the repository.

**Licensing, unresolved and blocking publication.** the extraction plan records that
`src/fidelity/link_allowlist.py` came across "largely intact" from prior client
work, and `FAILURES.md` is derived from that system's production incidents. An
MIT licence on this repository asserts the right to publish and sublicense all
of it. That authority has not been established here, and no code change makes it
true. Nothing is published yet, so nothing is wrong — but it must be settled
before anything is.

4 tests added (655 total).

## [0.1.20] - 2026-08-12

Fixture defect 16: a schema that contradicted its own data.

`partial-results` declared `customer_id` as `uuid` while its data response
returned `C-100234`. Models noticed and refused to produce a mailing list from
data they could not reconcile — correct behaviour, and all three of
`claude-sonnet-5`'s flagged runs in that case were it.

Fixed on the **schema** side. Changing the data to real UUIDs would have been
the obvious move and the wrong one: UUIDs contain digit runs, `numbers_in`
would have added them to the allowed set, and `numeric_fabrication` would have
become more permissive. All sixteen measurement defects in this project have
inflated the fabrication rate; that would have been the first to deflate one,
which is the direction that flatters the project and the harder kind to notice.

The same column was declared `uuid` in two other cases that never return values
for it. Aligned as well, so the suite does not disagree with itself.

No re-run — the re-run of `partial-results` against the previous fixture fix is
already recorded, and it barely moved: 3/20 → 3/20 at baseline, 1/20 → 0/20
instructed. Worth noting against my own warning that those numbers were invalid;
a fixture defect is not automatically a large effect.

**Still open and named rather than quietly fixed:** `completeness_disclosure`
cannot tell "did not state the total" from "declined to answer". Same shape as
defect 10, but fixing it changes a published number.

## [0.1.19] - 2026-08-12

Four more measurement defects fixed, and the cloud sweep re-scored. All four
inflated the fabrication rate, which makes fifteen for fifteen.

- **The refusal detector added this morning was too narrow.** It caught "I
  won't repeat" and missed "not going to repeat", "isn't something I've
  confirmed" and "not something I can verify" — all of which appeared in the
  very next run. Enumerating phrasings was the mistake; it now pairs any
  negation with any assert-or-verify verb in the same sentence. The known limit
  — a single compound sentence that both refuses and asserts is excused — is
  written into the code rather than left to be discovered.
- **Quarter labels** (`Q3 2025`) counted as figures.
- **Template placeholders** counted as data: a model that emitted
  `[User 1]: [X] questions` instead of an answer had the `1` scored as a
  fabricated number. Both bracket styles are masked.
- **`partial-results` replayed one payload for every query**, so a model's own
  sanity probe came back with fifty unrelated rows and it concluded the tool
  was broken — the same defect as `stale-fact`'s fixed columns, in a second
  case. The payload is now gated on the question actually being asked, and
  anything else answers as a database would: the columns requested, no rows.

Re-scoring moved six cells. `claude-sonnet-5` `stale-fact` goes 6/20 → 4/20 at
baseline and 1/20 → **0/20** instructed; `gpt-4.1` `own-history` 10/20 → 5/20.

**The headline the sweep produced**, on the fixed fixture: the anti-fabrication
instruction drives `stale-fact` to **0/20 on all three models**, while baseline
recitation is **20/20 on both OpenAI models** and 4/20 on `claude-sonnet-5`.
Cross-provider variation of that size is the concrete form of the caveat this
README has carried since the multi-provider work began.

`partial-results` figures still predate its fixture fix and are due a re-run.

## [0.1.18] - 2026-08-12

Licence and a README opening.

MIT, in a `LICENSE` file. `pyproject.toml` had declared `license = "MIT"` since
the beginning with no licence text anywhere, which made "open-source" in the
README a claim the repository did not actually support — a poor look in a
project whose entire argument is not claiming more than the evidence carries.
The metadata now points at the file.

New opening, Jim's words: *"The prose should follow from the rows."* It states
the thesis in seven words, which is six fewer than anything here managed. The
paragraph beneath it is the sentence a reader wants first and the README did not
have.

A markdown editor had stripped 19 blank lines between paragraphs — which in
markdown merges paragraphs rather than tightening them — and inserted double
blank lines inside four list items, splitting bullets. Restored from the
committed copy rather than repaired, so the body is byte-identical to what it
was, and the new opening reapplied on top.

## [0.1.17] - 2026-08-12

Three review findings, all confirmed by reproduction before being fixed.

**P1 — SQL Server connections could execute writes (data loss).** The read-only
gate matched a *leading* keyword, so three shapes walked past it: a CTE-backed
write, `SELECT ... INTO`, and anything behind a comment. Demonstrated against a
live SQL Server through the connector:

    WITH d AS (SELECT * FROM t) DELETE FROM d OUTPUT deleted.id

That returned rows, reported success, and emptied the table. It was survivable
only on the other three products, which hold a server- or driver-level read-only
mode; SQL Server has none, so the gate stood alone there.

The blocklist is replaced by an allowlist: one statement, must parse as SELECT
(optional leading CTEs), no `INTO` and no data-modifying keyword at the top
level, with comments and string literals stripped before anything is judged.
SQL Server additionally runs every statement in a transaction that is rolled
back afterwards. **Neither is the real control** — that is a principal granted
`SELECT` and nothing else, now stated in the README where an operator reads it.

**P2 — secret ciphertext was deterministic.** The keystream was `key || nonce`,
so for any secret shorter than the 32-byte key the nonce never entered it:
identical secrets produced identical ciphertext, and only the stored random
prefix differed. Equality between secrets leaked, and known plaintext would
expose key bytes directly. Replaced with AES-GCM.

The test guarding this asserted `encrypt(x) != encrypt(x)` and passed the entire
time, because it compared whole blobs — including the random prefix — rather
than the ciphertext. It now compares the bodies, and a tampering test checks the
authentication the old scheme never had. Existing secrets stay readable and are
rewritten on first load, so no credential needs re-entering.

**P3 — concurrent saves of one conversation collided.** Every save of a thread
used the same `<id>.json.tmp`, so two overlapping writes renamed it out from
under each other; reproduced as `FileNotFoundError` from `os.replace`. The
quieter half was worse: load-append-save as three steps means the later write
discards whatever the earlier one added. Temp files are now unique per write,
and `append_event` does the whole read-modify-write inside a per-thread lock.
A stress test that lost events and raised before now retains 160 of 160.

`DESIGN.md` still said "Application not started" and is corrected, along with
the read-only, secrets and conversation sections.

32 tests added (651 total).

## [0.1.16] - 2026-08-12

Named **EntailDB**, and a documentation pass to match reality. Landed directly
on `main`.

The name is the claim: every comparable project asks whether the generated SQL
is correct; this one asks whether the answer is *entailed* by the rows the tools
returned. The library keeps the module name `fidelity` — it implements the
fidelity layer, and renaming a package that `src/` and `evals/` both import buys
nothing a docstring cannot.

A rename that nearly cost data, recorded because the near-miss is the lesson:
naming the compose project `entaildb` changed the derived volume name, so the
new container came up against a **fresh empty volume** while every connection,
credential and conversation sat in the old one. The volume is now pinned by
name, independent of the project and therefore of the checkout directory, and
the existing data was migrated explicitly rather than left to chance.

`DESIGN.md` was a day stale and had drifted furthest:

- The measurement section carried the pre-re-score numbers and no mention of
  defects 10 and 11.
- The architecture diagram showed `auth + per-user access control` as though it
  existed. **It does not**, and the diagram now says so in the box.
- Connectors were described as "Postgres first, MySQL and SQL Server next";
  four are built and they are plugins.
- Providers were "Anthropic first, then an OpenAI-compatible path"; three are
  built, and `Turn.raw` turned out to be the concrete argument for having
  budgeted a real abstraction rather than a shim.
- Pillar 1 listed four structural controls without saying which ship. Two do;
  the table now names each and why.
- Conversations were undocumented entirely.
- **"A plugin registry is still a smell" directly contradicted the registry
  built in 0.1.14.** Reversed, with the reasoning for reversing it.

`FAILURES.md` and `evals/README.md` carry the re-scored figures and the caveat
that every §8 number predates the fixture fix. the extraction plan is marked
complete and kept as the record of what was deliberately not taken.
`evals/README.md` documents `--provider` and what `regrade` is for.

## [0.1.15] - 2026-08-12

Two measurement defects fixed, and every published number re-scored. Landed
directly on `main`. **No new runs were started before the documentation caught
up**, which is the point of the entry.

**Defect 10 — a refusal counted as a recitation.** Every grader works on
numeric membership, and a model told not to recite a pasted figure frequently
names it in order to refuse it: *"I won't repeat the ~68% figure from the prompt
as fact, since I wasn't able to verify it."* That was scored as reciting the
figure it declines to use. Re-scoring stored transcripts moves `claude-sonnet-5`
`baseline-instructed` on `stale-fact` from **18/20 to 0/20** and `baseline` from
18/20 to **9/20**.

The fix is deliberately narrow. Suppression is the first change here that could
make the suite *under*-report, which is worse than the bug it fixes, so a figure
is excused only when a refusal marker appears in the same sentence and **every
excused span is recorded** on the result. Tests pin both directions: six
disclaimer phrasings are excused, four assertions still fail, and a refusal
followed by an assertion still fails.

**Defect 11 — an empty result that read as a broken tool.** `stale-fact` returns
an empty table so the model has a working warehouse and no figure. But the
fixture returned the columns `bucket`/`share` whatever was asked, so models
concluded the tool was malfunctioning and refused on those grounds — a different
behaviour, reached for a different reason. The case was measuring distrust. A
new `empty` response kind echoes the query's own aliases, as a database does.

**A correction I have to own.** Seeing 18/20 in a stored file, I reported that
the README's "drops to 0/20" claim did not survive. It did: the file was wrong,
not the claim. I treated a deterministic screen as a measurement — the exact
thing this project's own doctrine forbids, about a screen that had already been
wrong nine times.

Documentation re-scored: `README.md` now carries a per-model table and says
plainly that every `stale-fact` number predates the fixture fix; `MEASUREMENT.md`
records defects 10 and 11 and the pilot's cost and wall-clock figures;
`runs/AUDIT.md` opens with what changed and why the originals are kept.

20 tests added (619 total).

## [0.1.14] - 2026-08-12

Databases and model providers are plugins. Landed directly on `main`.

`app/connectors.py` and `app/providers.py` are now packages with one module per
driver and per adapter. Each declares its own `kind`, `label`, defaults, and —
for a database — the shape of DSN it wants and the dialect it speaks. Discovery
imports every module in the package, so dropping a file in is the whole
installation step.

The DSN shapes moved out of `config.py`, where they were a chain of
`if kind ==`: adding a database used to mean editing a file about settings that
has nothing to do with any particular database.

**The settings page builds itself from the registries** via a new `/api/kinds`.
That is what makes "drop it in" true rather than nearly true — and it was not
hypothetical: SQLite shipped in 0.1.3 and the connection form never listed it,
because the options were hard-coded HTML. It appears now without anyone adding
it.

`tests/test_plugins.py` proves the claim the only honest way: it **writes a
module into the package at run time** and asserts the new kind registers,
builds, resolves a stored connection, runs a turn through the real runner, and
reaches the settings catalogue — with no existing file edited. Asserting the
built-ins are present would say nothing about the fifth.

Both registries refuse a class with no `kind` and refuse a duplicate, since two
drivers claiming one name would make which-one-answers depend on import order.

15 tests added (599 total). No behaviour change: all connections still test OK
and the four database kinds and three providers arrive by the new route.

## [0.1.13] - 2026-08-12

OpenAI's Responses protocol, so current OpenAI models work. Landed directly on
`main`.

`gpt-5.6-terra` and its peers accept function tools only on `/v1/responses`,
which is a different shape from chat completions rather than a variant: the
system prompt is `instructions`, tools are flat, tool results return as
`function_call_output` items, and the model's own output items are replayed as
input.

- `OpenAIResponsesProvider`, selectable as **OpenAI Responses** in Settings.
- **Reasoning items round-trip.** The output carries opaque `reasoning` items
  this code cannot reconstruct; `Turn.raw` replays them verbatim. Dropping them
  would lose the model's own chain of thought between rounds of one question —
  the case that justified `raw` existing when the neutral transcript was
  designed.
- **`store` is false.** The endpoint retains conversations by default, and the
  traffic here is database schemas and query results. Leaving that to a default
  nobody chose is not acceptable for a tool that reads production data.
- Tool arguments are read complete from the terminal `response.completed`
  event, so the fragment-assembly risk the chat-completions adapter must handle
  does not arise here and is not taken on.

Verified end to end: `gpt-5.6-terra` answered a real question against the
dvdrental database with the SQL and rows shown.

15 tests added (584 total), including reasoning replay, `store: false`, and the
flat tool envelope.

## [0.1.12] - 2026-08-12

Adapt to endpoints that disagree about parameter names. Landed directly on
`main`.

Reported: adding an OpenAI profile produced

    Unsupported parameter: 'max_tokens' is not supported with this model.
    Use 'max_completion_tokens' instead.

"OpenAI-compatible" is a family, not a standard. Newer OpenAI models reject
`max_tokens`; Ollama accepts it and knows nothing of `max_completion_tokens`.
Neither can be hard-coded, and the user should not have to know which their
endpoint wants. This is exactly the `drop_params` idea noted from ardua-ai in
0.1.11 and then not implemented — the concept was recorded and the code was
not, which is its own small lesson.

- A request now adapts **once** to a parameter the endpoint explicitly names as
  unsupported: renamed where a known equivalent exists, dropped otherwise, and
  remembered per provider instance so a tool loop pays at most one rejection.
- **Only an explicit "unsupported parameter" code is adapted.** A 400 about a
  parameter's *value* is a real error and still surfaces as one; dropping it
  would send a different request than intended and answer a question nobody
  asked.
- Adaptation state is class-level and immutable, rebound per instance, so one
  endpoint's quirk cannot leak into another provider object.

That fix exposed a second, unfixable one: `gpt-5.6-terra` accepts function
tools only on OpenAI's `/v1/responses` protocol, not chat-completions. No
parameter this adapter can send satisfies it. The error now says so and names
models that do work, rather than leaving vendor JSON in the answer pane.
Verified on the same key: `gpt-4.1-mini` calls the tool and answers correctly.

6 tests added (569 total).

## [0.1.11] - 2026-08-12

Provider and model independence. Landed directly on `main`.

`FidelityRunner` no longer speaks any provider's wire format. It keeps a
neutral transcript — `Turn`, `ToolCall`, `ToolOutcome`, `ToolSpec` — and each
adapter translates. "Provider-agnostic" was previously true of the protocol
and false of the code: the runner built `tool_use_id`, Anthropic's tool-result
message shape, and `input_schema` directly.

`Turn.raw` carries an opaque provider-native echo of an assistant turn.
Anthropic requires its own content blocks be replayed verbatim — a thinking
block's signature cannot be recomputed — so rebuilding an assistant message
from its parts would quietly corrupt any conversation that used one.

- **`OpenAICompatibleProvider`**, reaching OpenAI, Ollama, OpenRouter, vLLM,
  Groq, Together, Azure, and a LiteLLM proxy. Streaming tool calls are the
  delicate part: arguments arrive as fragments of a JSON string across deltas
  and are assembled, then parsed exactly once. Parsing early does not raise —
  it runs a *different query* than the model asked for. An unparseable payload
  is reported, never repaired.
- **Model profiles.** A named model plus how to reach it, configured in the UI.
  The name is yours: "strong", "private (local)", "cheap".
- **Per-connection binding.** A database can pin its own model; unpinned ones
  use the default. This is the privacy case — a sensitive database kept on a
  local model while other connections use a frontier one. The header names the
  model that will answer, so "which model produced this?" is never a guess.
- Existing installs migrate on load: `anthropic_api_key` + `model` become a
  profile, and the legacy fields are kept so a downgrade still works.

From `~/Developer/ardua-ai` (MIT, same author) the reusable asset was
architecture, not code — its "LiteLLM component" is a compose service and a
config file. Adopted: **logical names rather than provider model ids** (ADR
0001 §2–3). Not adopted: the proxy itself, which would move model
configuration into a YAML file in a second container and contradict this
project's "no configuration files" premise. Building the OpenAI-compatible
adapter instead makes LiteLLM an *optional* deployment rather than a
dependency.

Verified end to end: the OpenAI adapter against Anthropic's own
OpenAI-compatible endpoint (tool call and answer correct), and against a local
Ollama `qwen3.6` — which queried the real Sakila database and answered "2
stores" with the SQL and rows shown, without anything leaving the machine.

**This widens what the tool can do and narrows what the project can claim.**
Every measured number in `MEASUREMENT.md` was produced against Claude. The
accuracy instruction's 20/20 → 0/20 result is a fact about Sonnet 4.5 and 5,
not about language models, and it does not transfer to a local 8B model that
the app now makes easy to use. `README.md` says so. Running the eval harness
per provider is the next piece of work.

42 tests added (563 total): transcript translation in both directions, `raw`
replay, streaming tool-call assembly from split fragments, malformed arguments
refused rather than guessed, profile resolution order, and migration.

## [0.1.10] - 2026-08-12

Survive an overload episode instead of reporting one. Landed directly on `main`.

Reported: four "overloaded" failures in a row on one connection, while another
answered instantly — which looked like the connection was at fault. Measured
against the live API at the time: `postgres_air` was failing **0 of 4** while
`AdventureWorks` managed **1 of 4**, and a minute later all three models
answered 4 of 4. The episode was API-wide and fluctuating; which connection
succeeded was timing, not cause.

What was genuinely wrong was the retry ladder. It summed to about 12 seconds,
which does not cover an episode lasting minutes, so failures reached the user
in a row.

- The ladder is now 2s, 5s, 10s, 20s, 30s — about 65 seconds of cover.
- **Every wait is announced.** A silent minute-long pause is indistinguishable
  from a hung request, and a user who cannot see a retry retries by hand, which
  adds load during exactly the wrong moment. The pane shows "Claude's API is
  busy — waiting 10s and trying again (3 of 5)."
- Notices are transport status, not model output: never collected for the link
  allowlist, never part of the answer, and never written to the transcript. A
  stored conversation records what was said, not how hard the transport worked.
- The notice is cleared the moment real output arrives, so a finished answer
  never carries a stale "waiting" line above it.

7 tests added (521 total), including proof that a notice never reaches the
answer text and that the ladder covers at least a minute.

## [0.1.9] - 2026-08-12

Provider failures are reported in words, and a transient overload is retried.
Landed directly on `main`.

Reported from use: a question against a working connection filled the answer
pane with

    APIStatusError: {'type': 'error', 'error': {'details': None, 'type':
    'overloaded_error', 'message': 'Overloaded'}, 'request_id': 'req_011...'}

The database was fine — the same connection's Test passed, and it returned
19,972 rows from `Person.Person` when driven directly. But nothing in that
message said so, so an outage at Anthropic read as a broken connection.

- Provider failures are translated into sentences that say what failed, whose
  side it is on, and what to do. `ProviderError` lives in `fidelity.runner` so
  the library stays provider-agnostic; the Anthropic adapter owns the mapping.
- Unrecognised errors keep their class and message. An error nobody anticipated
  should look unhandled rather than be flattened into a reassuring sentence.
- **Overloads are classified by the API's own error type, not the HTTP status.**
  The first attempt at this fix keyed on status and would not have caught the
  reported failure at all: the overload arrived as an error *event inside an
  otherwise successful stream*, so `status_code` was **200**. Only reproducing
  it against a mocked SSE transport showed that.
- Mid-stream overloads are now retried here, because the SDK retries HTTP
  failures and an error event on a 200 is not one. Retries stop the moment any
  output has been emitted — restarting a turn that already streamed prose would
  repeat it, which is worse than the error.
- `max_retries` on the SDK client raised from 2 to 5 for the HTTP-level case.

A loose substring check for "overloaded" was caught by its own test before it
shipped: it would have classified any error mentioning the word — including a
4xx — as a transient outage. Matching the full `overloaded_error` token instead
keeps prose from being mistaken for a status.

19 tests added (515 total), including the mid-stream-on-200 case and proof that
a retry never follows partial output.

## [0.1.8] - 2026-08-11

Conversations persist, and each belongs to a connection. Landed directly on
`main`.

Threads are stored server-side as one JSON file per conversation under the data
directory, `0600` in a `0700` directory. A left sidebar lists the conversations
for the current connection; switching database switches to that database's most
recent conversation, and a connection can hold as many as you like. New
conversations are drafts until their first message, so browsing between
connections leaves no empty threads behind.

A restored conversation is not a summary — the SQL panels, result tables and
copy buttons all come back, because replay and live streaming run through the
same `applyEvent` renderer.

**The interesting part is what this deleted.** `POST /api/chat` now takes a
thread id and a question — no history, no connection id. Both are read from the
stored thread. The client-side `reconcileHistory` guard added in 0.1.5, which
stopped one database's tool results being sent as context for a question about
another, is gone: that failure is no longer something to reject, because there
is no field a caller could express it in. A structural guarantee replaced a
runtime check rather than accumulating next to it.

Decisions worth recording:

- **Deleting a connection does not delete its conversations.** Removing a
  connection to re-add it with a corrected password is ordinary, and destroying
  every transcript held against it would be data loss. Orphaned threads stay
  readable and say they cannot be continued.
- **Timestamps carry microseconds.** Second resolution reads fine and sorts
  badly: two conversations touched in the same second ordered arbitrarily in a
  list people navigate by recency.
- **Thread ids arriving from a URL are checked against a hex pattern** before
  being joined onto a path, so `..` cannot read or overwrite outside the data
  directory.
- Tool results are still not replayed to the model — a follow-up sees prior
  prose, as before. Persistence should not quietly change what the model sees.

A scripted edit deleted `loadSettings` and the Send button's binding by taking a
slice that reached further than intended. The page still parsed, loaded, and did
nothing, because a missing function is a runtime error rather than a syntax one.
Tests now name the functions and control bindings the page must have.

72 tests added (496 total), including a store, an API suite, and path-traversal
cases for ids that arrive from a URL.

## [0.1.7] - 2026-08-11

A copy control on every result panel. Landed directly on `main`.

Each `details.trace` — SQL, result rows, query errors, and the generated facts
document — carries a copy button in its header. Rows copy as **tab-separated
values with a header row**, which is what a spreadsheet expects from the
clipboard.

Two decisions worth recording, both about not changing values on the way out:

- **Cells containing a tab, newline or quote are wrapped in double quotes with
  internal quotes doubled**, the convention Excel and Sheets parse. Unquoted,
  a single newline inside an address field shifts every column to its right —
  a paste that looks plausible and is wrong.
- **NULL is copied as the literal `NULL`,** matching what the table displays.
  An empty cell would paste more tidily but would make a NULL
  indistinguishable from an empty string. Silently altering a value between
  the screen and the clipboard is the same class of error as altering it
  between the database and the answer.

Where a result is a truncated preview, the button's tooltip says so and gives
the true match count. That stays out of the clipboard itself: a note pasted
into a spreadsheet corrupts the sheet it lands in.

Clipboard writes go through `navigator.clipboard` — `http://127.0.0.1` is a
secure context — with a textarea fallback for a host where that API is absent.
The button suppresses both the click default and its propagation, or copying
would collapse the panel being copied from.

11 tests added (427 total).

## [0.1.6] - 2026-08-11

Makes the previous fix actually reachable. Landed directly on `main`.

The 0.1.5 fix was correct and verified, and did nothing for anyone whose tab
was already open: the page carried no `Cache-Control`, and dismissing a dialog
is not a page load. A stale tab runs old code against a current server, which
is indistinguishable from a logic bug — the fix "did not work" for the only
reason a working fix ever doesn't.

- The page is served `no-store, must-revalidate`. One extra request on
  loopback, against a class of false diagnosis that costs a round trip each
  time.
- The running build is shown in the header, read from `/api/version`. There
  was previously no way — for the user or for me — to tell which build a tab
  was running.
- `VERSION` comes from installed distribution metadata, falling back to
  `pyproject.toml` when running from an uninstalled source tree. Still one
  source of truth; a test asserts the served value matches `pyproject.toml`
  and says to reinstall when it does not.

A scripted edit spliced the startup version fetch into the Settings click
handler, so the build number only appeared after opening Settings. Caught in
the browser, and there is now a test asserting that fetch runs at top level.

3 tests added (416 total).

## [0.1.5] - 2026-08-11

Fixes a silent database switch. Landed directly on `main`.

Opening Settings rebuilt the connection picker's option list, which reset the
select to its first entry. Dismissing the panel therefore changed the active
database without saying so, and the next question ran against a different
connection than the one on screen.

The second half is the one that matters. The conversation's history came along
for the ride, so turns whose tool results came from one database were sent as
context for a question about another. Observed: a thread about flight data
continued against a DVD-rental database, and the model — correctly — queried,
found no such tables, and retracted its earlier answers. It recovered, but
results from the wrong database are stale data presented as a live
measurement, which is the failure class in `FAILURES.md` §7 arriving by a
route the runner's guards cannot see.

- The picker now keeps its selection across a settings reload, falling back to
  the first connection only when the selected one is gone.
- Changing connections starts a new conversation and says so with a divider in
  the thread, rather than leaving the user to infer it from a strange answer.
- The check runs on every send, not only on the select's change event. The
  reported bug was a *programmatic* reset, which fires no change event at all,
  so a guard hanging off that event would have missed exactly this case.

Both helpers are pure functions, tested under Node against the page source.
7 tests added (413 total).

## [0.1.4] - 2026-08-11

Presentation fixes in the chat pane, all reported from real use. Landed
directly on `main`.

- **The answer rendered above the SQL that produced it,** because one prose
  element was created up front and every tool block appended below it. Events
  now render in arrival order.
- **Prose emitted before a tool call was glued to prose emitted after it,** by
  the same cause — the model's preamble and its answer concatenated into one
  string with no separator, so "Here are the 2 stores:" appeared twice in a
  row. A block now closes the current prose run.
- **Markdown was not rendered.** A model's table arrived as a wall of pipes
  directly above the same rows rendered properly from the tool result. Added a
  small renderer: no library, no build step, nothing loaded off-host. Model
  output is escaped before any transform runs, and link targets are restricted
  to http/https/relative, so a `javascript:` URL renders as text.
- **The settings dialog had no visible way out.** `showModal()` closes on Esc,
  but nothing on screen said so, and reloading to escape discarded the
  conversation. It now has a × , a Done button, and backdrop dismissal, and it
  scrolls rather than overflowing the viewport.

One bug was introduced and caught during verification rather than by the
suite: the first version reset the text accumulator inside the element-creating
helper, and `proseEl().innerHTML = md(runText)` evaluates its left side first,
so every answer silently lost its opening delta. The stream renderer now takes
its document as an argument, which is what let it be tested outside a browser.

26 tests added (406 total). The JavaScript is extracted from the page and run
under Node, so there is no second copy to drift; they skip when Node is absent.

## [0.1.3] - 2026-08-11

The application: a chat interface that queries the configured databases, in
Docker, on loopback. Landed directly on `main`.

`FidelityRunner` (`src/fidelity/runner.py`) is the shipping tool loop — the
accuracy instruction as a constructor default rather than a caller's
responsibility, and the link allowlist filtering the stream. Two-phase
collection is deliberately absent: it moved nothing across four measured
configurations and doubles API calls per turn. Per `DESIGN.md`, it stays a
separate runner class rather than becoming a flag on this one.

`app/` is FastAPI plus a settings page and an SSE chat view, with connection
details and API keys stored on disk rather than in environment variables.
SQL is shown inline with the rows it returned.

Fixed, all surfaced by running real questions against real databases:

- **Double row limit.** `query()` appended the dialect's limit to whatever SQL
  it was handed, so a model writing its own `LIMIT 5` — most of them — got
  `LIMIT 5 LIMIT 51` and a syntax error. It now executes the statement verbatim
  and bounds the cursor with `fetchmany(N+1)`, which needs no dialect
  involvement at all. Wrapping in a subquery would have traded this for a worse
  bug: SQL Server does not permit `WITH` inside a derived table.
- **No dialect in the prompt.** Asked a question against SQL Server, a model
  opened with `table_schema = 'public'`, got zero rows, and had to recover. Each
  dialect now carries a `prompt_note` describing its own syntax — on the dialect,
  because that is already the one place that knows it.
- **Unknown connection kinds fell through** to the SQL Server dialect and DSN
  shape instead of being refused.
- **An exact total that cannot be counted** is now reported as unknown, with the
  model told not to present the preview as the complete set. It previously
  reported the preview size, which is the fabrication this project exists to
  prevent, produced by the code rather than the model.

Added SQLite as a connection kind — a real feature for file databases, and what
lets the connector be tested against a genuine engine with no container.

65 tests added (380 total) covering the runner, connectors, and settings store.
The connector tests run against a real SQLite database rather than fakes: the
0.1.2 MySQL NULL defect passed 300 fake-backed tests because the fakes did not
model NULL. The hygiene sweep now covers `app/`, which it did not.

## [0.1.2] - 2026-08-11

Three more dialects, verified against five sample databases across four
products. Landed directly on `main`.

**MySQL/MariaDB** (Sakila) and **SQL Server** (AdventureWorksDW star schema;
AdventureWorks OLTP, 71 tables across 6 schemas). The MySQL dialect was written
once and correct on first run — evidence the Protocol survives a change of
product. SQL Server exercised the parts Postgres and MySQL both agreed on and
therefore never tested: `[bracket]` quoting, and `TOP (n)` as a statement prefix
rather than a trailing `LIMIT`.

Each product found a defect the others structurally could not:

- **MySQL** — the bounded-`DISTINCT` optimisation from 0.1.1 counted NULL as a
  value, inflating every nullable column and reclassifying constants as
  enumerations. All 300 tests passed throughout; the fakes did not model NULL.
- **SQL Server (DW)** — join inference paired incompatible types; percentages
  rounded to "100% NULL" on a column that was not all-NULL.
- **SQL Server (OLTP)** — the join-probe budget bound at realistic width and
  was spent in arbitrary order, producing a **wrong** `join_miss` fact rather
  than a missing one. Probes are now ordered by name affinity, budget raised to
  600; the wrong fact disappeared and a genuine relationship replaced it.

`type_family()` added to the Dialect Protocol, so an incompatible join is never
emitted — it fails the statement on SQL Server and aborts the transaction on
Postgres. `render(max_per_section=)` bounds a document for prompt budgets and
always states what it omitted. The no-writes test now inspects string literals
via AST rather than grepping raw text, which was firing on its own docs.

## [0.1.1] - 2026-08-11

Re-scope, the schema profiler, and 1,000 measured evaluation runs.

**Scope.** Re-scoped from a library-only project to a self-hosted analytics chat
application plus a separable `fidelity` library. `DESIGN.md` rewritten; two
former non-goals (auth/user management, "not a framework") superseded and marked
as such, with a security posture section for the credential storage that
entails.

**Schema profiler** (`src/fidelity/profiler/`) — pillar 2. Dialect-agnostic,
read-only, driver-free. Rediscovers all five facts `DESIGN.md` cites as having
been found by hand after production incidents, from data alone, in an end-to-end
test against SQLite. Adds join inference by value overlap, for keys whose column
name has nothing in common with the target table — the shape the motivating
incident had. Postgres dialect written and unit-tested but **not yet run against
a live server**.

**Postgres, live-verified.** The dialect ran against PostgreSQL 17 on
`dvdrental` and `postgres_air` (78M rows, 77 columns, 67s). The live run found
four defects that unit tests and a SQLite fixture had both missed: `min(boolean)`
does not exist, soft-key inference assumed every target key was named `id`, the
selectivity thresholds produced 50 facts for a 15-table database, and exact
`count(DISTINCT)` was 78% of runtime for a number no derivation reads.

**Eval harness.** `TwoPhaseRunner` (unguarded, for the ablation); a realistic
`domain` prompt; multi-turn fixture history; prompt caching with hit counters;
`unavailable` and `silent` response kinds; and a precondition mechanism that
prints NOT TRIGGERED rather than a rate when a case never fires its own
condition.

**Measurement.** Of eight catalogued failure modes, one reproduces. Corrections
recorded rather than quietly dropped:

- `MEASUREMENT.md` case 1 specified a condition ("all tools return empty") that
  is not the failure it derives from ("zero tool results collected"). The
  empty-collection guard had never been triggered by any fixture. Rewritten; the
  condition now reproduces 80/80 and the failure 0/80.
- `stale-fact` survived four structural interventions unchanged and was closed
  completely by instruction. `DESIGN.md`'s "prefer structural impossibility to
  instruction" is split accordingly: structure where the model *produces*
  something it was never given, instruction where it *trusts* something it was
  given.
- A facts document carrying magnitudes was itself cited as an answer. Counts
  moved into machine-readable evidence that never reaches a prompt.

Order of work inverted: schema profiler before the fidelity runner.

## [0.1.0] - 2026-08-10

- Baseline entry. Repo contains the four design documents, the ported
  `link_allowlist` stream filter, and the eval harness (`evals/`) with the
  eight cases from `MEASUREMENT.md` §2.
- First measured baseline: 320 runs on `claude-sonnet-5` across `baseline` and
  `baseline-instructed`, every flagged run hand-audited. Results and caveats in
  `runs/AUDIT.md`.
- No fidelity runner yet. No guard has yet earned a place in the ablation
  table, and the README carries no performance claim.
