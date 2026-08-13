"""The `baseline` runner: single phase, tools available throughout, no guards.

This is the control the whole ablation table is measured against. It is a plain
agentic tool loop — the shape every NL-to-SQL project ships — and it is
deliberately unremarkable. Anything clever here would contaminate the number
every guard is later judged by.

Two implementation choices worth defending:

**Non-streaming.** The source deployment streams SSE, and the link allowlist is
an incremental stream filter. Streaming is a transport concern; the filter also
works on a complete string (`feed(text)` then `flush()`), and the graders only
ever see final text. Using `messages.create` keeps the harness small and the
runs easier to retry. When the fidelity runner lands, its streaming path can
reuse this same `RunResult` shape.

**`answer_text` is the final assistant turn only.** A single-phase loop can emit
prose in a round that also calls tools ("Let me check that..."). Grading all of
it would flag thinking-aloud as fabrication and, worse, would make the baseline
incomparable to a two-phase runner, where the answer is by definition Phase 2's
output. Intermediate prose is still recorded in `all_assistant_text`, so nothing
is lost to audit.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..fixtures import Case, FixtureToolLayer
from .base import RunResult

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 4096
# Raised from 8 after the second measured run: `count-without-rows` burned the
# budget in 13 of 20 baseline runs and returned no text at all, leaving nothing
# to grade. The looping itself is real -- it is FAILURES.md §4, schema-guessing
# loops -- but a turn that never answers cannot be scored for fabrication, and
# an unmeasurable run is worse than a slow one. Recorded per run so a rate is
# always attributable to the budget that produced it.
DEFAULT_MAX_ROUNDS = 16


class BaselineRunner:
    """Single-phase tool loop. No two-phase split, no guards, no filtering."""

    name = "baseline"

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
            for _ in range(self.max_rounds):
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system,
                    tools=case.api_tools(),
                    messages=messages,
                )
                result.rounds += 1
                result.stop_reason = getattr(response, "stop_reason", None)
                _accumulate_usage(result, response)

                text = _text_of(response.content)
                if text:
                    result.all_assistant_text.append(text)

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if not tool_uses:
                    result.answer_text = text
                    result.collected_results = tools.collected_count
                    break

                messages.append(
                    {"role": "assistant", "content": _serialise(response.content)}
                )
                tool_results = []
                for block in tool_uses:
                    tool_input = dict(block.input or {})
                    rendered, is_error = tools.execute(block.name, tool_input)
                    result.tool_calls.append(
                        {"name": block.name, "input": tool_input}
                    )
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
            else:
                # Loop finished without the model ever ending its turn. The
                # answer is whatever prose it last produced, which may be
                # nothing -- recorded rather than retried, because a runner
                # that never converges is itself a result.
                result.exhausted_rounds = True
                result.collected_results = tools.collected_count
                result.answer_text = (
                    result.all_assistant_text[-1]
                    if result.all_assistant_text
                    else ""
                )
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            result.error = f"{type(exc).__name__}: {exc}"

        result.duration_s = round(time.monotonic() - started, 3)
        return result


def _text_of(content: list[Any]) -> str:
    return "".join(b.text for b in content if b.type == "text").strip()


def _serialise(content: list[Any]) -> list[dict[str, Any]]:
    """Convert response blocks back to the dict form the API accepts."""
    out: list[dict[str, Any]] = []
    for block in content:
        if block.type == "text":
            out.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            out.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
    return out


# Cache fields are accumulated too: a prompt cache that silently stops working
# costs full price and looks identical in the results. `cache_read_input_tokens`
# staying at zero across a batch is the signal that something in the prefix is
# varying per request.
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _accumulate_usage(result: RunResult, response: Any) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    for field_name in _USAGE_FIELDS:
        value = getattr(usage, field_name, None)
        if value is not None:
            result.usage[field_name] = result.usage.get(field_name, 0) + value
