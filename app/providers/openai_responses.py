"""OpenAI's `/v1/responses` protocol, where current OpenAI models live."""

from __future__ import annotations

import json
from typing import Any, Iterator

from fidelity.runner import ProviderError, ToolCall, ToolSpec, Turn

from .base import MAX_RETRIES, RetryingProvider, register
from .openai_chat import _translate_openai


@register
class OpenAIResponsesProvider(RetryingProvider):
    """OpenAI's `/v1/responses` protocol.

    Where the newest OpenAI models live. It is not a variant of
    chat-completions but a different shape: the system prompt is
    `instructions`, tools are flat rather than nested under `function`, tool
    results go back as `function_call_output` items, and the model's own output
    items — including opaque `reasoning` items — are replayed as input.

    Those reasoning items are why `Turn.raw` exists. They carry state this code
    cannot reconstruct, so an assistant turn is echoed exactly as it arrived
    rather than rebuilt from its text and calls.

    Tool arguments are read from the terminal `response.completed` event, where
    each `function_call` item carries its arguments complete. The
    chat-completions adapter has to assemble fragments and risk parsing a
    partial payload; here that risk simply does not exist, so it is not taken.
    """

    kind = "openai_responses"
    label = "OpenAI Responses (gpt-5 and later)"
    model_hint = "gpt-5.6-terra"
    base_url_hint = "blank for the provider default"
    vendor = "The model API"

    def __init__(self, api_key: str = "", model: str = "gpt-5.6-terra",
                 base_url: str = "", max_tokens: int = 4096) -> None:
        import openai
        self.client = openai.OpenAI(
            api_key=api_key or "not-needed",
            max_retries=MAX_RETRIES,
            **({"base_url": base_url} if base_url else {}),
        )
        self.model = model
        self.max_tokens = max_tokens

    # ── translation ───────────────────────────────────────────────────────

    @staticmethod
    def _input(transcript: list[Turn]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for turn in transcript:
            if turn.role == "tool":
                items.extend({"type": "function_call_output", "call_id": o.call_id,
                              "output": f"[error] {o.content}" if o.is_error
                                        else o.content}
                             for o in turn.outcomes)
            elif turn.role == "assistant":
                native = turn.native("openai_responses")
                if native is not None:
                    items.extend(native)
                    continue
                if turn.text:
                    items.append({"role": "assistant", "content": turn.text})
                items.extend({"type": "function_call", "call_id": c.id,
                              "name": c.name, "arguments": json.dumps(c.arguments)}
                             for c in turn.calls)
            else:
                items.append({"role": "user", "content": turn.text})
        return items

    @staticmethod
    def _tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
        # Flat, unlike chat-completions' `{"function": {...}}` envelope.
        return [{"type": "function", "name": t.name,
                 "description": t.description, "parameters": t.schema}
                for t in tools]

    def _stream(self, system: str, transcript: list[Turn],
                tools: list[ToolSpec]) -> Iterator[tuple[str, Any]]:
        request: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": self._input(transcript),
            "max_output_tokens": self.max_tokens,
            "stream": True,
            # This endpoint retains conversations by default. A tool whose
            # traffic is database schemas and query results must not leave them
            # sitting in a vendor's store as a side effect of a default.
            "store": False,
        }
        if tools:
            request["tools"] = self._tools(tools)

        turn = Turn(role="assistant")
        final = None
        for event in self.client.responses.create(**request):
            kind = getattr(event, "type", "")
            if kind == "response.output_text.delta":
                delta = getattr(event, "delta", "")
                if delta:
                    turn.text += delta
                    yield "text", delta
            elif kind == "response.completed":
                final = getattr(event, "response", None)

        if final is None:
            yield "assistant", turn
            return

        # Every output item is replayed on the next round, reasoning included.
        turn.raw = [item.model_dump(exclude_none=True) for item in final.output]
        turn.raw_kind = "openai_responses"
        for item in final.output:
            if getattr(item, "type", "") != "function_call":
                continue
            body = (getattr(item, "arguments", "") or "").strip() or "{}"
            try:
                arguments = json.loads(body)
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    f"The model sent tool arguments that are not valid JSON "
                    f"({exc.msg}). Nothing was run."
                ) from exc
            if not isinstance(arguments, dict):
                raise ProviderError(
                    "The model sent tool arguments that are not an object. "
                    "Nothing was run."
                )
            call = ToolCall(id=item.call_id, name=item.name, arguments=arguments)
            turn.calls.append(call)
            yield "tool_call", call

        usage = getattr(final, "usage", None)
        if usage is not None:
            yield "usage", {
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                "cache_read_tokens": 0,
            }
        yield "assistant", turn

    def _translate(self, exc: Exception) -> Exception:
        return _translate_openai(exc)
