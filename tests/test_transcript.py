"""Translation between the neutral transcript and each provider's wire format.

The runner used to build Anthropic messages itself, which made
"provider-agnostic" true of the protocol and false of the code. These tests
hold the boundary: the same conversation, expressed once, must come out correct
in two formats that disagree about almost everything — where tool results live
(a user message for Anthropic, a `tool` role for OpenAI), whether a tool
failure has a field or must be said in words, and where the system prompt goes.

The `raw` case is the subtle one and the reason the field exists.
"""

from __future__ import annotations

import json

import pytest

from app.providers import AnthropicProvider, OpenAICompatibleProvider
from fidelity.runner import ToolCall, ToolOutcome, ToolSpec, Turn

SPEC = ToolSpec(name="run_sql", description="Run a query.",
                schema={"type": "object", "properties": {"query": {"type": "string"}}})


@pytest.fixture
def conversation() -> list[Turn]:
    """One full round: question, tool call, result, follow-up question."""
    return [
        Turn(role="user", text="how many stores?"),
        Turn(role="assistant", text="Let me check.",
             calls=[ToolCall(id="c1", name="run_sql",
                             arguments={"query": "SELECT count(*) FROM store"})]),
        Turn(role="tool", outcomes=[
            ToolOutcome(call_id="c1", content='{"rows": [[2]]}')]),
        Turn(role="user", text="and in Utah?"),
    ]


# ── Anthropic ─────────────────────────────────────────────────────────────

def test_anthropic_puts_tool_results_in_a_user_message(conversation):
    wire = AnthropicProvider._messages(conversation)
    assert [m["role"] for m in wire] == ["user", "assistant", "user", "user"]
    block = wire[2]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "c1"
    assert block["content"] == '{"rows": [[2]]}'


def test_anthropic_carries_a_real_error_flag(conversation):
    conversation[2].outcomes[0].is_error = True
    wire = AnthropicProvider._messages(conversation)
    assert wire[2]["content"][0]["is_error"] is True


def test_anthropic_tool_specs_use_input_schema():
    assert AnthropicProvider._tools([SPEC]) == [
        {"name": "run_sql", "description": "Run a query.",
         "input_schema": SPEC.schema}
    ]


def test_a_raw_assistant_turn_is_replayed_verbatim():
    """The reason `Turn.raw` exists.

    Anthropic requires its own content blocks be echoed back exactly; a
    thinking block's signature cannot be recomputed from text. Rebuilding an
    assistant turn from its parts would silently corrupt any conversation that
    used one, so the adapter's own blocks are replayed untouched.
    """
    raw = [{"type": "thinking", "thinking": "...", "signature": "sig-abc"},
           {"type": "text", "text": "Two."}]
    wire = AnthropicProvider._messages([
        Turn(role="assistant", text="Two.", raw=raw, raw_kind="anthropic")])
    assert wire[0]["content"] is raw


def test_another_provider_s_raw_is_ignored_rather_than_replayed():
    """`raw` is provider-native, and replaying it into a different API sends
    content that API has no concept of. Not hypothetical: the eval harness
    builds transcripts from Anthropic wire format, and feeding those to
    OpenAI's Responses endpoint failed 40 of 50 runs on `Invalid value:
    'tool_use'`. A foreign `raw` is dropped and the turn is rebuilt."""
    foreign = [{"type": "reasoning", "id": "rs_1"}]
    wire = AnthropicProvider._messages([
        Turn(role="assistant", text="Two.", raw=foreign,
             raw_kind="openai_responses")])
    assert wire[0]["content"] != foreign
    assert wire[0]["content"][0]["text"] == "Two."


def test_an_assistant_turn_without_raw_is_rebuilt_from_its_parts():
    wire = AnthropicProvider._messages([
        Turn(role="assistant", text="Checking.",
             calls=[ToolCall(id="c9", name="run_sql", arguments={"query": "SELECT 1"})])
    ])
    kinds = [b["type"] for b in wire[0]["content"]]
    assert kinds == ["text", "tool_use"]
    assert wire[0]["content"][1]["input"] == {"query": "SELECT 1"}


# ── OpenAI-compatible ─────────────────────────────────────────────────────

def test_openai_puts_the_system_prompt_in_a_message(conversation):
    wire = OpenAICompatibleProvider._messages("SYSTEM", conversation)
    assert wire[0] == {"role": "system", "content": "SYSTEM"}


def test_openai_uses_a_tool_role_for_results(conversation):
    wire = OpenAICompatibleProvider._messages("s", conversation)
    assert [m["role"] for m in wire] == [
        "system", "user", "assistant", "tool", "user"]
    assert wire[3]["tool_call_id"] == "c1"
    assert wire[3]["content"] == '{"rows": [[2]]}'


def test_openai_serialises_tool_arguments_as_a_json_string(conversation):
    wire = OpenAICompatibleProvider._messages("s", conversation)
    call = wire[2]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "run_sql"
    assert json.loads(call["function"]["arguments"]) == {
        "query": "SELECT count(*) FROM store"}


def test_openai_says_an_error_in_words_because_it_has_no_field(conversation):
    """Anthropic gets a real `is_error` flag; this format has nowhere to put
    one, so the failure is marked in the content rather than silently becoming
    indistinguishable from a successful result."""
    conversation[2].outcomes[0].is_error = True
    wire = OpenAICompatibleProvider._messages("s", conversation)
    assert wire[3]["content"].startswith("[error]")


def test_openai_emits_null_content_for_a_pure_tool_turn():
    """A tool-only assistant turn must not send an empty string, which some
    backends reject."""
    wire = OpenAICompatibleProvider._messages("s", [
        Turn(role="assistant", calls=[ToolCall(id="c", name="run_sql", arguments={})])
    ])
    assert wire[1]["content"] is None


def test_openai_tool_specs_use_the_function_envelope():
    assert OpenAICompatibleProvider._tools([SPEC]) == [
        {"type": "function",
         "function": {"name": "run_sql", "description": "Run a query.",
                      "parameters": SPEC.schema}}
    ]


# ── the two formats describe the same conversation ────────────────────────

def test_both_formats_preserve_the_question_and_the_result(conversation):
    anthropic_wire = json.dumps(AnthropicProvider._messages(conversation))
    openai_wire = json.dumps(OpenAICompatibleProvider._messages("s", conversation))
    for fragment in ("how many stores?", "SELECT count(*) FROM store",
                     '[[2]]', "and in Utah?"):
        assert fragment in anthropic_wire
        assert fragment in openai_wire
