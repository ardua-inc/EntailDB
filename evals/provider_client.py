"""Present the Anthropic SDK's `messages.create` surface over any provider.

The measured runners (`evals/runners/`) call `client.messages.create(...)` and
read Anthropic-shaped content blocks back. Rewriting them to speak the neutral
transcript would have meant changing the measurement instrument itself, and
then every cross-provider difference would be confounded with a change in how
the runs were driven.

So the runners are left exactly as they were and the *client* is swapped. Every
provider — Anthropic included — is now driven through this one shim, which
makes the four sets of numbers comparable to each other. It also means the
existing Anthropic results were produced by a different instrument than the new
ones, which is why Anthropic is re-run rather than quoted.

This is a test harness, not shipping code: it exists to make one interface look
like another, and it says so rather than pretending to be a general adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app import providers
from fidelity.runner import ToolCall, ToolOutcome, ToolSpec, Turn


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class _ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _Response:
    content: list[Any] = field(default_factory=list)
    stop_reason: str | None = None
    usage: _Usage = field(default_factory=_Usage)


def _to_transcript(messages: list[dict[str, Any]]) -> list[Turn]:
    """Anthropic wire format back into neutral turns.

    The runners build Anthropic-shaped messages, so this is the inverse of
    `AnthropicProvider._messages`. Assistant turns keep their original blocks
    in `raw`, so an Anthropic run replays byte-identically to what it would
    have sent before this shim existed.
    """
    turns: list[Turn] = []
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if isinstance(content, str):
            turns.append(Turn(role=role, text=content))
            continue
        blocks = content or []
        if role == "user" and any(b.get("type") == "tool_result" for b in blocks):
            turns.append(Turn(role="tool", outcomes=[
                ToolOutcome(call_id=b["tool_use_id"], content=b.get("content", ""),
                            is_error=bool(b.get("is_error")))
                for b in blocks if b.get("type") == "tool_result"
            ]))
        elif role == "assistant":
            turn = Turn(role="assistant", raw=blocks)
            for b in blocks:
                if b.get("type") == "text":
                    turn.text += b.get("text", "")
                elif b.get("type") == "tool_use":
                    turn.calls.append(ToolCall(id=b["id"], name=b["name"],
                                               arguments=dict(b.get("input") or {})))
            turns.append(turn)
        else:
            turns.append(Turn(role="user", text="".join(
                b.get("text", "") for b in blocks)))
    return turns


class ProviderClient:
    """A stand-in for `anthropic.Anthropic()` backed by any registered kind."""

    def __init__(self, kind: str, model: str, api_key: str = "",
                 base_url: str = "", max_tokens: int = 4096) -> None:
        self.kind = kind
        self.model = model
        self.provider = providers.build(kind, model=model, api_key=api_key,
                                        base_url=base_url, max_tokens=max_tokens)

    # `client.messages.create(...)` with no `messages` attribute to speak of.
    @property
    def messages(self) -> "ProviderClient":
        return self

    def create(self, *, system: Any, tools: list[dict[str, Any]],
               messages: list[dict[str, Any]], model: str = "",
               max_tokens: int = 4096, **_ignored: Any) -> _Response:
        prompt = system if isinstance(system, str) else "".join(
            block.get("text", "") for block in (system or []))
        specs = [ToolSpec(name=t["name"], description=t.get("description", ""),
                          schema=t.get("input_schema", {})) for t in (tools or [])]

        response = _Response()
        text_parts: list[str] = []
        for kind, payload in self.provider.stream_turn(
            prompt, _to_transcript(messages), specs
        ):
            if kind == "text":
                text_parts.append(payload)
            elif kind == "tool_call":
                response.content.append(_ToolUseBlock(
                    id=payload.id, name=payload.name, input=payload.arguments))
            elif kind == "usage":
                response.usage = _Usage(
                    input_tokens=payload.get("input_tokens", 0),
                    output_tokens=payload.get("output_tokens", 0),
                    cache_read_input_tokens=payload.get("cache_read_tokens", 0),
                )

        text = "".join(text_parts)
        if text:
            response.content.insert(0, _TextBlock(text=text))
        response.stop_reason = ("tool_use"
                                if any(b.type == "tool_use" for b in response.content)
                                else "end_turn")
        return response


def from_spec(spec: str, api_key: str = "", base_url: str = "") -> ProviderClient:
    """Build from a `kind:model` string, e.g. `openai:qwen3.6`."""
    kind, _, model = spec.partition(":")
    if not model:
        raise ValueError(f"expected 'kind:model', got {spec!r}")
    return ProviderClient(kind, model, api_key=api_key, base_url=base_url)
