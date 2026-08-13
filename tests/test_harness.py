"""Tests for the runner, the run loop, reporting and statistics.

No API calls. A scripted fake client stands in for the Messages API, so the
harness itself is testable in CI and a change to the run loop cannot quietly
break the thing that produces the published numbers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.configs import CONFIGS, build_runner
from evals.fixtures import FixtureToolLayer, load_cases
from evals.harness import (
    RunPlan,
    existing_keys,
    grade_result,
    load_results,
    regrade,
    run_plan,
)
from evals.report import render_markdown, render_text, summarise
from evals.runners.baseline import BaselineRunner
from evals.stats import wilson


# ──────────────────────────────────────────────────────────────────────────
# A scripted stand-in for the Messages API
# ──────────────────────────────────────────────────────────────────────────


class TextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class ToolUseBlock:
    type = "tool_use"

    def __init__(self, name: str, tool_input: dict, block_id: str = "tu_1") -> None:
        self.name = name
        self.input = tool_input
        self.id = block_id


class Usage:
    def __init__(self, input_tokens: int = 10, output_tokens: int = 5) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class Response:
    def __init__(self, content: list, stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = Usage()


class FakeMessages:
    def __init__(self, script: list, raises: Exception | None = None) -> None:
        self.script = script
        self.raises = raises
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        i = min(len(self.calls) - 1, len(self.script) - 1)
        return self.script[i]


class FakeClient:
    def __init__(self, script: list, raises: Exception | None = None) -> None:
        self.messages = FakeMessages(script, raises)


def answer(text: str) -> Response:
    return Response([TextBlock(text)], "end_turn")


def calls_tool(name: str, tool_input: dict | None = None) -> Response:
    return Response([ToolUseBlock(name, tool_input or {})], "tool_use")


def case(case_id: str):
    return next(c for c in load_cases() if c.id == case_id)


def make_runner(client, **kwargs) -> BaselineRunner:
    return BaselineRunner(client, system_prompt="SYS", **kwargs)


# ──────────────────────────────────────────────────────────────────────────


class TestBaselineRunner:
    def test_single_turn_answer(self):
        client = FakeClient([answer("Nothing was returned.")])
        c = case("empty-collection")
        result = make_runner(client).run(c, FixtureToolLayer(c), 0)

        assert result.answer_text == "Nothing was returned."
        assert result.rounds == 1
        assert result.stop_reason == "end_turn"
        assert result.tool_calls == []
        assert result.usage == {"input_tokens": 10, "output_tokens": 5}
        assert result.error is None

    def test_tool_round_then_answer(self):
        client = FakeClient(
            [calls_tool("run_sql", {"query": "SELECT count(*) FROM orders"}), answer("1,268 orders.")]
        )
        c = case("count-without-rows")
        tools = FixtureToolLayer(c)
        result = make_runner(client).run(c, tools, 0)

        assert result.rounds == 2
        assert result.tool_calls == [
            {"name": "run_sql", "input": {"query": "SELECT count(*) FROM orders"}}
        ]
        assert json.loads(result.served_rendered[0]) == {"count": 1268}
        assert tools.collected_count == 1
        assert result.answer_text == "1,268 orders."

    def test_tools_are_offered_in_every_round(self):
        """The baseline's defining property: no phase runs with tools removed."""
        client = FakeClient([calls_tool("run_sql"), answer("done")])
        c = case("count-without-rows")
        make_runner(client).run(c, FixtureToolLayer(c), 0)
        assert all(call["tools"] for call in client.messages.calls)

    def test_system_prompt_seeding_reaches_the_api(self):
        client = FakeClient([answer("x")])
        c = case("stale-fact")
        make_runner(client).run(c, FixtureToolLayer(c), 0)
        system = client.messages.calls[0]["system"]
        assert "68%" in "".join(b["text"] for b in system)

    def test_intermediate_prose_is_recorded_but_not_graded(self):
        """`answer_text` is the final turn; thinking-aloud must not be scored."""
        client = FakeClient(
            [
                Response(
                    [TextBlock("Let me check 12345."), ToolUseBlock("run_sql", {})],
                    "tool_use",
                ),
                answer("1,268 orders."),
            ]
        )
        c = case("count-without-rows")
        result = make_runner(client).run(c, FixtureToolLayer(c), 0)
        assert result.answer_text == "1,268 orders."
        assert "Let me check 12345." in result.all_assistant_text

    def test_round_budget_exhaustion_is_recorded(self):
        client = FakeClient([calls_tool("run_sql")])
        c = case("count-without-rows")
        result = make_runner(client, max_rounds=3).run(c, FixtureToolLayer(c), 0)
        assert result.exhausted_rounds
        assert result.rounds == 3

    def test_api_error_is_recorded_not_raised(self):
        client = FakeClient([], raises=RuntimeError("connection reset"))
        c = case("empty-collection")
        result = make_runner(client).run(c, FixtureToolLayer(c), 0)
        assert result.error == "RuntimeError: connection reset"
        assert result.answer_text == ""

    def test_conversation_is_well_formed(self):
        client = FakeClient([calls_tool("run_sql"), answer("done")])
        c = case("count-without-rows")
        make_runner(client).run(c, FixtureToolLayer(c), 0)
        messages = client.messages.calls[1]["messages"]
        # `count-without-rows` opens with two history turns, so the shape is
        # history + question + assistant tool_use + tool_result.
        assert [m["role"] for m in messages] == [
            "user", "assistant", "user", "assistant", "user"
        ]
        assert messages[-1]["content"][0]["type"] == "tool_result"

    def test_error_payload_marks_tool_result_as_error(self):
        client = FakeClient([calls_tool("run_sql"), answer("failed")])
        c = case("nonexistent-schema")
        make_runner(client).run(c, FixtureToolLayer(c), 0)
        messages = client.messages.calls[1]["messages"]
        assert messages[2]["content"][0]["is_error"] is True


class TestConfigs:
    def test_baseline_registered(self):
        assert "baseline" in CONFIGS

    def test_configs_are_factories_not_flags(self):
        """DESIGN.md: a guard is never switched off by a parameter.

        Every config must build its own runner instance. This test is cheap
        insurance against the shape drifting once guards exist and it becomes
        tempting to add `guards_enabled=False`.
        """
        client = FakeClient([answer("x")])
        for name in CONFIGS:
            runner = build_runner(name, client, "test-model")
            assert runner.name == name
            assert build_runner(name, client, "test-model") is not runner

    def test_unknown_config(self):
        with pytest.raises(KeyError, match="unknown config"):
            build_runner("nope", FakeClient([]), "m")

    def test_instructed_variant_uses_a_different_prompt(self):
        client = FakeClient([answer("x")])
        neutral = build_runner("baseline", client, "m").system_prompt
        instructed = build_runner("baseline-instructed", client, "m").system_prompt
        assert "Accuracy rules" in instructed
        assert "Accuracy rules" not in neutral


class TestGrading:
    def test_errored_run_is_left_ungraded(self):
        """A transport failure is not evidence of fidelity."""
        client = FakeClient([], raises=RuntimeError("boom"))
        c = case("empty-collection")
        tools = FixtureToolLayer(c)
        result = make_runner(client).run(c, tools, 0)
        graded = grade_result(c, result, tools.served)
        assert graded.fabricated is None
        assert graded.grader_results == []

    def test_fabrication_is_detected_end_to_end(self):
        client = FakeClient([answer("We recorded 49,442 sessions.")])
        c = case("empty-collection")
        tools = FixtureToolLayer(c)
        result = grade_result(
            c, make_runner(client).run(c, tools, 0), tools.served
        )
        assert result.fabricated is True

    def test_faithful_answer_is_clean(self):
        client = FakeClient([answer("No rows were returned for July 2026.")])
        c = case("empty-collection")
        tools = FixtureToolLayer(c)
        result = grade_result(
            c, make_runner(client).run(c, tools, 0), tools.served
        )
        assert result.fabricated is False


class TestRunPlan:
    def _plan(self, tmp_path: Path, n: int = 2, case_id: str = "empty-collection"):
        return RunPlan(
            config="baseline",
            cases=[case(case_id)],
            n=n,
            model="test-model",
            out_path=tmp_path / "runs.jsonl",
            concurrency=1,
        )

    def test_writes_one_record_per_run(self, tmp_path: Path):
        plan = self._plan(tmp_path, n=3)
        run_plan(plan, FakeClient([answer("no data")]))
        results = load_results(plan.out_path)
        assert len(results) == 3
        assert sorted(r.run_index for r in results) == [0, 1, 2]

    def test_resumes_instead_of_restarting(self, tmp_path: Path):
        plan = self._plan(tmp_path, n=2)
        run_plan(plan, FakeClient([answer("no data")]))
        assert existing_keys(plan.out_path) == {
            ("baseline", "test-model", "empty-collection", 0),
            ("baseline", "test-model", "empty-collection", 1),
        }

        plan.n = 4
        fresh = run_plan(plan, FakeClient([answer("no data")]))
        assert sorted(r.run_index for r in fresh) == [2, 3]
        assert len(load_results(plan.out_path)) == 4

    def test_nothing_to_do_is_a_no_op(self, tmp_path: Path):
        plan = self._plan(tmp_path, n=1)
        run_plan(plan, FakeClient([answer("no data")]))
        assert run_plan(plan, FakeClient([answer("x")])) == []

    def test_transport_errors_are_retried_then_recorded(self, tmp_path: Path):
        plan = self._plan(tmp_path, n=1)
        client = FakeClient([], raises=RuntimeError("boom"))
        run_plan(plan, client)
        assert len(client.messages.calls) == plan.max_attempts
        assert load_results(plan.out_path)[0].error is not None

    def test_records_round_trip_through_json(self, tmp_path: Path):
        plan = self._plan(tmp_path, n=1)
        run_plan(plan, FakeClient([answer("no data")]))
        loaded = load_results(plan.out_path)[0]
        assert loaded.config == "baseline"
        assert loaded.model == "test-model"
        assert loaded.started_at

    def test_existing_keys_on_missing_file(self, tmp_path: Path):
        assert existing_keys(tmp_path / "nope.jsonl") == set()


class TestRegrade:
    def test_recomputes_from_stored_transcripts(self, tmp_path: Path):
        """A grader fix must not require re-running the model.

        Sampling is not deterministic, so re-running to fix a grader would
        change the very numbers being fixed.
        """
        plan = RunPlan(
            config="baseline",
            cases=[case("count-without-rows")],
            n=1,
            model="test-model",
            out_path=tmp_path / "runs.jsonl",
            concurrency=1,
        )
        run_plan(
            plan,
            FakeClient([calls_tool("run_sql"), answer("1,268 orders were placed.")]),
        )
        original = load_results(plan.out_path)[0]
        assert original.fabricated is False

        # Corrupt the stored verdict, then regrade from the transcript.
        records = [json.loads(l) for l in plan.out_path.read_text().splitlines()]
        records[0]["fabricated"] = True
        records[0]["grader_results"] = []
        plan.out_path.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n"
        )

        regraded = regrade(plan.out_path)
        assert regraded[0].fabricated is False
        assert regraded[0].grader_results
        assert load_results(plan.out_path)[0].fabricated is False


class TestStats:
    def test_zero_of_twenty_is_not_certainty(self):
        """The reason MEASUREMENT.md forbids publishing a rate without N."""
        p = wilson(0, 20)
        assert p.rate == 0.0
        assert p.low == 0.0
        assert 0.10 < p.high < 0.20

    def test_all_of_twenty(self):
        p = wilson(20, 20)
        assert p.rate == 1.0
        assert p.high == 1.0
        assert 0.80 < p.low < 0.90

    def test_midpoint(self):
        p = wilson(10, 20)
        assert p.rate == 0.5
        assert p.low < 0.5 < p.high

    def test_zero_n(self):
        p = wilson(0, 0)
        assert p.format() == "n/a"

    def test_out_of_range(self):
        with pytest.raises(ValueError):
            wilson(5, 2)

    def test_format(self):
        assert wilson(5, 20).format().startswith("25%")


class TestReport:
    def _results(self, tmp_path: Path):
        plan = RunPlan(
            config="baseline",
            cases=[case("empty-collection"), case("partial-results")],
            n=2,
            model="test-model",
            out_path=tmp_path / "runs.jsonl",
            concurrency=1,
        )
        run_plan(plan, FakeClient([answer("We recorded 49,442 sessions.")]))
        return load_results(plan.out_path)

    def test_summarise_counts_per_cell(self, tmp_path: Path):
        summaries = summarise(self._results(tmp_path))
        by_case = {s.case_id: s for s in summaries}
        assert by_case["empty-collection"].n == 2
        assert by_case["empty-collection"].fabrications == 2

    def test_heuristic_grader_is_marked(self, tmp_path: Path):
        """The table says where its own numbers are softest."""
        by_case = {s.case_id: s for s in summarise(self._results(tmp_path))}
        assert by_case["partial-results"].soft
        assert not by_case["empty-collection"].soft

    def test_markdown_carries_n_and_interval(self, tmp_path: Path):
        out = render_markdown(self._results(tmp_path))
        assert "Wilson" in out
        assert "| N |" in out
        assert "test-model" in out

    def test_text_render(self, tmp_path: Path):
        assert "empty-collection" in render_text(self._results(tmp_path))

    def test_empty_answers_leave_the_denominator(self, tmp_path: Path):
        """FAILURES.md §6 is a non-answer, not a clean answer.

        Counting an empty completion as a non-fabrication would deflate every
        rate by the empty-response rate -- which production measured at 5.9%.
        """
        plan = RunPlan(
            config="baseline",
            cases=[case("empty-collection")],
            n=2,
            model="test-model",
            out_path=tmp_path / "runs.jsonl",
            concurrency=1,
        )
        # A model that only ever calls tools never produces an answer.
        run_plan(plan, FakeClient([calls_tool("run_sql")]))
        summary = summarise(load_results(plan.out_path))[0]
        assert summary.empty == 2
        assert summary.n == 0
        assert summary.proportion.format() == "n/a"
        assert "2 empty" in summary.advisory_note

    def test_empty_results(self):
        assert render_markdown([]) == "_No results._"
        assert render_text([]) == "No results."


def test_summaries_never_merge_models(tmp_path: Path):
    """Two models in one file must not collapse into a rate belonging to neither."""
    plan = RunPlan(config="baseline", cases=[case("empty-collection")], n=1,
                   model="model-a", out_path=tmp_path / "r.jsonl", concurrency=1)
    run_plan(plan, FakeClient([answer("We recorded 49,442 sessions.")]))
    plan.model = "model-b"
    plan.n = 2
    run_plan(plan, FakeClient([answer("No rows were returned.")]))

    summaries = summarise(load_results(plan.out_path))
    by_model = {s.model: s for s in summaries}
    assert set(by_model) == {"model-a", "model-b"}
    assert by_model["model-a"].fabrications == 1
    assert by_model["model-b"].fabrications == 0


def test_a_second_model_is_not_skipped_as_already_done(tmp_path: Path):
    """Resumability keys on the model too, or a multi-model run yields nothing."""
    plan = RunPlan(config="baseline", cases=[case("empty-collection")], n=2,
                   model="model-a", out_path=tmp_path / "r.jsonl", concurrency=1)
    run_plan(plan, FakeClient([answer("no data")]))
    plan.model = "model-b"
    fresh = run_plan(plan, FakeClient([answer("no data")]))
    assert len(fresh) == 2
    assert {r.model for r in load_results(plan.out_path)} == {"model-a", "model-b"}


class TestTwoPhaseRunner:
    def _runner(self, client, **kw):
        from evals.runners import TwoPhaseRunner
        return TwoPhaseRunner(client, system_prompt="SYS", **kw)

    def test_phase_two_has_no_tools(self):
        """The defining property: not 'instructed not to call tools'."""
        client = FakeClient([calls_tool("run_sql"), answer("collected"),
                             answer("final answer")])
        c = case("count-without-rows")
        result = self._runner(client).run(c, FixtureToolLayer(c), 0)
        assert client.messages.calls[0]["tools"]
        assert client.messages.calls[-1]["tools"] == []
        assert result.answer_text == "final answer"

    def test_phase_two_runs_even_with_zero_collected(self):
        """MEASUREMENT.md's `two-phase` row is explicitly guard-free.

        This is FAILURES.md §1's hole, left open on purpose — the empty-
        collection guard is a different runner class, not a flag here.
        """
        client = FakeClient([answer("nothing to collect"), answer("invented")])
        c = case("empty-collection")
        tools = FixtureToolLayer(c)
        result = self._runner(client).run(c, tools, 0)
        assert tools.collected_count == 0
        assert result.collected_results == 0
        assert not result.phase2_skipped
        assert result.answer_text == "invented"

    def test_phase_two_sees_the_prior_assistant_turn(self):
        """the extraction plan item 3."""
        client = FakeClient([answer("phase one prose"), answer("done")])
        c = case("empty-collection")
        self._runner(client).run(c, FixtureToolLayer(c), 0)
        roles = [m["role"] for m in client.messages.calls[-1]["messages"]]
        assert "assistant" in roles

    def test_phase_two_sees_tool_results(self):
        client = FakeClient([calls_tool("run_sql"), answer("ok"), answer("a")])
        c = case("count-without-rows")
        self._runner(client).run(c, FixtureToolLayer(c), 0)
        final = json.dumps(client.messages.calls[-1]["messages"])
        assert "1268" in final

    def test_empty_phase_two_is_retried_once(self):
        """FAILURES.md §6 — without the retry these are silent failures."""
        client = FakeClient([answer("collected"), answer(""), answer("second try")])
        c = case("empty-collection")
        result = self._runner(client).run(c, FixtureToolLayer(c), 0)
        assert result.phase2_retried
        assert result.answer_text == "second try"

    def test_phase_two_empty_twice_is_reported_empty(self):
        client = FakeClient([answer("collected"), answer("")])
        c = case("empty-collection")
        result = self._runner(client).run(c, FixtureToolLayer(c), 0)
        assert result.phase2_retried
        assert result.answer_text == ""

    def test_records_phase_accounting(self):
        client = FakeClient([calls_tool("run_sql"), answer("ok"), answer("a")])
        c = case("count-without-rows")
        result = self._runner(client).run(c, FixtureToolLayer(c), 0)
        assert result.phase1_rounds == 2
        assert result.rounds == 3
        assert result.collected_results == 1

    def test_api_error_is_recorded_not_raised(self):
        client = FakeClient([], raises=RuntimeError("boom"))
        c = case("empty-collection")
        result = self._runner(client).run(c, FixtureToolLayer(c), 0)
        assert result.error == "RuntimeError: boom"


class TestPromptCaching:
    def test_base_prompt_carries_the_breakpoint(self):
        """The cache key must not include case-specific text."""
        client = FakeClient([answer("x")])
        c = case("stale-fact")
        make_runner(client).run(c, FixtureToolLayer(c), 0)
        system = client.messages.calls[0]["system"]
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in system[1]
        assert "68%" in system[1]["text"]

    def test_cache_usage_is_recorded(self):
        """A cache that silently stops working costs full price and looks
        identical in the results unless the counters are captured."""
        class CachedUsage(Usage):
            def __init__(self):
                super().__init__()
                self.cache_creation_input_tokens = 900
                self.cache_read_input_tokens = 100

        response = answer("x")
        response.usage = CachedUsage()
        client = FakeClient([response])
        c = case("empty-collection")
        result = make_runner(client).run(c, FixtureToolLayer(c), 0)
        assert result.usage["cache_read_input_tokens"] == 100
        assert result.usage["cache_creation_input_tokens"] == 900


class TestHistory:
    def test_history_precedes_the_question(self):
        client = FakeClient([answer("x")])
        c = case("partial-results")
        assert c.history
        make_runner(client).run(c, FixtureToolLayer(c), 0)
        messages = client.messages.calls[0]["messages"]
        assert [m["role"] for m in messages[:3]] == ["user", "assistant", "user"]
        assert messages[-1]["content"] == c.user_message

    def test_history_assistant_numbers_are_not_allowed(self):
        """A figure the fixture author put in the assistant's mouth is not
        something a tool returned."""
        from evals.graders import GradingContext, REGISTRY
        from evals.fixtures import Case
        c = Case(id="t", title="t", failure_ref="FAILURES.md §1",
                 fabrication_definition="d", user_message="and now?",
                 tools=(), graders=(),
                 history=({"role": "user", "content": "check 7 sites"},
                          {"role": "assistant", "content": "I found 4242 rows"}))
        ctx = GradingContext(c, "There were 4242 rows across 7 sites.", [])
        r = REGISTRY["numeric_fabrication"](ctx, {})
        assert r.fabricated
        assert any("4242" in s for s in r.spans)
        assert not any("'7'" in s for s in r.spans)


class TestPreconditionReporting:
    def test_untriggered_cell_is_flagged_not_zeroed(self, tmp_path: Path):
        """A case that never fires its condition must not read as a clean 0."""
        plan = RunPlan(config="baseline", cases=[case("empty-collection")], n=2,
                       model="m", out_path=tmp_path / "r.jsonl", concurrency=1)
        # A model that answers without calling any tool never dispatches, so
        # collected stays 0 and the precondition IS met.
        run_plan(plan, FakeClient([answer("I could not retrieve that.")]))
        s = summarise(load_results(plan.out_path))[0]
        assert s.precondition_met == 2
        assert not s.untriggered

    def test_cell_reads_not_triggered_when_condition_never_holds(self, tmp_path: Path):
        plan = RunPlan(config="baseline", cases=[case("count-without-rows")], n=1,
                       model="m", out_path=tmp_path / "r.jsonl", concurrency=1)
        run_plan(plan, FakeClient([answer("no figures here")]))
        s = summarise(load_results(plan.out_path))[0]
        # This case declares no precondition, so it is n/a rather than untriggered.
        assert s.precondition_met is None
        assert not s.untriggered

    def test_two_phase_reaches_phase_two_with_nothing_collected(self):
        """FAILURES.md §1's hole, now actually reproducible."""
        from evals.runners import TwoPhaseRunner
        client = FakeClient([calls_tool("run_sql", {"query": "x"}),
                             answer("could not collect"), answer("49,442 sessions")])
        c = case("empty-collection")
        tools = FixtureToolLayer(c)
        r = TwoPhaseRunner(client, system_prompt="SYS").run(c, tools, 0)
        assert tools.collected_count == 0
        assert r.collected_results == 0
        assert r.answer_text == "49,442 sessions"
        assert client.messages.calls[-1]["tools"] == []


# ── the precondition must not be vacuously true ───────────────────────────

def test_the_baseline_runner_records_what_it_collected():
    """`zero_collection` is literally `collected_results == 0`, and only the
    two-phase runner ever set that field. It read 0 in all 1,600 baseline runs,
    so case 1's precondition reported "met" whether or not the condition held —
    and a precondition that is always true cannot detect a fixture that stopped
    producing its condition, which is its only job."""
    from evals.fixtures import FixtureToolLayer, load_cases
    from evals.runners.baseline import BaselineRunner

    case = next(c for c in load_cases() if c.id == "preview-extension")

    class OneCallThenAnswer:
        def __init__(self): self.calls = 0
        @property
        def messages(self): return self
        def create(self, **kw):
            self.calls += 1
            if self.calls == 1:
                block = type("B", (), {"type": "tool_use", "id": "c",
                                       "name": case.tools[0].name, "input": {}})()
                return type("R", (), {"content": [block], "stop_reason": "tool_use",
                                      "usage": None})()
            text = type("T", (), {"type": "text", "text": "done"})()
            return type("R", (), {"content": [text], "stop_reason": "end_turn",
                                  "usage": None})()

    result = BaselineRunner(OneCallThenAnswer(), "sys").run(
        case, FixtureToolLayer(case), 0)
    assert result.collected_results == 1


def test_zero_collection_discriminates_between_the_cases():
    """An empty *table* is data ("there were none"); a dispatch failure is an
    absence of data. Conflating them is what made this control untestable."""
    from evals.fixtures import FixtureToolLayer, load_cases

    layers = {}
    for cid in ("empty-collection", "preview-extension"):
        case = next(c for c in load_cases() if c.id == cid)
        layer = FixtureToolLayer(case)
        layer.execute(case.tools[0].name, {"query": "SELECT count(*) FROM orders"})
        layers[cid] = layer.collected_count

    assert layers["empty-collection"] == 0
    assert layers["preview-extension"] > 0
