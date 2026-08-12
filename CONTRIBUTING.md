# Contributing to EntailDB

Thanks for looking. This project has a few norms that are unusual enough to be
worth stating plainly, because a reasonable contributor would otherwise break
them in good faith and find out from a failing test.

The short version: **claims here are backed by measurement, and the measurement
is assumed to be wrong until it has been audited.**

## What this project is trying to be

An answer should be *entailed* by the rows the tools returned. Everything else
— the connectors, the provider adapters, the chat UI — is commodity work in
service of that. `DESIGN.md` has the thesis and the non-goals; read the
non-goals before proposing a feature, because several attractive directions
(semantic layers, SQL-generation quality, business-domain features) are
deliberately out of scope rather than merely unbuilt.

## Getting set up

Python 3.11 or newer.

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests -q
```

The whole suite runs offline in a few seconds: no API key, no database, no
network. If a change makes that untrue, that is the thing to reconsider first.

To run the application:

```bash
docker compose up -d --build
```

It binds to `127.0.0.1` and **has no authentication**. Do not put it on a
network. See the security section of `README.md`.

## Rules the tests enforce

These fail in CI, so here is why they exist.

**Guards must not be disableable by a flag.** If a guard needs to be off for a
test, the test injects a different runner class. `tests/test_hygiene.py` checks
this over the AST, so a `bypass_*` or `disable_*` identifier fails even in a
parameter name. The rule comes from a production system where an auth-bypass
flag sat enabled on a staging host for months.

**The library never learns what an application is.** Nothing under `src/` reads
the environment, opens a connection, or knows what a user is. That boundary is
what makes the guards portable, and it is asserted rather than hoped for.

**No client identifiers anywhere git tracks.** This work derives from a private
deployment. The sweep covers every tracked file, not just source — documentation
is the most-read part of a public repository and was once the only part nobody
checked.

**One canonical version number**, in `pyproject.toml`. Anything that displays a
version reads it from there.

## Adding a database or a model provider

This is the most likely useful contribution, and it should be one file.

Drop a module into `app/connectors/` or `app/providers/`, subclass the base,
declare a `kind` and a `label`, implement the two or three methods, and decorate
the class with `@register`. Discovery imports every module in the package, and
the settings page builds its dropdowns from what is registered — so a plugin
that is present is a plugin that is offered, with no existing file edited.

`tests/test_plugins.py` demonstrates the contract by writing a module at run
time and asserting it becomes fully usable. Read that file first; it is shorter
than this section.

Two things a new driver must not reimplement: the read-only statement gate and
the bounded preview. Both live in the base class. A driver free to get those
subtly wrong is a driver free to produce "wrong in a way that still returns
rows", which is the failure this project exists to prevent.

A provider adapter is expected to come with a fidelity run before it is
advertised as supported — see below.

## Changing a grader, a fixture, or a case

This is the delicate part, and the part where good intentions have done the most
damage.

**The deterministic graders are a screen, not a metric.** They generate
candidates; a person reads the flagged answers and that produces the number.
`runs/AUDIT.md` is the record of that reading. A rate quoted straight from the
screen has been wrong every time it mattered.

**Sixteen measurement defects have been found so far, and every single one
inflated the fabrication rate** — the direction that flatters this project. That
is not a coincidence to be proud of; it is the reason the audit step is
mandatory. `MEASUREMENT.md` §6 lists them.

So, when touching this area:

- A change that makes the screen flag *less* is the dangerous direction. It can
  hide a real failure, and nothing fails when a grader stops noticing things.
  Such a change must record what it excused — see `GraderResult.disclaimed` —
  so an audit reviews the suppression instead of trusting it.
- Fixtures must behave like databases. Two defects came from fixtures that
  returned the same payload regardless of the query, or columns the query never
  asked for; models correctly concluded the tool was broken and refused, and the
  case measured distrust rather than the thing it was built to measure.
- A grader change can be applied to existing runs with
  `python -m evals regrade <file>` — no new API calls. A **fixture** change
  cannot: it alters what the model saw, so it needs a fresh run.
- Never let the system under test grade itself.

## Claims

**No performance claim goes in the README that the ablation table does not
support.** If a change is meant to reduce fabrication, the evidence is a
measured before-and-after at N≥20 with a Wilson interval, not a plausible
argument. A clean 0/20 still carries a 95% upper bound above 16%, so twenty runs
cannot support "this eliminated it".

Numbers measured on one provider do not transfer to another. This is not
theoretical: on the one case that reproduces, baseline recitation is 20/20 on
two OpenAI models and 4/20 on Claude Sonnet 5.

## Pull requests

- Branch from `main`. Keep the change to one concern.
- Tests are not optional. Prefer a real engine to a fake where one is free:
  the connector tests run against real SQLite, because a defect once passed 300
  fake-backed tests that did not model NULL.
- Update the documentation in the same PR. `README.md`, `DESIGN.md` and
  `MEASUREMENT.md` are load-bearing, not decoration.
- Add a `CHANGELOG.md` entry as the last commit before merge, bump the patch
  version in `pyproject.toml`, and tag the merge commit.
- Commit messages: say what changed and *why it was wrong before*. The history
  here is meant to be readable as an argument.

Explaining a limitation in the PR is welcome and is not a weakness. Several of
the most useful changes in this project's history are the ones that recorded
what they could not do.

## Reporting a security issue

Do not open a public issue. Use GitHub's private vulnerability reporting on this
repository.

Two known and documented limitations, which are design state rather than
findings: the application has **no authentication** and is loopback-only, and
stored conversations contain query results in plain text on disk. Reports that
deepen either — a way to reach the app from off-host, a way to escape the
read-only statement gate, a way to read another connection's data — are very
much wanted.

## Licence

By contributing, you agree that your contributions are licensed under the MIT
licence that covers this repository.
