"""The `baseline-claim-guard` runner: refuse only an *unsupported claim*.

The refinement the two ablations argued for. The blunt empty-collection guard
replaces any answer produced with nothing collected; measured, that halved
fabrication on `qwen3.6` (56/200 → 27/200) and was pure loss on
`claude-sonnet-5` (40 firings, 0 fabrications prevented, 40 good refusals
destroyed).

The two populations differ in one thing: whether the model, having collected
nothing, went on to state something anyway. A model already saying "I could not
reach the warehouse" needs no help. So this fires on the same structural
condition **and** only when the answer asserts something — `fidelity.claims`
decides that, and returns "asserting" whenever it is unsure, so this can only
ever fire where the blunt guard fired.

A separate class, not a flag on `GuardedRunner`. Both remain measurable side by
side, which is the point: the blunt one is kept so the comparison stays
runnable, not because it is the recommendation.
"""

from __future__ import annotations

from fidelity.claims import asserts_data

from ..fixtures import Case, FixtureToolLayer
from .base import RunResult
from .baseline import BaselineRunner
from .guarded import REFUSAL


class ClaimGuardRunner(BaselineRunner):
    """Single phase, tools throughout; refuses an assertion made on no data."""

    name = "baseline-claim-guard"

    def run(self, case: Case, tools: FixtureToolLayer, run_index: int) -> RunResult:
        result = super().run(case, tools, run_index)
        if (tools.collected_count == 0
                and not result.error
                and asserts_data(result.answer_text)):
            result.guard_fired = True
            result.suppressed_text = result.answer_text
            result.answer_text = REFUSAL
        return result
