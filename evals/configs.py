"""The configuration registry — one entry per row of the ablation table.

A config maps a name to a **runner constructor**. Adding `two-phase` or
`+empty-guard` later means adding a class and an entry here, never a parameter
that switches a guard off. See `evals/runners/__init__.py` for why that shape is
fixed before there is anything to switch.

Only the single-phase rows exist so far, which is the intended sequencing:
`MEASUREMENT.md` requires a real baseline number before any guard is built, so
that each guard has to earn its place in the table rather than be justified by
the story it comes with.

    baseline              single phase, tools throughout, no guards
    baseline-instructed   identical runner, prompt carrying explicit
                          anti-fabrication instructions

`baseline-instructed` is not in `MEASUREMENT.md`'s config list, and it is here
deliberately. Real deployments *do* instruct against fabrication — the source
system's prompt did so in four separate sections. Measuring guards against a
prompt that never tries would inflate every improvement they appear to make,
which is precisely the sleight of hand `MEASUREMENT.md` §4 forbids. It is also
the direct test of `DESIGN.md`'s claim that structural impossibility beats
instruction: if instruction alone closes a case, the guard for that case has to
justify itself on something other than fabrication rate.
"""

from __future__ import annotations

from typing import Any, Callable

from .fixtures import load_prompt
from .runners import BaselineRunner, Runner, TwoPhaseRunner
from .runners.baseline import DEFAULT_MAX_ROUNDS

RunnerFactory = Callable[[Any, str, int], Runner]


def _baseline(client: Any, model: str, max_rounds: int) -> Runner:
    return BaselineRunner(
        client,
        system_prompt=load_prompt("neutral"),
        model=model,
        max_rounds=max_rounds,
        name="baseline",
    )


def _baseline_instructed(client: Any, model: str, max_rounds: int) -> Runner:
    return BaselineRunner(
        client,
        system_prompt=load_prompt("instructed"),
        model=model,
        max_rounds=max_rounds,
        name="baseline-instructed",
    )


def _baseline_domain(client: Any, model: str, max_rounds: int) -> Runner:
    return BaselineRunner(
        client,
        system_prompt=load_prompt("domain"),
        model=model,
        max_rounds=max_rounds,
        name="baseline-domain",
    )


def _baseline_profiled(client: Any, model: str, max_rounds: int) -> Runner:
    return BaselineRunner(
        client,
        system_prompt=load_prompt("profiled"),
        model=model,
        max_rounds=max_rounds,
        name="baseline-profiled",
    )


def _two_phase(client: Any, model: str, max_rounds: int) -> Runner:
    return TwoPhaseRunner(
        client,
        system_prompt=load_prompt("neutral"),
        model=model,
        max_rounds=max_rounds,
        name="two-phase",
    )


def _two_phase_domain(client: Any, model: str, max_rounds: int) -> Runner:
    return TwoPhaseRunner(
        client,
        system_prompt=load_prompt("domain"),
        model=model,
        max_rounds=max_rounds,
        name="two-phase-domain",
    )


# Each entry names a (runner class, prompt) pair explicitly rather than taking
# the prompt as a parameter. Configs are the unit the ablation table reports on,
# so a config name has to identify everything that varies -- and keeping the
# runner a constructor choice rather than a flag is the DESIGN.md rule above.
CONFIGS: dict[str, RunnerFactory] = {
    "baseline": _baseline,
    "baseline-instructed": _baseline_instructed,
    "baseline-domain": _baseline_domain,
    "baseline-profiled": _baseline_profiled,
    "two-phase": _two_phase,
    "two-phase-domain": _two_phase_domain,
}


def build_runner(
    config: str, client: Any, model: str, max_rounds: int = DEFAULT_MAX_ROUNDS
) -> Runner:
    if config not in CONFIGS:
        raise KeyError(
            f"unknown config {config!r}; known: {sorted(CONFIGS)}"
        )
    return CONFIGS[config](client, model, max_rounds)
