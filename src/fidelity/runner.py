"""The fidelity runner: an agentic tool loop with the guards attached.

Provider-agnostic and transport-agnostic. It takes an object that can stream a
model turn, a set of tools, and a list of guards; it yields events. It does not
know what a database, a credential, or an HTTP response is.

**What is wired in here, and why**, because the evaluation contradicted parts of
the original design and the code should reflect the measurement rather than the
intention:

* **The accuracy instruction is not optional.** It is the only intervention
  with a measured effect — `stale-fact` went 20/20 to 0/20 on the model that
  produced the original incident. It is a constructor default rather than
  something a caller assembles, because leaving it out is the one change known
  to make output worse.
* **The link allowlist filters the stream.** Cheap, already built, and the one
  guard with production telemetry behind it.
* **Two-phase is not here.** It is a separate runner class (`evals/runners`)
  used for measurement. Across four measured configurations it moved nothing
  and it doubles API calls per turn, so wiring it in by default would spend
  real money for no observed benefit. If that changes, it arrives as a
  different class — never as a flag on this one (`DESIGN.md`).

Preview enforcement is deliberately absent too: it compares an answer against
the rows a tool returned, which is a *grading* operation over a finished
message, not a stream filter. Doing it properly means re-checking the answer
after generation, and that is not built.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol, Sequence

from .link_allowlist import LinkAllowlist, extract_urls

ACCURACY_INSTRUCTION = """\
## Accuracy

These are not optional. A wrong number that looks right is worse than no
answer, because nobody catches it.

1. Never state a figure you did not receive from a tool. Not an estimate, not
   a typical value, not something you remember. If you did not query it, you
   do not know it.
2. Never invent rows. If a tool gave you a count but not the underlying
   records, report the count and say the records were not retrieved.
3. Reproduce rows exactly as returned. Do not reorder them, do not round or
   reformat the values, and do not extend the list beyond what you were given.
4. Never write a link a tool did not give you.
5. If a query fails, say it failed. Report the error. Do not substitute a
   number from any other source.
6. If a result set is partial, say so and state how many rows matched in total.
7. Figures written in these instructions or in any reference material are not
   query results. Anything stated there was true when it was written and may
   be stale now. Query before answering.
"""


class ProviderError(Exception):
    """A model-provider failure, already phrased for a person to read.

    The runner is provider-agnostic and must stay that way, so it cannot know
    what a 529 means or which vendor emits one. Adapters translate their own
    failures into this, and the runner passes the message straight through
    instead of printing an exception class at the user — which is what it did
    when Anthropic returned `overloaded_error` mid-question and the answer pane
    filled with `APIStatusError: {'type': 'error', ...}`.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class ToolResult:
    content: str
    is_error: bool = False


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, Any]

    def __call__(self, arguments: dict[str, Any]) -> ToolResult: ...


# ── the provider-neutral transcript ───────────────────────────────────────
#
# The runner used to build Anthropic messages directly — `tool_use_id`, the
# `{"role": "user", "content": [tool results]}` shape, `input_schema`. That
# made "provider-agnostic" true of the protocol and false of the code. These
# types are what the runner keeps instead; each adapter translates them to its
# own wire format and nothing above the adapter knows what that looks like.


@dataclass
class ToolCall:
    """A model asking for a tool, in nobody's format in particular."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolOutcome:
    call_id: str
    content: str
    is_error: bool = False


@dataclass
class Turn:
    """One message in the conversation.

    ``raw`` is an opaque provider-native echo of an assistant turn, written and
    read only by the adapter that produced it. Anthropic requires its own
    content blocks be replayed verbatim — a thinking block's signature cannot
    be reconstructed from text — so reconstructing an assistant message from
    its parts would quietly corrupt a conversation that used one. The runner
    never inspects it.

    ``raw_kind`` names the adapter that wrote it, and an adapter must ignore
    `raw` it did not write. Without that, one provider's content can be
    replayed into another's API — which is not hypothetical: the eval harness
    builds transcripts from Anthropic wire format, and feeding those blocks to
    OpenAI's Responses endpoint failed 40 of 50 runs with `Invalid value:
    'tool_use'`. Rebuilding from `text` and `calls` is always correct; using
    `raw` is an optimisation that is only safe within one provider.
    """

    role: str                                    # user | assistant | tool
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    outcomes: list[ToolOutcome] = field(default_factory=list)
    raw: Any = None
    raw_kind: str = ""

    def native(self, kind: str) -> Any:
        """The provider-native echo, but only for the provider that wrote it."""
        return self.raw if self.raw is not None and self.raw_kind == kind else None


@dataclass
class ToolSpec:
    """A tool as described to a model, before any provider's naming of it."""

    name: str
    description: str
    schema: dict[str, Any]


class Provider(Protocol):
    """A model that can stream a turn.

    Yields ``("text", str)`` for prose deltas, ``("notice", str)`` for
    transport status, ``("tool_call", ToolCall)`` once per requested call, and
    finally ``("assistant", Turn)`` — the turn to append to the transcript.

    Narrow on purpose: every provider can do this, and the guards need only the
    text stream and the tool boundary. Everything provider-shaped lives on the
    far side of this interface.
    """

    def stream_turn(
        self,
        system: str,
        transcript: list[Turn],
        tools: list[ToolSpec],
    ) -> Iterator[tuple[str, Any]]: ...


@dataclass
class Event:
    """One thing worth showing the user."""

    type: str          # text | tool_call | tool_result | notice | stripped_link | done | error
    text: str = ""
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    is_error: bool = False


@dataclass
class TurnStats:
    rounds: int = 0
    tool_calls: int = 0
    collected_results: int = 0
    links_stripped: int = 0
    links_normalised: int = 0


class FidelityRunner:
    """Single-phase tool loop with the link allowlist applied to the stream."""

    def __init__(
        self,
        provider: Provider,
        tools: Sequence[Tool],
        system_prompt: str = "",
        accuracy_instruction: str = ACCURACY_INSTRUCTION,
        relative_url_prefixes: Sequence[str] = (),
        max_rounds: int = 12,
    ) -> None:
        self.provider = provider
        self.tools = {t.name: t for t in tools}
        # The instruction is appended, not prepended: it is the last thing the
        # model reads before the conversation, and it must not be buried under
        # whatever domain prose a deployment adds.
        self.system_prompt = f"{system_prompt.rstrip()}\n\n{accuracy_instruction}".strip()
        self.relative_url_prefixes = tuple(relative_url_prefixes)
        self.max_rounds = max_rounds

    def run(self, messages: list[dict[str, Any] | Turn]) -> Iterator[Event]:
        """Drive one turn to completion, yielding events as they happen.

        Accepts the plain ``{"role", "content"}`` dicts callers already use and
        normalises them into `Turn`s; adapters see only `Turn`s.
        """
        transcript = [_as_turn(m) for m in messages]
        stats = TurnStats()
        collected: list[str] = []

        try:
            for _ in range(self.max_rounds):
                stats.rounds += 1
                # The allowlist is rebuilt each round from everything collected so
                # far, so a URL becomes quotable the moment a tool returns it
                # and never before.
                allowed = extract_urls(
                    "\n".join(collected), self.relative_url_prefixes
                )
                guard = LinkAllowlist(allowed_urls=allowed)

                assistant = Turn(role="assistant")
                pending: list[ToolCall] = []

                for kind, payload in self.provider.stream_turn(
                    self.system_prompt, transcript, self._tool_specs()
                ):
                    if kind == "text":
                        safe = guard.feed(payload)
                        if safe:
                            yield Event("text", text=safe)
                    elif kind == "notice":
                        # Transport status, not model output: never collected,
                        # never part of the answer, shown so a wait is visible.
                        yield Event("notice", text=payload)
                    elif kind == "tool_call":
                        pending.append(payload)
                    elif kind == "assistant":
                        assistant = payload

                tail = guard.flush()
                if tail:
                    yield Event("text", text=tail)
                stats.links_stripped += guard.stripped_count
                stats.links_normalised += guard.normalised_count
                for url in guard.stripped_urls:
                    yield Event("stripped_link", text=url)

                if not pending:
                    yield Event("done")
                    return

                if not assistant.calls:
                    # An adapter that reported calls but no assistant turn: keep
                    # the transcript consistent rather than dropping them.
                    assistant.calls = list(pending)
                transcript.append(assistant)

                outcomes: list[ToolOutcome] = []
                for call in pending:
                    stats.tool_calls += 1
                    arguments = dict(call.arguments or {})
                    yield Event("tool_call", name=call.name, arguments=arguments)
                    tool = self.tools.get(call.name)
                    if tool is None:
                        outcome = ToolResult(
                            f"unknown tool: {call.name}", is_error=True
                        )
                    else:
                        outcome = tool(arguments)
                    if not outcome.is_error:
                        collected.append(outcome.content)
                        stats.collected_results += 1
                    yield Event("tool_result", name=call.name,
                                result=outcome.content, is_error=outcome.is_error)
                    outcomes.append(ToolOutcome(
                        call_id=call.id, content=outcome.content,
                        is_error=outcome.is_error,
                    ))
                transcript.append(Turn(role="tool", outcomes=outcomes))

            yield Event("error", text=(
                f"Stopped after {self.max_rounds} tool rounds without reaching "
                "an answer."
            ))
        except ProviderError as exc:
            # Already written for a person; printing its class name would only
            # bury the sentence that tells them what to do.
            yield Event("error", text=str(exc))
        except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
            yield Event("error", text=f"{type(exc).__name__}: {exc}")

    def _tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(name=t.name, description=t.description, schema=t.input_schema)
            for t in self.tools.values()
        ]


def _as_turn(message: dict[str, Any] | Turn) -> Turn:
    """Accept the plain message dicts callers already hold."""
    if isinstance(message, Turn):
        return message
    return Turn(role=message.get("role", "user"),
                text=message.get("content", "") or "")
