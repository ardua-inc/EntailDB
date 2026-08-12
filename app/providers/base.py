"""What every model adapter shares: the retry loop and the registry.

An adapter is a self-contained module in this package. It subclasses
`RetryingProvider`, declares a `kind` and a `label`, and calls `register`.
Discovery is automatic — dropping a file in this directory is the whole
installation step, and no existing file mentions it.

The retry loop lives here rather than in each adapter because a second adapter
that quietly lacked it would fail during an overload episode in a way that
looked like a worse provider rather than an unwrapped one.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterator

from fidelity.runner import ProviderError, ToolSpec, Turn

DEFAULT_MODEL = "claude-sonnet-5"

# The SDK retries 408/409/429/5xx with exponential backoff; 529 "overloaded" is
# in that set. The default of 2 was not enough during a real overload episode,
# and an interactive question is worth waiting a few more seconds for rather
# than handing back a failure the caller can only respond to by asking again.
MAX_RETRIES = 5

# Retries above cover *HTTP* failures. An overload can also arrive on a 200 as
# an error event inside the stream, which the SDK does not retry — that is the
# form the real outage took, and the form that reached the answer pane as a raw
# exception. Retried here instead, but only while nothing has been emitted:
# restarting after partial output would repeat it.
# A real overload episode lasts minutes, not seconds. The first ladder here
# summed to about 12s and produced four visible failures in a row during one,
# while a different connection succeeded moments later purely by timing. This
# covers ~65s, and every wait is announced rather than looking like a hang.
STREAM_RETRIES = 5
STREAM_BACKOFF = (2.0, 5.0, 10.0, 20.0, 30.0)


REGISTRY: dict[str, type["RetryingProvider"]] = {}


def register(cls: type["RetryingProvider"]) -> type["RetryingProvider"]:
    """Announce an adapter. Used as a decorator on the class itself."""
    if not getattr(cls, "kind", ""):
        raise ValueError(f"{cls.__name__} must declare a `kind`")
    if cls.kind in REGISTRY:
        raise ValueError(f"duplicate provider kind: {cls.kind!r}")
    REGISTRY[cls.kind] = cls
    return cls


class RetryingProvider:
    """The retry-with-notice loop, shared by every adapter.

    Extracted rather than copied: a second adapter that quietly lacked the
    retry would fail during an overload episode in a way that looked like a
    provider being worse, not a provider being unwrapped.
    """

    # Identity, and what the settings page shows. A new adapter supplies
    # these and appears in the UI without any existing file being edited.
    kind: str = ""
    label: str = ""
    wants_base_url: bool = True
    wants_api_key: bool = True
    model_hint: str = ""
    base_url_hint: str = ""

    vendor = "The model API"

    def stream_turn(self, system: str, transcript: list[Turn],
                    tools: list[ToolSpec]) -> Iterator[tuple[str, Any]]:
        for attempt in range(STREAM_RETRIES + 1):
            emitted = False
            try:
                for item in self._stream(system, transcript, tools):
                    emitted = True
                    yield item
                return
            except Exception as exc:  # noqa: BLE001 — translated, not swallowed
                error = self._translate(exc)
                retryable = (isinstance(error, ProviderError) and error.retryable
                             and not emitted and attempt < STREAM_RETRIES)
                if not retryable:
                    raise error from exc
                wait = STREAM_BACKOFF[min(attempt, len(STREAM_BACKOFF) - 1)]
                # Announced, because a silent 65-second pause is
                # indistinguishable from a hung request, and a user who cannot
                # see the retry will retry by hand and make the load worse.
                yield "notice", (
                    f"{self.vendor} is busy — waiting {wait:.0f}s and trying "
                    f"again ({attempt + 1} of {STREAM_RETRIES})."
                )
                time.sleep(wait)

    def _stream(self, system, transcript, tools):      # pragma: no cover
        raise NotImplementedError

    def _translate(self, exc: Exception) -> Exception:  # pragma: no cover
        return exc
