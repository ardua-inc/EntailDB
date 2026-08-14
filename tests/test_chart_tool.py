"""Unit tests for `ChartTool` (`app/main.py`) — the tool that draws a chart
from the rows a prior `run_sql` call already returned this turn.

Run against a real SQLite database, the same policy `tests/test_connectors.py`
states and was bought with a defect: a hand-mocked `QueryResult` only knows the
behaviours its author already thought of, and this tool's entire job is
disclosing NULLs and truncation faithfully — exactly the kind of behaviour a
fake tends to get wrong by omission.

`ChartTool` never trusts model-supplied data, only a chart type and two column
names; it always reads the real rows via `SqlTool.calls`. These tests are
built around that boundary: which rows get plotted, in what order, and what
gets disclosed rather than silently dropped.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.connectors import Connector, QueryResult
from app.main import ChartTool, SqlTool


@pytest.fixture
def db(tmp_path):
    """Same shape as `tests/test_connectors.py`'s fixture: 120 rows, `qty`
    NULL on every 10th, one string column."""
    path = tmp_path / "fixture.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, label TEXT, qty INTEGER)")
    conn.executemany(
        "INSERT INTO item (id, label, qty) VALUES (?, ?, ?)",
        [(i, f"item-{i:03d}", None if i % 10 == 0 else i * 2) for i in range(1, 121)],
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


@pytest.fixture
def chart_tool(sql_tool):
    return ChartTool(sql_tool)


def run(sql_tool: SqlTool, sql: str) -> None:
    """Drive a query through the same path `FidelityRunner` would."""
    sql_tool({"query": sql})


def chart(chart_tool: ChartTool, **kwargs) -> tuple[dict, bool]:
    result = chart_tool(kwargs)
    return json.loads(result.content), result.is_error


# ── no data to chart yet ────────────────────────────────────────────────────

def test_refuses_when_no_query_has_run_yet(chart_tool):
    payload, is_error = chart(chart_tool, chart_type="bar", x_column="label", y_column="qty")
    assert is_error
    assert "run a query" in payload["error"].lower()


def test_refuses_when_only_a_prior_query_failed(sql_tool, chart_tool):
    run(sql_tool, "DELETE FROM item")  # refused by the read-only gate
    payload, is_error = chart(chart_tool, chart_type="bar", x_column="label", y_column="qty")
    assert is_error
    assert "no successful" in payload["error"].lower()


def test_a_failed_query_after_a_successful_one_is_ignored(sql_tool, chart_tool):
    """The failed call must not blank out the successful one before it."""
    run(sql_tool, "SELECT label, qty FROM item WHERE id = 1")  # qty = 2
    run(sql_tool, "DELETE FROM item")  # refused
    payload, is_error = chart(chart_tool, chart_type="bar", x_column="label", y_column="qty")
    assert not is_error
    assert payload["points"] == [{"x": "item-001", "y": 2.0}]


def test_uses_the_most_recent_successful_call(sql_tool, chart_tool):
    run(sql_tool, "SELECT label, qty FROM item WHERE id = 1")  # qty = 2
    run(sql_tool, "SELECT label, qty FROM item WHERE id = 2")  # qty = 4
    payload, is_error = chart(chart_tool, chart_type="bar", x_column="label", y_column="qty")
    assert not is_error
    assert payload["points"] == [{"x": "item-002", "y": 4.0}]


# ── column and type validation ──────────────────────────────────────────────

def test_unknown_column_refuses_and_lists_actual_columns(sql_tool, chart_tool):
    run(sql_tool, "SELECT label, qty FROM item WHERE id = 1")
    payload, is_error = chart(chart_tool, chart_type="bar", x_column="label", y_column="nope")
    assert is_error
    assert "label" in payload["error"] and "qty" in payload["error"]


def test_unsupported_chart_type_refuses_at_runtime(sql_tool, chart_tool):
    """Not just a schema `enum` — the tool checks this itself, the same
    belt-and-suspenders posture as the SQL read-only gate not trusting the
    model to only ever send `SELECT`."""
    run(sql_tool, "SELECT label, qty FROM item WHERE id = 1")
    payload, is_error = chart(chart_tool, chart_type="pie", x_column="label", y_column="qty")
    assert is_error
    assert "pie" in payload["error"]


def test_non_numeric_y_column_refuses_without_plotting(sql_tool, chart_tool):
    run(sql_tool, "SELECT qty, label FROM item WHERE id <= 3 ORDER BY id")
    payload, is_error = chart(chart_tool, chart_type="bar", x_column="qty", y_column="label")
    assert is_error
    assert "not numeric" in payload["error"].lower()


def test_bool_y_value_refuses_as_non_numeric(sql_tool, chart_tool):
    """SQLite's driver never returns a Python `bool` for a numeric column
    (it's stored and read back as 0/1), so this one case is hand-built rather
    than run through the real engine — the deliberate, narrow exception to
    this file's real-database policy, since the driver simply cannot produce
    it. `bool` is an `int` subclass in Python, so this is the case that would
    slip through an `isinstance(y, (int, float, Decimal))` check without the
    explicit exclusion."""
    result = QueryResult(columns=["label", "flag"],
                         rows=[["a", True], ["b", False]],
                         total_rows=2, truncated=False)
    sql_tool.calls.append({"sql": "SELECT label, flag FROM x", "result": result})
    payload, is_error = chart(chart_tool, chart_type="bar", x_column="label", y_column="flag")
    assert is_error
    assert "not numeric" in payload["error"].lower()


# ── the rows plotted must be the real rows, unmodified ──────────────────────

def test_bar_points_match_real_rows_in_original_order(sql_tool, chart_tool):
    run(sql_tool, "SELECT label, qty FROM item WHERE id IN (3, 1, 2) ORDER BY id DESC")
    payload, is_error = chart(chart_tool, chart_type="bar", x_column="label", y_column="qty")
    assert not is_error
    assert payload["points"] == [
        {"x": "item-003", "y": 6.0},
        {"x": "item-002", "y": 4.0},
        {"x": "item-001", "y": 2.0},
    ]


def test_duplicate_x_categories_are_not_aggregated(sql_tool, chart_tool):
    """Aggregation belongs in SQL, not the renderer — the chart is of the
    rows, not of a transform of them nobody can see."""
    run(sql_tool, "SELECT 'same' AS label, qty FROM item WHERE id IN (1, 2) ORDER BY id")
    payload, is_error = chart(chart_tool, chart_type="bar", x_column="label", y_column="qty")
    assert not is_error
    assert len(payload["points"]) == 2
    assert payload["points"][0]["x"] == payload["points"][1]["x"] == "same"
    assert {p["y"] for p in payload["points"]} == {2.0, 4.0}


# ── NULLs: disclosed, never silently dropped or invented ───────────────────

def test_null_y_values_are_skipped_and_disclosed(sql_tool, chart_tool):
    run(sql_tool, "SELECT label, qty FROM item WHERE id IN (9, 10, 11) ORDER BY id")
    payload, is_error = chart(chart_tool, chart_type="bar", x_column="label", y_column="qty")
    assert not is_error
    assert [p["x"] for p in payload["points"]] == ["item-009", "item-011"]
    assert "1 row" in payload["note"]
    assert "qty" in payload["note"]


def test_null_x_value_becomes_the_string_NULL(sql_tool, chart_tool):
    """Matches the table and TSV-export convention: NULL is shown, not
    blanked, which would make it indistinguishable from an empty string."""
    run(sql_tool, "SELECT NULL AS label, qty FROM item WHERE id = 1")
    payload, is_error = chart(chart_tool, chart_type="bar", x_column="label", y_column="qty")
    assert not is_error
    assert payload["points"][0]["x"] == "NULL"


def test_all_null_y_column_refuses(sql_tool, chart_tool):
    run(sql_tool, "SELECT label, qty FROM item WHERE qty IS NULL")
    payload, is_error = chart(chart_tool, chart_type="bar", x_column="label", y_column="qty")
    assert is_error
    assert "no numeric values" in payload["error"].lower()


# ── preview truncation, disclosed exactly as the table already does ────────

def test_truncated_source_discloses_the_preview_boundary(sql_tool, chart_tool):
    run(sql_tool, "SELECT label, qty FROM item")  # no LIMIT: 120 rows, 50-row preview
    payload, is_error = chart(chart_tool, chart_type="bar", x_column="label", y_column="qty")
    assert not is_error
    assert payload["total_rows_matched"] == 120
    assert "50" in payload["note"] and "120" in payload["note"]


# ── the one field that is model prose, not evidence ─────────────────────────

def test_title_passes_through_unvalidated(sql_tool, chart_tool):
    run(sql_tool, "SELECT label, qty FROM item WHERE id = 1")
    payload, is_error = chart(chart_tool, chart_type="bar", x_column="label", y_column="qty",
                              title="Whatever the model wants to say")
    assert not is_error
    assert payload["title"] == "Whatever the model wants to say"


def test_title_is_optional(sql_tool, chart_tool):
    run(sql_tool, "SELECT label, qty FROM item WHERE id = 1")
    payload, is_error = chart(chart_tool, chart_type="bar", x_column="label", y_column="qty")
    assert not is_error
    assert "title" not in payload


# ── error shape matches SqlTool's, so the frontend needs no second branch ──

def test_error_payload_matches_sql_tools_shape(sql_tool, chart_tool):
    payload, is_error = chart(chart_tool, chart_type="bar", x_column="label", y_column="qty")
    assert is_error
    assert set(payload.keys()) == {"error"}
