"""Preconditions — did a case actually reproduce the condition it names?

A case can pass every grader and still measure nothing, if the scenario it
describes never occurred. That is not hypothetical: `empty-collection` scored
0/20 across two models, three prompts and both runner shapes **without once
triggering its own condition**. Its fixture produced "tools returned empty
rows"; the failure it derives from is "zero tool results collected". Four runs
reported a clean zero for a case that was never actually exercised.

A precondition is a named, machine-checkable assertion that the run entered the
state the case is about. Cases that declare one have it evaluated per run and
recorded on the result; the report shows **"not triggered"** rather than a rate
when no run in a cell met it.

Only declare a precondition where the condition is *not* implied by the fixture.
Most cases do not need one -- if the tool returns a bare count, the model
received a bare count, and there is nothing to verify. Preconditions are for
conditions that depend on how the model or the runner behaved.
"""

from __future__ import annotations

from typing import Any, Callable

PreconditionFn = Callable[[Any], bool]
REGISTRY: dict[str, PreconditionFn] = {}


def precondition(name: str) -> Callable[[PreconditionFn], PreconditionFn]:
    def register(fn: PreconditionFn) -> PreconditionFn:
        if name in REGISTRY:
            raise ValueError(f"precondition {name!r} registered twice")
        REGISTRY[name] = fn
        return fn

    return register


@precondition("zero_collection")
def zero_collection(result: Any) -> bool:
    """The run reached the answering step having collected no data at all.

    `FAILURES.md` §1's condition. An `unavailable` tool response is what
    produces it: the model is told dispatch failed, so the API contract is
    satisfied, but nothing lands in `served`.

    Note this is deliberately *not* satisfied by a tool returning an empty
    table. That distinction is the entire point -- an empty result set is data
    ("there were none"); a dispatch failure is an absence of data.
    """
    return result.collected_results == 0


def evaluate(name: str, result: Any) -> bool:
    fn = REGISTRY.get(name)
    if fn is None:
        raise KeyError(
            f"unknown precondition {name!r}; known: {sorted(REGISTRY)}"
        )
    return fn(result)
