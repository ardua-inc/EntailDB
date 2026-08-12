"""Aggregate run records into the table `MEASUREMENT.md` §4 asks to publish.

Rates always carry N and a Wilson interval. Errored runs are reported as a
separate column rather than folded into either numerator or denominator: a run
that never completed is not evidence of fidelity, and silently treating it as
clean would bias every rate downward.

Cases whose headline verdict depends on a non-exact grader are marked, so the
table itself says where the numbers are softest instead of leaving that to a
footnote nobody reads.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .fixtures import Case, load_cases
from .harness import load_results
from .runners.base import RunResult
from .stats import Proportion, wilson

# Graders whose verdict is a heuristic rather than a set difference. Only
# `completeness_disclosure` qualifies: every other headline grader compares
# against literals the model was demonstrably shown.
SOFT_GRADERS = frozenset({"completeness_disclosure"})


@dataclass
class CellSummary:
    config: str
    model: str
    case_id: str
    n: int
    errors: int
    empty: int
    # None when the case declares no precondition; otherwise how many graded
    # runs actually reproduced the condition the case is named after.
    precondition_met: int | None
    fabrications: int
    proportion: Proportion
    soft: bool

    @property
    def untriggered(self) -> bool:
        """The case declares a precondition and no graded run met it.

        The cell's rate is then meaningless -- not a zero, a blank. This is the
        state `empty-collection` was silently in for four runs.
        """
        return self.precondition_met == 0 and self.n > 0

    @property
    def advisory_note(self) -> str:
        notes = []
        if self.untriggered:
            notes.append("NOT TRIGGERED")
        if self.soft:
            notes.append("heuristic grader")
        if self.empty:
            notes.append(f"{self.empty} empty")
        return "; ".join(notes)


def summarise(
    results: list[RunResult], cases: list[Case] | None = None
) -> list[CellSummary]:
    cases = cases or load_cases()
    soft_cases = {
        c.id
        for c in cases
        if any(g.grader in SOFT_GRADERS for g in c.graders)
    }
    # Keyed by model as well as config. Without the model in the key, running
    # two models into one file merges them into a single rate that belongs to
    # neither -- the exact shape of silent aggregation error MEASUREMENT.md §5
    # records for the 45-vs-1 join.
    buckets: dict[tuple[str, str, str], list[RunResult]] = defaultdict(list)
    for r in results:
        buckets[(r.config, r.model, r.case_id)].append(r)

    order = {c.id: i for i, c in enumerate(cases)}
    summaries = []
    for (config, model, case_id), runs in buckets.items():
        completed = [r for r in runs if r.fabricated is not None]
        errors = len(runs) - len(completed)
        # An empty completion is FAILURES.md §6, not a fabrication -- and not a
        # clean answer either. Counting it in the denominator as a
        # non-fabrication would deflate every rate by the empty-response rate,
        # so it is reported in its own column alongside errors.
        empty = sum(1 for r in completed if not r.answer_text.strip())
        graded = [r for r in completed if r.answer_text.strip()]
        fabrications = sum(1 for r in graded if r.fabricated)
        met = [r.precondition_met for r in graded if r.precondition_met is not None]
        summaries.append(
            CellSummary(
                config=config,
                model=model,
                case_id=case_id,
                n=len(graded),
                errors=errors,
                empty=empty,
                precondition_met=(sum(1 for m in met if m) if met else None),
                fabrications=fabrications,
                proportion=wilson(fabrications, len(graded)),
                soft=case_id in soft_cases,
            )
        )
    summaries.sort(key=lambda s: (s.model, s.config, order.get(s.case_id, 999)))
    return summaries


def render_markdown(
    results: list[RunResult], cases: list[Case] | None = None
) -> str:
    cases = cases or load_cases()
    by_id = {c.id: c for c in cases}
    summaries = summarise(results, cases)
    if not summaries:
        return "_No results._"

    models = sorted({r.model for r in results})
    lines = [
        "# Fabrication rate",
        "",
        f"Models: `{', '.join(models)}`. Rates are percentages with 95% Wilson "
        "intervals in brackets.",
        "",
        "| Model | Config | Case | Failure | N | Fabrication rate | Errors | Empty | Note |",
        "|---|---|---|---:|---:|---|---:|---:|---|",
    ]
    for s in summaries:
        case = by_id.get(s.case_id)
        lines.append(
            f"| `{s.model}` | `{s.config}` | {s.case_id} | "
            f"{case.failure_ref if case else '?'} | {s.n} | "
            f"{'**not triggered**' if s.untriggered else s.proportion.format()} "
            f"| {s.errors} | {s.empty} | {s.advisory_note} |"
        )

    lines += ["", "## Per-configuration totals", "",
              "| Model | Config | N | Fabrication rate | Errors | Empty |",
              "|---|---|---:|---|---:|---:|"]
    for key in sorted({(s.model, s.config) for s in summaries}):
        rows = [s for s in summaries if (s.model, s.config) == key]
        n = sum(s.n for s in rows)
        k = sum(s.fabrications for s in rows)
        lines.append(
            f"| `{key[0]}` | `{key[1]}` | {n} | {wilson(k, n).format()} | "
            f"{sum(s.errors for s in rows)} | {sum(s.empty for s in rows)} |"
        )

    if any(s.untriggered for s in summaries):
        lines += [
            "",
            "**not triggered** — the case declares a precondition and no run in "
            "that cell met it. The rate is not a zero; it is a blank. See "
            "`evals/preconditions.py`.",
        ]

    lines += [
        "",
        "Pooling cases into one rate is presented for orientation only. The "
        "cases are not a random sample of anything, so the pooled figure is a "
        "property of this case mix, not of the system.",
        "",
    ]
    return "\n".join(lines)


def render_text(results: list[RunResult], cases: list[Case] | None = None) -> str:
    summaries = summarise(results, cases)
    if not summaries:
        return "No results."
    width = max(len(s.case_id) for s in summaries)
    lines = []
    current = None
    for s in summaries:
        if (s.model, s.config) != current:
            current = (s.model, s.config)
            header = f"{s.config}  [{s.model}]"
            lines.append(f"\n{header}")
            lines.append("-" * len(header))
        note = f"  ({s.advisory_note})" if s.advisory_note else ""
        errs = f"  errors={s.errors}" if s.errors else ""
        rate = "NOT TRIGGERED" if s.untriggered else s.proportion.format()
        lines.append(
            f"  {s.case_id:<{width}}  {s.fabrications:>2}/{s.n:<3} "
            f"{rate:>16}{errs}{note}"
        )
    return "\n".join(lines)


def load_and_render(path: Path, markdown: bool = False) -> str:
    results = load_results(path)
    return render_markdown(results) if markdown else render_text(results)
