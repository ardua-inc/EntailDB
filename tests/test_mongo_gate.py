"""Tests for `refuse_reason` (`app/connectors/mongodb.py`) — the MongoDB
read-only gate.

There is no SQL string to parse here, so this validates a structured
operation instead: a `collection` + `operation` enum + filter/pipeline. The
same rigor as `base.py`'s SQL gate tests in `tests/test_connectors.py`, aimed
at the two things Mongo can do that SQL can't — execute arbitrary
server-side JavaScript (`$where`/`$function`/`$accumulator`) and write
results to a collection from inside an aggregation (`$out`/`$merge`) — plus
the structural check that this connector's tool never accepts free text in
the first place, so there is no "detect a disguised write" step to fool.
"""

from __future__ import annotations

import pytest

from app.connectors.mongodb import refuse_reason


def assert_refused(query: dict, contains: str = "") -> None:
    reason = refuse_reason(query)
    assert reason is not None, f"expected a refusal for {query!r}"
    if contains:
        assert contains.lower() in reason.lower()


def assert_permitted(query: dict) -> None:
    assert refuse_reason(query) is None, f"unexpectedly refused: {query!r}"


# ── legitimate reads ────────────────────────────────────────────────────────

def test_find_is_permitted():
    assert_permitted({"collection": "Deal", "operation": "find",
                      "filter": {"status": "active"}})


def test_aggregate_with_allowed_stages_is_permitted():
    assert_permitted({"collection": "Deal", "operation": "aggregate", "pipeline": [
        {"$match": {"status": "active"}},
        {"$group": {"_id": "$ownerId", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
        {"$limit": 10},
    ]})


def test_count_is_permitted():
    assert_permitted({"collection": "Deal", "operation": "count",
                      "filter": {}})


def test_distinct_is_permitted():
    assert_permitted({"collection": "Deal", "operation": "distinct",
                      "field": "status"})


def test_lookup_with_a_sub_pipeline_is_permitted():
    """A correlated sub-query, not a write — the sub-pipeline is still
    walked by the recursive scan, but nothing in it is forbidden."""
    assert_permitted({"collection": "Deal", "operation": "aggregate", "pipeline": [
        {"$lookup": {"from": "Company", "localField": "companyId",
                     "foreignField": "_id", "as": "company",
                     "pipeline": [{"$match": {"disabled": False}}]}},
    ]})


# ── operation and collection validation ─────────────────────────────────────

@pytest.mark.parametrize("operation", ["insert", "update", "delete", "drop",
                                       "insertOne", "updateMany", "dropDatabase",
                                       "", None, "FIND"])
def test_an_unrecognized_operation_refuses(operation):
    assert_refused({"collection": "Deal", "operation": operation},
                   "read-only")


@pytest.mark.parametrize("collection", ["", None, 123, "$cmd", "$where"])
def test_an_invalid_collection_refuses(collection):
    assert_refused({"collection": collection, "operation": "find"},
                   "collection")


# ── the two write-capable aggregation stages ────────────────────────────────

def test_out_stage_refuses():
    assert_refused({"collection": "Deal", "operation": "aggregate", "pipeline": [
        {"$match": {}}, {"$out": "some_other_collection"},
    ]}, "$out")


def test_merge_stage_refuses():
    assert_refused({"collection": "Deal", "operation": "aggregate", "pipeline": [
        {"$match": {}}, {"$merge": {"into": "some_other_collection"}},
    ]}, "$merge")


def test_out_nested_inside_a_facet_sub_pipeline_still_refuses():
    """MongoDB itself rejects $out inside $facet, but this gate does not
    lean on that — the recursive scan catches it independently, the belt to
    the stage allowlist's braces."""
    assert_refused({"collection": "Deal", "operation": "aggregate", "pipeline": [
        {"$facet": {"a": [{"$out": "elsewhere"}]}},
    ]}, "$out")


def test_an_unrecognized_stage_refuses():
    assert_refused({"collection": "Deal", "operation": "aggregate", "pipeline": [
        {"$graphLookup": {}},
    ]}, "read-only")


# ── JavaScript-eval operators ───────────────────────────────────────────────

def test_where_in_a_find_filter_refuses():
    assert_refused({"collection": "Deal", "operation": "find",
                    "filter": {"$where": "function() { return true; }"}},
                   "$where")


def test_where_nested_inside_a_match_stage_refuses():
    """The stage allowlist alone would accept $match; the recursive scan is
    what catches an operator hidden inside it."""
    assert_refused({"collection": "Deal", "operation": "aggregate", "pipeline": [
        {"$match": {"$where": "function() { return true; }"}},
    ]}, "$where")


def test_function_in_a_group_expression_refuses():
    assert_refused({"collection": "Deal", "operation": "aggregate", "pipeline": [
        {"$group": {"_id": None, "n": {"$function": {
            "body": "function() {}", "args": [], "lang": "js"}}}},
    ]}, "$function")


def test_accumulator_in_a_group_expression_refuses():
    assert_refused({"collection": "Deal", "operation": "aggregate", "pipeline": [
        {"$group": {"_id": None, "n": {"$accumulator": {
            "init": "function() {}", "accumulate": "function() {}",
            "accumulateArgs": [], "merge": "function() {}", "lang": "js"}}}},
    ]}, "$accumulator")


def test_where_in_a_distinct_filter_refuses():
    assert_refused({"collection": "Deal", "operation": "distinct", "field": "status",
                    "filter": {"$where": "function() { return true; }"}},
                   "$where")


# ── shape validation ─────────────────────────────────────────────────────────

def test_aggregate_without_a_pipeline_refuses():
    assert_refused({"collection": "Deal", "operation": "aggregate"})


def test_aggregate_pipeline_must_be_a_list():
    assert_refused({"collection": "Deal", "operation": "aggregate",
                    "pipeline": {"$match": {}}})


def test_a_stage_with_more_than_one_key_refuses():
    assert_refused({"collection": "Deal", "operation": "aggregate",
                    "pipeline": [{"$match": {}, "$sort": {}}]})


def test_a_non_dict_stage_refuses():
    assert_refused({"collection": "Deal", "operation": "aggregate",
                    "pipeline": ["$match"]})
