"""Turning measurements into facts, and facts into a document.

The selectivity here is the product, not a nicety. `DESIGN.md`'s five worked
examples are all *surprising* facts — a 75%-NULL abandoned column, a key that
resolves 29% of the time, a status column NULL on every row, a table with zero
rows, a name format that holds 87% of the time. None of them is "this column is
0% NULL". A profiler that emitted every measured number would replace an
832-line hand-written prompt with a longer generated one, and would have solved
nothing.

So every derivation below is threshold-driven, and a database with no surprises
produces a short document.

**Statements carry no magnitudes.** Counts live in `evidence`, which is
machine-readable and never reaches a prompt. This is a measured correction, not
a style preference: with an earlier version in the system prompt, a model that
could not query answered a question by citing the document — "based on the
documented data facts, `service_requests` contains 120 rows". A generated
document that carries magnitudes is a fresh source of prompt-embedded figures,
which is the failure this pillar exists to remove rather than re-home.

The line drawn is magnitude versus reliability. A row count or a per-value
count is answer-shaped: it is the sort of thing a user asks for, so a model
lacking data will offer it. A null rate or a join hit rate describes how far
the data can be trusted, is not an answer to any business question, and is what
makes the fact actionable — so rates stay.

The document is **generated, never hand-edited**. That is the entire argument
for pillar 2: `FAILURES.md` §8's frozen statistics were true when someone pasted
them and rotted silently, and it is the one failure this project's evaluation
actually reproduces — at 90-100% on the model that was in production. Generated
facts regenerate. To make staleness detectable rather than invisible, the
document carries its generation time and a digest of its own content.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Sequence

from .model import ColumnProfile, Fact, JoinProfile, TableProfile

# Thresholds. Defaults chosen so DESIGN.md's five examples all clear them, and
# so an ordinary well-maintained column does not.
MOSTLY_NULL = 0.50
SOFT_KEY_CONCERN = 0.95
DECLARED_KEY_CONCERN = 1.0
FORMAT_MIN_COVERAGE = 0.60
FORMAT_MIN_SAMPLES = 20
ENUM_MAX_DISTINCT_REPORTED = 12
CONSTANT_COLUMN_MAX_ROWS = 1
# Below this, a distribution is not a finding -- it is the table. A two-row
# table makes every column "constant" and every column "enumerated", which is
# how the first live run produced 50 facts for a 15-table sample database.
MIN_ROWS_FOR_DISTRIBUTION = 50
# A shape covering essentially everything is the type, not a convention.
# Reported coverage is rounded, so an exact 1.0 test let 99.5% through and
# rendered as "matches in 100% of values -- the remainder does not".
FORMAT_MAX_COVERAGE = 0.98
# More same-kind facts than this get rolled into one line. They are each true;
# fifty true bullets is still a document nobody reads.
ROLLUP_THRESHOLD = 4


def shape_of(value: str) -> str:
    """Collapse a value to its character shape: `Doe (3), Jane` -> `Aaa (9), Aaaa`.

    Runs of letters become `A`/`a`, runs of digits become `9`, and everything
    else is kept literally. Crude, and that is the point — it finds the format
    conventions humans actually impose on free-text identifier columns without
    needing a grammar.
    """
    out: list[str] = []
    for chunk in re.findall(r"[A-Z]+|[a-z]+|\d+|[^A-Za-z\d]+", value):
        first = chunk[0]
        if first.isdigit():
            out.append("9")
        elif first.isupper():
            out.append("A")
        elif first.islower():
            out.append("a")
        else:
            out.append(chunk)
    return "".join(out)


def dominant_format(samples: Sequence[str]) -> tuple[str, float] | None:
    """The most common shape among samples, and the fraction it covers."""
    if len(samples) < FORMAT_MIN_SAMPLES:
        return None
    counts: dict[str, int] = {}
    for value in samples:
        counts[shape_of(value)] = counts.get(shape_of(value), 0) + 1
    shape, hits = max(counts.items(), key=lambda kv: kv[1])
    coverage = hits / len(samples)
    if coverage < FORMAT_MIN_COVERAGE or coverage > FORMAT_MAX_COVERAGE:
        # Below the floor there is no convention; above the ceiling the "shape"
        # is just the type (all dates, all integers) and says nothing a reader
        # did not already know from the column definition.
        return None
    return shape, coverage


def _pct(x: float) -> str:
    """Percentage that never rounds into a claim the data does not support.

    A column 99.7% NULL rendered as "100% NULL" contradicts its own
    classification -- if it were 100% it would be the `always_null` fact, which
    is blocking rather than caution. Observed on a real star schema:
    `DimCustomer.Suffix is 100% NULL. Aggregates over it describe the populated
    minority` reads as a bug because it is one.
    """
    pct = x * 100
    if pct >= 99.5 and x < 1.0:
        return ">99%"
    if 0 < pct <= 0.5:
        return "<1%"
    return f"{pct:.0f}%"


def _show(value: str) -> str:
    """Render a value so an empty or blank one is visible.

    Sakila's `address.address2` is the empty string on every populated row.
    Rendered bare it produced "takes these values: ." — a fact that reads as a
    formatting bug and tells the reader nothing.
    """
    if value == "":
        return "'' (empty string)"
    if not value.strip():
        return f"{value!r} (whitespace)"
    return value


def column_facts(profile: ColumnProfile) -> list[Fact]:
    subject = profile.column.qualified()
    facts: list[Fact] = []

    if profile.all_null:
        facts.append(
            Fact(
                subject=subject,
                kind="always_null",
                statement=(
                    f"`{subject}` is NULL on every row. It exists in the schema "
                    "and was never populated, so any filter on it silently "
                    "matches nothing."
                ),
                severity="blocking",
                evidence={"rows": profile.rows},
            )
        )
        return facts

    if profile.rows and profile.null_rate >= MOSTLY_NULL:
        facts.append(
            Fact(
                subject=subject,
                kind="mostly_null",
                statement=(
                    f"`{subject}` is {_pct(profile.null_rate)} NULL. Aggregates "
                    "over it describe the populated minority, not the table."
                ),
                severity="caution",
                evidence={"null_rate": round(profile.null_rate, 4)},
            )
        )

    # A primary key is all-distinct by definition; reporting its values is a
    # data dump, not a fact.
    if profile.column.primary_key:
        return facts

    if profile.rows < MIN_ROWS_FOR_DISTRIBUTION:
        return facts

    if profile.rows > CONSTANT_COLUMN_MAX_ROWS and profile.distinct == 1:
        only = (_show(profile.observed_values[0][0])
                if profile.observed_values else "unknown")
        facts.append(
            Fact(
                subject=subject,
                kind="constant",
                statement=(
                    f"`{subject}` holds one value on every populated row "
                    f"({only}). It cannot discriminate anything."
                ),
                severity="caution",
                evidence={"value": only},
            )
        )
    elif 1 < profile.distinct <= ENUM_MAX_DISTINCT_REPORTED and profile.observed_values:
        listed = ", ".join(_show(v) for v, _ in profile.observed_values)
        facts.append(
            Fact(
                subject=subject,
                kind="enumerated",
                statement=f"`{subject}` takes these values: {listed}.",
                evidence={"values": dict(profile.observed_values)},
            )
        )
        declared = set(profile.column.declared_values)
        if declared:
            unseen = sorted(declared - {v for v, _ in profile.observed_values})
            if unseen:
                facts.append(
                    Fact(
                        subject=subject,
                        kind="declared_but_absent",
                        statement=(
                            f"`{subject}` declares {', '.join(unseen)} but no row "
                            "uses them. A filter excluding those values excludes "
                            "nothing."
                        ),
                        severity="caution",
                        evidence={"unseen": unseen},
                    )
                )

    fmt = dominant_format(profile.samples)
    if fmt:
        shape, coverage = fmt
        facts.append(
            Fact(
                subject=subject,
                kind="format",
                statement=(
                    f"`{subject}` matches the shape `{shape}` in "
                    f"{_pct(coverage)} of sampled values — a convention, not a "
                    "constraint. The remainder does not."
                ),
                evidence={"shape": shape, "coverage": round(coverage, 4)},
            )
        )

    return facts


def table_facts(profile: TableProfile) -> list[Fact]:
    if profile.rows == 0:
        return [
            Fact(
                subject=profile.table.qualified(),
                kind="empty_table",
                statement=(
                    f"`{profile.table.qualified()}` exists in the schema and "
                    "contains zero rows. Queries against it return nothing "
                    "without erroring."
                ),
                severity="blocking",
                evidence={"rows": 0},
            )
        ]
    return [f for c in profile.columns for f in column_facts(c)]


def join_facts(profile: JoinProfile) -> list[Fact]:
    fk = profile.foreign_key
    threshold = DECLARED_KEY_CONCERN if fk.declared else SOFT_KEY_CONCERN
    if profile.non_null == 0 or profile.hit_rate >= threshold:
        return []
    how = {
        "declared": "declared foreign key",
        "name": "soft key, inferred from the column name",
        "value_overlap": "undeclared relationship, found by measuring value "
                         "overlap — nothing in the schema records it",
    }.get(fk.inferred_by, "soft key")
    return [
        Fact(
            subject=fk.column.qualified(),
            kind="join_miss",
            statement=(
                f"`{fk.column.qualified()}` → `{fk.target.qualified()}` resolves "
                f"{_pct(profile.hit_rate)} of the time ({how}). An inner join "
                "silently drops the rest."
            ),
            severity="caution",
            evidence={
                "hit_rate": round(profile.hit_rate, 4),
                "non_null": profile.non_null,
                "declared": fk.declared,
                "inferred_by": fk.inferred_by,
            },
        )
    ]


def _rollup(facts: Sequence[Fact]) -> list[Fact]:
    """Collapse a repeated finding into one line.

    Twelve separate bullets saying a `last_update` column never varies are each
    true and collectively unreadable. The information is preserved -- every
    subject is named, and the individual facts stay in `evidence` for tooling --
    but the document says it once.

    Only `constant` and `format` roll up. A blocking fact never does: an empty
    table or an always-NULL column is individually actionable, and burying it in
    a list is how it gets skimmed past.
    """
    rollable = {"constant", "format"}
    grouped: dict[str, list[Fact]] = {}
    out: list[Fact] = []
    for f in facts:
        if f.kind in rollable and f.severity != "blocking":
            grouped.setdefault(f.kind, []).append(f)
        else:
            out.append(f)

    labels = {
        "constant": ("hold a single value on every populated row, so they "
                     "cannot discriminate anything"),
        "format": ("follow a shape convention that most but not all values "
                   "match -- treat the pattern as a habit, not a guarantee"),
    }
    for kind, group in grouped.items():
        if len(group) <= ROLLUP_THRESHOLD:
            out.extend(group)
            continue
        subjects = ", ".join(f"`{f.subject}`" for f in group)
        out.append(
            Fact(
                subject=f"{len(group)} columns",
                kind=kind,
                statement=f"{len(group)} columns {labels[kind]}: {subjects}.",
                severity=group[0].severity,
                evidence={"columns": [f.subject for f in group]},
            )
        )
    return out


def derive(
    tables: Sequence[TableProfile], joins: Sequence[JoinProfile]
) -> list[Fact]:
    facts: list[Fact] = []
    for t in tables:
        facts.extend(table_facts(t))
    for j in joins:
        facts.extend(join_facts(j))
    facts = _rollup(facts)
    order = {"blocking": 0, "caution": 1, "info": 2}
    facts.sort(key=lambda f: (order.get(f.severity, 3), f.subject))
    return facts


def render(
    facts: Sequence[Fact],
    database: str,
    generated_at: datetime | None = None,
    max_per_section: int | None = None,
) -> str:
    """The generated facts document.

    Carries its own provenance — when, against what — because the failure this
    pillar exists to prevent is a true statement outliving its truth. A reader
    (human or model) can see how old it is; a scheduler can see whether to
    regenerate; the digest lets a consumer detect that it changed.

    **The header instructs the editor, not the model.** An earlier version
    explained *why* not to hand-edit — "a figure that was true once, kept past
    its truth, and recited as current" — which is a behavioural hint aimed
    squarely at the failure this document is meant to be measured against. A
    document that smuggles in an anti-fabrication instruction cannot have its
    effect attributed to the facts it carries. Keep the header factual:
    provenance, freshness, digest, and a do-not-edit notice with no rationale.
    """
    generated_at = generated_at or datetime.now(timezone.utc)
    body: list[str] = []

    if not facts:
        body.append(
            "No notable facts. Every column measured within normal bounds: no "
            "empty tables, no all-NULL columns, no unreliable joins, no "
            "surprising formats.\n"
        )
    else:
        by_severity: dict[str, list[Fact]] = {}
        for f in facts:
            by_severity.setdefault(f.severity, []).append(f)
        headings = {
            "blocking": "Blocking — a query written without knowing this returns wrong or empty results",
            "caution": "Caution — affects how results should be interpreted",
            "info": "Observed shape",
        }
        for severity in ("blocking", "caution", "info"):
            group = by_severity.get(severity)
            if not group:
                continue
            body.append(f"## {headings[severity]}\n")
            shown = group if max_per_section is None else group[:max_per_section]
            for f in shown:
                body.append(f"- {f.statement}")
            omitted = len(group) - len(shown)
            if omitted:
                # Never a silent cap. A document that quietly drops findings
                # reads as complete and is not, which is the same defect class
                # as a truncated join search reporting success.
                body.append(
                    f"- _…and {omitted} further {severity} fact"
                    f"{'s' if omitted != 1 else ''}, omitted for length. "
                    "Raise `max_per_section` or read the machine-readable "
                    "facts to see them all._"
                )
            body.append("")

    text = "\n".join(body).rstrip() + "\n"
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]

    header = (
        "# Generated data facts\n\n"
        f"Database: `{database}`  \n"
        f"Generated: {generated_at.isoformat(timespec='seconds')}  \n"
        f"Digest: `{digest}`  \n"
        f"Facts: {len(facts)}\n\n"
        "**Generated file — do not edit.** Every statement here was measured "
        "from the database at the time above and will be overwritten on the "
        "next run.\n\n"
        "---\n\n"
    )
    return header + text
