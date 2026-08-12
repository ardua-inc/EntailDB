"""Running the probes.

the extraction plan's minimum viable set: per-column null rate, distinct
cardinality, min/max, observed values for enum-like columns, join hit rate
across declared and soft foreign keys, and samples for format inference.

Every query here is ANSI SQL assembled from dialect-quoted identifiers. The
profiler issues only aggregates and a bounded sample — it never selects a whole
table, so profiling a large warehouse costs a scan per column, not a transfer.

**Read-only by construction.** Nothing in this module emits DDL or DML. That is
a property of the code, not a promise in a docstring: the caller should also
supply a read-only connection, because a library asserting its own good
behaviour is exactly the guarantee `DESIGN.md` says not to rely on.
"""

from __future__ import annotations

from typing import Sequence

from .dialects import Dialect, infer_by_value_overlap, infer_soft_keys
from .model import (
    ColumnProfile,
    ColumnRef,
    ForeignKey,
    JoinProfile,
    Runner,
    TableProfile,
    TableRef,
)

# A column with more distinct values than this is not an enum, and listing its
# values would bloat the document rather than inform it.
ENUM_MAX_DISTINCT = 25
SAMPLE_SIZE = 200


def _scalar(run: Runner, sql: str, default=0):
    rows = run(sql)
    if not rows or rows[0][0] is None:
        return default
    return rows[0][0]


def profile_table(
    run: Runner, dialect: Dialect, table: TableRef, columns: Sequence[ColumnRef]
) -> TableProfile:
    """One table: row count, then per-column aggregates."""
    q = dialect.quote
    name = dialect.qualify(table)
    profile = TableProfile(table=table)
    profile.rows = int(_scalar(run, f"SELECT count(*) FROM {name}"))

    for column in columns:
        col = q(column.name)
        cp = ColumnProfile(column=column, rows=profile.rows)

        if profile.rows == 0:
            # Nothing to measure, and the emptiness is itself the fact.
            profile.columns.append(cp)
            continue

        cp.nulls = int(
            _scalar(run, f"SELECT count(*) FROM {name} WHERE {col} IS NULL")
        )
        if cp.nulls == cp.rows:
            # Every aggregate below is degenerate on an all-null column, and
            # "always null" is the fact worth emitting.
            profile.columns.append(cp)
            continue

        # Type guards, not optimisations. A probe that a product rejects
        # aborts the surrounding transaction in Postgres, so the profiler emits
        # only SQL that can succeed rather than catching failures.
        countable = dialect.supports_distinct(column.declared_type)
        if countable:
            # Counted up to a cap rather than exactly. Measured against 78M
            # rows, an exact `count(DISTINCT)` was 78% of total profiling time
            # -- and no derivation ever reads the exact number. They ask only
            # whether the column is small enough to enumerate, so the probe
            # stops as soon as it knows the answer is "no": 8.7s -> 1.4s on the
            # worst column, ~1ms on a low-cardinality one.
            # `IS NOT NULL` is load-bearing, not tidiness. `count(DISTINCT c)`
            # excludes NULL; `SELECT DISTINCT c` returns it as a row. Swapping
            # one for the other without this predicate inflates the distinct
            # count by exactly one on every nullable column -- which silently
            # reclassified a constant column as enumerated, and shifted the
            # enum threshold off by one everywhere. Found by reading output,
            # not by a test: the test fakes did not model NULL.
            bounded = dialect.limit(
                f"SELECT DISTINCT {col} FROM {name} WHERE {col} IS NOT NULL",
                ENUM_MAX_DISTINCT + 1,
            )
            cp.distinct = int(
                _scalar(run, f"SELECT count(*) FROM ({bounded}) s")
            )
            cp.distinct_capped = cp.distinct > ENUM_MAX_DISTINCT

        if dialect.supports_min_max(column.declared_type):
            bounds = run(
                f"SELECT min({col}), max({col}) FROM {name} "
                f"WHERE {col} IS NOT NULL"
            )
            if bounds:
                cp.minimum, cp.maximum = bounds[0][0], bounds[0][1]

        if countable and 0 < cp.distinct <= ENUM_MAX_DISTINCT:
            counted = run(
                f"SELECT {col}, count(*) FROM {name} WHERE {col} IS NOT NULL "
                f"GROUP BY {col} ORDER BY count(*) DESC"
            )
            cp.observed_values = tuple((str(r[0]), int(r[1])) for r in counted)

        sample_sql = dialect.limit(
            f"SELECT {col} FROM {name} WHERE {col} IS NOT NULL", SAMPLE_SIZE
        )
        cp.samples = tuple(str(r[0]) for r in run(sample_sql))
        profile.columns.append(cp)

    return profile


def profile_join(
    run: Runner, dialect: Dialect, fk: ForeignKey
) -> JoinProfile:
    """Measure what fraction of non-null values actually resolve.

    Counted against *non-null* values, not all rows. A column that is 90% NULL
    and resolves every time it is populated is a different problem from one
    that is fully populated and resolves 29% of the time, and collapsing them
    into a single "match rate" hides which one you have.
    """
    q = dialect.quote
    src = dialect.qualify(fk.column.table)
    tgt = dialect.qualify(fk.target)
    col, tcol = q(fk.column.name), q(fk.target_column)

    profile = JoinProfile(foreign_key=fk)
    profile.non_null = int(
        _scalar(run, f"SELECT count(*) FROM {src} WHERE {col} IS NOT NULL")
    )
    if profile.non_null == 0:
        return profile
    profile.matched = int(
        _scalar(
            run,
            f"SELECT count(*) FROM {src} s JOIN {tgt} t "
            f"ON s.{col} = t.{tcol}",
        )
    )
    return profile


def profile_database(
    run: Runner,
    dialect: Dialect,
    include_soft_keys: bool = True,
    infer_joins_by_value: bool = True,
    probe_budget: int = 600,
) -> tuple[list[TableProfile], list[JoinProfile]]:
    """Profile every table the dialect can see.

    `infer_joins_by_value` measures value overlap to find joins whose column
    name does not contain the target table's name. It is on by default because
    the motivating incident is exactly that shape -- a `clientid` resolving
    against a *customer* table -- and name-based inference cannot see it. The
    cost is one bounded query per (candidate column, primary key) pair, capped
    by `probe_budget`; turn it off for a very wide schema, or raise the budget.
    The default was raised from 200 after a 71-table OLTP schema left 156 pairs
    untested.
    """
    tables = dialect.tables(run)
    columns_by_table = {t.name: dialect.columns(run, t) for t in tables}

    table_profiles = [
        profile_table(run, dialect, t, columns_by_table[t.name]) for t in tables
    ]

    declared: list[ForeignKey] = []
    for table in tables:
        declared.extend(dialect.foreign_keys(run, table))

    keys = list(declared)
    if include_soft_keys:
        keys += infer_soft_keys(dialect, tables, columns_by_table, declared)
    if infer_joins_by_value:
        by_value, skipped = infer_by_value_overlap(
            run, dialect, tables, columns_by_table, keys,
            probe_budget=probe_budget,
        )
        keys += by_value
        if skipped:
            # No silent caps: a truncated search that reports itself as
            # complete is how a profiler quietly becomes untrustworthy.
            import warnings
            warnings.warn(
                f"join inference hit its probe budget; {skipped} candidate "
                "pair(s) were not tested. Pairs are probed in order of name "
                "affinity, so the most plausible were covered — but coverage "
                "is not complete. Raise probe_budget to finish the search.",
                stacklevel=2,
            )

    non_empty = {p.table.name for p in table_profiles if p.rows > 0}
    join_profiles = [
        profile_join(run, dialect, fk)
        for fk in keys
        if fk.column.table.name in non_empty and fk.target.name in non_empty
    ]

    return table_profiles, join_profiles
