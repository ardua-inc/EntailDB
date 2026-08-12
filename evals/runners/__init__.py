"""Runners — the systems under test.

Each configuration in the ablation table is a **different class**, not a flag
on a shared one. `DESIGN.md` is unambiguous about why:

    Guards must not be disableable by a convenience flag. [...] If a guard
    needs to be off for testing, the test injects a different runner — it does
    not set a flag that production can also read.

That principle came out of a `BYPASS_AUTH` env var in the source deployment
which sat enabled on a staging host sharing production credentials for months.
An eval harness is exactly the sort of legitimate need that grows such a flag,
so the shape is fixed here before there is anything to disable: `evals/configs.py`
maps a config name to a constructor, and the library never reads a config file
or environment variable to decide whether a guard runs.
"""

from .base import RunResult, Runner  # noqa: F401
from .baseline import BaselineRunner  # noqa: F401
from .two_phase import TwoPhaseRunner  # noqa: F401
