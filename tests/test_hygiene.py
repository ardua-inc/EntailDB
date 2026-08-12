"""Repo hygiene, enforced rather than remembered.

`DESIGN.md`: *"Nothing here may assume Blue Boutique's schema, deployment, or
vocabulary."* the extraction plan records the concrete way that gets violated — the
most reusable file in the source codebase had a purchasing-feature URL path
baked into a module-level constant, exactly the sort of thing that ships to a
public repo carrying a client's internal scheme.

A convention nobody checks is a convention that decays. These run in CI.

The design documents themselves are excluded: they name the source system
deliberately, and licensing for the derivation is cleared. The constraint is on
code, fixtures and docstrings.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CODE_DIRS = ("src", "tests", "evals", "app")

# Every file git would publish, not just the code directories. The client
# name survived in `DESIGN.md` for four days because the sweep only looked
# at `src/`, `tests/`, `evals/` and `app/` — the documentation, which is the
# most-read part of a public repository, was never checked.
def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True, check=True)
    return [ROOT / line for line in out.stdout.splitlines()
            if (ROOT / line).is_file()]

# Written split so this file does not itself contain the literals it bans.
BANNED = [
    "blue" + "boutique",
    "blue boutique",
    "erply",
    "shopify",
    "spiff",
]

# `metabase` is deliberately absent. It appears in `DESIGN.md` naming a
# competitor, beside Snowflake Cortex Analyst and Databricks Genie — a public
# product in a landscape survey identifies nobody. The terms above are ones
# that name or imply the source deployment or its sector.

SKIP_DIRS = {"__pycache__", ".git", ".venv", ".pytest_cache", "runs"}


def code_files() -> list[Path]:
    out: list[Path] = []
    for directory in CODE_DIRS:
        for path in (ROOT / directory).rglob("*"):
            if not path.is_file():
                continue
            if set(path.parts) & SKIP_DIRS:
                continue
            if path.suffix in {".py", ".yaml", ".yml", ".md", ".json", ".toml",
                               ".html", ".css", ".js"}:
                out.append(path)
    return out


def test_code_files_exist():
    """Guards the guard: an empty file list would make the sweep vacuous."""
    files = code_files()
    assert len(files) > 10
    assert any(f.name.endswith(".yaml") for f in files)


@pytest.mark.parametrize("term", BANNED)
def test_no_client_reference_in_anything_git_tracks(term):
    """The published surface, not just the code."""
    hits = []
    for path in tracked_files():
        # This file names the terms in order to ban them; `.gitignore` names
        # the private document in order to exclude it.
        if path.name in (Path(__file__).name, ".gitignore"):
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if term in line.lower():
                hits.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()[:100]}")
    assert not hits, f"{term!r} appears in a tracked file:\n" + "\n".join(hits)


def test_the_extraction_plan_is_not_tracked():
    """It names the source deployment and describes its internals. Kept
    locally, ignored by git, and referenced by nothing that is published."""
    tracked = {p.name for p in tracked_files()}
    assert "EXTRACTION.md" not in tracked


def test_nothing_published_points_at_the_private_document():
    """A public README citing a file nobody can see is a broken reference and
    an advertisement for the thing it should not mention."""
    hits = [str(p.relative_to(ROOT)) for p in tracked_files()
            if p.name not in (Path(__file__).name, ".gitignore")
            and "EXTRACTION" in p.read_text(errors="ignore")]
    assert not hits, f"tracked files still cite it: {hits}"


@pytest.mark.parametrize("term", BANNED)
def test_no_client_domain_references(term: str):
    hits = []
    for path in code_files():
        if path.name == Path(__file__).name:
            continue
        for i, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if term in line.lower():
                hits.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()[:100]}")
    assert not hits, f"{term!r} appears in code:\n" + "\n".join(hits)


def test_no_client_couplings_baked_into_the_library():
    """The three couplings the extraction plan requires removed from the port.

    The check is on *code*, not prose. `link_allowlist.py` discusses the
    production incident in its docstrings, including the relative path shape
    that caused it, and that discussion is the reason the parameter exists —
    deleting it would remove the explanation and keep the risk.

    What must not survive is the coupling itself: a module-level constant a
    caller cannot override.
    """
    source = (ROOT / "src" / "fidelity" / "link_allowlist.py").read_text()
    tree = ast.parse(source)

    constants = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        if isinstance(target, ast.Name)
    }
    assert "ALWAYS_ALLOWED_URLS" not in constants

    # The default prefix list must be empty: a caller that does not declare its
    # own download paths gets no hardcoded ones.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_DEFAULT_RELATIVE_PREFIXES"
        ):
            assert isinstance(node.value, ast.Tuple)
            assert node.value.elts == []
            break
    else:
        pytest.fail("_DEFAULT_RELATIVE_PREFIXES is missing")

    # The purchasing-feature path is client business logic, not an example.
    assert "po-translate" not in source.lower()

    # No string literal in the module is a download path prefix. A bare "/" is
    # the `startswith` test that recognises relative URLs generically, not a
    # path; anything longer would be a specific deployment's route.
    literals = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    paths = [s for s in literals if s.startswith("/") and len(s) > 1]
    assert not paths, f"hardcoded relative path literal(s): {paths}"


# Names that are, or grow into, a switch that turns a guard off.
_FLAG_SHAPES = re.compile(
    r"(?i)^(bypass_|dev_mode|disable_|skip_guard|guards?_enabled|"
    r"no_guard|unsafe_mode)"
)


def test_library_never_reads_the_environment():
    """A guard whose behaviour depends on deployment config is a guard with an
    off switch, whether or not anyone named it one."""
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {
                "environ",
                "getenv",
            }:
                pytest.fail(f"{path.relative_to(ROOT)} reads the environment")


def test_no_guard_disabling_flags():
    """DESIGN.md: guards must not be disableable by a convenience flag.

    Checked over the AST rather than the text, so a docstring quoting the
    anti-pattern — `evals/runners/__init__.py` names the `BYPASS_AUTH` variable
    the principle came from — does not trip it. Only real identifiers count:
    assignments, parameters, keyword arguments.
    """
    paths = [p for d in ("src", "evals", "app")
             for p in (ROOT / d).rglob("*.py")
             if not set(p.parts) & SKIP_DIRS]
    assert paths
    for path in paths:
        tree = ast.parse(path.read_text())
        identifiers: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.arg):
                identifiers.add(node.arg)
            elif isinstance(node, ast.keyword) and node.arg:
                identifiers.add(node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                identifiers.add(node.name)
        hits = sorted(i for i in identifiers if _FLAG_SHAPES.match(i))
        assert not hits, f"{path.relative_to(ROOT)} defines guard-toggle {hits}"


def test_no_source_repo_history_imported():
    """the extraction plan: fresh repo, no imported history."""
    import subprocess

    log = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).stdout.lower()
    for term in BANNED:
        assert term not in log, f"{term!r} appears in commit history"
