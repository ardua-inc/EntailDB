"""Command line: `python -m evals <command>`.

    validate      load every case and check it parses. No API key needed.
    run           execute a config against the case set, N times each
    report        aggregate a run file into the ablation table
    audit         print flagged spans for hand review
    regrade       recompute verdicts from stored transcripts
    cross-check   model cross-check of the one heuristic grader
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .configs import CONFIGS
from .fixtures import load_cases, load_prompt
from .graders import REGISTRY
from .preconditions import REGISTRY as PRECONDITIONS
from .harness import RunPlan, load_results, regrade, run_plan
from .report import load_and_render
from .runners.baseline import DEFAULT_MAX_ROUNDS, DEFAULT_MODEL

DEFAULT_RUNS_DIR = Path(__file__).parent.parent / "runs"


def _load_dotenv(path: Path | None = None) -> None:
    """Read `KEY=value` pairs from a project `.env` into the environment.

    Stdlib only, and it never overwrites a variable already set — an explicit
    `export` on the command line wins over a file. `.env` is gitignored.

    This lives in the CLI, not the library. Nothing under `src/` reads the
    environment, and a test enforces that: a guard whose behaviour depends on
    deployment config is a guard with an off switch, whether or not anyone
    named it one.
    """
    path = path or (Path(__file__).parent.parent / ".env")
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _client(provider: str = "", base_url: str = ""):
    """The client the runners drive.

    Every provider, Anthropic included, goes through `ProviderClient` so the
    four sets of numbers are produced by one instrument. Quoting the older
    Anthropic results beside these would compare a provider difference with an
    instrument difference.
    """
    _load_dotenv()
    if provider:
        from .provider_client import from_spec

        key = os.environ.get("EVAL_PROVIDER_KEY", "")
        if provider.startswith("anthropic:"):
            key = key or os.environ.get("ANTHROPIC_API_KEY", "")
        elif provider.startswith("openai") and not base_url:
            key = key or os.environ.get("OPENAI_API_KEY", "")
        return from_spec(provider, api_key=key, base_url=base_url)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ANTHROPIC_API_KEY is not set, and no .env in the repo root "
            "defines it. `validate`, `report` and `audit` work without it; "
            "`run` and `cross-check` do not."
        )
    from .provider_client import ProviderClient

    return ProviderClient("anthropic", DEFAULT_MODEL,
                          api_key=os.environ["ANTHROPIC_API_KEY"])


def cmd_validate(args: argparse.Namespace) -> int:
    cases = load_cases()
    print(f"{len(cases)} case(s) loaded from {load_cases.__module__}:")
    for case in cases:
        graders = ", ".join(g.grader for g in case.graders)
        unknown = [g.grader for g in case.graders if g.grader not in REGISTRY]
        served = sum(len(t.responses) for t in case.tools)
        print(f"  {case.id:<22} {case.failure_ref:<16} {served} payload(s)")
        print(f"    {case.title}")
        print(f"    graders: {graders}")
        if case.precondition:
            print(f"    precondition: {case.precondition}")
        if unknown:
            print(f"    !! unknown grader(s): {unknown}")
            return 1
        if case.precondition and case.precondition not in PRECONDITIONS:
            print(f"    !! unknown precondition: {case.precondition}")
            return 1
    for name in ("neutral", "instructed"):
        load_prompt(name)
    print(f"\nprompts ok; configs: {', '.join(sorted(CONFIGS))}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    # The provider spec becomes the recorded model name, so the report buckets
    # `openai:qwen3.6` apart from `anthropic:claude-sonnet-5` instead of
    # merging two providers under one label.
    if args.provider:
        args.model = args.provider
    cases = load_cases()
    if args.case:
        wanted = set(args.case)
        cases = [c for c in cases if c.id in wanted]
        missing = wanted - {c.id for c in cases}
        if missing:
            sys.exit(f"unknown case id(s): {sorted(missing)}")

    out = Path(args.out) if args.out else DEFAULT_RUNS_DIR / f"{args.config}.jsonl"
    plan = RunPlan(
        config=args.config,
        cases=cases,
        n=args.n,
        model=args.model,
        out_path=out,
        concurrency=args.concurrency,
        max_rounds=args.max_rounds,
    )
    total = len(cases) * args.n
    print(
        f"config={args.config} model={args.model} cases={len(cases)} "
        f"n={args.n} -> {total} run(s) max, appending to {out}"
    )

    done = [0]

    def progress(result) -> None:
        done[0] += 1
        state = "ERROR" if result.error else (
            "FABRICATED" if result.fabricated else "clean"
        )
        print(f"  [{done[0]}] {result.case_id}#{result.run_index} {state}")

    run_plan(plan, _client(getattr(args, "provider", ""),
                           getattr(args, "base_url", "")), on_result=progress)
    print()
    print(load_and_render(out))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    print(load_and_render(Path(args.run), markdown=args.markdown))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Print flagged spans so verdicts can be checked by hand.

    The deterministic graders over-flag in known ways -- an answer saying "I ran
    2 queries" trips the numeric grader. This is the path `MEASUREMENT.md`
    assumes exists: read the flagged runs and classify them, rather than
    defending or dismissing the aggregate.
    """
    results = load_results(Path(args.run))
    shown = 0
    for r in sorted(results, key=lambda r: (r.case_id, r.run_index)):
        if args.case and r.case_id not in args.case:
            continue
        flagged = [g for g in r.grader_results if g["spans"]]
        if not flagged and not (args.all or r.error):
            continue
        if shown >= args.limit:
            break
        shown += 1
        print("=" * 72)
        print(
            f"{r.case_id}#{r.run_index}  config={r.config}  "
            f"fabricated={r.fabricated}  rounds={r.rounds}  "
            f"tool_calls={len(r.tool_calls)}"
        )
        if r.error:
            print(f"  ERROR: {r.error}")
        for g in flagged:
            tag = " (advisory)" if g["advisory"] else ""
            print(f"  -- {g['grader']}{tag}: {g['detail']}")
            for span in g["spans"][: args.spans]:
                print(f"       {span}")
        print("\n  answer:")
        answer = r.answer_text or "(empty)"
        for line in answer.splitlines()[: args.lines]:
            print(f"    {line}")
        print()
    print(f"{shown} run(s) shown.")
    return 0


def cmd_regrade(args: argparse.Namespace) -> int:
    path = Path(args.run)
    results = regrade(path)
    print(f"regraded {len(results)} run(s) in {path}\n")
    print(load_and_render(path))
    return 0


def cmd_cross_check(args: argparse.Namespace) -> int:
    from .model_grader import cross_check, summarise_cross_check

    checks = cross_check(
        Path(args.run), _client(), sample=args.sample, grader_model=args.grader_model
    )
    print(summarise_cross_check(checks))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evals", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="load and check every case")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("run", help="execute a config against the case set")
    p.add_argument("--config", default="baseline", choices=sorted(CONFIGS))
    p.add_argument("--n", type=int, default=20, help="runs per case (>=20)")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--provider", default="",
                   help="kind:model, e.g. openai:qwen3.6 (default anthropic)")
    p.add_argument("--base-url", default="",
                   help="endpoint for an OpenAI-compatible provider")
    p.add_argument("--case", action="append", help="limit to case id (repeatable)")
    p.add_argument("--out", help="output JSONL (default runs/<config>.jsonl)")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS,
                   help="tool-round budget per run")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("report", help="aggregate a run file")
    p.add_argument("run")
    p.add_argument("--markdown", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("audit", help="print flagged spans for hand review")
    p.add_argument("run")
    p.add_argument("--case", action="append")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--spans", type=int, default=8, help="max spans per grader")
    p.add_argument("--lines", type=int, default=25, help="max answer lines")
    p.add_argument("--all", action="store_true", help="include unflagged runs")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("regrade", help="recompute verdicts from transcripts")
    p.add_argument("run")
    p.set_defaults(func=cmd_regrade)

    p = sub.add_parser("cross-check", help="model cross-check of soft grader")
    p.add_argument("run")
    p.add_argument("--sample", type=int, default=20)
    p.add_argument("--grader-model", default="claude-opus-5")
    p.set_defaults(func=cmd_cross_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
