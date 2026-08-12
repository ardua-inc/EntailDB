"""Tests for translating provider failures into something worth reading.

Prompted by a real one. Asked a question against a freshly added connection,
the answer pane filled with:

    APIStatusError: {'type': 'error', 'error': {'details': None, 'type':
    'overloaded_error', 'message': 'Overloaded'}, 'request_id': 'req_011...'}

That is an HTTP 529 from Anthropic. The database was fine — the same
connection's Test button passed, and it queried 19,972 rows when driven
directly. But the message gave no way to know that, so a transient upstream
outage read as a broken connection. The wording of these messages is the
feature: each says what failed, whether it is the user's side, and what to do.

`_translate` deliberately returns unrecognised exceptions untouched. An error
nobody anticipated should look unhandled rather than be flattened into a
reassuring sentence that hides it.
"""

from __future__ import annotations

import httpx
import pytest

from app.providers import MAX_RETRIES, _translate
from fidelity.runner import ProviderError

anthropic = pytest.importorskip("anthropic")


def _response(status: int) -> httpx.Response:
    return httpx.Response(
        status_code=status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )


def status_error(status: int, message: str = "") -> Exception:
    """An SDK status error of the shape the real API produces.

    The message defaults to something status-appropriate rather than always
    saying "Overloaded" — an earlier version of this helper said that for every
    status and hid a real bug, where any error mentioning the word was reported
    as a transient overload regardless of its status code.
    """
    return anthropic.APIStatusError(
        message or f"HTTP {status}", response=_response(status), body=None
    )


# ── the one that prompted this ────────────────────────────────────────────

def test_an_overloaded_api_says_it_is_not_your_database():
    translated = _translate(status_error(529))
    assert isinstance(translated, ProviderError)
    assert translated.retryable is True
    text = str(translated)
    assert "overloaded" in text.lower()
    # The reassurance that was missing: this is not the user's fault.
    assert "database" in text.lower()


def test_an_overload_arriving_mid_stream_on_a_200_is_recognised():
    """The form the real outage actually took, and the one an earlier fix
    missed completely.

    An overload can arrive as an error *event* inside an otherwise successful
    stream. The HTTP status is then 200, so classifying on status skips it and
    the raw payload reaches the user — which is exactly what happened:
    `APIStatusError: {'type': 'error', 'error': {'type': 'overloaded_error'...`
    """
    translated = _translate(
        anthropic.APIStatusError(
            "{'type': 'error', 'error': {'type': 'overloaded_error', "
            "'message': 'Overloaded'}}",
            response=_response(200), body=None,
        )
    )
    assert isinstance(translated, ProviderError)
    assert translated.retryable is True
    assert "overloaded" in str(translated).lower()


def test_the_error_type_is_read_from_a_parsed_body_when_there_is_one():
    translated = _translate(
        anthropic.APIStatusError(
            "no useful text here", response=_response(200),
            body={"type": "error", "error": {"type": "overloaded_error"}},
        )
    )
    assert isinstance(translated, ProviderError)


def test_a_rate_limit_reported_mid_stream_is_recognised():
    translated = _translate(
        anthropic.APIStatusError(
            "{'type': 'error', 'error': {'type': 'rate_limit_error'}}",
            response=_response(200), body=None,
        )
    )
    assert isinstance(translated, ProviderError)
    assert "rate limited" in str(translated).lower()


def test_a_server_error_is_reported_as_upstream():
    translated = _translate(status_error(503))
    assert isinstance(translated, ProviderError)
    assert translated.retryable is True
    assert "503" in str(translated)


# ── the ones the user can act on ──────────────────────────────────────────

def test_a_rejected_key_points_at_settings():
    translated = _translate(
        anthropic.AuthenticationError("bad key", response=_response(401), body=None)
    )
    assert isinstance(translated, ProviderError)
    assert translated.retryable is False
    assert "settings" in str(translated).lower()


def test_an_unknown_model_points_at_the_model_field():
    translated = _translate(
        anthropic.NotFoundError("nope", response=_response(404), body=None)
    )
    assert "model" in str(translated).lower()
    assert translated.retryable is False


def test_rate_limiting_is_marked_retryable():
    translated = _translate(
        anthropic.RateLimitError("slow down", response=_response(429), body=None)
    )
    assert isinstance(translated, ProviderError)
    assert translated.retryable is True


def test_an_unreachable_api_is_distinguished_from_a_rejected_one():
    translated = _translate(
        anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    )
    assert isinstance(translated, ProviderError)
    assert "network" in str(translated).lower()


# ── the deliberate gap ────────────────────────────────────────────────────

def test_an_unrecognised_error_is_passed_through_unchanged():
    """An error nobody anticipated must look unhandled, not be dressed up as a
    known condition."""
    original = ValueError("something nobody predicted")
    assert _translate(original) is original


def test_a_4xx_that_is_not_special_cased_keeps_its_own_message():
    original = status_error(422)
    assert _translate(original) is original


def test_the_word_overloaded_in_a_4xx_does_not_make_it_an_outage():
    """Status leads, text only refines. A client-side error that happens to
    mention overloading is a different problem and must not be reported as a
    transient outage to wait out."""
    original = status_error(400, "request rejected: account overloaded quota")
    assert _translate(original) is original


# ── how the runner presents it ────────────────────────────────────────────

def test_the_runner_shows_the_message_without_an_exception_class():
    """The whole point: a person reads a sentence, not a traceback fragment."""
    from fidelity.runner import FidelityRunner

    class Overloaded:
        def stream_turn(self, system, messages, tools):
            raise ProviderError("Claude's API is temporarily overloaded.",
                                retryable=True)
            yield  # pragma: no cover

    events = list(FidelityRunner(Overloaded(), tools=[]).run([]))
    assert events[-1].type == "error"
    assert events[-1].text == "Claude's API is temporarily overloaded."
    assert "ProviderError" not in events[-1].text


def test_an_unexpected_failure_still_names_its_class():
    """Losing the class name on a genuine bug would make it harder to find."""
    from fidelity.runner import FidelityRunner

    class Broken:
        def stream_turn(self, system, messages, tools):
            raise KeyError("content")
            yield  # pragma: no cover

    events = list(FidelityRunner(Broken(), tools=[]).run([]))
    assert "KeyError" in events[-1].text


# ── the retry budget ──────────────────────────────────────────────────────

def test_the_client_is_built_with_the_raised_retry_budget(monkeypatch):
    """The SDK default of 2 was not enough during a real overload episode."""
    seen = {}

    class FakeAnthropic:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    from app.providers import AnthropicProvider

    AnthropicProvider(api_key="sk-test")
    assert seen["max_retries"] == MAX_RETRIES
    assert MAX_RETRIES > 2


# ── retrying an overload that the SDK will not retry ──────────────────────

def test_a_mid_stream_overload_is_retried_when_nothing_was_emitted(monkeypatch):
    """The SDK retries HTTP failures; an error event on a 200 is not one, so it
    reaches the caller on the first attempt unless this layer retries it."""
    from app import providers
    from app.providers import base as providers_base

    monkeypatch.setattr(providers_base, "STREAM_BACKOFF", (0, 0, 0))
    attempts = []

    class Flaky(providers.AnthropicProvider):
        def __init__(self):
            pass

        def _stream(self, system, messages, tools):
            attempts.append(1)
            if len(attempts) < 3:
                raise anthropic.APIStatusError(
                    "{'type': 'error', 'error': {'type': 'overloaded_error'}}",
                    response=_response(200), body=None,
                )
            yield "text", "recovered"

    got = list(Flaky().stream_turn("s", [], []))
    assert ("text", "recovered") in got
    assert len(attempts) == 3
    # Each wait is announced rather than looking like a hang.
    notices = [t for kind, t in got if kind == "notice"]
    assert len(notices) == 2
    assert all("busy" in n for n in notices)


def test_a_retry_never_happens_after_output_has_been_emitted(monkeypatch):
    """Restarting a turn that already streamed prose would repeat it, which is
    worse than the error: the user would read a duplicated half-answer."""
    from app import providers
    from app.providers import base as providers_base

    monkeypatch.setattr(providers_base, "STREAM_BACKOFF", (0, 0, 0))
    attempts = []

    class FailsLate(providers.AnthropicProvider):
        def __init__(self):
            pass

        def _stream(self, system, messages, tools):
            attempts.append(1)
            yield "text", "partial answer"
            raise anthropic.APIStatusError(
                "{'type': 'error', 'error': {'type': 'overloaded_error'}}",
                response=_response(200), body=None,
            )

    got = []
    with pytest.raises(ProviderError):
        for item in FailsLate().stream_turn("s", [], []):
            got.append(item)

    assert got == [("text", "partial answer")]
    assert len(attempts) == 1


def test_a_non_retryable_failure_is_not_retried(monkeypatch):
    from app import providers
    from app.providers import base as providers_base

    monkeypatch.setattr(providers_base, "STREAM_BACKOFF", (0, 0, 0))
    attempts = []

    class BadKey(providers.AnthropicProvider):
        def __init__(self):
            pass

        def _stream(self, system, messages, tools):
            attempts.append(1)
            raise anthropic.AuthenticationError(
                "bad key", response=_response(401), body=None)
            yield  # pragma: no cover

    with pytest.raises(ProviderError, match="Settings"):
        list(BadKey().stream_turn("s", [], []))
    assert len(attempts) == 1


def test_an_overload_that_never_clears_is_reported_not_looped(monkeypatch):
    from app import providers
    from app.providers import base as providers_base

    monkeypatch.setattr(providers_base, "STREAM_BACKOFF", (0, 0, 0))
    attempts = []

    class AlwaysDown(providers.AnthropicProvider):
        def __init__(self):
            pass

        def _stream(self, system, messages, tools):
            attempts.append(1)
            raise anthropic.APIStatusError(
                "{'type': 'error', 'error': {'type': 'overloaded_error'}}",
                response=_response(200), body=None,
            )
            yield  # pragma: no cover

    with pytest.raises(ProviderError, match="overloaded"):
        list(AlwaysDown().stream_turn("s", [], []))
    assert len(attempts) == providers_base.STREAM_RETRIES + 1


def test_a_notice_is_emitted_before_each_wait(monkeypatch):
    """A silent minute-long pause is indistinguishable from a hung request, and
    a user who cannot see the retry retries by hand, adding load."""
    from app import providers
    from app.providers import base as providers_base

    monkeypatch.setattr(providers_base, "STREAM_BACKOFF", (0, 0, 0, 0, 0))
    calls = []

    class AlwaysDown(providers.AnthropicProvider):
        def __init__(self):
            pass

        def _stream(self, system, messages, tools):
            calls.append(1)
            raise anthropic.APIStatusError(
                "{'type': 'error', 'error': {'type': 'overloaded_error'}}",
                response=_response(200), body=None,
            )
            yield  # pragma: no cover

    seen = []
    with pytest.raises(ProviderError):
        for item in AlwaysDown().stream_turn("s", [], []):
            seen.append(item)

    assert len(seen) == providers_base.STREAM_RETRIES
    assert all(k == "notice" for k, _ in seen)
    assert "1 of" in seen[0][1]


def test_the_retry_ladder_covers_a_real_episode():
    """The first ladder summed to ~12s and produced four visible failures in a
    row during an actual overload; a real episode lasts minutes."""
    from app import providers
    from app.providers import base as providers_base

    assert sum(providers_base.STREAM_BACKOFF) >= 60
    assert providers_base.STREAM_RETRIES == len(providers_base.STREAM_BACKOFF)


def test_a_notice_is_not_collected_as_model_output():
    """A notice is transport status. It must never reach the answer, the link
    allowlist's collected text, or the stored transcript."""
    from fidelity.runner import FidelityRunner

    class Noisy:
        def stream_turn(self, system, messages, tools):
            yield "notice", "Claude's API is busy — waiting 2s."
            yield "text", "The answer."
            yield "assistant_content", []

    events = list(FidelityRunner(Noisy(), tools=[]).run([]))
    kinds = [e.type for e in events]
    assert "notice" in kinds
    text = "".join(e.text for e in events if e.type == "text")
    assert text == "The answer."
