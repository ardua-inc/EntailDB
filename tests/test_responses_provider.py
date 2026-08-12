"""OpenAI's `/v1/responses` protocol.

A different shape from chat completions, not a variant of it: the system prompt
is `instructions`, tools are flat, tool results return as `function_call_output`
items, and the model's own output items — including opaque `reasoning` items —
are replayed as input on the next round.

Two properties are worth more than the rest. Reasoning items must survive a
round trip, because this code cannot reconstruct them and a model that loses
them loses its own chain of thought mid-conversation. And `store` must be
false: the endpoint retains conversations by default, and this tool's traffic
is database schemas and query results.
"""

from __future__ import annotations

import json

import httpx
import pytest

from fidelity.runner import ProviderError, ToolCall, ToolOutcome, ToolSpec, Turn

openai = pytest.importorskip("openai")

from app.providers import OpenAIResponsesProvider  # noqa: E402

SPEC = ToolSpec(name="run_sql", description="Run a query.",
                schema={"type": "object", "properties": {"query": {"type": "string"}}})


def _response(output: list[dict]) -> dict:
    return {"id": "resp_1", "object": "response", "created_at": 0, "model": "m",
            "status": "completed", "output": output, "parallel_tool_calls": False,
            "tool_choice": "auto", "tools": []}


def sse(*events: dict) -> bytes:
    body = b""
    for i, event in enumerate(events):
        payload = {**event, "sequence_number": i}
        body += (f"event: {event['type']}\ndata: ".encode()
                 + json.dumps(payload).encode() + b"\n\n")
    return body


def text_delta(delta: str) -> dict:
    return {"type": "response.output_text.delta", "delta": delta, "item_id": "i",
            "output_index": 0, "content_index": 0}


def completed(output: list[dict]) -> dict:
    return {"type": "response.completed", "response": _response(output)}


def function_call(name="run_sql", arguments='{"query": "SELECT 1"}',
                  call_id="call_1") -> dict:
    return {"type": "function_call", "id": "fc_1", "call_id": call_id,
            "name": name, "arguments": arguments, "status": "completed"}


def provider_for(body: bytes, capture: list | None = None):
    def handler(request):
        if capture is not None:
            capture.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=body)

    p = OpenAIResponsesProvider.__new__(OpenAIResponsesProvider)
    p.model, p.max_tokens = "gpt-5.6-terra", 4096
    p.client = openai.OpenAI(api_key="k", max_retries=0, base_url="http://stub/v1",
                             http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return p


def run(body: bytes, tools=(SPEC,), transcript=None, capture=None):
    p = provider_for(body, capture)
    return list(p.stream_turn("system", transcript or [Turn(role="user", text="hi")],
                              list(tools)))


# ── streaming ─────────────────────────────────────────────────────────────

def test_text_deltas_stream_through():
    events = run(sse(text_delta("There are "), text_delta("2."), completed([])))
    assert "".join(v for k, v in events if k == "text") == "There are 2."


def test_a_tool_call_is_read_complete_from_the_final_response():
    """No fragment assembly here — the terminal event carries whole arguments,
    so the risk of parsing a partial payload simply does not arise."""
    events = run(sse(completed([function_call()])))
    calls = [v for k, v in events if k == "tool_call"]
    assert len(calls) == 1
    assert calls[0].name == "run_sql"
    assert calls[0].id == "call_1"
    assert calls[0].arguments == {"query": "SELECT 1"}


def test_parallel_calls_are_all_reported():
    events = run(sse(completed([
        function_call(call_id="a", arguments='{"query": "A"}'),
        function_call(call_id="b", arguments='{"query": "B"}'),
    ])))
    calls = [v for k, v in events if k == "tool_call"]
    assert [c.id for c in calls] == ["a", "b"]


def test_unparseable_arguments_are_reported_rather_than_guessed():
    with pytest.raises(ProviderError, match="not valid JSON"):
        run(sse(completed([function_call(arguments='{"query": "SELECT')])))


def test_arguments_that_are_not_an_object_are_refused():
    with pytest.raises(ProviderError, match="not an object"):
        run(sse(completed([function_call(arguments='"a string"')])))


# ── reasoning items must survive the round trip ───────────────────────────

def test_reasoning_items_are_kept_for_replay():
    """`Turn.raw` earns its place here. A reasoning item carries state this
    code cannot rebuild; dropping it loses the model's own chain of thought
    between rounds of the same question."""
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": []}
    events = run(sse(completed([reasoning, function_call()])))
    turn = next(v for k, v in events if k == "assistant")
    assert [item["type"] for item in turn.raw] == ["reasoning", "function_call"]


def test_a_replayed_assistant_turn_is_sent_back_verbatim():
    captured: list[dict] = []
    raw = [{"type": "reasoning", "id": "rs_1", "summary": []},
           {"type": "function_call", "call_id": "c1", "name": "run_sql",
            "arguments": "{}"}]
    transcript = [
        Turn(role="user", text="q"),
        Turn(role="assistant", raw=raw, raw_kind="openai_responses"),
        Turn(role="tool", outcomes=[ToolOutcome(call_id="c1", content="rows")]),
    ]
    run(sse(completed([])), transcript=transcript, capture=captured)
    sent = captured[0]["input"]
    assert sent[1] == raw[0]                  # reasoning replayed untouched
    assert sent[2] == raw[1]
    assert sent[3] == {"type": "function_call_output", "call_id": "c1",
                       "output": "rows"}


def test_anthropic_raw_is_not_replayed_into_this_endpoint():
    """The failure this tag exists to prevent, from the other side."""
    captured: list[dict] = []
    transcript = [Turn(role="assistant", text="Checking.",
                       calls=[ToolCall(id="c", name="run_sql", arguments={})],
                       raw=[{"type": "tool_use", "id": "c", "name": "run_sql",
                             "input": {}}],
                       raw_kind="anthropic")]
    run(sse(completed([])), transcript=transcript, capture=captured)
    kinds = [i.get("type") or i.get("role") for i in captured[0]["input"]]
    assert "tool_use" not in kinds
    assert "function_call" in kinds


def test_an_assistant_turn_without_raw_is_rebuilt():
    captured: list[dict] = []
    transcript = [Turn(role="assistant", text="Checking.",
                       calls=[ToolCall(id="c", name="run_sql",
                                       arguments={"query": "SELECT 1"})])]
    run(sse(completed([])), transcript=transcript, capture=captured)
    sent = captured[0]["input"]
    assert sent[0] == {"role": "assistant", "content": "Checking."}
    assert sent[1]["type"] == "function_call"
    assert json.loads(sent[1]["arguments"]) == {"query": "SELECT 1"}


# ── request shape ─────────────────────────────────────────────────────────

def test_the_system_prompt_goes_in_instructions():
    captured: list[dict] = []
    run(sse(completed([])), capture=captured)
    assert captured[0]["instructions"] == "system"
    # ...and not smuggled in as a message, which would double it.
    assert all(m.get("role") != "system" for m in captured[0]["input"])


def test_tools_are_flat_rather_than_wrapped_in_a_function_envelope():
    captured: list[dict] = []
    run(sse(completed([])), capture=captured)
    tool = captured[0]["tools"][0]
    assert tool == {"type": "function", "name": "run_sql",
                    "description": "Run a query.", "parameters": SPEC.schema}


def test_conversations_are_not_stored_by_the_vendor():
    """This endpoint retains conversations by default. The traffic here is
    database schemas and query results, which must not be left in a vendor's
    store as a side effect of a default nobody chose."""
    captured: list[dict] = []
    run(sse(completed([])), capture=captured)
    assert captured[0]["store"] is False


def test_the_token_limit_uses_this_protocol_s_name():
    captured: list[dict] = []
    run(sse(completed([])), capture=captured)
    assert captured[0]["max_output_tokens"] == 4096
    assert "max_tokens" not in captured[0]


def test_tools_are_omitted_when_there_are_none():
    captured: list[dict] = []
    run(sse(completed([])), tools=(), capture=captured)
    assert "tools" not in captured[0]


def test_a_tool_failure_is_marked_in_the_output():
    captured: list[dict] = []
    transcript = [Turn(role="tool", outcomes=[
        ToolOutcome(call_id="c1", content="syntax error", is_error=True)])]
    run(sse(completed([])), transcript=transcript, capture=captured)
    assert captured[0]["input"][0]["output"].startswith("[error]")


def test_a_stream_that_never_completes_still_yields_its_turn():
    """A truncated stream must not lose the prose already shown to the user."""
    events = run(sse(text_delta("partial")))
    turn = next(v for k, v in events if k == "assistant")
    assert turn.text == "partial"
    assert turn.calls == []
