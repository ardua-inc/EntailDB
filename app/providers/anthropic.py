"""The Anthropic adapter, native rather than through a compatibility layer.

Kept separate from the OpenAI-compatible one for prompt caching and for tool
use that needs no fragment assembly.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterator

from fidelity.runner import ProviderError, ToolCall, ToolSpec, Turn

from .base import DEFAULT_MODEL, MAX_RETRIES, RetryingProvider, register


@register
class AnthropicProvider(RetryingProvider):
    """Native Anthropic, kept for prompt caching and first-class tool use."""

    kind = "anthropic"
    label = "Anthropic"
    model_hint = "claude-sonnet-5"
    base_url_hint = "blank for the provider default"
    vendor = "Claude's API"

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 max_tokens: int = 4096, base_url: str = "") -> None:
        import anthropic
        self.client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
            max_retries=MAX_RETRIES,
            **({"base_url": base_url} if base_url else {}),
        )
        self.model = model
        self.max_tokens = max_tokens

    def _translate(self, exc: Exception) -> Exception:
        return _translate(exc)

    # ── translation ───────────────────────────────────────────────────────

    @staticmethod
    def _messages(transcript: list[Turn]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for turn in transcript:
            if turn.role == "tool":
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": o.call_id,
                     "content": o.content, "is_error": o.is_error}
                    for o in turn.outcomes
                ]})
            elif turn.role == "assistant":
                # `raw` replayed verbatim where the adapter produced it: an
                # assistant turn rebuilt from text and calls would drop block
                # signatures, which Anthropic requires and cannot be recomputed.
                native = turn.native("anthropic")
                if native is not None:
                    out.append({"role": "assistant", "content": native})
                    continue
                blocks: list[dict[str, Any]] = []
                if turn.text:
                    blocks.append({"type": "text", "text": turn.text})
                blocks.extend({"type": "tool_use", "id": c.id, "name": c.name,
                               "input": c.arguments} for c in turn.calls)
                out.append({"role": "assistant", "content": blocks or ""})
            else:
                out.append({"role": "user", "content": turn.text})
        return out

    @staticmethod
    def _tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [{"name": t.name, "description": t.description,
                 "input_schema": t.schema} for t in tools]

    def _stream(self, system: str, transcript: list[Turn],
                tools: list[ToolSpec]) -> Iterator[tuple[str, Any]]:
        with self.client.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            tools=self._tools(tools),
            messages=self._messages(transcript),
        ) as stream:
            for text in stream.text_stream:
                yield "text", text
            final = stream.get_final_message()

        raw: list[dict[str, Any]] = []
        turn = Turn(role="assistant")
        for block in final.content:
            if block.type == "text":
                raw.append({"type": "text", "text": block.text})
                turn.text += block.text
            elif block.type == "tool_use":
                raw.append({"type": "tool_use", "id": block.id,
                            "name": block.name, "input": block.input})
                call = ToolCall(id=block.id, name=block.name,
                                arguments=dict(block.input or {}))
                turn.calls.append(call)
                yield "tool_call", call
        turn.raw, turn.raw_kind = raw, "anthropic"
        usage = getattr(final, "usage", None)
        if usage is not None:
            yield "usage", {
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            }
        yield "assistant", turn


def _error_type(exc: Exception) -> str:
    """The API's own name for what went wrong, e.g. `overloaded_error`.

    Read from the parsed body where there is one. An error that arrives *inside*
    a stream has no parsed body — the SDK stringifies the event payload — so the
    token is recovered from the message as a fallback. Matching the full
    `*_error` token rather than a bare word keeps prose that happens to mention
    overloading from being mistaken for an outage.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict) and isinstance(inner.get("type"), str):
            return inner["type"]
    match = re.search(r"""['"]type['"]:\s*['"](\w+_error)['"]""", str(exc))
    return match.group(1) if match else ""


def _translate(exc: Exception) -> Exception:
    """Turn an SDK failure into something worth reading.

    Only the cases a person can act on are named. Anything else keeps its class
    and message rather than being flattened into a vague apology — an
    unrecognised error should look unrecognised, not handled.

    Classification leads on the API's own error type, not the HTTP status. The
    outage that prompted this arrived as an `overloaded_error` event on a **200**
    response, so a status-led check skipped it entirely and the raw payload went
    to the answer pane.
    """
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError):
        return ProviderError(
            "The Anthropic API key was rejected. Check it in Settings."
        )
    if isinstance(exc, anthropic.PermissionDeniedError):
        return ProviderError(
            "This API key is not permitted to use that model. Check the model "
            "name in Settings."
        )
    if isinstance(exc, anthropic.NotFoundError):
        return ProviderError(
            "That model does not exist. Check the model name in Settings."
        )

    kind = _error_type(exc)
    status = getattr(exc, "status_code", None)

    if kind == "overloaded_error" or status == 529:
        return ProviderError(
            "Claude's API is temporarily overloaded. This is on Anthropic's "
            "side — not your database, your connection, or your question. It "
            "was retried automatically; ask again in a moment.",
            retryable=True,
        )
    if kind == "rate_limit_error" or isinstance(exc, anthropic.RateLimitError):
        return ProviderError(
            "Rate limited by the Anthropic API. Wait a moment and ask again.",
            retryable=True,
        )
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderError(
            "Could not reach the Anthropic API. Check the network and ask "
            "again.",
            retryable=True,
        )
    if kind == "api_error" or (status is not None and status >= 500):
        return ProviderError(
            f"The Anthropic API returned a server error"
            f"{f' ({status})' if status and status >= 500 else ''}. Nothing is "
            "wrong with your database or your question; ask again shortly.",
            retryable=True,
        )
    return exc
