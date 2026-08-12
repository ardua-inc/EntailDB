"""Confidence intervals.

`MEASUREMENT.md` §4: never publish a fabrication rate without N and an interval.
At the specified N of 20 the intervals are wide enough that this is not a
formality — a clean 0/20 still has a 95% upper bound above 16%, so "the guard
eliminated it" is not a claim 20 runs can support, and two configs differing by
less than roughly 20 points are not distinguishable at that N.

Wilson rather than the normal approximation, because the normal interval
degenerates exactly where these results will cluster: at k=0 it produces the
interval [0, 0], asserting certainty from twenty samples.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Z_95 = 1.959963984540054


@dataclass(frozen=True)
class Proportion:
    successes: int
    n: int
    low: float
    high: float

    @property
    def rate(self) -> float:
        return self.successes / self.n if self.n else 0.0

    def format(self, pct: bool = True) -> str:
        if not self.n:
            return "n/a"
        if pct:
            return (
                f"{self.rate * 100:.0f}% "
                f"[{self.low * 100:.0f}–{self.high * 100:.0f}]"
            )
        return f"{self.rate:.3f} [{self.low:.3f}–{self.high:.3f}]"


def wilson(successes: int, n: int, z: float = Z_95) -> Proportion:
    """Wilson score interval for a binomial proportion."""
    if n <= 0:
        return Proportion(successes, n, 0.0, 0.0)
    if successes < 0 or successes > n:
        raise ValueError(f"successes {successes} out of range for n={n}")
    p = successes / n
    denominator = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return Proportion(
        successes,
        n,
        max(0.0, (centre - margin) / denominator),
        min(1.0, (centre + margin) / denominator),
    )
