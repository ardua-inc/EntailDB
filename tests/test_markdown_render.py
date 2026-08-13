"""Tests for the browser-side markdown renderer in `app/static/index.html`.

The renderer is JavaScript with no build step, so these tests extract the
functions straight out of the page and run them under Node. That keeps a single
copy of the source — a JS file duplicated for testing would drift from the page
within a week.

Two things are being protected. The first is presentational: a model answers in
markdown, and before this existed a table arrived as a wall of pipes directly
above the same rows rendered properly from the tool result. The second is not
presentational at all — this renderer is the one place model output becomes
HTML, so the injection tests below matter more than the formatting ones.

Skipped rather than failed when Node is absent, so the suite still runs on a
machine that has no JS runtime.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "app" / "static" / "index.html"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


def _renderer_source() -> str:
    """Lift `esc`, `mdInline`, `mdTableRow` and `md` out of the page."""
    page = PAGE.read_text()
    start = page.index("const esc =")
    end = page.index("function el(")
    source = page[start:end]
    assert "function md(" in source, "renderer moved; update this extraction"
    return source


def _helpers_source() -> str:
    """Lift `selectedAfterReload` and `reconcileHistory` out of the page."""
    page = PAGE.read_text()
    start = page.index("/* Which connection is selected")
    end = page.index("/* Stream rendering.")
    return page[start:end]


def _tsv_source() -> str:
    """Lift `toTSV` out of the page."""
    page = PAGE.read_text()
    start = page.index("function toTSV(")
    return page[start:page.index("function copyText(")]


def _stream_source() -> str:
    """Lift `createStreamRenderer` out of the page, with what it depends on."""
    page = PAGE.read_text()
    start = page.index("function createStreamRenderer(")
    end = page.index("/* Results render as a real table")
    return _renderer_source() + page[start:end]


def render(markdown: str) -> str:
    script = (
        _renderer_source()
        + "\nconst input = JSON.parse(process.argv[1]);"
        + "\nprocess.stdout.write(md(input));"
    )
    done = subprocess.run(
        ["node", "-e", script, json.dumps(markdown)],
        capture_output=True, text=True, timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


# ── injection: the reason this file matters ───────────────────────────────

def test_html_in_model_output_is_not_executed():
    out = render("Look: <img src=x onerror=alert(1)> and <script>alert(2)</script>")
    assert "<img" not in out
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_html_inside_a_table_cell_is_escaped():
    out = render("| a |\n|---|\n| <b>bold</b> |")
    assert "<b>" not in out
    assert "&lt;b&gt;" in out


def test_a_javascript_link_is_left_as_text():
    out = render("[click](javascript:alert(1))")
    assert "<a" not in out
    assert "javascript:alert(1)" in out


def test_a_data_uri_link_is_left_as_text():
    out = render("[x](data:text/html;base64,PHNjcmlwdD4=)")
    assert "<a" not in out


def test_an_http_link_is_rendered_with_a_safe_rel():
    out = render("[report](https://example.com/r/1)")
    assert '<a href="https://example.com/r/1"' in out
    assert 'rel="noopener noreferrer"' in out


def test_a_relative_link_is_allowed():
    assert '<a href="/reports/7"' in render("[r](/reports/7)")


# ── tables, the case that prompted this ───────────────────────────────────

def test_a_table_renders_as_a_table():
    out = render(
        "| Store ID | City |\n|---|---|\n| 1 | Lethbridge |\n| 2 | Woodridge |"
    )
    assert out.count("<tr>") == 3
    assert "<th>Store ID</th>" in out
    assert "<td>Lethbridge</td>" in out
    assert "|" not in out


def test_an_aligned_delimiter_row_is_accepted():
    out = render("| a | b |\n|:--|--:|\n| 1 | 2 |")
    assert "<table>" in out
    assert "<td>1</td>" in out


def test_pipes_in_prose_do_not_become_a_table():
    """A delimiter row is required, or ordinary prose containing a pipe would
    silently turn into a malformed table."""
    out = render("The column a | b is unusual.")
    assert "<table>" not in out
    assert "a | b" in out


def test_a_table_may_follow_prose():
    out = render("Here are the stores:\n\n| id |\n|---|\n| 1 |")
    assert out.index("<p>") < out.index("<table>")
    assert "Here are the stores:" in out


# ── ordinary formatting ───────────────────────────────────────────────────

def test_bold_and_code_and_headings():
    assert "<strong>683,178</strong>" in render("There are **683,178** flights")
    assert "<code>store</code>" in render("the `store` table")
    assert "<h2>Summary</h2>" in render("## Summary")


def test_headings_do_not_exceed_h3():
    """Deeper levels collapse to h3 rather than emitting h4-h6 the page has no
    styles for."""
    assert "<h3>deep</h3>" in render("###### deep")


def test_bullet_and_numbered_lists():
    bullets = render("- one\n- two")
    assert bullets.count("<li>") == 2 and "<ul>" in bullets
    numbered = render("1. first\n2. second")
    assert "<ol>" in numbered and "<li>first</li>" in numbered


def test_a_fenced_block_is_preserved_verbatim():
    out = render("```\nSELECT *\nFROM store\n```")
    assert "<pre><code>SELECT *\nFROM store</code></pre>" in out


def test_markdown_inside_a_fence_is_not_interpreted():
    out = render("```\n**not bold**\n| a |\n```")
    assert "<strong>" not in out
    assert "**not bold**" in out


def test_a_line_break_inside_a_paragraph_survives():
    assert "<br>" in render("first line\nsecond line")


def test_blank_input_renders_to_nothing():
    assert render("") == ""


def test_a_lone_asterisk_is_not_treated_as_emphasis():
    out = render("2 * 3 = 6")
    assert "<em>" not in out


# ── the shape of a real answer ────────────────────────────────────────────

def test_a_representative_answer_renders_without_stray_markup():
    out = render(
        "Here are the **2 stores**:\n\n"
        "| Store ID | Address | Country |\n"
        "|---|---|---|\n"
        "| 1 | 47 MySakila Drive | Canada |\n"
        "| 2 | 28 MySQL Boulevard | Australia |\n\n"
        "Both are retail locations."
    )
    assert "<table>" in out and out.count("<tr>") == 3
    assert "<strong>2 stores</strong>" in out
    assert "Both are retail locations." in out
    # No raw markdown syntax left anywhere in the output.
    assert "|" not in out and "**" not in out
    assert not re.search(r"^\s*\|", out, re.M)


# ── the streaming renderer ────────────────────────────────────────────────

STREAM_HARNESS = """
// A DOM stub small enough to be obviously correct: the renderer only ever
// creates a div, sets className/innerHTML, and appends to a parent.
const makeDoc = () => ({ createElement: () => {
  const node = { tagName: "DIV", className: "", innerHTML: "", textContent: "" };
  node.remove = () => { const i = body.kids.indexOf(node); if (i >= 0) body.kids.splice(i, 1); };
  return node;
} });
const makeBody = () => { const kids = []; return { kids, appendChild: n => kids.push(n) }; };
const body = makeBody();
const stream = createStreamRenderer(body, makeDoc());
for (const ev of JSON.parse(process.argv[1])) {
  if (ev.k === "text") stream.text(ev.v);
  else if (ev.k === "block") stream.block({ tagName: "DETAILS", block: ev.v });
  else if (ev.k === "notice") stream.notice(ev.v);
  else if (ev.k === "fail") stream.fail(ev.v);
}
process.stdout.write(JSON.stringify({
  answer: stream.answer,
  kids: body.kids.map(n => n.tagName === "DETAILS"
    ? { block: n.block }
    : { prose: n.innerHTML + (n.textContent || "") }),
}));
"""


def play(events: list[dict]) -> dict:
    """Run a scripted event sequence through the renderer under Node."""
    done = subprocess.run(
        ["node", "-e", _stream_source() + STREAM_HARNESS, json.dumps(events)],
        capture_output=True, text=True, timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_first_delta_is_not_dropped():
    """Regression, and the reason this renderer takes `doc` as an argument.

    The accumulator used to be reset inside the element-creating helper, and
    `proseEl().innerHTML = md(runText)` evaluates its left side first — so the
    opening delta was cleared before `md` ever saw it. Every answer silently
    lost its first word, which is a fidelity bug wearing a cosmetic disguise.
    """
    out = play([{"k": "text", "v": "There"}, {"k": "text", "v": " are "},
                {"k": "text", "v": "2 stores."}])
    assert out["answer"] == "There are 2 stores."
    assert "There are 2 stores." in out["kids"][0]["prose"]


def test_streaming_deltas_re_render_rather_than_append():
    """Markdown must be parsed over the whole run, not per delta, or a table
    split across deltas never forms."""
    out = play([{"k": "text", "v": "| a |\n|-"}, {"k": "text", "v": "--|\n| 1 |"}])
    assert len(out["kids"]) == 1
    assert "<table>" in out["kids"][0]["prose"]
    assert out["kids"][0]["prose"].count("<table>") == 1


def test_prose_before_and_after_a_block_are_separate_elements():
    """The bug from the screenshot: a preamble emitted before a tool call was
    glued onto the answer after it, rendering as one doubled sentence."""
    out = play([
        {"k": "text", "v": "Here are the 2 stores:"},
        {"k": "block", "v": "sql"},
        {"k": "text", "v": "Here are the 2 stores:"},
    ])
    assert [k.get("block", "prose") for k in out["kids"]] == ["prose", "sql", "prose"]
    assert out["kids"][0]["prose"] == out["kids"][2]["prose"]
    assert "storesHere" not in out["kids"][2]["prose"]


def test_blocks_appear_in_arrival_order():
    """Previously every block landed below a prose element created up front,
    so the answer always rendered above the SQL that produced it."""
    out = play([
        {"k": "block", "v": "sql"}, {"k": "block", "v": "rows"},
        {"k": "text", "v": "Two stores."},
    ])
    assert [k.get("block", "prose") for k in out["kids"]] == ["sql", "rows", "prose"]


def test_the_full_answer_is_accumulated_across_blocks():
    """What gets pushed to history must be the whole turn's prose, since the
    next question is answered against it."""
    out = play([{"k": "text", "v": "Checking. "}, {"k": "block", "v": "sql"},
                {"k": "text", "v": "There are 2."}])
    assert out["answer"] == "Checking. There are 2."


def test_an_error_message_is_escaped():
    out = play([{"k": "fail", "v": "<script>alert(1)</script>"}])
    assert "<script>" not in out["kids"][0]["prose"]
    assert "&lt;script&gt;" in out["kids"][0]["prose"]


def test_a_turn_with_no_prose_creates_no_empty_bubble():
    out = play([{"k": "block", "v": "sql"}])
    assert out["answer"] == ""
    assert [k.get("block") for k in out["kids"]] == ["sql"]


# ── the active connection ─────────────────────────────────────────────────

def call(fn: str, *args) -> object:
    """Evaluate one of the page's pure helpers under Node."""
    script = (
        _helpers_source()
        + f"\nconst a = JSON.parse(process.argv[1]);"
        + f"\nprocess.stdout.write(JSON.stringify({fn}(...a)));"
    )
    done = subprocess.run(
        ["node", "-e", script, json.dumps(list(args))],
        capture_output=True, text=True, timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


CONNS = [{"id": "a", "label": "air"}, {"id": "d", "label": "dvdrental"}]


def test_the_selected_connection_survives_a_settings_reload():
    """The reported bug: opening Settings rebuilt the option list, which reset
    the select to its first entry, so the next question silently ran against a
    different database."""
    assert call("selectedAfterReload", "d", CONNS) == "d"


def test_a_deleted_connection_falls_back_to_the_first():
    assert call("selectedAfterReload", "gone", CONNS) == "a"


def test_no_connections_selects_nothing():
    assert call("selectedAfterReload", "a", []) == ""


# `reconcileHistory` and its tests were deleted with the divider it drove.
# The invariant it enforced — history must not cross databases — is now a
# property of the API's shape rather than a check that runs: a thread names its
# connection, and the client cannot supply history at all. That is asserted
# server-side in `tests/test_api_threads.py`, where it belongs.


def test_a_replayed_transcript_uses_the_same_renderer_as_a_live_one():
    """A restored conversation must look identical to a live one — same SQL
    panels, tables and copy buttons. The only way to guarantee that is for both
    paths to go through `applyEvent`."""
    page = PAGE.read_text()
    assert "function applyEvent(" in page
    replay = page[page.index("function replay("):page.index("async function send(")]
    assert "applyEvent(" in replay
    send = page[page.index("async function send("):page.index("/* ── conversations")]
    assert "applyEvent(" in send
    # ...and no second copy of the panel-building markup in either path.
    assert send.count("details class=\"trace\"") == 0


def test_the_version_is_fetched_at_startup_not_inside_a_handler():
    """A scripted edit once spliced this into the Settings click handler, so
    the build number only appeared after opening Settings. Asserting it sits at
    top level keeps the check honest."""
    page = PAGE.read_text()
    script = page[page.index("<script>"):]
    for line in script.splitlines():
        if 'fetch("/api/version")' in line:
            assert not line.startswith((" ", "\t")), (
                "the version fetch is nested inside a function; it must run at "
                "startup"
            )
            return
    pytest.fail("no startup fetch of /api/version found")


# ── clipboard payloads ────────────────────────────────────────────────────

def tsv(columns: list, rows: list) -> str:
    script = (
        _tsv_source()
        + "\nconst [c, r] = JSON.parse(process.argv[1]);"
        + "\nprocess.stdout.write(toTSV(c, r));"
    )
    done = subprocess.run(
        ["node", "-e", script, json.dumps([columns, rows])],
        capture_output=True, text=True, timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_rows_are_tab_separated_with_a_header():
    out = tsv(["id", "city"], [[1, "Lethbridge"], [2, "Woodridge"]])
    assert out == "id\tcity\n1\tLethbridge\n2\tWoodridge"


def test_a_value_containing_a_tab_is_quoted():
    """Unquoted, one stray tab shifts every column to its right — the paste
    looks plausible and is wrong, which is the worst kind of wrong."""
    out = tsv(["a", "b"], [["x\ty", "z"]])
    assert out.split("\n")[1] == '"x\ty"\tz'


def test_a_value_containing_a_newline_is_quoted():
    out = tsv(["addr"], [["12 High St\nApt 4"]])
    assert out.split("\n", 1)[1].startswith('"12 High St')
    assert out.rstrip().endswith('Apt 4"')


def test_internal_quotes_are_doubled():
    out = tsv(["note"], [['he said "hi"']])
    assert out.split("\n")[1] == '"he said ""hi"""'


def test_a_column_name_needing_quotes_is_quoted_too():
    assert tsv(["od\td"], [[1]]).split("\n")[0] == '"od\td"'


def test_null_is_copied_as_shown_rather_than_blanked():
    """The table displays NULL; the clipboard says NULL. Blanking would make a
    NULL indistinguishable from an empty string, which is a value change made
    silently on the way to the clipboard."""
    out = tsv(["a", "b"], [[None, ""]])
    assert out.split("\n")[1] == "NULL\t"


def test_numbers_are_not_reformatted():
    """Decimals arrive as strings from the API and must not be rounded, padded
    or localised on their way to a spreadsheet."""
    out = tsv(["amount"], [["91427.60"], ["0.99"], ["1e10"]])
    assert out.split("\n")[1:] == ["91427.60", "0.99", "1e10"]


def test_an_empty_result_still_carries_its_header():
    assert tsv(["id", "city"], []) == "id\tcity"


def test_booleans_survive():
    assert tsv(["ok"], [[True], [False]]).split("\n")[1:] == ["true", "false"]


def test_every_trace_panel_offers_a_copy_control():
    """Each `details.trace` the page builds should be copyable; a new block
    type added without one would be an inconsistency users notice before
    developers do."""
    page = PAGE.read_text()
    script = page[page.index("<script>"):]
    built = script.count('<details class="trace"')
    wired = script.count("addCopy(")
    assert built > 0
    # One addCopy per constructed panel, plus the helper's own definition.
    assert wired >= built, f"{built} trace panels built but only {wired} addCopy calls"


def test_the_copy_control_suppresses_the_summary_toggle():
    """The button sits inside a <summary>; without both suppressions, copying
    would also collapse the panel being copied from."""
    page = PAGE.read_text()
    handler = page[page.index("function addCopy("):page.index("/* Results render")]
    assert "e.preventDefault()" in handler
    assert "e.stopPropagation()" in handler


ESSENTIAL = [
    "loadSettings", "send", "boot", "applyEvent", "replay", "openThread",
    "startDraft", "refreshThreads", "renderThreads", "openMostRecentOrDraft",
    "renderResult", "createStreamRenderer", "addCopy", "md", "toTSV", "api",
    "whenLabel", "selectedAfterReload", "renderProfiles", "showActiveModel",
]


@pytest.mark.parametrize("name", ESSENTIAL)
def test_the_page_defines_every_function_it_relies_on(name):
    """A scripted edit deleted `loadSettings` and the Send binding by taking a
    slice that reached further than intended. The page still parsed — a missing
    function is a runtime ReferenceError, not a syntax error — so it loaded
    looking normal and did nothing. Syntax checking cannot catch this; naming
    the functions can."""
    page = PAGE.read_text()
    assert (f"function {name}(" in page) or (f"{name} = " in page), (
        f"{name} is referenced by the page but no longer defined"
    )


@pytest.mark.parametrize("binding", [
    '$("#send").onclick',
    '$("#input").addEventListener("keydown"',
    '$("#conn").onchange',
    '$("#newThread").onclick',
    '$("#threadsBtn").onclick',
    '$("#settingsBtn").onclick',
    '$("#addProfile").onclick',
])
def test_the_controls_are_wired(binding):
    """Same failure took the Send button's binding with it: the button was
    present, styled, and inert."""
    assert binding in PAGE.read_text()


def test_a_notice_is_cleared_when_real_output_arrives():
    """A finished answer must not carry a stale "waiting…" line above it."""
    out = play([{"k": "notice", "v": "Claude's API is busy — waiting 2s."},
                {"k": "text", "v": "There are 2 stores."}])
    rendered = " ".join(k.get("prose", "") for k in out["kids"])
    assert "waiting" not in rendered
    assert "There are 2 stores." in rendered


def test_a_notice_is_replaced_not_stacked():
    out = play([{"k": "notice", "v": "waiting 2s"},
                {"k": "notice", "v": "waiting 5s"}])
    text = " ".join(str(k) for k in out["kids"])
    assert "waiting 5s" in text
    assert "waiting 2s" not in text


def test_a_notice_is_not_part_of_the_answer():
    out = play([{"k": "notice", "v": "Claude's API is busy."},
                {"k": "text", "v": "Real answer."}])
    assert out["answer"] == "Real answer."


# ── issue #2: the work is available, not in the way ───────────────────────

def test_result_and_sql_panels_are_collapsed_by_default():
    """GitHub #2. The summary carries what a reader needs at a glance — "SQL",
    "50 of 4312 rows" — so the answer stays readable and the evidence is one
    click away rather than something to scroll past."""
    page = PAGE.read_text()
    script = page[page.index("<script>"):]
    assert 'details class="trace" open' not in script
    assert script.count('details class="trace"') >= 2


def test_a_failed_query_panel_stays_open():
    """The deliberate exception. A failure is not work to inspect on request;
    a collapsed panel reads like a step that went fine."""
    render = PAGE.read_text()
    block = render[render.index("function renderResult("):render.index("async function send(")]
    error_branch = block[block.index("data.error"):block.index("} else if")]
    assert "box.open = true" in error_branch


def test_the_generated_facts_document_wraps():
    """GitHub #1. The facts document is prose; horizontal scrolling to read a
    sentence is not reading. SQL keeps its own scroll, where a broken line
    changes meaning."""
    page = PAGE.read_text()
    assert "pre.doc" in page
    assert "white-space:pre-wrap" in page[page.index("pre.doc"):page.index("pre.doc") + 200]
    assert 'class="doc"' in page[page.index("facts generated") - 400:
                                 page.index("facts generated") + 400]


def test_sql_is_not_forced_to_wrap():
    """Deliberately different from the facts document: a wrapped SQL line reads
    as a different statement than the one that ran."""
    page = PAGE.read_text()
    sql_panel = page[page.index("<summary>SQL</summary>") - 200:
                     page.index("<summary>SQL</summary>") + 200]
    assert 'class="doc"' not in sql_panel
