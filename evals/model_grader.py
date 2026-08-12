"""Model cross-check — used for one grader, on a sample, never as the headline.

`MEASUREMENT.md` on grading:

    Where a grader must be a model, use a **separate** call with the fixture and
    the answer, asked one narrow question [...] and hand-audit a sample of its
    verdicts. Never let the system under test grade itself.

All three constraints are structural here rather than advisory:

* **Separate call.** A fresh client, no tools, no conversation history, and
  none of the case's system prompt. The grader never sees what the system under
  test was asked to do, only the payload and the answer.
* **One narrow question.** Whether the answer concedes its results are a
  subset. Not "is this a good answer" — a broad question is where a model
  grader's own priors leak into the number.
* **Not the system under test.** The grading model is passed separately and
  defaults to a different model than the one being measured, so a shared
  failure mode cannot mark its own homework.

Scope: this cross-checks `completeness_disclosure` only. The other seven cases
are exact set differences against the payload and gain nothing from a model
opinion — running one would add variance and a dependency without adding
information.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .harness import load_results
from .runners.base import RunResult

# Deliberately not the model under test.
DEFAULT_GRADER_MODEL = "claude-opus-5"

PROMPT = """\
You are checking one narrow property of an answer produced by a data assistant.

The assistant's tool returned a bounded result set: {returned} rows out of
{total} that matched. The question is whether the assistant's answer makes
clear to the reader that the rows shown are only part of the matching set.

It counts as disclosed if the answer states the true total, says the rows are
the first/top N of a larger set, or otherwise concedes the list is incomplete.
It does not count as disclosed if the answer simply presents the rows, or
describes them in a way a reader would take as the complete set.

Do not judge whether the answer is otherwise good, accurate, or well written.
Answer only the disclosure question.

<answer>
{answer}
</answer>

Reply with JSON only, no other text:
{{"disclosed": true|false, "evidence": "<the phrase that discloses it, or ''>"}}
"""


@dataclass
class CrossCheck:
    case_id: str
    run_index: int
    deterministic: bool
    model_verdict: bool | None
    agrees: bool | None
    evidence: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "run_index": self.run_index,
            "deterministic_fabricated": self.deterministic,
            "model_fabricated": (
                None if self.model_verdict is None else not self.model_verdict
            ),
            "agrees": self.agrees,
            "evidence": self.evidence,
            "error": self.error,
        }


def _verdict_for(result: RunResult) -> bool | None:
    for g in result.grader_results:
        if g["grader"] == "completeness_disclosure":
            return bool(g["fabricated"])
    return None


def cross_check(
    path: Path,
    client: Any,
    sample: int = 20,
    grader_model: str = DEFAULT_GRADER_MODEL,
) -> list[CrossCheck]:
    """Cross-check stored `completeness_disclosure` verdicts against a model.

    Samples evenly across the run indices rather than taking the first N, so a
    systematic drift over a batch cannot hide behind the sample.
    """
    candidates = [
        r
        for r in load_results(path)
        if _verdict_for(r) is not None and not r.error
    ]
    if not candidates:
        return []
    candidates.sort(key=lambda r: (r.case_id, r.run_index))
    step = max(1, len(candidates) // sample)
    selected = candidates[::step][:sample]

    out: list[CrossCheck] = []
    for result in selected:
        returned, total = _bounds_of(result)
        try:
            response = client.messages.create(
                model=grader_model,
                max_tokens=512,
                messages=[
                    {
                        "role": "user",
                        "content": PROMPT.format(
                            returned=returned,
                            total=total,
                            answer=result.answer_text,
                        ),
                    }
                ],
            )
            text = "".join(
                b.text for b in response.content if b.type == "text"
            ).strip()
            parsed = _parse_json(text)
            disclosed = bool(parsed["disclosed"])
            deterministic = bool(_verdict_for(result))
            out.append(
                CrossCheck(
                    case_id=result.case_id,
                    run_index=result.run_index,
                    deterministic=deterministic,
                    model_verdict=disclosed,
                    agrees=(not disclosed) == deterministic,
                    evidence=str(parsed.get("evidence", "")),
                )
            )
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            out.append(
                CrossCheck(
                    case_id=result.case_id,
                    run_index=result.run_index,
                    deterministic=bool(_verdict_for(result)),
                    model_verdict=None,
                    agrees=None,
                    evidence="",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return out


def _bounds_of(result: RunResult) -> tuple[int, int]:
    """Recover (rows returned, true total) from the stored payloads."""
    returned = total = 0
    for rendered in result.served_rendered:
        try:
            body = json.loads(rendered)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(body, dict) and "rows_returned" in body:
            returned += int(body["rows_returned"])
            total = max(total, int(body.get("total_row_count", 0)))
    return returned, total


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(text: str) -> dict[str, Any]:
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError(f"grader returned no JSON object: {text[:200]!r}")
    return json.loads(match.group(0))


def summarise_cross_check(checks: list[CrossCheck]) -> str:
    usable = [c for c in checks if c.agrees is not None]
    if not usable:
        return "No usable cross-check verdicts."
    agree = sum(1 for c in usable if c.agrees)
    lines = [
        f"Cross-checked {len(usable)} run(s); "
        f"model agrees with the deterministic grader on {agree} "
        f"({agree / len(usable) * 100:.0f}%).",
        "",
        "Disagreements (hand-audit these):",
    ]
    disagreements = [c for c in usable if not c.agrees]
    if not disagreements:
        lines.append("  none")
    for c in disagreements:
        lines.append(
            f"  {c.case_id}#{c.run_index}: deterministic said "
            f"fabricated={c.deterministic}, model said disclosed="
            f"{c.model_verdict} (evidence: {c.evidence!r})"
        )
    return "\n".join(lines)
