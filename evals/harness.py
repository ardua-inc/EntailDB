"""The run loop: N executions per case per config, graded and appended to JSONL.

Three properties the harness has to have, each earned from something in
`MEASUREMENT.md`:

**Resumable.** Results append to JSONL and existing `(config, case_id, run_index)`
keys are skipped, so an interrupted run resumes instead of restarting. At N=20
across 8 cases and several configs, losing a run to a transport error would
otherwise cost the whole batch.

**Re-gradeable without re-running.** Every record carries the answer text and
the served payloads. When a grader is corrected, `evals regrade` recomputes
verdicts from the stored transcripts. Model output is not deterministic, so
re-running to fix a grader would silently change the numbers being fixed.

**Fully attributed.** Each record names the model and config that produced it.
A rate quoted without those is not a measurement.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .configs import build_runner
from .runners.baseline import DEFAULT_MAX_ROUNDS
from .fixtures import Case, FixtureToolLayer, load_cases
from .graders import GradingContext, grade, is_fabrication
from .preconditions import evaluate as evaluate_precondition
from .runners.base import RunResult


@dataclass
class RunPlan:
    config: str
    cases: list[Case]
    n: int
    model: str
    out_path: Path
    concurrency: int = 4
    max_attempts: int = 3
    max_rounds: int = DEFAULT_MAX_ROUNDS


def existing_keys(path: Path) -> set[tuple[str, str, str, int]]:
    """Keys already present in an output file, so a run can resume.

    The model is part of the key. Without it, running a second model into the
    same file skips every run as "already done" and silently produces nothing --
    the same omission that let `summarise` merge two models into one rate.
    """
    if not path.exists():
        return set()
    keys: set[tuple[str, str, str, int]] = set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        keys.add((d["config"], d.get("model", ""), d["case_id"], d["run_index"]))
    return keys


def load_results(path: Path) -> list[RunResult]:
    out: list[RunResult] = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(RunResult.from_dict(json.loads(line)))
    return out


def grade_result(case: Case, result: RunResult, served: list[Any]) -> RunResult:
    """Attach grader verdicts to a completed run.

    A run that errored is left ungraded rather than scored as clean. Counting a
    transport failure as "no fabrication" would quietly bias every rate
    downward in exactly the direction this project would like them to go.
    """
    if case.precondition:
        result.precondition_met = evaluate_precondition(case.precondition, result)
    if result.error:
        result.fabricated = None
        result.grader_results = []
        return result
    ctx = GradingContext(case=case, answer_text=result.answer_text, served=served)
    results = grade(ctx)
    result.grader_results = [r.to_dict() for r in results]
    result.fabricated = is_fabrication(results)
    return result


def _execute_one(
    plan: RunPlan,
    client: Any,
    case: Case,
    run_index: int,
) -> RunResult:
    last: RunResult | None = None
    for attempt in range(plan.max_attempts):
        runner = build_runner(plan.config, client, plan.model, plan.max_rounds)
        tools = FixtureToolLayer(case)
        result = runner.run(case, tools, run_index)
        last = grade_result(case, result, tools.served)
        if not result.error:
            return last
        # Retry transport failures only; a run that completed and produced a
        # bad answer is a result, not an error.
        if attempt == plan.max_attempts - 1:
            break
    assert last is not None
    return last


def run_plan(
    plan: RunPlan,
    client: Any,
    on_result: Callable[[RunResult], None] | None = None,
) -> list[RunResult]:
    """Execute a plan, appending each result to `plan.out_path` as it lands."""
    plan.out_path.parent.mkdir(parents=True, exist_ok=True)
    done = existing_keys(plan.out_path)

    pending: list[tuple[Case, int]] = [
        (case, i)
        for case in plan.cases
        for i in range(plan.n)
        if (plan.config, plan.model, case.id, i) not in done
    ]
    if not pending:
        return []

    write_lock = threading.Lock()
    collected: list[RunResult] = []

    with plan.out_path.open("a") as handle:
        with ThreadPoolExecutor(max_workers=plan.concurrency) as pool:
            futures = {
                pool.submit(_execute_one, plan, client, case, i): (case, i)
                for case, i in pending
            }
            for future in as_completed(futures):
                result = future.result()
                with write_lock:
                    handle.write(json.dumps(result.to_dict()) + "\n")
                    handle.flush()
                    collected.append(result)
                if on_result:
                    on_result(result)
    return collected


def regrade(path: Path, cases: Iterable[Case] | None = None) -> list[RunResult]:
    """Recompute verdicts from stored transcripts and rewrite the file.

    Uses the served payloads recorded in each run, reconstructed as text
    responses. Table-structure graders (`row_provenance`, `table_rows_exceed`
    defaults, `completeness_disclosure`) need the parsed structure, so those are
    rebuilt from the case declaration by matching rendered strings — a stored
    payload is always one the case declared.
    """
    from .fixtures import build_response  # local import: avoids a cycle at load

    by_id = {c.id: c for c in (cases or load_cases())}
    declared: dict[str, Any] = {}
    for case in by_id.values():
        for tool in case.tools:
            for response in tool.all_responses:
                declared[response.rendered] = response

    results = load_results(path)
    for result in results:
        case = by_id.get(result.case_id)
        if case is None:
            continue
        served = [
            declared.get(rendered)
            or build_response({"kind": "text", "body": rendered})
            for rendered in result.served_rendered
        ]
        grade_result(case, result, served)

    with path.open("w") as handle:
        for result in results:
            handle.write(json.dumps(result.to_dict()) + "\n")
    return results
