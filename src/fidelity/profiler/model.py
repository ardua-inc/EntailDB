"""Types the profiler works in.

Deliberately free of any database library. A `Runner` is any callable that
takes SQL and returns rows; a `Dialect` knows how one product spells
introspection. Nothing here imports a driver, reads the environment, or knows
what a connection string is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

# Takes SQL, returns rows. The caller supplies read-only access; the profiler
# never opens a connection and never writes.
Runner = Callable[[str], Sequence[Sequence[Any]]]


@dataclass(frozen=True)
class TableRef:
    name: str
    schema: str | None = None

    def qualified(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


@dataclass(frozen=True)
class ColumnRef:
    table: TableRef
    name: str
    declared_type: str
    nullable: bool = True
    primary_key: bool = False
    # Values the schema *says* are allowed (CHECK constraint, ENUM type).
    # Present so the profiler can report declared values with zero rows --
    # `FAILURES.md` §4's "NOT IN (...) built on an empty set is a no-op".
    declared_values: tuple[str, ...] = ()

    def qualified(self) -> str:
        return f"{self.table.qualified()}.{self.name}"


@dataclass(frozen=True)
class ForeignKey:
    """A join the profiler will measure the hit rate of.

    `declared` distinguishes a real constraint from a soft key inferred by
    naming convention. Soft keys are where the interesting failures live: the
    source system's `clientid` resolved to a customer row 29% of the time and
    nothing in the schema said so.
    """

    column: ColumnRef
    target: TableRef
    target_column: str
    declared: bool = True
    # How this key was found. "declared" is a real constraint; "name" is a
    # naming convention; "value_overlap" is a relationship nothing in the
    # schema records, discovered by measuring. The last is the strongest claim
    # and the one worth labelling as such.
    inferred_by: str = "declared"


@dataclass
class ColumnProfile:
    column: ColumnRef
    rows: int = 0
    nulls: int = 0
    # Counted up to a cap, not exactly. Every derivation that reads it asks
    # "is this small enough to enumerate", never "how many exactly", so the
    # exact value on a high-cardinality column is pure cost. See `probe.py`.
    distinct: int = 0
    distinct_capped: bool = False
    minimum: Any = None
    maximum: Any = None
    samples: tuple[str, ...] = ()
    # Populated only for columns that look enum-like.
    observed_values: tuple[tuple[str, int], ...] = ()

    @property
    def null_rate(self) -> float:
        return self.nulls / self.rows if self.rows else 0.0

    @property
    def all_null(self) -> bool:
        return self.rows > 0 and self.nulls == self.rows


@dataclass
class TableProfile:
    table: TableRef
    rows: int = 0
    columns: list[ColumnProfile] = field(default_factory=list)


@dataclass
class JoinProfile:
    foreign_key: ForeignKey
    non_null: int = 0
    matched: int = 0

    @property
    def hit_rate(self) -> float:
        return self.matched / self.non_null if self.non_null else 0.0


@dataclass
class Fact:
    """One notable, actionable statement about the data.

    Not every statistic is a fact. `order_id is 0% NULL` is true and useless;
    the document exists to replace hand-written lore, and lore is what someone
    found surprising enough to write down. A profiler that emits every measured
    number just swaps one bloated prompt for another -- so derivation is
    threshold-driven and selective by design.
    """

    subject: str
    kind: str
    statement: str
    severity: str = "info"  # info | caution | blocking
    evidence: dict[str, Any] = field(default_factory=dict)
