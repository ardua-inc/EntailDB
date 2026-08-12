"""Streaming against an OpenAI-compatible endpoint, driven by a mock transport.

The delicate part is tool calls. Arguments arrive as **fragments of a JSON
string spread across deltas**, keyed by index, and are meaningless until every
fragment has landed. Parsing early does not raise — it produces a *different
query* than the model asked for, runs it, and reports the answer confidently.
That is the exact failure this whole project exists to prevent, arriving
through the transport rather than the model, so it gets its own tests.

Everything here runs against `httpx.MockTransport` emitting real
chat-completions SSE, the same approach `test_providers.py` uses for Anthropic.
No network, no key, no model.
"""

from __future__ import annotations

import json

import httpx
import pytest

from fidelity.runner import ProviderError, ToolSpec, Turn

openai = pytest.importorskip("openai")

from app.providers import OpenAICompatibleProvider  # noqa: E402

SPEC = ToolSpec(name="run_sql", description="Run a query.",
                schema={"type": "object", "properties": {"query": {"type": "string"}}})


def sse(*chunks: dict) -> bytes:
    body = b""
    for chunk in chunks:
        payload = {"id": "x", "object": "chat.completion.chunk", "created": 0,
                   "model": "m", **chunk}
        body += b"data: " + json.dumps(payload).encode() + b"\n\n"
    return body + b"data: [DONE]\n\n"


def delta(**fields) -> dict:
    return {"choices": [{"index": 0, "delta": fields, "finish_reason": None}]}


def provider_for(body: bytes) -> OpenAICompatibleProvider:
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=body)

    p = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
    p.model, p.max_tokens = "test-model", 64
    p.client = openai.OpenAI(
        api_key="not-needed", max_retries=0, base_url="http://stub/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return p


def run(body: bytes, tools=(SPEC,)) -> list[tuple[str, object]]:
    return list(provider_for(body).stream_turn(
        "system", [Turn(role="user", text="hi")], list(tools)))


# ── prose ─────────────────────────────────────────────────────────────────

def test_text_deltas_stream_through():
    events = run(sse(delta(content="There are "), delta(content="2 stores.")))
    text = "".join(v for k, v in events if k == "text")
    assert text == "There are 2 stores."


def test_the_assistant_turn_carries_the_whole_answer():
    events = run(sse(delta(content="Two"), delta(content=" stores.")))
    turn = next(v for k, v in events if k == "assistant")
    assert turn.role == "assistant"
    assert turn.text == "Two stores."
    assert turn.calls == []


# ── tool calls assembled from fragments ───────────────────────────────────

def test_arguments_split_across_deltas_are_assembled_before_parsing():
    """The case that matters. Each fragment is invalid JSON on its own."""
    events = run(sse(
        delta(tool_calls=[{"index": 0, "id": "call_1", "type": "function",
                           "function": {"name": "run_sql", "arguments": '{"que'}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": 'ry": "SELECT '}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": 'count(*) FROM store"}'}}]),
    ))
    calls = [v for k, v in events if k == "tool_call"]
    assert len(calls) == 1
    assert calls[0].id == "call_1"
    assert calls[0].name == "run_sql"
    assert calls[0].arguments == {"query": "SELECT count(*) FROM store"}


def test_a_tool_call_is_only_emitted_once_complete():
    """No partially-assembled call may reach the runner: it would execute a
    query the model never asked for."""
    events = run(sse(
        delta(tool_calls=[{"index": 0, "id": "c", "type": "function",
                           "function": {"name": "run_sql", "arguments": '{"query": "SEL'}}]),
        delta(content="thinking"),
        delta(tool_calls=[{"index": 0, "function": {"arguments": 'ECT 1"}'}}]),
    ))
    kinds = [k for k, _ in events]
    assert kinds.count("tool_call") == 1
    # Emitted after the stream is drained, never mid-assembly.
    assert kinds.index("tool_call") > kinds.index("text")


def test_parallel_tool_calls_are_kept_apart_by_index():
    events = run(sse(
        delta(tool_calls=[
            {"index": 0, "id": "a", "type": "function",
             "function": {"name": "run_sql", "arguments": '{"query": "A"}'}},
            {"index": 1, "id": "b", "type": "function",
             "function": {"name": "run_sql", "arguments": '{"query": '}},
        ]),
        delta(tool_calls=[{"index": 1, "function": {"arguments": '"B"}'}}]),
    ))
    calls = [v for k, v in events if k == "tool_call"]
    assert [c.id for c in calls] == ["a", "b"]
    assert [c.arguments["query"] for c in calls] == ["A", "B"]


def test_a_call_with_no_arguments_becomes_an_empty_object():
    events = run(sse(
        delta(tool_calls=[{"index": 0, "id": "c", "type": "function",
                           "function": {"name": "run_sql", "arguments": ""}}]),
    ))
    calls = [v for k, v in events if k == "tool_call"]
    assert calls[0].arguments == {}


def test_the_assistant_turn_records_the_calls_for_replay():
    events = run(sse(
        delta(content="Checking."),
        delta(tool_calls=[{"index": 0, "id": "c", "type": "function",
                           "function": {"name": "run_sql",
                                        "arguments": '{"query": "SELECT 1"}'}}]),
    ))
    turn = next(v for k, v in events if k == "assistant")
    assert turn.text == "Checking."
    assert [c.name for c in turn.calls] == ["run_sql"]


# ── malformed payloads are reported, never guessed ────────────────────────

def test_unparseable_arguments_are_reported_rather_than_guessed():
    """A truncated payload must not be repaired or partially executed. Running
    a query the model did not ask for is worse than failing the turn."""
    with pytest.raises(ProviderError, match="not valid JSON"):
        run(sse(delta(tool_calls=[
            {"index": 0, "id": "c", "type": "function",
             "function": {"name": "run_sql", "arguments": '{"query": "SELECT'}}])))


def test_arguments_that_are_not_an_object_are_refused():
    with pytest.raises(ProviderError, match="not an object"):
        run(sse(delta(tool_calls=[
            {"index": 0, "id": "c", "type": "function",
             "function": {"name": "run_sql", "arguments": '"just a string"'}}])))


def test_a_fragment_with_no_name_is_ignored_not_invented():
    events = run(sse(delta(tool_calls=[
        {"index": 0, "function": {"arguments": "{}"}}])))
    assert not [v for k, v in events if k == "tool_call"]


# ── request shaping ───────────────────────────────────────────────────────

def test_tools_are_omitted_entirely_when_there_are_none():
    """Some backends reject an empty `tools` array outright."""
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse(delta(content="ok")))

    p = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
    p.model, p.max_tokens = "test-model", 64
    p.client = openai.OpenAI(api_key="k", max_retries=0, base_url="http://stub/v1",
                             http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    list(p.stream_turn("system", [Turn(role="user", text="hi")], []))
    assert "tools" not in seen
    assert seen["messages"][0]["role"] == "system"


# ── error translation ─────────────────────────────────────────────────────

def _error(status: int) -> OpenAICompatibleProvider:
    def handler(request):
        return httpx.Response(status, json={"error": {"message": "nope"}})

    p = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
    p.model, p.max_tokens = "m", 16
    p.client = openai.OpenAI(api_key="k", max_retries=0, base_url="http://stub/v1",
                             http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return p


@pytest.mark.parametrize("status,fragment", [
    (401, "rejected"),
    (404, "does not exist"),
    (429, "Rate limited"),
])
def test_endpoint_errors_are_translated(status, fragment, monkeypatch):
    from app import providers
    from app.providers import base as providers_base

    monkeypatch.setattr(providers_base, "STREAM_BACKOFF", (0, 0, 0, 0, 0))
    with pytest.raises(ProviderError, match=fragment):
        list(_error(status).stream_turn("s", [Turn(role="user", text="hi")], []))


def test_an_unreachable_endpoint_names_the_base_url_setting(monkeypatch):
    """The most likely misconfiguration for a local model is a wrong or
    unstarted endpoint, so the message points at it."""
    from app import providers
    from app.providers import base as providers_base

    monkeypatch.setattr(providers_base, "STREAM_BACKOFF", (0, 0, 0, 0, 0))

    def handler(request):
        raise httpx.ConnectError("refused")

    p = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
    p.model, p.max_tokens = "m", 16
    p.client = openai.OpenAI(api_key="k", max_retries=0, base_url="http://stub/v1",
                             http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError, match="base URL"):
        list(p.stream_turn("s", [Turn(role="user", text="hi")], []))


# ── endpoints that disagree about parameter names ─────────────────────────

def adapting_provider(reject: str | None, code: str = "unsupported_parameter"):
    """An endpoint that refuses one parameter, then answers."""
    seen: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if reject and reject in body:
            return httpx.Response(400, json={"error": {
                "message": f"Unsupported parameter: '{reject}' is not supported "
                           f"with this model.",
                "type": "invalid_request_error", "param": reject, "code": code}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse(delta(content="ok")))

    p = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
    p.model, p.max_tokens = "gpt-5.6-terra", 4096
    p.client = openai.OpenAI(api_key="k", max_retries=0, base_url="http://stub/v1",
                             http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return p, seen


def test_max_tokens_is_renamed_when_the_model_demands_it():
    """The reported failure: a newer OpenAI model rejects `max_tokens` and asks
    for `max_completion_tokens`. Ollama wants the opposite, so neither can be
    hard-coded and the user should not have to know which."""
    p, seen = adapting_provider("max_tokens")
    events = list(p.stream_turn("s", [Turn(role="user", text="hi")], []))

    assert "".join(v for k, v in events if k == "text") == "ok"
    assert "max_tokens" in seen[0]                    # tried the common spelling
    assert seen[1]["max_completion_tokens"] == 4096   # then the one it asked for
    assert "max_tokens" not in seen[1]


def test_the_rename_is_remembered_for_later_rounds():
    """A tool loop makes several requests; paying a rejection on each would
    double the round trips for the whole conversation."""
    p, seen = adapting_provider("max_tokens")
    list(p.stream_turn("s", [Turn(role="user", text="hi")], []))
    list(p.stream_turn("s", [Turn(role="user", text="again")], []))
    assert len(seen) == 3                             # 2 attempts, then 1
    assert "max_completion_tokens" in seen[2]


def test_an_unsupported_parameter_with_no_rename_is_dropped():
    p, seen = adapting_provider("tools")
    list(p.stream_turn("s", [Turn(role="user", text="hi")], [SPEC]))
    assert "tools" in seen[0]
    assert "tools" not in seen[1]


def test_a_rejected_value_is_not_quietly_adapted():
    """A 400 about a parameter's *value* is a real error. Dropping the
    parameter would send a different request than the one intended and report
    an answer to a question nobody asked."""
    def handler(request):
        return httpx.Response(400, json={"error": {
            "message": "Invalid value for 'max_tokens': must be positive.",
            "type": "invalid_request_error", "param": "max_tokens",
            "code": "invalid_value"}})

    p = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
    p.model, p.max_tokens = "m", -1
    p.client = openai.OpenAI(api_key="k", max_retries=0, base_url="http://stub/v1",
                             http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(Exception) as caught:
        list(p.stream_turn("s", [Turn(role="user", text="hi")], []))
    assert "Invalid value" in str(caught.value)


def test_an_unknown_parameter_is_not_invented_from_a_vague_400():
    """Nothing is adapted unless the endpoint names the parameter."""
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "something is wrong"}})

    p = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
    p.model, p.max_tokens = "m", 16
    p.client = openai.OpenAI(api_key="k", max_retries=0, base_url="http://stub/v1",
                             http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(Exception):
        list(p.stream_turn("s", [Turn(role="user", text="hi")], []))


def test_a_model_needing_the_responses_api_says_which_models_work():
    """`gpt-5.6-terra` accepts function tools only on `/v1/responses`. No
    parameter this adapter can send will satisfy it, so the message names the
    actual choice instead of leaving vendor JSON in the answer pane."""
    from app.providers import _translate_openai

    exc = openai.BadRequestError(
        "Function tools with reasoning_effort are not supported for "
        "gpt-5.6-terra in /v1/chat/completions. To use function tools, use "
        "/v1/responses.",
        response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
        body=None,
    )
    translated = _translate_openai(exc)
    assert isinstance(translated, ProviderError)
    text = str(translated)
    assert "chat-completions" in text
    assert "gpt-4.1" in text                     # names a model that works
    assert "your database" in text               # and rules out the obvious fear
