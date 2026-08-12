"""OpenAI chat-completions, and everything that imitates it.

One adapter, many backends: OpenAI up to gpt-4.1/gpt-4o, Ollama, OpenRouter,
vLLM, Groq, Together, and a LiteLLM proxy.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

from fidelity.runner import ProviderError, ToolCall, ToolSpec, Turn

from .base import MAX_RETRIES, RetryingProvider, register


@register
class OpenAICompatibleProvider(RetryingProvider):
    """Anything speaking OpenAI chat-completions.

    One adapter, many backends: OpenAI, Ollama (`/v1`), OpenRouter, vLLM,
    Groq, Together, Azure — and a LiteLLM proxy, which is how this project can
    adopt a central-routing deployment without requiring one.

    The delicate part is streaming tool calls. Arguments arrive as **fragments
    of a JSON string spread across deltas**, keyed by index, and are only valid
    once complete. They are assembled and parsed exactly once; a fragment
    parsed early would produce a *different query* than the model asked for,
    which is precisely the class of silent wrongness this project exists to
    prevent. A payload that never parses is reported, never guessed at.
    """

    kind = "openai"
    label = "OpenAI-compatible (chat completions)"
    model_hint = "qwen3.6 / gpt-4.1"
    base_url_hint = "http://host.docker.internal:11434/v1"
    vendor = "The model API"

    # "OpenAI-compatible" is a family, not a standard. Newer OpenAI models
    # reject `max_tokens` and demand `max_completion_tokens`; Ollama accepts
    # the former and knows nothing of the latter. Rather than making the user
    # know which, the request adapts once to whatever the endpoint says it
    # wants — the local equivalent of LiteLLM's `drop_params`.
    _RENAMES = {"max_tokens": "max_completion_tokens"}

    # Class-level and immutable on purpose. Rebinding in `_create` creates an
    # *instance* attribute, so one endpoint's quirk can never leak into another
    # provider object -- which a shared mutable set would do silently.
    _token_param = "max_tokens"
    _dropped: frozenset[str] = frozenset()

    def __init__(self, api_key: str = "", model: str = "gpt-4o",
                 base_url: str = "", max_tokens: int = 4096) -> None:
        import openai
        self.client = openai.OpenAI(
            # Local backends accept any key; an empty one would fail client
            # construction before it ever reached them.
            api_key=api_key or "not-needed",
            max_retries=MAX_RETRIES,
            **({"base_url": base_url} if base_url else {}),
        )
        self.model = model
        self.max_tokens = max_tokens

    # ── translation ───────────────────────────────────────────────────────

    @staticmethod
    def _messages(system: str, transcript: list[Turn]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for turn in transcript:
            if turn.role == "tool":
                # One message per outcome, and no `is_error` field exists here,
                # so a failure is marked in the content. Anthropic gets a real
                # flag; this is the closest honest equivalent.
                out.extend({
                    "role": "tool", "tool_call_id": o.call_id,
                    "content": f"[error] {o.content}" if o.is_error else o.content,
                } for o in turn.outcomes)
            elif turn.role == "assistant":
                message: dict[str, Any] = {"role": "assistant",
                                           "content": turn.text or None}
                if turn.calls:
                    message["tool_calls"] = [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.name,
                                      "arguments": json.dumps(c.arguments)}}
                        for c in turn.calls
                    ]
                out.append(message)
            else:
                out.append({"role": "user", "content": turn.text})
        return out

    @staticmethod
    def _tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [{"type": "function",
                 "function": {"name": t.name, "description": t.description,
                              "parameters": t.schema}} for t in tools]

    def _stream(self, system: str, transcript: list[Turn],
                tools: list[ToolSpec]) -> Iterator[tuple[str, Any]]:
        request: dict[str, Any] = {
            "model": self.model,
            self._token_param: self.max_tokens,
            "messages": self._messages(system, transcript),
            "stream": True,
            # Usage is not reported on a stream unless asked for, and the eval
            # harness needs it to price a run.
            "stream_options": {"include_usage": True},
        }
        if tools:
            request["tools"] = self._tools(tools)
        for name in self._dropped:
            request.pop(name, None)

        turn = Turn(role="assistant")
        fragments: dict[int, dict[str, str]] = {}
        usage = None

        for chunk in self._create(request):
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if getattr(delta, "content", None):
                turn.text += delta.content
                yield "text", delta.content
            for part in (getattr(delta, "tool_calls", None) or []):
                slot = fragments.setdefault(
                    part.index, {"id": "", "name": "", "arguments": ""})
                if part.id:
                    slot["id"] = part.id
                function = getattr(part, "function", None)
                if function is not None:
                    if getattr(function, "name", None):
                        slot["name"] = function.name
                    if getattr(function, "arguments", None):
                        slot["arguments"] += function.arguments

        # Parsed only now that every fragment has arrived.
        for index in sorted(fragments):
            slot = fragments[index]
            if not slot["name"]:
                continue
            body = slot["arguments"].strip() or "{}"
            try:
                arguments = json.loads(body)
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    f"The model sent tool arguments that are not valid JSON "
                    f"({exc.msg}). Nothing was run — the request was not "
                    "guessed at."
                ) from exc
            if not isinstance(arguments, dict):
                raise ProviderError(
                    "The model sent tool arguments that are not an object. "
                    "Nothing was run."
                )
            call = ToolCall(id=slot["id"] or f"call_{index}",
                            name=slot["name"], arguments=arguments)
            turn.calls.append(call)
            yield "tool_call", call

        if usage is not None:
            yield "usage", {
                "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
                "cache_read_tokens": 0,
            }
        yield "assistant", turn

    def _create(self, request: dict[str, Any]):
        """Send the request, adapting once to a parameter the endpoint refuses.

        Only a parameter the API *names as unsupported* is touched, and only
        once — a rejection of a parameter's **value** is a real error and must
        surface as one rather than being retried into silence.
        """
        try:
            return self.client.chat.completions.create(**request)
        except Exception as exc:  # noqa: BLE001 — inspected, then re-raised
            param = _unsupported_param(exc)
            if not param or param not in request:
                raise
            replacement = self._RENAMES.get(param)
            value = request.pop(param)
            if replacement:
                request[replacement] = value
                if param == "max_tokens":
                    self._token_param = replacement
            else:
                # Remembered per instance so the adaptation costs one rejected
                # request per turn at most, not one per tool round.
                self._dropped = self._dropped | {param}
            return self.client.chat.completions.create(**request)

    def _translate(self, exc: Exception) -> Exception:
        return _translate_openai(exc)


def _translate_openai(exc: Exception) -> Exception:
    """The OpenAI-side equivalent of `_translate`.

    Written against the SDK's exception classes rather than status codes where
    possible, for the same reason the Anthropic version leads on error type: an
    overload can arrive without the status that would identify it.
    """
    try:
        import openai
    except ImportError:                                   # pragma: no cover
        return exc

    if isinstance(exc, openai.AuthenticationError):
        return ProviderError(
            "The API key for this model was rejected. Check it in Settings."
        )
    if isinstance(exc, openai.PermissionDeniedError):
        return ProviderError(
            "This key is not permitted to use that model. Check the model name "
            "in Settings."
        )
    if isinstance(exc, openai.NotFoundError):
        return ProviderError(
            "That model does not exist at this endpoint. Check the model name "
            "and base URL in Settings."
        )
    if isinstance(exc, openai.RateLimitError):
        return ProviderError(
            "Rate limited by the model API. Wait a moment and ask again.",
            retryable=True,
        )
    if isinstance(exc, openai.APIConnectionError):
        return ProviderError(
            "Could not reach the model endpoint. Check the base URL in "
            "Settings and that the service is running.",
            retryable=True,
        )
    # Some newer OpenAI models only accept function tools on `/v1/responses`,
    # a different protocol from chat-completions. Nothing this adapter sends
    # can satisfy them, so the message names the real choice rather than
    # leaving a wall of vendor JSON in the answer pane.
    text = str(exc)
    if "/v1/responses" in text or "Function tools" in text:
        return ProviderError(
            "This model does not support tool use over the chat-completions "
            "API, which is what this connector speaks — it needs OpenAI's "
            "newer /v1/responses protocol. Nothing is wrong with your database "
            "or your question. Choose a model that supports chat-completions "
            "tools (gpt-4.1, gpt-4.1-mini, gpt-4o all do) in Settings."
        )

    status = getattr(exc, "status_code", None)
    if isinstance(exc, openai.APIStatusError) and status is not None and status >= 500:
        return ProviderError(
            f"The model endpoint returned a server error ({status}). Nothing "
            "is wrong with your database or your question; ask again shortly.",
            retryable=True,
        )
    return exc


def _unsupported_param(exc: Exception) -> str:
    """The parameter an endpoint says it does not support, if it said so.

    Deliberately narrow. Only an explicit "unsupported/unknown parameter" code
    counts: a 400 about a parameter's *value* means the request was wrong, and
    quietly dropping the parameter would turn a real error into a different
    request than the one intended.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            code = error.get("code") or ""
            param = error.get("param")
            if isinstance(param, str) and code in (
                "unsupported_parameter", "unknown_parameter", "unsupported_value"
            ):
                return param
    match = re.search(r"Unsupported parameter: '([^']+)'", str(exc))
    return match.group(1) if match else ""
