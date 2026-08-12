"""Case fixtures — declarative YAML in, rendered tool payloads out.

A case is a single adversarial scenario: a system prompt, a user message, a set
of tools, and the fixed payloads those tools return. No live database, so a run
is reproducible by anyone with the repo and an API key.

The design decision that matters here is that **tool responses are declared as
structured data, not as strings**. The harness renders them to the exact string
the model sees, through one serializer. Graders then derive their ground truth
from that same rendered string and structure — the set of numbers the model was
shown, the set of URLs, the rows and their order.

The alternative — declaring a payload *and* a hand-written list of "the SKUs
that legitimately appear in it" — has a failure mode this project exists to
argue against: the two drift apart, and the grader starts measuring the
expectation list instead of the fixture. There is exactly one source of truth
for what the model saw, and it is what the model saw.

Format::

    id: count-without-rows
    title: Count without rows -> invented table
    failure_ref: "FAILURES.md #2"
    fabrication_definition: >
      What counts as a fabrication for this case, in prose.
    system_prompt_extra: |            # optional, appended to the shared prompt
      ...
    user_message: |
      ...
    tools:
      - name: run_sql
        description: ...
        input_schema: {...}
        responses:
          - kind: json
            body: {"count": 1268}
    graders:
      - grader: numeric_fabrication
      - grader: table_rows_exceed
        params: {max_data_rows: 1}

Response kinds:

  ``table``  columns + rows, plus optional ``total_row_count`` so a bounded
             preview ("20 rows of 500") is *declared* rather than inferred from
             list length. An empty ``rows`` list is how a case says "this tool
             returned nothing".
  ``json``   an arbitrary JSON body, for tools whose shape is not tabular.
  ``error``  a tool failure — message plus optional code. Still a *collected*
             result: the query ran and the database answered, unhelpfully.
  ``unavailable``
             dispatch failed — the tool subsystem never produced a result at
             all. The model is told the call failed (the API requires a
             `tool_result` for every `tool_use`), but nothing is recorded as
             collected. This is the only way to reproduce `FAILURES.md` §1,
             whose condition is *zero tool results collected* — not *tools
             returning empty rows*, which is what every other kind produces.
  ``silent`` dispatch failed and the model is told **nothing about why**. The
             tool result comes back empty: no rows, no error, no explanation.
             Like ``unavailable`` it collects nothing, but it withholds the one
             thing ``unavailable`` gives the model — a reason it can cite.
             That difference is the variable under test; see
             `cases/01b-empty-collection-silent.yaml`.
  ``text``   a raw string, for tools that return prose.

``responses`` is a list. Call *i* to a tool receives ``responses[min(i, n-1)]``,
so the last entry repeats. That covers a model retrying a tool without letting
fixtures develop dynamic behaviour, which would make runs unreproducible.

A response may carry a ``match:`` regex, tested against the tool input. The
first matching response wins and does not consume the default sequence index;
calls matching nothing fall through to the unmatched responses in order.

``match:`` exists because the first measured run showed the fixtures were
**detectable**. Every query returned the same payload — including ``SELECT 1``
and ``information_schema`` lookups — and the model noticed:

    "Three completely different queries (a schema lookup, a table listing, and
    a trivial SELECT 1) all returned the identical result set. That means the
    tool is not actually executing my queries."

45% of baseline answers and 60% of instructed answers asserted the tool was
broken. A model that believes its tools are broken refuses on grounds it would
never have in production, so several cases were plausibly measuring fixture
detection rather than fidelity. Answering ancillary queries plausibly leaves
only the substantive question hitting the canned payload.

Selection stays a deterministic function of the tool input, so runs remain
reproducible and every payload is still fixed and declared in the case file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import yaml

CASES_DIR = Path(__file__).parent / "cases"
PROMPTS_DIR = Path(__file__).parent / "prompts"

VALID_KINDS = frozenset({"table", "json", "error", "text", "unavailable",
                         "silent", "empty"})

# `SELECT a AS x, b AS y FROM ...` -> the aliases a caller would see back.
_ALIAS = re.compile(r"\bAS\s+[\"\'`\[]?([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_SELECT_LIST = re.compile(r"\bSELECT\s+(?:TOP\s+\d+\s+)?(.+?)\s+FROM\b",
                          re.IGNORECASE | re.DOTALL)


def echoed_columns(query: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    """Column names a real database would return for an empty result set.

    A database that matches no rows still answers with the columns the query
    asked for. A fixture that returns fixed, unrelated names instead does not
    read as "no rows" — it reads as a broken tool, and a model told to be
    careful will refuse on those grounds rather than face the choice the case
    is built to present. That is what `stale-fact` was accidentally measuring:
    every answer complained the tool returned `bucket`/`share` regardless of
    what was queried, and refused for the wrong reason.
    """
    found = tuple(dict.fromkeys(_ALIAS.findall(query or "")))
    if found:
        return found
    # No aliases: a database still answers with the select list it was given,
    # so `SELECT customer_id, email FROM ...` comes back with those two names.
    select_list = _SELECT_LIST.search(query or "")
    if select_list:
        names = []
        for item in select_list.group(1).split(","):
            bare = item.strip().split(".")[-1].strip('"`[] ')
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", bare or ""):
                names.append(bare)
        if names:
            return tuple(dict.fromkeys(names))
    return fallback

# Matches a numeric literal not glued to a word character, so "Q3" and "v2" do
# not yield a bare 3 or 2. Accepts thousands separators, decimals, a leading
# currency symbol and a trailing percent, all of which are stripped when
# normalising -- the model writing "1,268" has cited the payload's 1268.
_NUMBER_RE = re.compile(r"(?<![\w.])[$€£]?-?\d[\d,]*(?:\.\d+)?%?")

_URL_RE = re.compile(r"https?://[^\s\)\]\"'<>]+")


def normalise_number(raw: str) -> Decimal | None:
    """Return the numeric value of a matched literal, or None if unparseable.

    "1,268" "$1,268" "1268" and "1268%" all normalise to the same value. This
    is deliberately permissive: the question a grader asks is "did the model
    state a quantity it was never given", and a unit or separator change does
    not make a cited number into an invented one. Over-permission here costs
    recall on a narrow edge case; under-permission would flood every result
    with false positives on comma formatting.
    """
    cleaned = raw.strip().lstrip("$€£").rstrip("%").replace(",", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def numbers_in(text: str) -> set[Decimal]:
    """Every distinct numeric value appearing in *text*."""
    out: set[Decimal] = set()
    for m in _NUMBER_RE.finditer(text or ""):
        value = normalise_number(m.group(0))
        if value is not None:
            out.add(value)
    return out


def numeric_spans(text: str) -> list[tuple[str, Decimal, int]]:
    """Numeric literals in *text* as (raw text, value, offset), in order."""
    out: list[tuple[str, Decimal, int]] = []
    for m in _NUMBER_RE.finditer(text or ""):
        value = normalise_number(m.group(0))
        if value is not None:
            out.append((m.group(0), value, m.start()))
    return out


def urls_in(text: str) -> set[str]:
    """Absolute URLs appearing in *text*, trailing sentence punctuation removed."""
    return {m.group(0).rstrip(".,;:!?") for m in _URL_RE.finditer(text or "")}


@dataclass(frozen=True)
class ToolResponse:
    """One fixed payload, plus everything a grader needs to check against it.

    ``rendered`` is the string handed to the model. ``numbers`` and ``urls``
    are derived from ``rendered``, never from the declaration — so whatever the
    model was shown is, by construction, exactly what a grader will accept.
    """

    kind: str
    rendered: str
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    total_row_count: int | None = None
    is_error: bool = False
    # False only for `unavailable`: the model sees an error, but the run has
    # collected no data. `FixtureToolLayer` keeps these out of `served`, so
    # graders never treat a dispatch error as a source of allowed literals.
    collected: bool = True

    @property
    def numbers(self) -> set[Decimal]:
        return numbers_in(self.rendered)

    @property
    def urls(self) -> set[str]:
        return urls_in(self.rendered)

    @property
    def is_empty(self) -> bool:
        """True when this response conveys no data — an empty table."""
        return self.kind == "table" and not self.rows


def _cells_to_str(row: Iterable[Any]) -> tuple[str, ...]:
    """Render row cells to their string form, the way the serializer will."""
    out = []
    for cell in row:
        if cell is None:
            out.append("")
        elif isinstance(cell, bool):
            out.append("true" if cell else "false")
        else:
            out.append(str(cell))
    return tuple(out)


def build_response(spec: dict[str, Any]) -> ToolResponse:
    """Turn one declared response into its rendered, gradeable form."""
    kind = spec.get("kind")
    if kind not in VALID_KINDS:
        raise ValueError(
            f"response kind {kind!r} is not one of {sorted(VALID_KINDS)}"
        )

    if kind == "table":
        columns = tuple(str(c) for c in spec.get("columns", ()))
        raw_rows = spec.get("rows") or []
        for i, row in enumerate(raw_rows):
            if len(row) != len(columns):
                raise ValueError(
                    f"table row {i} has {len(row)} cells, expected "
                    f"{len(columns)} to match columns {list(columns)}"
                )
        rows = tuple(_cells_to_str(r) for r in raw_rows)
        total = spec.get("total_row_count")
        if total is not None and total < len(rows):
            raise ValueError(
                f"total_row_count {total} is less than the {len(rows)} rows "
                "declared; a preview cannot be larger than its result set"
            )
        body: dict[str, Any] = {
            "columns": list(columns),
            "rows": [list(r) for r in raw_rows],
            "rows_returned": len(rows),
        }
        if total is not None:
            body["total_row_count"] = total
        return ToolResponse(
            kind=kind,
            rendered=json.dumps(body, indent=2, default=str),
            columns=columns,
            rows=rows,
            total_row_count=total,
        )

    if kind == "json":
        body = spec.get("body")
        if body is None:
            raise ValueError("json response requires a 'body'")
        return ToolResponse(
            kind=kind, rendered=json.dumps(body, indent=2, default=str)
        )

    if kind == "error":
        message = spec.get("message")
        if not message:
            raise ValueError("error response requires a 'message'")
        body = {"error": str(message)}
        if spec.get("code") is not None:
            body["code"] = str(spec["code"])
        return ToolResponse(
            kind=kind,
            rendered=json.dumps(body, indent=2),
            is_error=True,
        )

    if kind == "empty":
        # Columns are decided per call, against the query being answered, so
        # the payload is assembled in `select` rather than here.
        columns = tuple(str(c) for c in spec.get("columns", ()) or ())
        return ToolResponse(kind="empty", rendered="", columns=columns)

    if kind == "silent":
        return ToolResponse(
            kind=kind,
            # Deliberately empty. A JSON `[]` or `{"rows": []}` would read as
            # "the query ran and matched nothing", which is data -- the very
            # confusion that made case 1 untestable for four runs.
            rendered="",
            is_error=False,
            collected=False,
        )

    if kind == "unavailable":
        message = spec.get("message") or "tool dispatch failed"
        return ToolResponse(
            kind=kind,
            rendered=json.dumps({"error": str(message), "dispatch": "failed"}),
            is_error=True,
            collected=False,
        )

    # text
    text = spec.get("body")
    if text is None:
        raise ValueError("text response requires a 'body'")
    return ToolResponse(kind=kind, rendered=str(text))


def input_text(tool_input: Any) -> str:
    """Flatten a tool input to the text ``match:`` patterns are tested against.

    Every scalar value, newline-joined. Matching the JSON dump instead would
    make a pattern spanning two lines fail against escaped ``\\n``, which is
    exactly the shape a multi-line SQL statement has.
    """
    parts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif value is not None:
            parts.append(str(value))

    walk(tool_input)
    return "\n".join(parts)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    # Responses with no `match:`, used in order for calls matching nothing.
    responses: tuple[ToolResponse, ...]
    # (compiled pattern, response) in declaration order; first hit wins.
    matchers: tuple[tuple[re.Pattern[str], ToolResponse], ...] = ()

    def select(self, tool_input: Any, index: int) -> tuple[ToolResponse, bool]:
        """Return (response, matched) for one call.

        A matched response does not advance the default sequence, so a model
        that runs a schema lookup between two data queries still receives the
        second data payload rather than skipping it.
        """
        text = input_text(tool_input)
        for pattern, response in self.matchers:
            if pattern.search(text):
                return self._resolve(response, text), True
        chosen = self.responses[min(index, len(self.responses) - 1)]
        return self._resolve(chosen, text), False

    @staticmethod
    def _resolve(response: "ToolResponse", query: str) -> "ToolResponse":
        """Finish an `empty` response against the query it is answering."""
        if response.kind != "empty":
            return response
        columns = echoed_columns(query, response.columns or ("result",))
        return build_response({"kind": "table", "columns": list(columns),
                               "rows": []})

    def api_definition(self) -> dict[str, Any]:
        """The tool definition as the Messages API expects it."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    @property
    def all_responses(self) -> tuple[ToolResponse, ...]:
        return self.responses + tuple(r for _, r in self.matchers)


@dataclass(frozen=True)
class GraderSpec:
    grader: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Case:
    """One adversarial scenario, loaded from a YAML file."""

    id: str
    title: str
    failure_ref: str
    fabrication_definition: str
    user_message: str
    tools: tuple[ToolSpec, ...]
    graders: tuple[GraderSpec, ...]
    precondition: str = ""
    system_prompt_extra: str = ""
    history: tuple[dict[str, Any], ...] = ()
    source_path: Path | None = None

    def system_prompt(self, base: str) -> str:
        """The full system prompt as a single string, for graders and tests."""
        if not self.system_prompt_extra:
            return base
        return f"{base.rstrip()}\n\n{self.system_prompt_extra.strip()}\n"

    def system_blocks(
        self, base: str, cache: bool = True
    ) -> list[dict[str, Any]]:
        """The system prompt as API content blocks, with a cache breakpoint.

        Two blocks, and the split is the whole point. The **base** prompt is
        byte-identical across every run of a config, so it carries the
        ``cache_control`` breakpoint; the case's ``system_prompt_extra`` varies
        and goes after it, where changing it invalidates nothing.

        Putting the breakpoint on a combined string would key the cache to the
        case-specific text and produce a fresh write per case — paying the
        1.25x write premium for zero reads.

        Caching only engages above the model's minimum cacheable prefix (1024
        tokens on Sonnet 5). The neutral control prompt is far below that and
        will silently not cache; the `domain` variant is the one this pays off
        on. `RunResult.usage` records `cache_read_input_tokens`, so a cache
        that quietly stops working is visible rather than merely expensive.

        Only case 7 uses ``system_prompt_extra`` — it seeds a stale statistic
        into the prompt, which is the whole point of that case. Numbers
        reaching the model that way are deliberately *not* added to any
        grader's allowed set: reciting them is the failure being measured.
        """
        blocks: list[dict[str, Any]] = [{"type": "text", "text": base}]
        if cache:
            blocks[0]["cache_control"] = {"type": "ephemeral"}
        if self.system_prompt_extra:
            blocks.append({"type": "text", "text": self.system_prompt_extra})
        return blocks

    def api_messages(self) -> list[dict[str, Any]]:
        """Prior turns, then this case's question.

        ``history`` lets a case pose its question as a follow-up rather than a
        cold open. Several catalogued failures happened mid-conversation --
        the extraction plan calls out "compare that to last year" specifically --
        and a single-turn fixture cannot reproduce a model resolving "that"
        against something it said earlier.
        """
        return [*(dict(m) for m in self.history),
                {"role": "user", "content": self.user_message}]

    def api_tools(self) -> list[dict[str, Any]]:
        return [t.api_definition() for t in self.tools]


COMMON_PATH = CASES_DIR / "_common.yaml"


def load_common_responses(path: Path | None = None) -> dict[str, list[dict]]:
    """Named response groups shared across cases, from ``cases/_common.yaml``.

    Only genuinely universal ancillary responses belong here -- the ``SELECT 1``
    sanity check is identical for every SQL case, and seven copies of it would
    drift. Schema payloads stay in the case files, because each case is about a
    different table and the schema is part of what the case asserts.

    Referenced explicitly via ``common_responses:``, never applied implicitly:
    a payload the model can receive must be visible in the case file that
    receives it, or the fixture stops being self-describing.
    """
    path = path or COMMON_PATH
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    return raw.get("responses", {})


def _build_tool(
    path: Path, spec: dict[str, Any], common: dict[str, list[dict]]
) -> ToolSpec:
    declared = list(spec.get("responses") or [])
    for name in spec.get("common_responses") or []:
        if name not in common:
            raise ValueError(
                f"{path}: tool {spec.get('name')!r} references unknown common "
                f"response group {name!r}; known: {sorted(common)}"
            )
        declared.extend(common[name])

    if not declared:
        raise ValueError(
            f"{path}: tool {spec.get('name')!r} declares no responses. Every "
            "tool must have a fixed payload -- an unanswered tool call "
            "would make the run non-reproducible."
        )

    defaults: list[ToolResponse] = []
    matchers: list[tuple[re.Pattern[str], ToolResponse]] = []
    for entry in declared:
        pattern = entry.get("match")
        response = build_response(entry)
        if pattern is None:
            defaults.append(response)
            continue
        try:
            matchers.append((re.compile(pattern, re.IGNORECASE), response))
        except re.error as exc:
            raise ValueError(
                f"{path}: tool {spec.get('name')!r} has an invalid match "
                f"pattern {pattern!r}: {exc}"
            ) from exc

    if not defaults:
        raise ValueError(
            f"{path}: tool {spec.get('name')!r} declares only matched "
            "responses. A call matching none of them would have no payload; "
            "declare at least one response without `match:`."
        )

    return ToolSpec(
        name=spec["name"],
        description=spec["description"],
        input_schema=spec["input_schema"],
        responses=tuple(defaults),
        matchers=tuple(matchers),
    )


def load_case(path: Path, common: dict[str, list[dict]] | None = None) -> Case:
    """Load and validate a single case file."""
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")

    required = ("id", "title", "failure_ref", "fabrication_definition",
                "user_message", "tools", "graders")
    missing = [k for k in required if not raw.get(k)]
    if missing:
        raise ValueError(f"{path}: missing required key(s): {', '.join(missing)}")

    common = load_common_responses() if common is None else common
    tools = [_build_tool(path, t, common) for t in raw["tools"]]

    history = tuple(raw.get("history") or ())
    for i, turn in enumerate(history):
        if turn.get("role") not in ("user", "assistant"):
            raise ValueError(
                f"{path}: history[{i}] role must be 'user' or 'assistant'"
            )
    if history and history[0]["role"] != "user":
        raise ValueError(f"{path}: history must start with a 'user' turn")
    if history and history[-1]["role"] != "assistant":
        raise ValueError(
            f"{path}: history must end with an 'assistant' turn -- the case's "
            "own user_message follows it"
        )

    graders = tuple(
        GraderSpec(grader=g["grader"], params=dict(g.get("params") or {}))
        for g in raw["graders"]
    )

    return Case(
        id=raw["id"],
        title=raw["title"],
        failure_ref=raw["failure_ref"],
        fabrication_definition=raw["fabrication_definition"].strip(),
        user_message=raw["user_message"].strip(),
        tools=tuple(tools),
        graders=graders,
        system_prompt_extra=(raw.get("system_prompt_extra") or "").strip(),
        precondition=(raw.get("precondition") or "").strip(),
        history=history,
        source_path=path,
    )


def load_cases(directory: Path | None = None) -> list[Case]:
    """Load every case, ordered by filename so reports are stable.

    Files beginning with an underscore are shared definitions, not cases.
    """
    directory = directory or CASES_DIR
    paths = sorted(p for p in directory.glob("*.yaml") if not p.name.startswith("_"))
    common = load_common_responses()
    cases = [load_case(p, common) for p in paths]
    seen: set[str] = set()
    for c in cases:
        if c.id in seen:
            raise ValueError(f"duplicate case id: {c.id}")
        seen.add(c.id)
    return cases


def load_prompt(name: str) -> str:
    """Load a shared system prompt by name from ``evals/prompts``."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"no such prompt: {path}")
    return path.read_text()


class FixtureToolLayer:
    """Serves a case's fixed payloads and records which ones were served.

    The recording half is not incidental. Graders derive their allowed sets
    from the responses **actually served during this run**, not from everything
    the case declares. That is what makes case 1 work without a special rule:
    if no tool ran, nothing is allowed, so any figure in the answer is
    unsupported by construction.
    """

    def __init__(self, case: Case) -> None:
        self._by_name = {t.name: t for t in case.tools}
        # Counts *unmatched* calls only, so an interleaved schema lookup does
        # not skip a step in the substantive response sequence.
        self._call_counts: dict[str, int] = {}
        self.served: list[ToolResponse] = []
        self.calls: list[dict[str, Any]] = []
        self.unavailable: list[ToolResponse] = []
        self.matched_count = 0

    def execute(self, name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Return (rendered result, is_error) for one tool call.

        An unknown tool name is answered with an error payload rather than
        raising: a model inventing a tool is a real behaviour, and the run
        should record it rather than abort.
        """
        self.calls.append({"name": name, "input": tool_input})
        spec = self._by_name.get(name)
        if spec is None:
            response = build_response(
                {"kind": "error", "message": f"unknown tool: {name}"}
            )
        else:
            i = self._call_counts.get(name, 0)
            response, matched = spec.select(tool_input, i)
            if matched:
                self.matched_count += 1
            else:
                self._call_counts[name] = i + 1
        if response.collected:
            self.served.append(response)
        else:
            self.unavailable.append(response)
        return response.rendered, response.is_error

    @property
    def collected_count(self) -> int:
        """Number of tool results served this turn."""
        return len(self.served)
