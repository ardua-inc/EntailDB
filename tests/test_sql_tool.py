"""Unit tests for `SqlTool` (`app/main.py`) — the one tool every question goes
through, and, until now, the one tool with no dedicated test file of its own
(`tests/test_api_threads.py`'s own docstring explicitly scopes itself to the
failures that happen *before* a provider is reached, not to what the tool
itself does).

The `max_rows` tests here exist because of a real conversation: asked for all
692 rows of an airports table, the model ran fourteen separate 50-row queries
to assemble them, because `run_sql` had no way to ask for more than the
hard-coded preview in one call. `max_rows` is the fix — bounded by
`MAX_PREVIEW_ROWS` so "give me everything" on a 30M-row table can never
become an unbounded fetch.

Run against a real SQLite database, same policy as `tests/test_connectors.py`
and `tests/test_chart_tool.py`: a hand-mocked result would not exercise the
driver's own truncation and NULL behaviour.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.connectors import PREVIEW_ROWS, Connector
from app.main import MAX_PREVIEW_ROWS, SqlTool, _clamp_preview


@pytest.fixture
def db(tmp_path):
    """120 rows — enough to exceed the default 50-row preview but nowhere
    near `MAX_PREVIEW_ROWS`, so a clamp at the ceiling has to be checked
    separately (see `RecordingConnector` below) rather than by row count."""
    path = tmp_path / "fixture.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, label TEXT)")
    conn.executemany(
        "INSERT INTO item (id, label) VALUES (?, ?)",
        [(i, f"item-{i:03d}") for i in range(1, 121)],
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def connector(db):
    c = Connector("sqlite", {"path": str(db)})
    yield c
    c.close()


@pytest.fixture
def sql_tool(connector):
    return SqlTool(connector)


class RecordingConnector:
    """Wraps a real connector to record the `preview` it was actually called
    with — the only way to observe the ceiling being applied, since a fixture
    large enough to exceed `MAX_PREVIEW_ROWS` (1000 rows) would be needlessly
    slow to build for what is otherwise a one-line assertion."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.preview_calls: list[int] = []

    def query(self, sql, preview=PREVIEW_ROWS):
        self.preview_calls.append(preview)
        return self.inner.query(sql, preview=preview)

    def close(self) -> None:
        self.inner.close()


def run(sql_tool: SqlTool, max_rows=None):
    args = {"query": "SELECT id, label FROM item ORDER BY id"}
    if max_rows is not None:
        args["max_rows"] = max_rows
    sql_tool(args)
    return sql_tool.calls[-1]["result"]


# ── _clamp_preview: pure logic, no database needed ─────────────────────────

def test_omitted_falls_back_to_the_default():
    assert _clamp_preview(None) == PREVIEW_ROWS


def test_non_numeric_falls_back_to_the_default():
    """A model sending `max_rows: "lots"` must not crash the tool."""
    assert _clamp_preview("lots") == PREVIEW_ROWS


def test_zero_falls_back_to_the_default():
    assert _clamp_preview(0) == PREVIEW_ROWS


def test_negative_falls_back_to_the_default():
    assert _clamp_preview(-5) == PREVIEW_ROWS


def test_a_numeric_string_is_accepted():
    """Arguments arrive as JSON; a number sent as a string must still work."""
    assert _clamp_preview("200") == 200


def test_an_in_range_value_passes_through():
    assert _clamp_preview(200) == 200


def test_an_over_ceiling_value_is_clamped():
    assert _clamp_preview(999_999) == MAX_PREVIEW_ROWS


# ── SqlTool end to end, against the real 120-row fixture ───────────────────

def test_default_preview_is_used_when_max_rows_is_omitted(sql_tool):
    result = run(sql_tool)
    assert len(result.rows) == PREVIEW_ROWS
    assert result.truncated is True
    assert result.total_rows == 120


def test_max_rows_returns_more_than_the_default_preview(sql_tool):
    result = run(sql_tool, max_rows=100)
    assert len(result.rows) == 100
    assert result.truncated is True
    assert result.total_rows == 120


def test_max_rows_large_enough_returns_the_full_result(sql_tool):
    result = run(sql_tool, max_rows=200)
    assert len(result.rows) == 120
    assert result.truncated is False


def test_a_non_numeric_max_rows_does_not_error_and_uses_the_default(sql_tool):
    result = run(sql_tool, max_rows="lots")
    assert len(result.rows) == PREVIEW_ROWS
    assert result.error is None


def test_max_rows_is_wired_to_the_connectors_preview_argument(connector):
    """Confirms the value that reaches the connector directly, not just the
    row count it happens to produce on this fixture."""
    spy = RecordingConnector(connector)
    tool = SqlTool(spy)
    run(tool, max_rows=30)
    assert spy.preview_calls == [30]


def test_max_rows_over_the_ceiling_reaches_the_connector_clamped(connector):
    spy = RecordingConnector(connector)
    tool = SqlTool(spy)
    run(tool, max_rows=999_999)
    assert spy.preview_calls == [MAX_PREVIEW_ROWS]
