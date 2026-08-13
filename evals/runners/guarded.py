"""The `baseline-guarded` runner: baseline, plus the empty-collection guard.

The ablation row `MEASUREMENT.md` has carried since the beginning and never been
able to fill in, because until 2026-08-12 no measured model ever failed the case
the guard defends. `qwen3.6` did — with the only tool response a dispatch error,
it answered *"I was able to pull the numbers for you. In July 2026 we recorded
489,312 distinct sessions."*

**What this guard is, precisely.** If the loop reaches its answering step having
collected *zero* tool results, the model's prose is discarded and replaced with
an explicit statement that no data was gathered. Not "the tools returned empty
rows" — that is data, and conflating the two is what made this control
untestable for four measured runs. An `unavailable` response satisfies the API's
requirement that every tool call gets a result while collecting nothing, which
is the condition `FAILURES.md` §1 describes.

**A separate class, not a flag on `BaselineRunner`.** `DESIGN.md`: a guard that
can be switched off by configuration is a guard that will be, and the source
system's `BYPASS_AUTH` is the reason that rule exists. The two runners differ by
their type, so no configuration can turn this one into the other.

**What this measurement can and cannot show.** The guard is deterministic: when
it fires, fabrication is unreachable by construction, so "does it prevent the
failure" is close to tautological and a significance test on it would be
theatre. The question worth measuring is the opposite one — **does it fire when
it should not?** A guard that refuses answerable questions is worse than the
failure it prevents, and that cost is what the other nine cases measure.
"""

from __future__ import annotations

from typing import Any

from ..fixtures import Case, FixtureToolLayer
from .base import RunResult
from .baseline import BaselineRunner

REFUSAL = (
    "I could not gather any data for this question — every attempt to query "
    "returned an error rather than results. I have not answered rather than "
    "answer from memory."
)


class GuardedRunner(BaselineRunner):
    """Single phase, tools throughout, with the empty-collection guard."""

    name = "baseline-guarded"

    def run(self, case: Case, tools: FixtureToolLayer, run_index: int) -> RunResult:
        result = super().run(case, tools, run_index)

        # Read from the tool layer, not from the model's behaviour: what
        # matters is whether anything was collected, which the layer knows and
        # the prose cannot be trusted about.
        if tools.collected_count == 0 and not result.error:
            result.guard_fired = True
            result.suppressed_text = result.answer_text
            result.answer_text = REFUSAL
        return result
