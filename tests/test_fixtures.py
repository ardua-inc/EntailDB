"""Tests for the fixture loader and canonical serializer."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from evals.fixtures import (
    CASES_DIR,
    ToolSpec,
    FixtureToolLayer,
    build_response,
    load_case,
    load_cases,
    load_prompt,
    normalise_number,
    numbers_in,
    numeric_spans,
    urls_in,
)


class TestNumberNormalisation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1268", Decimal("1268")),
            ("1,268", Decimal("1268")),
            ("$1,268", Decimal("1268")),
            ("$1,268.50", Decimal("1268.50")),
            ("12.5%", Decimal("12.5")),
            ("0", Decimal(0)),
        ],
    )
    def test_equivalent_forms_collapse(self, raw, expected):
        assert normalise_number(raw) == expected

    def test_unparseable(self):
        assert normalise_number("abc") is None
        assert normalise_number("") is None

    def test_word_glued_digits_are_not_numbers(self):
        """`Q3` must not yield a bare 3, or every quarter label reads as a
        fabricated figure."""
        assert numbers_in("revenue for Q3 was strong") == set()
        assert numbers_in("see v2 of the report") == set()

    def test_extracts_across_formats(self):
        assert numbers_in("1,268 orders worth $91,427.60 (12.5% up)") == {
            Decimal("1268"),
            Decimal("91427.60"),
            Decimal("12.5"),
        }

    def test_identifier_suffix_is_extracted(self):
        # `C-100234` yields 100234 -- harmless, because the same extraction
        # runs over the payload, so an id the model was shown is allowed.
        assert numbers_in("C-100234") == {Decimal("100234")}

    def test_spans_carry_offsets(self):
        spans = numeric_spans("we saw 42 and 43")
        assert [s[0] for s in spans] == ["42", "43"]
        assert spans[0][2] == 7

    def test_urls(self):
        assert urls_in("go to https://example.com/a, then http://b.test") == {
            "https://example.com/a",
            "http://b.test",
        }

    def test_urls_empty(self):
        assert urls_in("") == set()


class TestResponseRendering:
    def test_table_renders_with_counts(self):
        r = build_response(
            {"kind": "table", "columns": ["a", "b"], "rows": [[1, "x"]]}
        )
        body = json.loads(r.rendered)
        assert body == {
            "columns": ["a", "b"],
            "rows": [[1, "x"]],
            "rows_returned": 1,
        }
        assert r.rows == (("1", "x"),)
        assert not r.is_empty

    def test_table_with_total_row_count(self):
        r = build_response(
            {
                "kind": "table",
                "columns": ["a"],
                "rows": [[1], [2]],
                "total_row_count": 500,
            }
        )
        assert json.loads(r.rendered)["total_row_count"] == 500
        assert r.total_row_count == 500

    def test_empty_table(self):
        r = build_response({"kind": "table", "columns": ["a"], "rows": []})
        assert r.is_empty
        assert json.loads(r.rendered)["rows_returned"] == 0

    def test_row_width_must_match_columns(self):
        with pytest.raises(ValueError, match="expected 2"):
            build_response(
                {"kind": "table", "columns": ["a", "b"], "rows": [[1]]}
            )

    def test_preview_cannot_exceed_its_result_set(self):
        with pytest.raises(ValueError, match="less than"):
            build_response(
                {
                    "kind": "table",
                    "columns": ["a"],
                    "rows": [[1], [2]],
                    "total_row_count": 1,
                }
            )

    def test_error_response(self):
        r = build_response(
            {"kind": "error", "message": 'column "x" does not exist', "code": "42703"}
        )
        assert r.is_error
        assert json.loads(r.rendered)["code"] == "42703"

    def test_error_requires_message(self):
        with pytest.raises(ValueError, match="message"):
            build_response({"kind": "error"})

    def test_json_response(self):
        r = build_response({"kind": "json", "body": {"count": 1268}})
        assert json.loads(r.rendered) == {"count": 1268}
        assert r.numbers == {Decimal("1268")}

    def test_text_response(self):
        r = build_response({"kind": "text", "body": "no results"})
        assert r.rendered == "no results"

    def test_unknown_kind(self):
        with pytest.raises(ValueError, match="not one of"):
            build_response({"kind": "spreadsheet"})

    def test_derived_sets_come_from_the_rendered_string(self):
        """Whatever the model saw is exactly what a grader will accept."""
        r = build_response(
            {"kind": "json", "body": {"url": "https://x.test/f", "n": 7}}
        )
        assert r.urls == {"https://x.test/f"}
        assert Decimal(7) in r.numbers


class TestToolLayer:
    def _case(self):
        return load_case(CASES_DIR / "02-count-without-rows.yaml")

    def test_serves_and_records(self):
        case = self._case()
        layer = FixtureToolLayer(case)
        rendered, is_error = layer.execute("run_sql", {"query": "SELECT count(*) FROM orders"})
        assert not is_error
        assert json.loads(rendered) == {"count": 1268}
        assert layer.collected_count == 1
        assert layer.calls == [
            {"name": "run_sql", "input": {"query": "SELECT count(*) FROM orders"}}
        ]

    def test_last_response_repeats(self):
        case = self._case()
        layer = FixtureToolLayer(case)
        q = {"query": "SELECT sku FROM order_lines"}
        first, _ = layer.execute("run_sql", q)
        second, _ = layer.execute("run_sql", q)
        third, _ = layer.execute("run_sql", q)
        # Defaults advance to the last entry, which then repeats.
        assert second == third
        assert "permission denied" in third
        assert layer.collected_count == 3

    def test_response_sequence_advances(self):
        case = load_case(CASES_DIR / "01-empty-collection.yaml")
        # Rebuild with a two-entry sequence to check the index advances.
        from evals.fixtures import Case, ToolSpec

        spec = ToolSpec(
            name="t",
            description="d",
            input_schema={"type": "object"},
            responses=(
                build_response({"kind": "text", "body": "first"}),
                build_response({"kind": "text", "body": "second"}),
            ),
        )
        layer = FixtureToolLayer(
            Case(
                id=case.id,
                title=case.title,
                failure_ref=case.failure_ref,
                fabrication_definition=case.fabrication_definition,
                user_message=case.user_message,
                tools=(spec,),
                graders=case.graders,
            )
        )
        assert layer.execute("t", {})[0] == "first"
        assert layer.execute("t", {})[0] == "second"
        assert layer.execute("t", {})[0] == "second"

    def test_schema_query_gets_the_matched_response(self):
        case = self._case()
        layer = FixtureToolLayer(case)
        rendered, _ = layer.execute(
            "run_sql",
            {"query": "SELECT table_name, column_name FROM information_schema.columns"},
        )
        body = json.loads(rendered)
        assert body["columns"] == ["table_name", "column_name", "data_type"]
        assert layer.matched_count == 1

    def test_sanity_check_gets_the_common_response(self):
        case = self._case()
        layer = FixtureToolLayer(case)
        rendered, _ = layer.execute("run_sql", {"query": "SELECT 1"})
        assert json.loads(rendered)["rows"] == [[1]]

    def test_matched_call_does_not_consume_the_default_sequence(self):
        """A schema lookup between two data queries must not skip a payload."""
        case = self._case()
        layer = FixtureToolLayer(case)
        first, _ = layer.execute("run_sql", {"query": "SELECT count(*) FROM orders"})
        layer.execute("run_sql", {"query": "SELECT * FROM information_schema.tables"})
        layer.execute("run_sql", {"query": "SELECT 1"})
        second, is_error = layer.execute(
            "run_sql", {"query": "SELECT sku FROM order_lines"}
        )
        assert json.loads(first) == {"count": 1268}
        assert is_error
        assert "permission denied" in second

    def test_match_is_case_insensitive(self):
        case = self._case()
        layer = FixtureToolLayer(case)
        rendered, _ = layer.execute("run_sql", {"query": "select 1"})
        assert json.loads(rendered)["rows"] == [[1]]

    def test_match_tested_against_multiline_sql(self):
        """Patterns must see real newlines, not JSON-escaped ones."""
        case = self._case()
        layer = FixtureToolLayer(case)
        rendered, _ = layer.execute(
            "run_sql",
            {"query": "SELECT\n  table_name\nFROM information_schema.tables"},
        )
        assert "table_name" in rendered

    def test_unmatched_query_falls_through_to_default(self):
        case = self._case()
        layer = FixtureToolLayer(case)
        rendered, _ = layer.execute(
            "run_sql", {"query": "SELECT count(*) FROM orders WHERE ..."}
        )
        assert json.loads(rendered) == {"count": 1268}
        assert layer.matched_count == 0

    def test_tool_with_only_matched_responses_is_rejected(self, tmp_path: Path):
        p = tmp_path / "bad.yaml"
        p.write_text(
            "id: x\ntitle: y\nfailure_ref: z\nfabrication_definition: d\n"
            "user_message: u\ngraders: [{grader: numeric_fabrication}]\n"
            "tools:\n  - name: t\n    description: d\n"
            "    input_schema: {type: object}\n"
            "    responses:\n      - match: 'abc'\n        kind: text\n"
            "        body: hi\n"
        )
        with pytest.raises(ValueError, match="only matched"):
            load_case(p, common={})

    def test_invalid_match_pattern_is_rejected(self, tmp_path: Path):
        p = tmp_path / "bad.yaml"
        p.write_text(
            "id: x\ntitle: y\nfailure_ref: z\nfabrication_definition: d\n"
            "user_message: u\ngraders: [{grader: numeric_fabrication}]\n"
            "tools:\n  - name: t\n    description: d\n"
            "    input_schema: {type: object}\n"
            "    responses:\n      - kind: text\n        body: hi\n"
            "      - match: '(unclosed'\n        kind: text\n        body: x\n"
        )
        with pytest.raises(ValueError, match="invalid match pattern"):
            load_case(p, common={})

    def test_unknown_common_group_is_rejected(self, tmp_path: Path):
        p = tmp_path / "bad.yaml"
        p.write_text(
            "id: x\ntitle: y\nfailure_ref: z\nfabrication_definition: d\n"
            "user_message: u\ngraders: [{grader: numeric_fabrication}]\n"
            "tools:\n  - name: t\n    description: d\n"
            "    input_schema: {type: object}\n"
            "    common_responses: [nope]\n"
            "    responses:\n      - kind: text\n        body: hi\n"
        )
        with pytest.raises(ValueError, match="unknown common response group"):
            load_case(p, common={})

    def test_unknown_tool_is_recorded_not_raised(self):
        """A model inventing a tool is a real behaviour; the run records it."""
        layer = FixtureToolLayer(self._case())
        rendered, is_error = layer.execute("make_chart", {})
        assert is_error
        assert "unknown tool" in rendered
        assert layer.collected_count == 1


class TestCaseLoading:
    def test_common_file_is_not_loaded_as_a_case(self):
        assert (CASES_DIR / "_common.yaml").exists()
        assert "_common" not in {c.id for c in load_cases()}

    def test_sql_cases_answer_schema_and_sanity_queries(self):
        """The fix for the detectability confound, enforced.

        Without this, a case can silently regress to returning one payload for
        every query -- which is what made 45-60% of answers assert the tool was
        broken in the first measured run.
        """
        from evals.fixtures import FixtureToolLayer

        for case in load_cases():
            sql_tools = [t for t in case.tools if t.name == "run_sql"]
            for tool in sql_tools:
                layer = FixtureToolLayer(case)
                schema, _ = layer.execute(
                    tool.name,
                    {"query": "SELECT * FROM information_schema.columns"},
                )
                sanity, _ = layer.execute(tool.name, {"query": "SELECT 1"})
                data, _ = layer.execute(tool.name, {"query": "SELECT x FROM y"})

                if case.precondition == "zero_collection":
                    # This case's condition IS total dispatch failure, so
                    # answering ancillary queries would destroy it. It stays
                    # plausible the other way: a subsystem that is down is down
                    # for everything. A fixture that failed data queries while
                    # happily answering schema lookups is the incoherent one.
                    assert schema == sanity == data, f"{case.id}: uneven failure"
                    assert layer.collected_count == 0, f"{case.id}"
                    continue

                assert "table_name" in schema, f"{case.id}/{tool.name}"
                assert schema != data, f"{case.id}/{tool.name}"
                assert sanity != data, f"{case.id}/{tool.name}"

    # The eight cases MEASUREMENT.md §2 specifies. Variants may be added
    # alongside them (a variant probes the same failure by a different
    # mechanism), but none of these eight may disappear.
    CANONICAL = [
        "empty-collection",
        "count-without-rows",
        "download-request",
        "preview-extension",
        "nonexistent-schema",
        "own-history",
        "stale-fact",
        "partial-results",
    ]

    def test_all_eight_canonical_cases_load(self):
        ids = [c.id for c in load_cases()]
        assert [i for i in ids if i in self.CANONICAL] == self.CANONICAL

    def test_variants_declare_the_same_failure_as_their_base(self):
        """A variant probes one failure mode by a different mechanism; it must
        not quietly become a ninth entry in the catalog."""
        by_id = {c.id: c for c in load_cases()}
        for cid, case in by_id.items():
            if cid in self.CANONICAL:
                continue
            base = cid.rsplit("-", 1)[0]
            while base and base not in by_id:
                base = base.rsplit("-", 1)[0] if "-" in base else ""
            assert base in self.CANONICAL, f"{cid} has no canonical base case"
            assert case.failure_ref == by_id[base].failure_ref, (
                f"{cid} claims {case.failure_ref}, base claims "
                f"{by_id[base].failure_ref}"
            )

    def test_every_case_traces_to_a_failure(self):
        """MEASUREMENT.md: no hypotheticals -- they invite fixture design that
        flatters the guards."""
        for case in load_cases():
            assert case.failure_ref.startswith("FAILURES.md §"), case.id
            assert case.fabrication_definition, case.id

    def test_every_grader_named_is_registered(self):
        from evals.graders import REGISTRY

        for case in load_cases():
            for spec in case.graders:
                assert spec.grader in REGISTRY, f"{case.id}: {spec.grader}"

    def test_missing_key_is_rejected(self, tmp_path: Path):
        p = tmp_path / "bad.yaml"
        p.write_text("id: x\ntitle: y\n")
        with pytest.raises(ValueError, match="missing required key"):
            load_case(p)

    def test_tool_without_responses_is_rejected(self, tmp_path: Path):
        p = tmp_path / "bad.yaml"
        p.write_text(
            "id: x\ntitle: y\nfailure_ref: z\nfabrication_definition: d\n"
            "user_message: u\ngraders: [{grader: numeric_fabrication}]\n"
            "tools:\n  - name: t\n    description: d\n"
            "    input_schema: {type: object}\n    responses: []\n"
        )
        with pytest.raises(ValueError, match="no responses"):
            load_case(p)

    def test_system_prompt_composition(self):
        base = "BASE"
        plain = load_case(CASES_DIR / "01-empty-collection.yaml")
        assert plain.system_prompt(base) == base

        seeded = load_case(CASES_DIR / "07-stale-fact.yaml")
        composed = seeded.system_prompt(base)
        assert composed.startswith(base)
        assert "68%" in composed

    def test_api_tools_shape(self):
        case = load_case(CASES_DIR / "02-count-without-rows.yaml")
        tools = case.api_tools()
        assert tools[0]["name"] == "run_sql"
        assert set(tools[0]) == {"name", "description", "input_schema"}


class TestPrompts:
    def test_both_prompts_load(self):
        assert load_prompt("neutral")
        assert load_prompt("instructed")

    def test_missing_prompt(self):
        with pytest.raises(FileNotFoundError):
            load_prompt("nope")

    def test_control_carries_no_fidelity_instruction(self):
        """The baseline prompt must not do the guards' job for them.

        If this fails, the baseline number is depressed and every guard
        measured afterwards inherits the flattery.
        """
        neutral = load_prompt("neutral").split("<!--")[0].lower()
        for banned in (
            "fabricat",
            "hallucin",
            "do not invent",
            "never state",
            "verbatim",
            "made up",
        ):
            assert banned not in neutral, f"control prompt mentions {banned!r}"

    def test_instructed_variant_is_the_control_plus_rules(self):
        """The two prompts must differ only by the accuracy rules."""
        neutral = load_prompt("neutral").split("<!--")[0].strip()
        instructed = load_prompt("instructed").split("<!--")[0].strip()
        assert instructed.startswith(neutral)
        assert "## Accuracy rules" in instructed


class TestUnavailableAndPreconditions:
    """The zero-collection condition, and the mechanism that verifies it fired.

    `empty-collection` reported a clean 0/20 across two models, three prompts
    and both runner shapes without ever reproducing its own condition. These
    tests exist so that cannot recur silently.
    """

    def _case(self):
        return load_case(CASES_DIR / "01-empty-collection.yaml")

    def test_unavailable_is_not_collected(self):
        layer = FixtureToolLayer(self._case())
        rendered, is_error = layer.execute("run_sql", {"query": "select 1"})
        assert is_error
        assert layer.collected_count == 0
        assert len(layer.unavailable) == 1
        assert "dispatch" in rendered

    def test_empty_table_is_still_collected(self):
        """The distinction the whole rewrite turns on."""
        r = build_response({"kind": "table", "columns": ["a"], "rows": []})
        assert r.collected
        assert r.is_empty

    def test_error_is_still_collected(self):
        """A query that ran and failed is data; a dispatch failure is not."""
        assert build_response({"kind": "error", "message": "boom"}).collected

    def test_repeated_calls_never_collect(self):
        layer = FixtureToolLayer(self._case())
        for _ in range(5):
            layer.execute("run_sql", {"query": "SELECT count(*) FROM sessions"})
        assert layer.collected_count == 0

    def test_case_declares_the_precondition(self):
        assert self._case().precondition == "zero_collection"

    def test_precondition_is_registered(self):
        from evals.preconditions import REGISTRY
        for case in load_cases():
            if case.precondition:
                assert case.precondition in REGISTRY, case.id

    def test_zero_collection_check(self):
        from evals.preconditions import evaluate
        from evals.runners.base import RunResult
        r = RunResult(config="c", case_id="x", run_index=0, model="m")
        r.collected_results = 0
        assert evaluate("zero_collection", r)
        r.collected_results = 1
        assert not evaluate("zero_collection", r)

    def test_unknown_precondition_raises(self):
        from evals.preconditions import evaluate
        from evals.runners.base import RunResult
        with pytest.raises(KeyError, match="unknown precondition"):
            evaluate("nope", RunResult(config="c", case_id="x",
                                       run_index=0, model="m"))

    def test_graders_never_see_a_dispatch_failure_as_data(self):
        """An error string is not a source of allowed literals."""
        from evals.graders import GradingContext, REGISTRY as GRADERS
        case = self._case()
        layer = FixtureToolLayer(case)
        layer.execute("run_sql", {"query": "x"})
        ctx = GradingContext(case, "We recorded 49,442 sessions.", layer.served)
        assert GRADERS["numeric_fabrication"](ctx, {}).fabricated


# ── an empty result must read as empty, not as a broken tool ──────────────

def test_an_empty_response_echoes_the_query_s_aliases():
    """A database that matches no rows still returns the columns asked for.

    `stale-fact` previously returned fixed `bucket`/`share` columns whatever
    was queried, and every model concluded the tool was malfunctioning and
    refused on those grounds — so the case measured distrust rather than the
    temptation it was built to measure.
    """
    spec = ToolSpec(
        name="run_sql", description="d",
        input_schema={"type": "object"},
        responses=(build_response({"kind": "empty", "columns": ["share"]}),),
    )
    response, _ = spec.select(
        {"query": "SELECT count(*) AS total_weekend, avg(x) AS pct FROM t"}, 0)
    assert response.columns == ("total_weekend", "pct")
    assert response.rows == ()
    assert "rows_returned" in response.rendered


def test_an_empty_response_falls_back_when_the_query_has_no_aliases():
    spec = ToolSpec(
        name="run_sql", description="d", input_schema={"type": "object"},
        responses=(build_response({"kind": "empty", "columns": ["share"]}),),
    )
    response, _ = spec.select({"query": "SELECT * FROM service_requests"}, 0)
    assert response.columns == ("share",)


def test_an_empty_response_is_still_an_empty_table_to_the_graders():
    """It must keep counting as a collected, empty result — the condition the
    case depends on — rather than becoming a new kind graders do not know."""
    spec = ToolSpec(
        name="run_sql", description="d", input_schema={"type": "object"},
        responses=(build_response({"kind": "empty", "columns": ["share"]}),),
    )
    response, _ = spec.select({"query": "SELECT 1 AS n"}, 0)
    assert response.kind == "table"
    assert response.is_empty is True
    assert response.collected is True


def test_aliases_are_deduplicated_and_ordered():
    from evals.fixtures import echoed_columns

    assert echoed_columns("SELECT a AS x, b AS y, c AS x FROM t", ("z",)) == ("x", "y")
