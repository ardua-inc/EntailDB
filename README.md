# EntailDB

*The prose should follow from the rows.*

EntailDB is an open-source natural-language interface to your databases that
shows its work and tries not to make claims the data doesn't support. It is a
self-hosted chat tool, and the output-fidelity library underneath it.

The name is the claim. Every comparable project asks whether the generated SQL
is correct; this one asks whether the answer you read is **entailed** by the
rows the tools actually returned.

**Status: library and profiler built and verified against live databases; the
application runs locally.** Not published, and not fit to expose — see
*Security* below.

## The idea

Every open-source natural-language-to-SQL project optimises whether the
generated SQL is *correct*. Almost none of the production incidents behind this
project were bad SQL. They were failures of **output fidelity**: the prose the
user read was not entailed by what the tools actually returned — invented row
counts, a twenty-row product table conjured from a single number, a fabricated
download link, a stale statistic recited as a live measurement.

Spider and BIRD cannot measure that. They score the SQL and discard the prose.

Building another NL-to-SQL chat app is not interesting on its own. Building one
that doesn't lie to you might be. The connectors and provider shims are
commodity; the fidelity layer is the asset.

## What's here

| Path | |
|---|---|
| `src/fidelity/` | the library — provider- and dialect-agnostic guards |
| `src/fidelity/profiler/` | the schema profiler — measures a database, writes facts |
| `app/` | the application — FastAPI, SSE chat, settings page, conversations |
| `app/connectors/` | one module per database; drop one in to add another |
| `app/providers/` | one module per model provider; same |
| `evals/` | the controlled evaluation: 8 cases, each traced to a real incident |
| `runs/` | measured results, raw records, and the hand audit |
| `DESIGN.md` | the spec — architecture, pillars, non-goals, security posture |
| `FAILURES.md` | the evidence base: 8 real production failures |
| `MEASUREMENT.md` | how claims get tested, and what testing has shown |
| `CONTRIBUTING.md` | how to work on this, and the norms the tests enforce |
| `LICENSE` | MIT |

Built so far: the link allowlist (a streaming filter that strips URLs a tool
never returned), the eval harness, the schema profiler, the runner, and the
application. Two-phase collection exists only in `evals/` — it moved nothing
across four measured configurations and costs an extra API call per turn, so it
is not wired into the shipping runner.

## What has actually been measured

**Read `runs/AUDIT.md` before quoting any number from this repo.**

800 runs across two models, three prompts and two runner shapes, plus a
cross-provider pilot at N=5. Of eight catalogued failure modes, the suite
reproduces **one**: a figure pasted into the system prompt, recited as a current
measurement.

| model | plain prompt | + anti-fabrication instruction |
|---|---:|---:|
| `claude-sonnet-4-5` | 20/20 | **0/20** |
| `claude-sonnet-5` | 9/20 | **0/20** |
| `gpt-4.1` | 5/5 | **0/5** |
| `gpt-5.6-terra` | 5/5 | **0/5** |

Everything else measured 0/20 on Claude. The OpenAI rows are pilot-scale (N=5)
reconnaissance, not a publishable rate, and the local-model column is not run
yet.

The `claude-sonnet-5` plain-prompt figure was **18/20 until the grader was
fixed**. A model told not to recite a pasted figure frequently names it in order
to refuse it — *"I won't repeat the ~68% figure from the prompt as fact"* — and
numeric membership scored that as the recitation it was declining to make. That
was the tenth measurement defect found in this project and, like the nine before
it, it inflated the fabrication rate. `MEASUREMENT.md` §6 has the list.

Three things that does *not* establish, stated plainly because they are easy to
elide:

- **It is not proof the guards are unnecessary.** N=20 cannot observe an event
  at the ~0.5% rate the link allowlist actually caught in production. A 0/20
  carries a 95% upper bound of 16%.
- **One case has never been tested at all.** The empty-collection guard's
  trigger condition — *zero tool results collected* — has never been produced by
  any fixture, because every fixture so far produces *tools returning empty
  rows*, which is a different thing. It has scored 0/20 six times without once
  firing.
- **One model in the incident window is unmeasured.**
- **The one reproduced case was, until now, measuring the wrong thing.** Its
  fixture returned fixed column names whatever was queried, so models concluded
  the *tool* was broken and refused on those grounds rather than facing the
  choice the case exists to present. The fixture now echoes the query's own
  aliases, as a database does, and **every number in the table above predates
  that fix** — they are due a re-run.
- **Nothing measured here transfers to another provider.** Every number in this
  repository was produced against Claude — Sonnet 4.5 and 5. The accuracy
  instruction's 20/20 → 0/20 result is a fact about those models, not about
  language models. A local 8B model may fabricate far more freely, and the tool
  now makes it easy to run one. Widening what this can do narrows what this can
  claim, and running the harness per provider is the work that would fix that.

Nine measurement defects were found and fixed across four runs — seven in the
graders, two in the fixtures — and **every one inflated the fabrication rate**,
which is the direction that would have flattered this project. Two were
invisible in every flagged result and obvious in the first paragraph of an
unflagged one.

## Where that leaves the design

The one reproduced failure is the one no model improvement can fix: the model is
misinformed by its own prompt and has no way to check. That is the argument for
the **schema profiler** — generated facts regenerate; pasted facts rot — which
is also what the application needs before it can answer questions against an
unfamiliar database.

The profiler is built, and has been run against five databases across four
products: two PostgreSQL (one of 78M rows), one MySQL, and two SQL Server
schemas including a 71-table OLTP database. Each product surfaced exactly one
defect that no amount of unit testing had caught — a boolean aggregate
PostgreSQL does not have, a `count(DISTINCT)` that silently excludes NULL, a
join inference pairing incompatible types, and a probe budget spent in
arbitrary order that produced a *wrong* fact rather than a missing one. Those
are recorded in `MEASUREMENT.md`.

Worth stating plainly: **generated facts did not stop the one failure this
suite reproduces.** Supplying them as prompt text scored 19/20, and as a tool
the model actually called, 19/20. Only the instruction moved it, to 0/20. The
profiler earns its place by letting the app answer questions about a database
nobody has hand-documented — not as a fabrication guard, and it ships alongside
the instruction rather than instead of it.

The structural guards ship unproven by this project's own suite, on the strength
of production telemetry rather than measurement, and the documentation says so
wherever they are claimed.

**There is no performance claim in this README, and there will not be one until
the ablation table supports it.**

## Running the application

```bash
docker compose up -d --build
```

Then open <http://127.0.0.1:8080>. Add a model and at least one database
connection on the settings page; there is no `.env` file and no environment
configuration to edit. PostgreSQL, MySQL, SQL Server, and SQLite are supported.

**Models are configured, not compiled in.** A model is a named profile — call
it what you like — pointing at either Anthropic or any OpenAI-compatible
endpoint: OpenAI, Ollama, OpenRouter, vLLM, or a LiteLLM proxy if you want
central routing. Each database can pin its own model, and anything unpinned
uses the default, so a sensitive database can be kept on a local model whose
schema and rows never leave the machine while other connections use a frontier
model. The header always names the model that will answer. Connections to databases running on the host are reachable at
`host.docker.internal:<port>`, the same port any desktop client would use.

"Profile" on a connection runs the schema profiler and attaches the generated
facts to that connection's system prompt.

Conversations are kept. Each belongs to one connection and is listed in the
left sidebar; switching database switches to that database's most recent
conversation, and a connection can hold as many as you like. This is also what
makes it impossible for one database's results to be replayed as context for a
question about another — a conversation names its connection, and the browser
cannot supply history at all.

### Adding a database or a model provider

One file, dropped into `app/connectors/` or `app/providers/`. Subclass the
base, declare a `kind` and a `label`, implement the two or three methods, and
decorate the class with `@register`. Discovery imports every module in the
package, and the settings page builds its dropdowns from what is registered —
so a plugin that is present is a plugin that is offered, with no existing file
edited. `tests/test_plugins.py` proves this by writing a module at run time and
asserting it becomes fully usable.

### Security

The container publishes to `127.0.0.1` only, and **the application has no
authentication whatsoever**. It is a single-user localhost tool. Do not put it
on a network, behind a tunnel, or in front of a proxy without building auth
first — anyone who can reach the port can read every configured database and
retrieve the stored credentials.

**Grant the database user `SELECT` and nothing else.** EntailDB refuses
anything that is not a single SELECT, and rolls back every SQL Server statement
afterwards because T-SQL has no read-only session — but a read-only principal is
the control that does not depend on this project's parser being right. It is one
`GRANT` and it is the difference between a bug here being an inconvenience and
being a deleted table.

Conversations are stored under the data directory as `0600` files in a `0700`
directory. They contain query results — real data from your databases — in
plain text, alongside the credentials already kept there.

Stored secrets are encrypted with AES-GCM, keyed from a `0600` keyfile beside
the store. That protects the file, not the endpoint — it is not a substitute for
the authentication this app still does not have. `app/config.py` says so in its own docstring. Queries are gated to
read-only statements, and SQLite connections are additionally opened `mode=ro`
at the driver level.

## Running the evaluation

```bash
python -m evals validate
```

`evals/README.md` has the fixture format, the grader set, and the cost notes.
