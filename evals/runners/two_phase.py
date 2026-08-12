"""The `two-phase` runner: collect with tools, then answer with `tools=[]`.

`MEASUREMENT.md`'s ablation lists this as the row between `baseline` and
`+empty-guard`: *"Two-phase, no additional guards."* That "no guards" is
load-bearing and is the reason this class exists separately.

**Phase 2 runs even when Phase 1 collected nothing.** That is the hole
`FAILURES.md` §1 describes — the model is made to answer with tools switched
off and an empty context, and invents both the figures and the narration of
having fetched them. The empty-collection guard closes it, and the guarded
version is a *different runner class*, not a constructor flag on this one:

    Guards must not be disableable by a convenience flag. [...] If a guard
    needs to be off for testing, the test injects a different runner.
                                                            -- DESIGN.md

Building the unguarded runner first is deliberate. `empty-collection` scored
0/20 against `baseline` across two models, and it had to: a single-phase runner
can always query again, so it never enters the state the guard defends. Until
this runner exists there is nothing for that guard to be measured against.

Behaviours carried across from the extraction plan's must-survive list:

1. Phase 2 is called with ``tools=[]`` — not instructed not to call tools.
3. Phase 2 receives the immediately-prior assistant message, so a follow-up
   like "compare that to last year" resolves against something real.
6. An empty Phase 2 completion is retried once, then reported as empty.

Item 2 (skip Phase 2 on empty collection) is the guard, and is deliberately
absent here. Item 5 (truncate tool results for history, restore in full for
answering) does not apply: nothing is truncated, so Phase 2 already sees every
result in full.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..fixtures import Case, FixtureToolLayer
from .base import RunResult
from .baseline import (
    DEFAULT_MAX_ROUNDS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    _accumulate_usage,
    _serialise,
    _text_of,
)

# Phase 2 answers from collected results only. It is told that plainly, because
# a model handed tools=[] with no explanation may otherwise narrate an intent to
# query. This is framing, not an anti-fabrication instruction -- adding one here
# would make the two-phase row incomparable to the baseline row.
PHASE_2_FRAMING = (
    "The data-gathering step is complete and the tools are no longer "
    "available. Answer the question using the results already collected above."
)


class TwoPhaseRunner:
    """Two-phase loop with no guards. Phase 2 runs unconditionally."""

    name = "two-phase"

    def __init__(
        self,
        client: Any,
        system_prompt: str,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        name: str | None = None,
    ) -> None:
        self.client = client
        self.system_prompt = system_prompt
        self.model = model
        self.max_tokens = max_tokens
        self.max_rounds = max_rounds
        if name:
            self.name = name

    def run(
        self,
        case: Case,
        tools: FixtureToolLayer,
        run_index: int,
    ) -> RunResult:
        result = RunResult(
            config=self.name,
            case_id=case.id,
            run_index=run_index,
            model=self.model,
            max_rounds=self.max_rounds,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        started = time.monotonic()
        system = case.system_blocks(self.system_prompt)
        messages: list[dict[str, Any]] = case.api_messages()

        try:
            self._phase_one(case, tools, result, system, messages)
            result.collected_results = tools.collected_count
            self._phase_two(result, system, messages)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            result.error = f"{type(exc).__name__}: {exc}"

        result.duration_s = round(time.monotonic() - started, 3)
        return result

    # ──────────────────────────────────────────────────────────────────────

    def _phase_one(
        self,
        case: Case,
        tools: FixtureToolLayer,
        result: RunResult,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> None:
        """Collect data. Prose produced here is recorded but never graded."""
        for _ in range(self.max_rounds):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                tools=case.api_tools(),
                messages=messages,
            )
            result.rounds += 1
            result.phase1_rounds += 1
            result.stop_reason = getattr(response, "stop_reason", None)
            _accumulate_usage(result, response)

            text = _text_of(response.content)
            if text:
                result.all_assistant_text.append(text)

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                # Phase 1 is done. The assistant turn stays in `messages` so
                # Phase 2 sees it -- extraction plan item 3.
                messages.append(
                    {"role": "assistant", "content": _serialise(response.content)}
                )
                return

            messages.append(
                {"role": "assistant", "content": _serialise(response.content)}
            )
            tool_results = []
            for block in tool_uses:
                tool_input = dict(block.input or {})
                rendered, is_error = tools.execute(block.name, tool_input)
                result.tool_calls.append({"name": block.name, "input": tool_input})
                result.served_rendered.append(rendered)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": rendered,
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": tool_results})
        result.exhausted_rounds = True

    def _phase_two(
        self,
        result: RunResult,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> None:
        """Answer with tools removed. Retried once if it comes back empty."""
        turn = [*messages, {"role": "user", "content": PHASE_2_FRAMING}]

        for attempt in range(2):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                tools=[],
                messages=turn,
            )
            result.rounds += 1
            result.stop_reason = getattr(response, "stop_reason", None)
            _accumulate_usage(result, response)

            text = _text_of(response.content)
            if text:
                result.answer_text = text
                result.all_assistant_text.append(text)
                return
            if attempt == 0:
                # FAILURES.md §6: an empty completion is retried once. In
                # production this converted most empty responses into answers;
                # without it they are silent failures.
                result.phase2_retried = True
