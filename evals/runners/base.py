"""Shared runner types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..fixtures import Case, FixtureToolLayer


@dataclass
class RunResult:
    """One execution of one case under one configuration.

    Records enough to re-grade without re-running, and enough to hand-audit
    without re-running either. `MEASUREMENT.md` asks for fixtures and grader
    code to be published so results are reproducible; keeping the full
    transcript here means a sceptic can also check the grader's verdicts
    against the actual model output rather than trusting the aggregate.
    """

    config: str
    case_id: str
    run_index: int
    model: str

    answer_text: str = ""
    # Text from every assistant turn, including rounds that also called tools.
    # Graded output is `answer_text` alone -- see BaselineRunner for why.
    all_assistant_text: list[str] = field(default_factory=list)

    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    served_rendered: list[str] = field(default_factory=list)

    rounds: int = 0
    max_rounds: int = 0
    # Two-phase runners only; 0 on single-phase.
    phase1_rounds: int = 0
    collected_results: int = 0
    # Set by a guarded runner: whether the guard fired, and what it replaced.
    # The suppressed prose is kept because a guard that silently discards the
    # thing it prevented cannot be audited for over-firing.
    guard_fired: bool = False
    suppressed_text: str = ""
    phase2_retried: bool = False
    phase2_skipped: bool = False
    # None when the case declares no precondition.
    precondition_met: bool | None = None
    stop_reason: str | None = None
    exhausted_rounds: bool = False
    usage: dict[str, int] = field(default_factory=dict)

    error: str | None = None
    duration_s: float = 0.0
    started_at: str = ""

    # Filled in by the harness after grading.
    grader_results: list[dict[str, Any]] = field(default_factory=list)
    fabricated: bool | None = None

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.config, self.case_id, self.run_index)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config,
            "case_id": self.case_id,
            "run_index": self.run_index,
            "model": self.model,
            "answer_text": self.answer_text,
            "all_assistant_text": self.all_assistant_text,
            "tool_calls": self.tool_calls,
            "served_rendered": self.served_rendered,
            "rounds": self.rounds,
            "max_rounds": self.max_rounds,
            "phase1_rounds": self.phase1_rounds,
            "collected_results": self.collected_results,
            "guard_fired": self.guard_fired,
            "suppressed_text": self.suppressed_text,
            "phase2_retried": self.phase2_retried,
            "phase2_skipped": self.phase2_skipped,
            "precondition_met": self.precondition_met,
            "stop_reason": self.stop_reason,
            "exhausted_rounds": self.exhausted_rounds,
            "usage": self.usage,
            "error": self.error,
            "duration_s": self.duration_s,
            "started_at": self.started_at,
            "grader_results": self.grader_results,
            "fabricated": self.fabricated,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunResult":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class Runner(Protocol):
    """A system under test.

    Implementations differ by structure -- single-phase, two-phase, two-phase
    with guards -- never by a flag toggling behaviour within one class.
    """

    name: str

    def run(
        self,
        case: Case,
        tools: FixtureToolLayer,
        run_index: int,
    ) -> RunResult: ...
