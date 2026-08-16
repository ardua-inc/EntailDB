"""Tests for `_mongo_shell_text` (`app/main.py`) — the function that turns a
`mongo_query` tool call into the text shown in the "Query" panel.

The bar this has to clear, reported live: what's shown must be an actual
Mongo query, not a dump of this tool's own JSON argument schema. The SQL
panel shows real, pasteable SQL; `db.Deal.find({"status": "OPEN"})` is the
equivalent for Mongo — copyable straight into `mongosh`, not
`{"collection": "Deal", "operation": "find", "filter": {"status": "OPEN"}}`,
which is not valid syntax anywhere.
"""

from __future__ import annotations

from app.main import _mongo_shell_text


def test_find_renders_as_a_shell_call():
    text = _mongo_shell_text({"collection": "Deal", "operation": "find",
                              "filter": {"status": "OPEN"}})
    assert text.startswith("db.Deal.find(")
    assert '"status": "OPEN"' in text


def test_find_with_projection_passes_both_arguments():
    text = _mongo_shell_text({"collection": "Deal", "operation": "find",
                              "filter": {"status": "OPEN"}, "projection": {"name": 1}})
    assert text.startswith("db.Deal.find(")
    assert '"status": "OPEN"' in text
    assert '"name": 1' in text


def test_find_with_sort_appends_a_sort_call():
    text = _mongo_shell_text({"collection": "Deal", "operation": "find",
                              "filter": {}, "sort": {"name": 1}})
    assert ".sort(" in text
    assert '"name": 1' in text


def test_aggregate_renders_as_a_shell_call_with_the_pipeline():
    text = _mongo_shell_text({"collection": "Deal", "operation": "aggregate",
                              "pipeline": [{"$match": {"status": "OPEN"}},
                                          {"$count": "n"}]})
    assert text.startswith("db.Deal.aggregate([")
    assert "$match" in text and "$count" in text


def test_count_renders_as_count_documents():
    text = _mongo_shell_text({"collection": "Deal", "operation": "count", "filter": {}})
    assert text == "db.Deal.countDocuments({})"


def test_distinct_without_a_filter_takes_one_argument():
    text = _mongo_shell_text({"collection": "Deal", "operation": "distinct",
                              "field": "status"})
    assert text == 'db.Deal.distinct("status")'


def test_distinct_with_a_filter_takes_two_arguments():
    text = _mongo_shell_text({"collection": "Deal", "operation": "distinct",
                              "field": "status", "filter": {"tenantId": "gbt"}})
    assert text.startswith('db.Deal.distinct("status", ')
    assert '"tenantId": "gbt"' in text


def test_an_unrecognized_operation_falls_back_to_a_plain_dump():
    """Reached only for a query the gate already refused before it ran —
    still shown, as what was actually asked for, not hidden."""
    text = _mongo_shell_text({"collection": "Deal", "operation": "delete"})
    assert '"operation": "delete"' in text
