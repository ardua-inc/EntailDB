"""Tests for `FidelityRunner` — the tool loop with the guards attached.

The provider is faked here, and that is the right call: what is under test is
the loop and the guards, not the wire format. The fake is scripted turn by
turn, so every test states exactly what the model did and asserts on what the
runner did about it.

The properties worth holding are narrow but load-bearing:

* the accuracy instruction cannot be left out (it is the only intervention with
  a measured effect — `stale-fact` went 20/20 to 0/20 with it);
* a URL is quotable only once a tool has actually returned it;
* an *errored* tool result authorises nothing, because its content is a failure
  message, not data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

import pytest

from fidelity.runner import (
    ACCURACY_INSTRUCTION,
    Event,
    FidelityRunner,
    ToolCall,
    ToolResult,
    ToolSpec,
    Turn,
)


def Block(name: str, arguments: dict[str, Any], id: str = "tu_1") -> ToolCall:
    """A tool call in the runner's own vocabulary.

    Once the runner stopped speaking Anthropic's wire format, a stand-in for an
    SDK block stopped being the right fixture: what a provider hands over now is
    a `ToolCall`, and a test that built anything else would be testing a shape
    no adapter produces.
    """
    return ToolCall(id=id, name=name, arguments=arguments)


class ScriptedProvider:
    """Replays a list of turns. Each turn is a list of ("kind", payload)."""

    def __init__(self, *turns: list[tuple[str, Any]]) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    def stream_turn(self, system, messages, tools) -> Iterator[tuple[str, Any]]:
        self.calls.append({"system": system, "messages": list(messages),
                           "tools": tools})
        if not self.turns:
            yield ("text", "done")
            return
        yield from self.turns.pop(0)


class ExplodingProvider:
    def stream_turn(self, system, messages, tools):
        raise RuntimeError("connection reset")
        yield  # pragma: no cover — generator marker


@dataclass
class RecordingTool:
    name: str = "run_sql"
    description: str = "Run a query."
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    result: str = "rows: 1"
    is_error: bool = False
    seen: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, arguments: dict[str, Any]) -> ToolResult:
        self.seen.append(arguments)
        return ToolResult(self.result, is_error=self.is_error)


def texts(events: list[Event]) -> str:
    return "".join(e.text for e in events if e.type == "text")


# ── the accuracy instruction ──────────────────────────────────────────────

def test_the_accuracy_instruction_is_present_by_default():
    provider = ScriptedProvider()
    runner = FidelityRunner(provider, tools=[])
    list(runner.run([{"role": "user", "content": "hi"}]))
    assert ACCURACY_INSTRUCTION.strip() in provider.calls[0]["system"]


def test_a_domain_prompt_does_not_displace_the_instruction():
    provider = ScriptedProvider()
    runner = FidelityRunner(provider, tools=[], system_prompt="You are an analyst.")
    list(runner.run([{"role": "user", "content": "hi"}]))
    system = provider.calls[0]["system"]
    assert "You are an analyst." in system
    assert ACCURACY_INSTRUCTION.strip() in system


def test_the_instruction_comes_last():
    """It is the last thing read before the conversation, so a long domain
    prompt cannot bury it."""
    provider = ScriptedProvider()
    runner = FidelityRunner(provider, tools=[], system_prompt="Domain prose.")
    list(runner.run([{"role": "user", "content": "hi"}]))
    system = provider.calls[0]["system"]
    assert system.index("Domain prose.") < system.index("## Accuracy")


def test_the_instruction_covers_every_catalogued_failure():
    """Each numbered rule maps to a case in the evaluation; none may be lost."""
    for phrase in (
        "Never state a figure you did not receive from a tool",
        "Never invent rows",
        "Reproduce rows exactly as returned",
        "Never write a link a tool did not give you",
        "say it failed",
        "say so and state how many rows matched",
        "in any reference material",
    ):
        assert phrase in ACCURACY_INSTRUCTION


# ── the tool loop ─────────────────────────────────────────────────────────

def test_a_tool_call_is_executed_and_fed_back():
    tool = RecordingTool(result="category | revenue\nSports | 4892.19")
    provider = ScriptedProvider(
        [("tool_call", Block("run_sql", {"query": "SELECT 1"})),
         ("assistant", Turn(role="assistant"))],
        [("text", "Sports made 4892.19.")],
    )
    events = list(FidelityRunner(provider, tools=[tool]).run([]))

    assert tool.seen == [{"query": "SELECT 1"}]
    assert "Sports made 4892.19." in texts(events)
    assert [e.type for e in events][-1] == "done"

    # The result reached the model on the following turn, as a neutral tool
    # turn rather than any provider's message shape.
    follow_up = provider.calls[1]["messages"][-1]
    assert follow_up.role == "tool"
    assert follow_up.outcomes[0].content == tool.result


def test_tool_call_and_result_are_both_surfaced():
    tool = RecordingTool()
    provider = ScriptedProvider(
        [("tool_call", Block("run_sql", {"query": "SELECT 1"})),
         ("assistant", Turn(role="assistant"))],
        [("text", "ok")],
    )
    events = list(FidelityRunner(provider, tools=[tool]).run([]))
    kinds = [e.type for e in events]
    assert kinds.index("tool_call") < kinds.index("tool_result")


def test_an_unknown_tool_is_reported_rather_than_crashing():
    provider = ScriptedProvider(
        [("tool_call", Block("drop_everything", {})), ("assistant", Turn(role="assistant"))],
        [("text", "I could not do that.")],
    )
    events = list(FidelityRunner(provider, tools=[]).run([]))
    results = [e for e in events if e.type == "tool_result"]
    assert results[0].is_error is True
    assert "unknown tool" in results[0].result
    assert "I could not do that." in texts(events)


def test_an_errored_tool_result_is_still_shown_to_the_model():
    """Rule 5: if a query fails, the model must be able to say it failed."""
    tool = RecordingTool(result="syntax error at or near LIMIT", is_error=True)
    provider = ScriptedProvider(
        [("tool_call", Block("run_sql", {"query": "bad"})), ("assistant", Turn(role="assistant"))],
        [("text", "The query failed.")],
    )
    events = list(FidelityRunner(provider, tools=[tool]).run([]))
    assert any(e.type == "tool_result" and e.is_error for e in events)
    assert provider.calls[1]["messages"][-1].outcomes[0].is_error is True


def test_the_loop_stops_and_says_so_when_rounds_run_out():
    turns = [[("tool_call", Block("run_sql", {})), ("assistant", Turn(role="assistant"))]
             for _ in range(10)]
    provider = ScriptedProvider(*turns)
    runner = FidelityRunner(provider, tools=[RecordingTool()], max_rounds=3)
    events = list(runner.run([]))
    errors = [e for e in events if e.type == "error"]
    assert errors and "3 tool rounds" in errors[0].text
    assert len(provider.calls) == 3


def test_a_provider_failure_is_surfaced_not_raised():
    events = list(FidelityRunner(ExplodingProvider(), tools=[]).run([]))
    assert events[-1].type == "error"
    assert "connection reset" in events[-1].text


# ── the link allowlist ────────────────────────────────────────────────────

def test_a_link_no_tool_returned_is_stripped():
    provider = ScriptedProvider(
        [("text", "See [the report](https://invented.example/report).")]
    )
    events = list(FidelityRunner(provider, tools=[]).run([]))
    body = texts(events)
    assert "https://invented.example/report" not in body
    assert "the report" in body          # the label survives; the link does not
    assert any(e.type == "stripped_link" for e in events)


def test_a_bare_url_in_prose_is_left_alone():
    """A documented boundary, asserted so it stays deliberate.

    The guard suppresses well-formed markdown links and nothing else — it will
    not eat prose, because a filter that rewrites arbitrary text is a worse
    failure than the one it prevents. Bare URLs are consequently *not* covered,
    and anything relying on this guard alone should know that.
    """
    provider = ScriptedProvider([("text", "See https://invented.example/report")])
    events = list(FidelityRunner(provider, tools=[]).run([]))
    assert "https://invented.example/report" in texts(events)


def test_a_url_a_tool_returned_survives():
    tool = RecordingTool(result="url: https://real.example/report/7")
    provider = ScriptedProvider(
        [("tool_call", Block("run_sql", {})), ("assistant", Turn(role="assistant"))],
        [("text", "It is [there](https://real.example/report/7) now.")],
    )
    events = list(FidelityRunner(provider, tools=[tool]).run([]))
    assert "https://real.example/report/7" in texts(events)
    assert not [e for e in events if e.type == "stripped_link"]


def test_an_errored_tool_result_does_not_authorise_its_urls():
    """The content of a failed call is an error message, not data. A URL that
    appears in one has not been returned by the database."""
    tool = RecordingTool(result="failed to reach https://real.example/x",
                         is_error=True)
    provider = ScriptedProvider(
        [("tool_call", Block("run_sql", {})), ("assistant", Turn(role="assistant"))],
        [("text", "Try [this](https://real.example/x).")],
    )
    events = list(FidelityRunner(provider, tools=[tool]).run([]))
    assert "https://real.example/x" not in texts(events)


def test_a_link_is_not_quotable_before_the_tool_returns_it():
    """The same URL, cited a round too early and then again after.

    The allowlist is rebuilt each round from what has been collected so far,
    never from what is about to be — so the first citation is stripped and the
    second survives.
    """
    tool = RecordingTool(result="url: https://real.example/late")
    provider = ScriptedProvider(
        [("text", "It is [here](https://real.example/late)."),
         ("tool_call", Block("run_sql", {})), ("assistant", Turn(role="assistant"))],
        [("text", "Confirmed: [here](https://real.example/late).")],
    )
    events = list(FidelityRunner(provider, tools=[tool]).run([]))
    body = texts(events)
    assert body.count("https://real.example/late") == 1
    assert body.index("Confirmed") < body.index("https://real.example/late")


# ── tool definitions ──────────────────────────────────────────────────────

def test_tools_are_advertised_to_the_provider():
    tool = RecordingTool()
    provider = ScriptedProvider()
    list(FidelityRunner(provider, tools=[tool]).run([]))
    assert provider.calls[0]["tools"] == [
        ToolSpec(name="run_sql", description="Run a query.",
                 schema=tool.input_schema)
    ]


def test_the_caller_s_history_is_not_mutated():
    """The runner normalises the caller's dicts into `Turn`s; the list it was
    handed must come back untouched."""
    messages = [{"role": "user", "content": "hi"}]
    provider = ScriptedProvider(
        [("tool_call", Block("run_sql", {})), ("assistant", Turn(role="assistant"))],
        [("text", "ok")],
    )
    list(FidelityRunner(provider, tools=[RecordingTool()]).run(messages))
    assert messages == [{"role": "user", "content": "hi"}]
