# EntailDB — design

Status: **library and profiler built and verified against five live databases;
the application runs, with conversations, four database drivers and three model
providers. No authentication — loopback only.** Revised
2026-08-11 after four measured evaluation runs. The previous revision specified
a library only; this one adds the application that consumes it. Where the
measurement contradicted the original design, this document says so rather than
quietly dropping the claim.

Read `FAILURES.md` for the evidence base, `MEASUREMENT.md` for how the claims
are tested and what testing has shown so far, and the extraction plan for what came
from the source repository.

Target org: **`ardua-inc`**. Derived from prior work product owned by the same
party, so publication is unencumbered. Not yet public — see `runs/AUDIT.md`
before publishing any performance claim.

---

## What this is

**EntailDB.** The name is the claim: an answer should be *entailed* by what the
tools returned. Every competing project asks whether the SQL is correct; this
one asks whether the prose follows from the rows.

Two artifacts, deliberately separable. The library keeps the module name
`fidelity` — it implements the fidelity layer, and renaming a package that
`src/` and `evals/` both import buys nothing a docstring cannot.

**`fidelity` — a library.** Output-fidelity guards for any tool-using
assistant. Provider-agnostic and dialect-agnostic by construction: the guards
operate on the boundary between what tools returned and what the model said,
which is the same boundary whether the database is Postgres or MongoDB and the
model is Claude or GPT.

**The application — a self-hosted analytics chat tool.** A chat window over one
or more databases, run by a single organisation for its own staff. Users
configure database connections and AI-provider credentials through the app's own
settings UI; there is no `.env` step for an operator to get wrong.

The application depends on the library. The library never depends on the
application. That boundary is load-bearing and is enforced by tests: nothing
under `src/` reads the environment, and nothing under `src/` knows what a
database connection or a user account is.

## The thesis

Every open-source natural-language-to-SQL project optimises the same thing:
**is the generated SQL correct?** Vanna, WrenAI, Dataherald, LangChain's SQL
agent, LlamaIndex's `NLSQLTableQueryEngine`, Defog/sqlcoder — plus the
vendor-locked options (Snowflake Cortex Analyst, Databricks Genie, Metabase's
AI). They compete on schema RAG, few-shot retrieval, semantic layers and
fine-tunes, and they benchmark on Spider and BIRD.

Almost none of the production incidents in the source system were bad SQL. They
were **output fidelity** failures — the prose the user read was not entailed by
what the tools returned. Spider and BIRD structurally cannot measure that: they
score the SQL and discard the prose.

Building another NL-to-SQL chat app is not, by itself, interesting. Building
one that does not lie to you is. The connectors and provider shims are
commodity integration work; the fidelity layer is the asset.

## What the measurement actually showed

`MEASUREMENT.md` §5 has the detail. The short version, because it constrains
everything below:

Across 800 measured runs on two models, three prompts and two runner shapes,
**one** of the eight catalogued failure modes reproduced: `stale-fact`, where a
figure pasted into the system prompt is recited as a current measurement. It
reproduces at 20/20 on `claude-sonnet-4-5` and 9/20 on `claude-sonnet-5`, and
is closed completely by an explicit anti-fabrication instruction (0/20 on both).

**Those numbers were re-scored on 2026-08-12 and the earlier ones were wrong.**
A refusal that names a figure in order to decline it — *"I won't repeat the ~68%
figure from the prompt as fact"* — was being counted as a recitation of it. That
was the tenth measurement defect in this project, and like the nine before it,
it inflated the fabrication rate. An eleventh, found the same day, is worse: the
`stale-fact` fixture returned column names the query never asked for, so models
concluded the *tool* was broken and refused on those grounds. The case was
measuring distrust rather than temptation. A cross-provider run against the
fixed fixture is the first measurement of this case that means what it says.

A second case, `preview-extension`, fires only against a realistic domain prompt
and only in its mildest form — the model rounds `91427.60` to `$91,428`. Real
against the definition; not the invented-rows failure the case derives from.

Everything else measured 0/20. That is **not** evidence the guards are
unnecessary — N=20 cannot observe an event at the ~0.5% rate the link allowlist
actually caught in production, and the empty-collection condition has never been
successfully reproduced by any fixture. But it does mean no structural guard has
yet earned its place, and the README must not claim otherwise.

Two consequences for this design:

1. **Pillar 2 is promoted.** It is the only pillar with a reproduced failure
   behind it, and it is independently required by the application.
2. **Pillar 1 ships unproven, and is labelled as such**, on the strength of
   `FAILURES.md`'s production evidence rather than this suite's.

## Architecture

```
┌─ application (self-hosted, single user today) ────────────────────┐
│  chat UI · conversations · settings UI (connections, providers)   │
│  NO AUTH — loopback only. See "Configuration and credentials".    │
│  ┌─ connectors/ (registry) ──┐  ┌─ providers/ (registry) ──────┐  │
│  │ postgres · mysql ·        │  │ anthropic · openai-chat ·    │  │
│  │ sqlserver · sqlite        │  │ openai-responses             │  │
│  └───────────────────────────┘  └──────────────────────────────┘  │
└───────────────────────────┬───────────────────────────────────────┘
                            │ depends on
┌─ fidelity (library, pip-installable) ─────────────────────────────┐
│  runner + guards  ·  schema profiler  ·  neutral transcript       │
└───────────────────────────────────────────────────────────────────┘
```

Both boxes marked *registry* are directories of self-contained modules,
discovered at import. Adding a database or a provider is one file dropped in;
no existing file is edited, and the settings page builds its own dropdowns from
what is registered. `tests/test_plugins.py` holds that claim by writing a module
at run time and asserting it becomes fully usable.

The library sees an abstract tool interface and an abstract model client. It
never sees a connection string, a credential, or a user.

## The three pillars (library)

### 1. Fidelity runner — structural guarantees

Controls that hold regardless of prompt quality.

- **Two-phase loop.** Phase 1 collects data with tools. Phase 2 answers with
  `tools=[]` and only the collected results in context.
- **Empty-collection guard.** If Phase 1 collected *zero tool results*, skip
  Phase 2 entirely and return an explicit "couldn't gather data". Note the
  precise trigger: zero results *collected*, not tools returning empty rows.
  Those are different conditions and conflating them is what made this control
  untestable for four runs — see `MEASUREMENT.md` §2.
- **Link allowlist.** Per-turn allowlist built from URLs appearing in tool
  results; markdown links to anything else are stripped from the stream.
  Built and tested; the only pillar-1 component that exists.
- **Preview enforcement.** When a tool returns a bounded preview, the model may
  reproduce those rows and nothing else — no extension, reordering, or
  reformatting. Rounding counts, and is the one thing this suite has caught it
  doing.

**What actually ships** (`src/fidelity/runner.py`) is narrower than this list,
and the gap is deliberate:

| control | shipped? | why |
|---|---|---|
| anti-fabrication instruction | **yes** | the only intervention with a measured effect |
| link allowlist | **yes** | production telemetry; cheap; already built |
| two-phase loop | no | moved nothing across four measured configurations, and doubles API calls per turn |
| empty-collection guard | no | its trigger has never been reproduced by a fixture, so removing it would fail no test |
| preview enforcement | no | it grades a finished answer rather than filtering a stream; not built |

Two-phase and the empty-collection guard exist only in `evals/`, as separate
runner classes. Per the design principle below they are never a flag on the
shipping runner.

**Status: the structural pillar remains unproven by this project's own
measurement.** Kept in the design because production telemetry supports the link
allowlist, because prompt drift and model churn are real (a model change moved
one measured rate by 47 points), and because the eval is structurally blind to
the tail these controls target. The README must not claim otherwise, and does
not.

### 2. Schema profiler — the pillar with evidence

Auto-derives the mechanically discoverable half of "tribal knowledge", so it
never has to be written by hand and can never go stale.

Null rates, join hit rates, zero-count enum values, cardinality, format regex
inference, value distributions. Emits a generated facts document, refreshed on
a schedule, never hand-edited.

This addresses the one failure mode the evaluation actually reproduced —
`stale-fact`, a *pasted* statistic recited as current — but by a narrower
mechanism than "generated facts win". **Measured:** adding a generated facts
block to the prompt does *not* stop the model reciting a pasted statistic
sitting beside it (19/20, against 20/20 without). What the profiler does is
remove the reason anyone hand-writes the statistic at all, and this eval cannot
measure an absence of authorship.

**A hazard found by measuring:** a facts document carrying magnitudes becomes a
fresh source of prompt-embedded figures, and was cited as an answer in 2/20 runs
("`service_requests` contains 120 rows"). Statements now carry reliability rates
but no counts; magnitudes live in machine-readable evidence that never reaches a
prompt. See `runs/AUDIT.md`.

**Built and live-verified, 2026-08-11.** `src/fidelity/profiler/`, 53 tests.
Rediscovers all five examples below from data alone, and runs against
PostgreSQL 17 on `dvdrental` and `postgres_air` (78M rows, 77 columns, 67s).

**Four products verified**: SQLite, PostgreSQL 17 (dvdrental, postgres_air),
MySQL 8.4 (Sakila), and SQL Server 2025 (AdventureWorksDW, a 31-table star
schema). Each new product found something the previous ones could not, which is
the argument for testing across products rather than across schemas:

| Product | What only it found |
|---|---|
| Postgres | `min(boolean)` has no aggregate; PKs are not all named `id`; thresholds tuned on a fixture yield 50 facts for 15 tables |
| MySQL | the bounded-`DISTINCT` optimisation counted NULL as a value, inflating every nullable column |
| SQL Server (star schema) | join inference paired incompatible types (`nvarchar` id against integer keys); percentages rounding to "100% NULL" on a column that is not all-NULL |
| SQL Server (OLTP, 71 tables, 6 schemas) | the join-probe budget bound at realistic width, and spent itself in arbitrary order — a truncated search produced a *wrong* `join_miss` fact, not merely a missing one |

SQL Server also exercised the parts of the Protocol the other two agreed on and
therefore never tested — `[bracket]` quoting and `TOP (n)` as a *prefix* rather
than a trailing `LIMIT`. `limit()` is a Protocol method rather than a format
string precisely so that fits. The strongest
evidence is that dvdrental and Sakila are ports of the same sample data, and
the profiler independently derives the same constants and the same shape
conventions from each through different dialects.

The live runs are worth their own note, because unit tests had passed and the
Postgres dialect still failed on its first real query. Three defects only a real server
found: `min(boolean)` does not exist in Postgres (SQLite accepts it silently);
soft-key inference assumed every target primary key was named `id` (real
schemas use `store_id`, `film_id`); and the selectivity thresholds — tuned on a
fixture built to yield exactly five facts — produced **50 facts for a 15-table
sample database**. A fourth was performance: exact `count(DISTINCT)` was 78% of
runtime and no derivation reads the exact value, so it is now counted only up
to the enumeration cap (135s → 67s, identical output).

That optimisation then introduced another defect, caught by reading MySQL
output rather than by any test: `count(DISTINCT c)` excludes NULL, `SELECT DISTINCT c`
returns it as a row. The rewrite inflated every nullable column's distinct
count by one and silently reclassified constant columns as enumerated. The test
fakes did not model NULL, so they all still passed. **An optimisation that looks
equivalent is a behaviour change until something real disagrees with it.**

Two design choices worth recording:

- **Selectivity is the product.** Only surprising facts are emitted. A profiler
  that reported every measured statistic would replace an 832-line hand-written
  prompt with a longer generated one and solve nothing. A clean database
  produces a document that says so in one line.
- **Name-based join inference finds only the easy half.** The motivating
  incident — `clientid` resolving against a *customer* table — has no name in
  common with its target, so a second pass measures value overlap and reports
  such keys as "nothing in the schema records it". That distinction is carried
  into the fact text, because a discovered relationship is a stronger claim
  than a guessed one.

It is also **required by the application regardless**. You cannot point a chat
tool at an arbitrary customer database and answer questions well without
deriving what its columns actually contain. The same probes serve both purposes,
which is why this is the next thing built.

Measured against the source system's 832-line hand-written prompt: roughly 55%
was genuine data semantics, and about 40% of *that* is profiler-derivable —
facts like a 75%-NULL abandoned column, a soft key resolving 29% of the time, a
`status` column NULL on every row, and a table with zero rows despite existing
in the vendor schema. All were discovered by hand, after incidents.

### 3. Annotation file — the irreducible residue

The semantic half no profiler can reach, kept deliberately small and
hand-written: which of two time columns is UTC and which is local wall-clock;
that a column named `amount` holds quantities; that the same column name means
different things in two tables.

Projected landing point: ~80 lines, down from 832.

## The application

### Connectors

One self-contained module per DBMS in `app/connectors/`, each declaring its own
kind, label, default port, DSN shape, dialect, and how to open a read-only
connection. **Built: PostgreSQL, MySQL/MariaDB, SQL Server, SQLite**, all
verified against live databases (`MEASUREMENT.md` §6). Document stores remain
later-and-only-if the profiler generalises, which is still not established.

Everything database-agnostic — the read-only statement gate, the bounded
preview, the exact-total rule — stays in the base class. A driver that
reimplemented those would be free to get them subtly wrong, and "wrong in a way
that still returns rows" is the failure this project exists to prevent.

Adapters return bounded previews with a true total row count — the shape
`preview-extension` and `partial-results` depend on. That is a library
requirement leaking into the connector contract, deliberately. Where the exact
total cannot be obtained, the result says so rather than reporting the preview
size, which would be the project's own headline failure emitted by its own code.

### Providers

**Built: Anthropic (native), OpenAI chat-completions, OpenAI Responses.** The
second reaches Ollama, OpenRouter, vLLM, Groq and a LiteLLM proxy as well as
OpenAI up to gpt-4.1/gpt-4o; the third is required for gpt-5 and later, which do
not accept function tools over chat completions.

The budget for a real abstraction rather than a shim was the right call, and the
proof is `Turn.raw`. Anthropic requires its own content blocks be replayed
verbatim — a thinking block's signature cannot be recomputed — and OpenAI's
Responses protocol replays opaque `reasoning` items for the same reason. A
transcript that flattened either to text would silently corrupt a conversation.
`raw` is tagged with the provider that wrote it, because replaying one
provider's blocks into another's API is not hypothetical: it failed 40 of 50
runs in the first cross-provider pilot.

"OpenAI-compatible" is a family, not a standard. Newer OpenAI models reject
`max_tokens` and demand `max_completion_tokens`; Ollama accepts the former and
knows nothing of the latter. A request adapts once to whatever parameter the
endpoint names as unsupported, and only to an explicit *unsupported parameter*
code — a 400 about a parameter's *value* is a real error and still surfaces.

**Each supported provider gets its own fidelity run before it is advertised.**
That run is what `MEASUREMENT.md` §6 is currently accumulating; until it lands,
the README says the fidelity numbers are Claude's and do not transfer.

### Configuration and credentials

Database connections and provider credentials are entered through the settings
UI and stored on disk. **This was an explicit non-goal in the previous revision
and is now in scope**, which means the project has taken on a security surface
it previously declined. Current state, stated plainly because the gap between
this list and what exists is the main thing standing between the tool and a
second user:

- **No authentication at all.** The container publishes to `127.0.0.1` only, and
  that binding is the entire access control. Anyone who reaches the port can
  query every configured database and read every stored conversation. This is
  the first thing to build before the app goes anywhere but localhost.
- Secrets are encrypted at rest with **AES-GCM**, keyed from a `0600` keyfile.
  What preceded it was a keyed XOR whose keystream was `key || nonce`, so a
  secret shorter than the 32-byte key never mixed the nonce in: identical
  secrets produced identical ciphertext, and a known plaintext would have handed
  over key bytes directly. The test guarding it compared whole blobs and passed
  throughout, because it was measuring the random prefix rather than the
  ciphertext. Old values are still readable and are rewritten on first load.
- Conversations are stored as `0600` files and contain query results — real
  data from the connected databases — in plain text.
- Per-user access control over which connections a user may query: **not built**,
  and meaningless before authentication exists.
- **Connection strings are an SSRF surface.** A user who can type a host can
  point the server at anything the server can reach. Allowlist or explicitly
  accept the risk; do not leave it undecided.
- Query execution is read-only at the connection level, not by prompt
  instruction.
- Credentials never enter a prompt, a tool result, or a log.

`FAILURES.md` records that the source system's `BYPASS_AUTH` flag sat enabled on
a staging host sharing production ERP credentials for months. That is the
failure mode this section exists to avoid repeating.

### Conversations

Stored server-side, one JSON file per conversation, each **bound to exactly one
connection**. The binding is not bookkeeping: before it existed, the browser held
the history and the active database was separate UI state, so a settings dialog
that quietly reset the connection picker sent one database's tool results as
context for a question about another. The first fix was a runtime check in the
client. The second was to make the state unrepresentable — `POST /api/chat`
takes a thread id and a question, with no field for a caller to put history in.

That is the design principle below applied to the application rather than the
model: prefer structure where something is *produced* that was never given.

Tool results are stored, so a restored conversation can still be checked against
its rows, but they are **not** replayed to the model — a follow-up sees prior
prose, exactly as before persistence existed. Persistence should not quietly
change what the model sees.

Concurrent writes to one conversation are serialised under a per-thread lock,
and each event is appended by re-reading inside that lock. Load-append-save as
three separate steps loses events when two turns overlap — the later write
discards what the earlier added — and the temp file used for the atomic rename
was shared per thread, so concurrent saves renamed it out from under each other.

Deleting a connection does not delete its conversations. Removing a connection to
re-add it with a corrected password is ordinary, and destroying every transcript
held against it would be data loss; orphaned threads stay readable and say why
they cannot be continued.

## Non-goals

Revised. Two of the previous entries no longer hold and are marked.

- **No semantic metric layer.** WrenAI and dbt's semantic layer do this well;
  building one is a multi-year product on someone else's strongest ground.
- **No SQL generation improvements.** Not our axis.
- **No business-domain features.** Purchasing, vendor imports, PO translation —
  none of it comes across.
- ~~No auth, sessions, or user management.~~ **Now in scope**, as a consequence
  of self-hosted multi-user deployment. See the security section above.
- ~~Not a framework.~~ **Superseded**: the library stays small and composable,
  but there is now an application around it. The library must remain usable
  without the application.
- ~~A plugin registry is a smell.~~ **Reversed 2026-08-12.** Two registries now
  exist, for databases and for model providers, and they earned it: the DSN
  shapes had accumulated in `config.py` as a chain of `if kind ==`, so adding a
  database meant editing a file about settings, and the settings page's
  hard-coded dropdowns had already gone stale — SQLite shipped and the
  connection form never listed it. The smell the original entry feared is a
  registry that exists before it has users; this one exists because four
  databases and three providers already did.

## Design principles

**Guards must not be disableable by a convenience flag.** Learned from the
source system's `BYPASS_AUTH`. If a guard needs to be off for testing, the test
injects a different runner — it does not set a flag production can also read.
Enforced over the AST in `tests/test_hygiene.py`.

**A guard that cannot be measured is a guard nobody will keep.** Every control
emits telemetry on whether it fired, and every control has an eval case that
fails when it is removed. Where a control has no such case — the empty-collection
guard, today — that must be stated wherever the control is claimed.

**Prefer structure where the model *produces* something it was never given;
prefer instruction where it *trusts* something it was given.**

The original form of this principle was unqualified — "prefer structural
impossibility to instruction" — and the measurement forced the split. Both
halves have evidence:

- *Producing.* The source prompt asked the model not to fabricate download URLs
  in four separate places and it kept doing it; a 211-line stream filter did
  more than all of it combined. A fabricated URL is generative, and a filter can
  make it unreachable.
- *Trusting.* `stale-fact` — reciting a figure pasted into the prompt — survived
  four structural interventions unchanged (generated facts in the prompt, the
  same facts behind a tool the model called in 20/20 runs, a realistic domain
  prompt, and a two-phase runner). A paragraph of instruction took it from 20/20
  to 0/20. The figure is legitimately in context; no structural control can
  distinguish "quote the prompt" from "quote the tool result" without being told
  which sources are authoritative.

Applying the wrong half wastes effort in a way that is invisible until measured:
four attempts at structure here moved nothing. See `runs/AUDIT.md`.

**The measurement apparatus fails more often than the system under test.** Seven
grader defects and two fixture defects across four runs, every one inflating the
fabrication rate. Read raw answers before believing any cell.

## Relationship to the source system

The source system keeps running its own code unchanged. This is a deliberate
fork, not an extraction-in-place. The source is a private retail analytics
deployment and is deliberately not named here; the private extraction manifest
records the specifics.

**Eventual adoption remains the intent** — the source system should one day
depend on the published `fidelity` package — which is why the library/application
boundary is enforced rather than conventional. Two consequences:

1. Nothing in the library may assume any particular schema, dialect, provider,
   or deployment.
2. The library must stay embeddable in the source system's existing
   `_stream_tool_use_loop` shape without a rewrite of the calling code.

The application has no such obligation and may diverge freely.
