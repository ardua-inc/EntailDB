"""Tests for link_allowlist — fabricated-URL stripping in streamed model output."""

from __future__ import annotations

import os
import sys
import unittest

# Allow `import fidelity.link_allowlist` when run from the repo root.
_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from fidelity.link_allowlist import (  # noqa: E402
    LinkAllowlist,
    extract_urls,
)

# The source system trusted one server-emitted URL unconditionally. That is now
# a constructor argument rather than a module constant; tests pass it explicitly.
SERVER_EMITTED = "https://bi.example.com"


def _stream(text: str, chunk_size: int = 1) -> str:
    """Feed *text* to a fresh LinkAllowlist in fixed-size chunks and assemble output."""
    f = LinkAllowlist()
    parts = []
    for i in range(0, len(text), chunk_size):
        parts.append(f.feed(text[i : i + chunk_size]))
    parts.append(f.flush())
    return "".join(parts), f


class ExtractUrlsTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(extract_urls(""), set())
        self.assertEqual(extract_urls(None), set())  # type: ignore[arg-type]

    def test_single_http(self):
        self.assertEqual(
            extract_urls("see http://example.com/page"),
            {"http://example.com/page"},
        )

    def test_strips_trailing_punctuation(self):
        self.assertEqual(
            extract_urls("Visit https://x.com/path."),
            {"https://x.com/path"},
        )

    def test_multiple_distinct(self):
        text = (
            "URL1 https://a.com/1\n"
            "URL2 https://b.com/2 and https://a.com/1 again"
        )
        self.assertEqual(
            extract_urls(text),
            {"https://a.com/1", "https://b.com/2"},
        )

    def test_ignores_non_http_schemes(self):
        # We only ever allow http(s) — javascript:, data:, mailto: are never
        # emitted by tools and would be fabricated.
        self.assertEqual(extract_urls("[x](javascript:void(0))"), set())
        self.assertEqual(extract_urls("[x](mailto:a@b.com)"), set())

    def test_url_inside_markdown_link(self):
        # extract_urls is regex-based and finds the URL regardless of context.
        self.assertEqual(
            extract_urls("[Download](https://downloads.example.com/csv/abc123)"),
            {"https://downloads.example.com/csv/abc123"},
        )


class LinkAllowlistPassThroughTests(unittest.TestCase):
    def test_plain_text_unchanged(self):
        out, f = _stream("Hello world. No links here.")
        self.assertEqual(out, "Hello world. No links here.")
        self.assertEqual(f.stripped_count, 0)

    def test_brackets_without_following_paren(self):
        out, f = _stream("This is in [brackets] but not a link.")
        self.assertEqual(out, "This is in [brackets] but not a link.")
        self.assertEqual(f.stripped_count, 0)

    def test_double_close_bracket(self):
        # `[[1]]` style — second `]` should re-arm the close-bracket state
        # but no `(` follows, so output is unchanged.
        out, f = _stream("See [[1]] and [[2]] for details.")
        self.assertEqual(out, "See [[1]] and [[2]] for details.")
        self.assertEqual(f.stripped_count, 0)


class LinkAllowlistAllowlistTests(unittest.TestCase):
    def test_allowed_url_passes_through(self):
        url = "https://downloads.example.com/api/csv-download/abc"
        text = f"Here it is: [Download report.csv]({url})"
        f = LinkAllowlist(allowed_urls={url})
        out = f.feed(text) + f.flush()
        self.assertEqual(out, text)
        self.assertEqual(f.stripped_count, 0)

    def test_always_allowed_is_trusted_without_a_tool_result(self):
        # `always_allowed` covers URLs the server emits itself. The model may
        # mention them even though no tool returned them this turn.
        text = f"For more, see [Dashboard]({SERVER_EMITTED})."
        f = LinkAllowlist(allowed_urls=(), always_allowed={SERVER_EMITTED})
        out = f.feed(text) + f.flush()
        self.assertEqual(out, text)
        self.assertEqual(f.stripped_count, 0)

    def test_always_allowed_is_empty_by_default(self):
        # Regression guard on the extraction: the source system hardcoded one
        # deployment's URL as a module constant. Defaulting to empty means a
        # new deployment cannot accidentally inherit someone else's trusted host.
        text = f"See [Dashboard]({SERVER_EMITTED})."
        f = LinkAllowlist()
        out = f.feed(text) + f.flush()
        self.assertEqual(out, "See [Dashboard].")
        self.assertEqual(f.stripped_count, 1)

    def test_unknown_url_is_stripped(self):
        text = "Here: [Download full list](javascript:void(0)) — enjoy."
        f = LinkAllowlist()  # nothing allowed
        out = f.feed(text) + f.flush()
        self.assertEqual(out, "Here: [Download full list] — enjoy.")
        self.assertEqual(f.stripped_count, 1)
        self.assertEqual(f.stripped_urls, ["javascript:void(0)"])

    def test_balanced_parens_inside_url(self):
        # The state machine must match the outer `)` of the markdown link,
        # not the inner one of `void(0)`.
        text = "[link](javascript:void(0))"
        f = LinkAllowlist()
        out = f.feed(text) + f.flush()
        self.assertEqual(out, "[link]")
        self.assertEqual(f.stripped_count, 1)
        self.assertEqual(f.stripped_urls, ["javascript:void(0)"])

    def test_url_with_balanced_parens_allowed(self):
        # Wikipedia-style URL with `(...)` should round-trip when allowlisted.
        url = "https://en.wikipedia.org/wiki/Foo_(bar)"
        text = f"see [Foo]({url}) please"
        f = LinkAllowlist(allowed_urls={url})
        out = f.feed(text) + f.flush()
        self.assertEqual(out, text)
        self.assertEqual(f.stripped_count, 0)

    def test_multiple_links_independent(self):
        good = "https://downloads.example.com/api/csv-download/good"
        text = (
            f"First: [A]({good}). "
            f"Second: [B](javascript:void(0)). "
            f"Third: [C]({good})."
        )
        f = LinkAllowlist(allowed_urls={good})
        out = f.feed(text) + f.flush()
        self.assertEqual(
            out,
            f"First: [A]({good}). Second: [B]. Third: [C]({good}).",
        )
        self.assertEqual(f.stripped_count, 1)


class LinkAllowlistStreamingTests(unittest.TestCase):
    """The filter must produce identical output regardless of how text is sliced."""

    def _assert_stable_under_chunking(self, text: str, allowed: set[str], expected: str, expected_strips: int):
        for chunk_size in (1, 2, 3, 5, 7, 13, 50, len(text)):
            f = LinkAllowlist(allowed_urls=allowed)
            parts = []
            for i in range(0, len(text), chunk_size):
                parts.append(f.feed(text[i : i + chunk_size]))
            parts.append(f.flush())
            out = "".join(parts)
            self.assertEqual(
                out, expected,
                f"output diverged at chunk_size={chunk_size}:\n  got={out!r}\n  exp={expected!r}",
            )
            self.assertEqual(
                f.stripped_count, expected_strips,
                f"strip count diverged at chunk_size={chunk_size}",
            )

    def test_chunked_stream_strips_consistently(self):
        text = "Result: [Download](javascript:void(0)) follows."
        self._assert_stable_under_chunking(
            text,
            allowed=set(),
            expected="Result: [Download] follows.",
            expected_strips=1,
        )

    def test_chunked_stream_preserves_allowed(self):
        url = "https://downloads.example.com/api/csv-download/xyz"
        text = f"Result: [Download]({url}) follows."
        self._assert_stable_under_chunking(
            text,
            allowed={url},
            expected=text,
            expected_strips=0,
        )

    def test_close_bracket_at_chunk_boundary(self):
        # `]` and `(` must straddle the boundary cleanly.
        text = "Hi [link](https://x.com/a)!"
        f = LinkAllowlist(allowed_urls={"https://x.com/a"})
        out = f.feed("Hi [link]") + f.feed("(https://x.com/a)!") + f.flush()
        self.assertEqual(out, text)
        self.assertEqual(f.stripped_count, 0)

    def test_url_split_across_chunks(self):
        url = "https://downloads.example.com/api/csv-download/ABCDEFG"
        text = f"see [Get it]({url})"
        f = LinkAllowlist(allowed_urls={url})
        # Deliberately split mid-host and mid-path: the state machine must
        # reassemble across feed() boundaries.
        out = (
            f.feed("see [Get it](https://downloads.")
            + f.feed("example.com/api/csv-")
            + f.feed("download/ABCDEFG)")
            + f.flush()
        )
        self.assertEqual(out, text)
        self.assertEqual(f.stripped_count, 0)


class LinkAllowlistMalformedTests(unittest.TestCase):
    def test_newline_inside_url_passes_through(self):
        # If a `(` opens a URL but a newline arrives before the closing `)`,
        # the construct is malformed and we must not silently swallow the
        # buffered content. Stripping prose because of an unfinished link
        # would be worse than the bug we're fixing.
        text = "Here is [text](\nnot really a url) end."
        f = LinkAllowlist()
        out = f.feed(text) + f.flush()
        self.assertIn("(", out)
        self.assertIn("not really a url)", out)
        self.assertEqual(f.stripped_count, 0)

    def test_eof_inside_url_flushes_buffer(self):
        text = "trailing [Download](https://incomplete-url"
        f = LinkAllowlist()
        first = f.feed(text)
        tail = f.flush()
        out = first + tail
        # The unfinished URL is preserved verbatim — we don't fabricate a fix.
        self.assertIn("(https://incomplete-url", out)
        self.assertEqual(f.stripped_count, 0)


if __name__ == "__main__":
    unittest.main()


class RelativePrefixTests(unittest.TestCase):
    """Relative download paths are caller-declared, not hardcoded.

    The source system baked two application-specific prefixes into a module
    constant — one of them belonging to an unrelated business feature. Worse,
    it shipped originally with *no* relative support at all, so the filter
    stripped its own legitimate download links until someone noticed in
    production.
    """

    REL = "/api/exports/"

    def test_relative_url_not_extracted_without_declared_prefix(self):
        tool_output = "Download: /api/exports/abc123"
        self.assertEqual(extract_urls(tool_output), set())

    def test_relative_url_extracted_when_prefix_declared(self):
        tool_output = "Download: /api/exports/abc123"
        self.assertEqual(
            extract_urls(tool_output, relative_prefixes=[self.REL]),
            {"/api/exports/abc123"},
        )

    def test_declared_relative_url_survives_the_filter(self):
        url = "/api/exports/abc123"
        allowed = extract_urls(f"url: {url}", relative_prefixes=[self.REL])
        text = f"[Download]({url})"
        f = LinkAllowlist(allowed_urls=allowed)
        self.assertEqual(f.feed(text) + f.flush(), text)
        self.assertEqual(f.stripped_count, 0)

    def test_multiple_prefixes(self):
        text = "a /api/exports/aaa and b /reports/download/bbb"
        got = extract_urls(text, relative_prefixes=["/api/exports/", "/reports/download/"])
        self.assertEqual(got, {"/api/exports/aaa", "/reports/download/bbb"})


class HostPrefixNormalisationTests(unittest.TestCase):
    """A host-prefixed allowlisted relative URL is repaired, not stripped.

    Observed four times in production: the tool returned
    `/api/exports/<token>`, the model emitted
    `https://<host>/api/exports/<token>` — same token, host prepended. The
    filter stripped it and the user lost a working download.

    It is counted separately from strips so metrics never conflate "prevented
    a fabrication" with "repaired a near-miss".
    """

    REL = "/api/exports/"
    TOKEN_URL = "/api/exports/tok_9f3a"

    def _filter(self):
        allowed = extract_urls(f"url: {self.TOKEN_URL}", relative_prefixes=[self.REL])
        return LinkAllowlist(allowed_urls=allowed)

    def test_host_prefixed_variant_is_rewritten_to_the_relative_form(self):
        f = self._filter()
        text = f"[Download](https://downloads.example.com{self.TOKEN_URL})"
        out = f.feed(text) + f.flush()
        self.assertEqual(out, f"[Download]({self.TOKEN_URL})")
        self.assertEqual(f.stripped_count, 0)
        self.assertEqual(f.normalised_count, 1)

    def test_normalisation_is_not_counted_as_a_strip(self):
        f = self._filter()
        f.feed(f"[a](https://downloads.example.com{self.TOKEN_URL})")
        f.flush()
        self.assertEqual(f.stripped_urls, [])

    def test_a_different_token_is_still_stripped(self):
        # Normalisation must not become a blanket pass for anything sharing
        # the prefix — a fabricated token is still a fabrication.
        f = self._filter()
        text = "[Download](https://downloads.example.com/api/exports/tok_FAKE)"
        out = f.feed(text) + f.flush()
        self.assertEqual(out, "[Download]")
        self.assertEqual(f.stripped_count, 1)
        self.assertEqual(f.normalised_count, 0)

    def test_absolute_allowlisted_url_is_untouched(self):
        url = "https://downloads.example.com/exports/xyz"
        f = LinkAllowlist(allowed_urls={url})
        text = f"[Download]({url})"
        self.assertEqual(f.feed(text) + f.flush(), text)
        self.assertEqual(f.normalised_count, 0)


if __name__ == "__main__":
    unittest.main()
