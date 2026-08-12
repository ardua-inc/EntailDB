"""
link_allowlist — strip fabricated URLs from streamed model output.

A model with tools will invent links. Not occasionally: reliably, and in
recognisable shapes — `[Download ...](javascript:void(0))`, `](#)`, the
literal text `](URL from tool result)`, and fully-constructed `https://…`
paths that look entirely plausible. The source deployment instructed against
this in four separate prompt sections, at length, and it kept happening.

The fix is structural rather than instructional. Build a per-turn allowlist
from the URLs that actually appear in tool results, then filter the outgoing
stream against it. A URL the model did not receive cannot reach the user,
regardless of what the prompt says or how convincing the model is.

Provides:

  * `extract_urls(text, relative_prefixes=...)`
        Pull URLs out of arbitrary text — normally the concatenated tool
        results for one turn — to build the allowlist. Absolute URLs are
        always recognised; declare `relative_prefixes` if your tools emit
        relative paths such as `/api/csv-download/<token>`.

  * `LinkAllowlist`
        Incremental stream filter. Feed it text deltas as they arrive; it
        returns text safe to forward. Markdown links `[text](url)` whose URL
        is not allowlisted are rewritten to bare `[text]` — the label
        survives, the clickable link does not. Counts strips so callers can
        append a warning and emit telemetry.

Implementation notes that matter:

  * A per-character state machine, so URLs split across streaming deltas are
    reassembled — state persists across `feed()` calls, and trailing buffered
    content is returned by `flush()`.
  * Parentheses inside a URL are balanced, so `javascript:void(0)` parses as
    one URL rather than confusing the parser.
  * A newline inside a URL is treated as malformed and the buffer is emitted
    verbatim. The filter only ever suppresses well-formed markdown links; it
    will not eat prose.
  * A host-prefixed variant of an allowlisted *relative* URL is repaired
    rather than stripped, and counted separately. See `LinkAllowlist._resolve`.
"""

from __future__ import annotations

import re
from typing import Iterable


_URL_RE = re.compile(r"https?://[^\s\)\]\"'<>]+")

# Default: no relative download paths are recognised. Callers whose tools
# return relative URLs must declare their prefixes — see `relative_prefixes`
# on LinkAllowlist and extract_urls. Getting this wrong is a live failure
# mode, not a theoretical one: the source system shipped without it and the
# filter stripped its own legitimate download links until someone noticed.
_DEFAULT_RELATIVE_PREFIXES: tuple[str, ...] = ()


def _relative_re(prefixes: Iterable[str]) -> re.Pattern[str] | None:
    """Build a matcher for relative download paths, or None if there are none."""
    prefixes = [p for p in prefixes if p]
    if not prefixes:
        return None
    alternation = "|".join(re.escape(p) for p in prefixes)
    return re.compile(rf"(?:{alternation})[A-Za-z0-9_\-]+")


def extract_urls(
    text: str,
    relative_prefixes: Iterable[str] = _DEFAULT_RELATIVE_PREFIXES,
) -> set[str]:
    """Return the set of URLs (absolute and relative) that appear in *text*.

    Used to build the per-turn allowlist from concatenated tool result
    strings. Trailing punctuation that is almost always a sentence
    boundary, not part of the URL, is stripped.

    `relative_prefixes` declares path prefixes your own tools emit as
    relative URLs, e.g. ``("/api/csv-download/",)``. Absolute URLs are
    always recognised.
    """
    if not text:
        return set()
    out: set[str] = set()
    for m in _URL_RE.finditer(text):
        out.add(m.group(0).rstrip(".,;:!?"))
    rel_re = _relative_re(relative_prefixes)
    if rel_re is not None:
        for m in rel_re.finditer(text):
            out.add(m.group(0))
    return out


class LinkAllowlist:
    """Incremental markdown-link validator.

    Usage::

        f = LinkAllowlist(allowed_urls={"https://example.com/exports/abc"})
        for delta in stream_of_text_chunks:
            user_visible = f.feed(delta)
            send_to_client(user_visible)
        send_to_client(f.flush())
        if f.stripped_count:
            ...

    Behaviour:
      * Pass-through for normal text.
      * Detects ``[text](url)`` constructs across delta boundaries.
      * Buffers the URL portion until the closing ``)``; balances nested
        parentheses inside the URL so ``[x](javascript:void(0))`` parses
        as a single link with URL ``javascript:void(0)``.
      * If the URL is in the allowlist, the original ``(url)`` is
        emitted verbatim. Otherwise the entire ``(url)`` segment is
        suppressed and ``stripped_count`` is incremented; the preceding
        ``[text]`` (already streamed) remains in the output as plain
        bracketed text.
      * Newline inside a URL (and end-of-stream) treats the construct
        as malformed and flushes the buffer verbatim — the validator
        only suppresses well-formed links, never legitimate prose.
    """

    __slots__ = (
        "_allowed",
        "_relative_allowed",
        "_state",
        "_buffer",
        "_paren_depth",
        "stripped_count",
        "stripped_urls",
        "normalised_count",
    )

    # State machine values.
    _NORMAL = 0
    _AFTER_CLOSE_BRACKET = 1
    _IN_URL = 2

    def __init__(
        self,
        allowed_urls: Iterable[str] = (),
        always_allowed: Iterable[str] = (),
    ) -> None:
        """
        allowed_urls   — the per-turn allowlist, normally
                         `extract_urls(concatenated_tool_results)`.
        always_allowed — URLs your server emits itself, not via a tool.
                         Keep this tight: everything here is trusted
                         unconditionally, regardless of model behaviour.
        """
        self._allowed: set[str] = set(allowed_urls) | set(always_allowed)
        # Relative entries, kept separately so a host-prefixed variant of an
        # allowlisted relative URL can be recognised. See _resolve().
        self._relative_allowed: set[str] = {
            u for u in self._allowed if u.startswith("/")
        }
        self._state = self._NORMAL
        self._buffer = ""        # text we've consumed but not yet decided on
        self._paren_depth = 0    # depth inside a URL `(...)`
        self.stripped_count = 0
        self.stripped_urls: list[str] = []
        self.normalised_count = 0

    def _resolve(self, url: str) -> str | None:
        """Return the emittable form of *url*, or None if it must be stripped.

        Exact allowlist matches pass through unchanged. Beyond that, one
        rehabilitation: an absolute URL whose path is an allowlisted relative
        URL is emitted **as the relative form**.

        That case is real. In the source deployment the tool returned
        `/api/csv-download/<token>` and the model emitted
        `https://<host>/api/csv-download/<token>` — same token, host prefixed.
        Stripping it cost the user a working download four separate times.
        Treating it as a fabrication is defensible on policy (the model did
        not paste verbatim) but is the wrong outcome for the user.

        Counted as `normalised_count`, never as a strip, so metrics never
        conflate "prevented a fabrication" with "repaired a near-miss".
        """
        if url in self._allowed:
            return url
        for rel in self._relative_allowed:
            if url.endswith(rel) and url.startswith(("http://", "https://")):
                self.normalised_count += 1
                return rel
        return None

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    def feed(self, text: str) -> str:
        """Process a delta and return the portion safe to forward to the user."""
        if not text:
            return ""
        out: list[str] = []
        for ch in text:
            self._step(ch, out)
        return "".join(out)

    def flush(self) -> str:
        """Called at end of stream. Returns any trailing buffered content.

        Open URL constructs left dangling at end-of-stream are treated as
        malformed and emitted as-is (we do not retroactively strip a link
        the model never finished writing).
        """
        if self._state == self._IN_URL and self._buffer:
            tail = self._buffer
            self._buffer = ""
            self._state = self._NORMAL
            self._paren_depth = 0
            return tail
        if self._state == self._AFTER_CLOSE_BRACKET:
            self._state = self._NORMAL
        return ""

    # ──────────────────────────────────────────────────────────────────────
    # Internals
    # ──────────────────────────────────────────────────────────────────────

    def _step(self, ch: str, out: list[str]) -> None:
        if self._state == self._NORMAL:
            out.append(ch)
            if ch == "]":
                self._state = self._AFTER_CLOSE_BRACKET
            return

        if self._state == self._AFTER_CLOSE_BRACKET:
            if ch == "(":
                # Begin URL capture. Buffer the `(` so we can re-emit it
                # verbatim if the URL turns out to be allowed.
                self._buffer = "("
                self._paren_depth = 0
                self._state = self._IN_URL
            else:
                out.append(ch)
                # Stay in NORMAL unless this is itself another `]`.
                self._state = (
                    self._AFTER_CLOSE_BRACKET if ch == "]" else self._NORMAL
                )
            return

        # _IN_URL
        if ch == "(":
            self._paren_depth += 1
            self._buffer += ch
            return
        if ch == ")":
            if self._paren_depth > 0:
                self._paren_depth -= 1
                self._buffer += ch
                return
            # Closing the URL.
            url = self._buffer[1:]  # drop the leading `(`
            self._buffer = ""
            self._state = self._NORMAL
            resolved = self._resolve(url)
            if resolved is not None:
                out.append(f"({resolved})")
            else:
                self.stripped_count += 1
                self.stripped_urls.append(url)
            return
        if ch == "\n":
            # Malformed — emit buffer verbatim and resume normal flow.
            out.append(self._buffer)
            out.append(ch)
            self._buffer = ""
            self._paren_depth = 0
            self._state = self._NORMAL
            return
        # Ordinary URL character.
        self._buffer += ch
