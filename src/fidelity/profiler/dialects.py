"""Per-product introspection.

Statistical probes are ANSI SQL and live in `probe.py`; only the parts that
genuinely differ between products are here. That split keeps the surface a new
dialect has to implement small — list tables, list columns, list foreign keys,
quote an identifier, cap a result set — and keeps the interesting logic in one
place rather than duplicated per product.

SQLite ships first because it is in the standard library, which means the
profiler has real end-to-end tests against a real database with no new
dependency. It is also the most *different* from `information_schema`, so an
abstraction that survives it is unlikely to be secretly Postgres-shaped.
"""

from __future__ import annotations

from typing import Protocol, Sequence

from .model import ColumnRef, ForeignKey, Runner, TableRef


class Dialect(Protocol):
    """What the profiler needs to know about one database product."""

    name: str

    # One paragraph telling a model how to write SQL for this product. It lives
    # on the dialect because the dialect is already the single place that knows
    # this product's syntax -- a lookup table in the application would be a
    # second copy to keep in step. Measured need: asked a question against SQL
    # Server, a model opened with `information_schema.tables WHERE table_schema
    # = 'public'`, which is Postgres idiom, got zero rows, and had to recover.
    prompt_note: str

    def quote(self, identifier: str) -> str: ...

    def qualify(self, table: TableRef) -> str:
        """The table as it must appear in a FROM clause.

        Separate from `quote` because a schema-qualified product needs both
        parts quoted independently. SQLite has no schemas and returns the bare
        quoted name; Postgres returns `"schema"."table"`. Assuming the
        unqualified form is a silent correctness bug on every product that has
        schemas, which is most of them.
        """
        ...

    def limit(self, sql: str, n: int) -> str: ...

    def supports_min_max(self, declared_type: str) -> bool:
        """Whether `min()`/`max()` aggregates exist for this type.

        Not the same as "is the type ordered". Postgres orders booleans but has
        no `min(boolean)` aggregate, and `min()` on `json` or an array fails
        outright. The profiler cannot recover by catching the error: in Postgres
        a failed statement aborts the surrounding transaction, so a probe that
        might fail would force every caller to manage rollback. Emitting only
        SQL that can succeed is the alternative.
        """
        ...

    def supports_distinct(self, declared_type: str) -> bool:
        """Whether the type has an equality operator, for `count(DISTINCT)`.

        `json` does not; `jsonb` does. Same reasoning as above.
        """
        ...

    def type_family(self, declared_type: str) -> str:
        """Coarse comparability class: `integer`, `text`, `uuid`, or `other`.

        Used to decide whether two columns could plausibly join. A star schema
        made this necessary: `DimEmployee.LoginID` is an `nvarchar` holding
        `adventure-works\\alan0` and ends in "ID", so name-shaped candidate
        selection offered it up to be joined against integer surrogate keys.
        SQL Server attempted the implicit conversion and failed the statement.

        Postgres and MySQL never surfaced it only because their sample schemas
        happened to use integers for every id-shaped column. `other` never
        matches anything, including itself — an unknown type is not evidence
        of compatibility.
        """
        ...

    def tables(self, run: Runner) -> list[TableRef]: ...

    def columns(self, run: Runner, table: TableRef) -> list[ColumnRef]: ...

    def foreign_keys(self, run: Runner, table: TableRef) -> list[ForeignKey]: ...


class SQLiteDialect:
    """SQLite, via `sqlite_master` and the `PRAGMA` family."""

    name = "sqlite"
    prompt_note = (
        "This connection is **SQLite**. Identifiers are quoted with double "
        "quotes. There are no schemas — tables are referenced by bare name. "
        "List tables with `SELECT name FROM sqlite_master WHERE type='table'`. "
        "Row limits use `LIMIT n`. There is no `FULL OUTER JOIN` and no "
        "`RIGHT JOIN` before 3.39."
    )

    def quote(self, identifier: str) -> str:
        # Doubling embedded quotes is the escape, per the SQL standard. This is
        # the only place identifiers from the database reach generated SQL, so
        # it is the only place that has to be right.
        escaped = identifier.replace('"', '""')
        return f'"{escaped}"'

    def qualify(self, table: TableRef) -> str:
        return self.quote(table.name)

    def limit(self, sql: str, n: int) -> str:
        return f"{sql} LIMIT {int(n)}"

    def supports_min_max(self, declared_type: str) -> bool:
        # SQLite is dynamically typed and applies min/max to anything.
        return True

    def supports_distinct(self, declared_type: str) -> bool:
        return True

    def type_family(self, declared_type: str) -> str:
        # SQLite type affinity rules, which are prefix-based by design.
        t = (declared_type or "").upper()
        if "INT" in t:
            return "integer"
        if any(k in t for k in ("CHAR", "CLOB", "TEXT")):
            return "text"
        if any(k in t for k in ("REAL", "FLOA", "DOUB", "NUM", "DEC")):
            return "numeric"
        return "other"

    def tables(self, run: Runner) -> list[TableRef]:
        rows = run(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        return [TableRef(name=r[0]) for r in rows]

    def columns(self, run: Runner, table: TableRef) -> list[ColumnRef]:
        rows = run(f"PRAGMA table_info({self.quote(table.name)})")
        # cid, name, type, notnull, dflt_value, pk
        return [
            ColumnRef(
                table=table,
                name=r[1],
                declared_type=(r[2] or "").upper() or "UNKNOWN",
                nullable=not bool(r[3]),
                primary_key=bool(r[5]),
            )
            for r in rows
        ]

    def foreign_keys(self, run: Runner, table: TableRef) -> list[ForeignKey]:
        rows = run(f"PRAGMA foreign_key_list({self.quote(table.name)})")
        # id, seq, table, from, to, on_update, on_delete, match
        out: list[ForeignKey] = []
        for r in rows:
            target, from_col, to_col = r[2], r[3], r[4]
            out.append(
                ForeignKey(
                    column=ColumnRef(
                        table=table, name=from_col, declared_type="UNKNOWN"
                    ),
                    target=TableRef(name=target),
                    target_column=to_col or "rowid",
                    declared=True,
                    inferred_by="declared",
                )
            )
        return out


def compatible(dialect: Dialect, a: ColumnRef, b: ColumnRef) -> bool:
    """Whether two columns could plausibly be joined.

    A join between incompatible types is not merely useless — on SQL Server it
    fails the statement outright, and on Postgres a failed statement aborts the
    surrounding transaction. The profiler emits only SQL that can succeed, so
    the check happens before the probe rather than around it.
    """
    fa = dialect.type_family(a.declared_type)
    fb = dialect.type_family(b.declared_type)
    return fa == fb and fa != "other"


def infer_soft_keys(
    dialect: Dialect,
    tables: Sequence[TableRef],
    columns_by_table: dict[str, Sequence[ColumnRef]],
    declared: Sequence[ForeignKey],
) -> list[ForeignKey]:
    """Guess joins the schema never declared, by naming convention.

    A column named `<something>_id` or `<something>id` is treated as pointing at
    a table whose name matches `<something>` (singular or plural), when such a
    table exists and no declared constraint already covers the column.

    These are the ones worth measuring. A declared foreign key is enforced by
    the database and will hit 100% by construction; a soft key is a convention
    somebody remembered, and the source system had one that resolved 29% of the
    time with nothing in the schema to warn anyone.

    **This finds only the easy half.** It requires the column name to contain
    the target table's name, and the motivating incident does not: a `clientid`
    column pointing at a *customer* table is exactly the shape naming
    conventions miss. `infer_by_value_overlap` covers that case by measuring
    instead of guessing, at the cost of real queries.
    """
    by_name = {t.name.lower(): t for t in tables}
    already = {(fk.column.table.name, fk.column.name) for fk in declared}
    out: list[ForeignKey] = []

    def sole_primary_key(table: TableRef) -> ColumnRef | None:
        """The target's actual primary key, or None if it has 0 or >1.

        An earlier version assumed every table's key was called `id`, which is
        true of hand-written fixtures and false of real schemas -- the first
        live database used `store_id`, `film_id`, and so on, and every inferred
        join failed with "column t.id does not exist". Composite keys are
        skipped rather than guessed at: a single-column soft key cannot express
        them.
        """
        keys = [c for c in columns_by_table.get(table.name, ())
                if c.primary_key]
        return keys[0] if len(keys) == 1 else None

    for table in tables:
        for column in columns_by_table.get(table.name, ()):
            if (table.name, column.name) in already:
                continue
            lowered = column.name.lower()
            if not lowered.endswith("id") or lowered in ("id", "rowid"):
                continue
            stem = lowered[:-2].rstrip("_")
            if not stem:
                continue
            target = by_name.get(stem) or by_name.get(f"{stem}s")
            if target is None or target.name == table.name:
                continue
            target_key = sole_primary_key(target)
            if target_key is None:
                continue
            if not compatible(dialect, column, target_key):
                continue
            out.append(
                ForeignKey(
                    column=column,
                    target=target,
                    target_column=target_key.name,
                    declared=False,
                    inferred_by="name",
                )
            )
    return out


def infer_by_value_overlap(
    run: Runner,
    dialect: Dialect,
    tables: Sequence[TableRef],
    columns_by_table: dict[str, Sequence[ColumnRef]],
    covered: Sequence[ForeignKey] = (),
    probe_budget: int = 200,
    sample: int = 200,
    min_overlap: float = 0.05,
) -> tuple[list[ForeignKey], int]:
    """Find joins by measuring value overlap rather than guessing from names.

    Necessary because the failure worth catching is the one naming conventions
    miss. A `clientid` column that resolves against a *customer* table has no
    name in common with its target, and that is the case `DESIGN.md` cites as
    having been found by hand after an incident.

    For each unclaimed id-shaped column, a bounded sample of its distinct values
    is joined against each candidate primary key. Cost is one query per
    (column, target) pair, so it is capped by `probe_budget`.

    Candidates are probed in order of name affinity, so a truncated search
    spends its budget on the most plausible pairs rather than on whatever the
    catalogue happened to list first.

    Returns the inferred keys **and the number of probes skipped** when the
    budget ran out. A truncated search that reports itself as complete is how a
    profiler quietly becomes untrustworthy — the caller is told, and the
    document can say so.
    """
    claimed = {(fk.column.table.name, fk.column.name) for fk in covered}
    targets: list[tuple[TableRef, ColumnRef]] = []
    for table in tables:
        for column in columns_by_table.get(table.name, ()):
            if column.primary_key:
                targets.append((table, column))

    candidates: list[ColumnRef] = []
    for table in tables:
        for column in columns_by_table.get(table.name, ()):
            if (table.name, column.name) in claimed or column.primary_key:
                continue
            if column.name.lower().endswith("id"):
                candidates.append(column)

    q = dialect.quote
    found: list[ForeignKey] = []
    spent = skipped = 0

    def affinity(column: ColumnRef, target: TableRef) -> int:
        """Probe the most plausible targets first.

        The budget is finite, so the order it is spent in decides what a
        truncated search finds. Measured on a 71-table OLTP schema, 156 of
        ~356 candidate pairs went untested — arbitrary order would have made
        that a coin flip. Name affinity is a weak signal (it is exactly what
        `infer_soft_keys` already exhausted) but it is better than none, and it
        costs nothing.
        """
        col = column.name.lower().removesuffix("id").rstrip("_")
        name = target.name.lower()
        if not col:
            return 3
        if col == name or f"{col}s" == name:
            return 0
        if col in name or name in col:
            return 1
        return 2

    for column in candidates:
        best: tuple[float, TableRef, ColumnRef] | None = None
        ordered = sorted(targets, key=lambda t: (affinity(column, t[0]), t[0].name))
        for target_table, target_col in ordered:
            if target_table.name == column.table.name:
                continue
            if not compatible(dialect, column, target_col):
                continue
            if spent >= probe_budget:
                skipped += 1
                continue
            spent += 1
            inner = dialect.limit(
                f"SELECT DISTINCT {q(column.name)} AS v "
                f"FROM {dialect.qualify(column.table)} "
                f"WHERE {q(column.name)} IS NOT NULL",
                sample,
            )
            rows = run(
                f"SELECT count(*) FROM ({inner}) s "
                f"JOIN {dialect.qualify(target_table)} t "
                f"ON s.v = t.{q(target_col.name)}"
            )
            matched = int(rows[0][0]) if rows and rows[0][0] is not None else 0
            total = run(f"SELECT count(*) FROM ({inner}) s")
            denom = int(total[0][0]) if total and total[0][0] else 0
            if not denom:
                continue
            rate = matched / denom
            if rate >= min_overlap and (best is None or rate > best[0]):
                best = (rate, target_table, target_col)
        if best is not None:
            found.append(
                ForeignKey(
                    column=column,
                    target=best[1],
                    target_column=best[2].name,
                    declared=False,
                    inferred_by="value_overlap",
                )
            )
    return found, skipped


class PostgresDialect:
    """PostgreSQL, via `information_schema` plus `pg_enum` for enum types.

    **Verified against PostgreSQL 17** on two sample databases: `dvdrental`
    (15 tables) and `postgres_air` (10 tables, 78M rows, non-`public` schema).
    That run was the acceptance test and it found three defects no unit test
    had: `min(boolean)` does not exist, soft-key inference assumed every target
    key was named `id`, and the selectivity thresholds produced 50 facts for a
    15-table database. All fixed; see `runs/` and the tests.

    `information_schema` is standard, so MySQL and SQL Server dialects can reuse
    most of this — the differences are identifier quoting, the row-limit clause,
    and how each product exposes enumerated types.
    """

    name = "postgres"
    prompt_note = (
        "This connection is **PostgreSQL**. Identifiers are quoted with double "
        "quotes and are case-sensitive when quoted. Tables live in schemas; "
        "`public` is the default search path. Row limits use `LIMIT n` and may "
        "be combined with `OFFSET n`. String concatenation is `||`. Casting is "
        "`value::type`. Date arithmetic uses `INTERVAL`."
    )

    # Schemas that belong to the server, not to the user's data.
    SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")

    # Types with no min()/max() aggregate, or none that means anything.
    # Found the hard way: the first live run crashed on `min(boolean)`, which
    # SQLite had accepted silently for every fixture written until then.
    NO_MIN_MAX = frozenset({
        "boolean", "json", "jsonb", "bytea", "xml", "point", "line", "lseg",
        "box", "path", "polygon", "circle", "tsvector", "tsquery",
        "user-defined", "array", "record", "void",
    })
    # Types with no equality operator, so no count(DISTINCT) and no GROUP BY.
    NO_EQUALITY = frozenset({"json", "xml", "point", "line", "lseg", "box",
                             "path", "polygon", "circle"})

    def _base_type(self, declared_type: str) -> str:
        return (declared_type or "").strip().lower()

    def supports_min_max(self, declared_type: str) -> bool:
        return self._base_type(declared_type) not in self.NO_MIN_MAX

    def supports_distinct(self, declared_type: str) -> bool:
        return self._base_type(declared_type) not in self.NO_EQUALITY

    _INTEGER = frozenset({"smallint", "integer", "bigint", "smallserial",
                          "serial", "bigserial", "int2", "int4", "int8"})
    _TEXT = frozenset({"text", "character varying", "character", "varchar",
                       "char", "name", "citext"})
    _NUMERIC = frozenset({"numeric", "decimal", "real", "double precision"})

    def type_family(self, declared_type: str) -> str:
        t = self._base_type(declared_type)
        if t in self._INTEGER:
            return "integer"
        if t in self._TEXT:
            return "text"
        if t == "uuid":
            return "uuid"
        if t in self._NUMERIC:
            return "numeric"
        return "other"

    def quote(self, identifier: str) -> str:
        escaped = identifier.replace('"', '""')
        return f'"{escaped}"'

    def qualify(self, table: TableRef) -> str:
        if table.schema:
            return f"{self.quote(table.schema)}.{self.quote(table.name)}"
        return self.quote(table.name)

    def limit(self, sql: str, n: int) -> str:
        return f"{sql} LIMIT {int(n)}"

    def _literal(self, value: str) -> str:
        """Single-quoted SQL literal.

        These are catalog values (schema and table names) read back from the
        server, not user input — but they are still interpolated into SQL, so
        they are escaped. A profiler that trusted its own catalog reads would
        break on a table whose name contains a quote, which is legal.
        """
        return "'" + value.replace("'", "''") + "'"

    def tables(self, run: Runner) -> list[TableRef]:
        excluded = ", ".join(self._literal(s) for s in self.SYSTEM_SCHEMAS)
        rows = run(
            "SELECT table_schema, table_name FROM information_schema.tables "
            f"WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ({excluded}) "
            "ORDER BY table_schema, table_name"
        )
        return [TableRef(name=r[1], schema=r[0]) for r in rows]

    def columns(self, run: Runner, table: TableRef) -> list[ColumnRef]:
        schema = self._literal(table.schema or "public")
        name = self._literal(table.name)

        rows = run(
            "SELECT c.column_name, c.data_type, c.is_nullable, c.udt_name "
            "FROM information_schema.columns c "
            f"WHERE c.table_schema = {schema} AND c.table_name = {name} "
            "ORDER BY c.ordinal_position"
        )

        pk_rows = run(
            "SELECT k.column_name "
            "FROM information_schema.table_constraints t "
            "JOIN information_schema.key_column_usage k "
            "  ON k.constraint_name = t.constraint_name "
            " AND k.table_schema = t.table_schema "
            "WHERE t.constraint_type = 'PRIMARY KEY' "
            f"  AND t.table_schema = {schema} AND t.table_name = {name}"
        )
        primary = {r[0] for r in pk_rows}

        # Declared enum values. Present so the profiler can report a declared
        # value with zero rows -- FAILURES.md §4's "NOT IN (...) built on an
        # empty set is a no-op". CHECK-constraint enumerations are *not*
        # covered; parsing arbitrary CHECK expressions is a different job.
        enum_rows = run(
            "SELECT t.typname, e.enumlabel FROM pg_type t "
            "JOIN pg_enum e ON e.enumtypid = t.oid ORDER BY e.enumsortorder"
        )
        by_type: dict[str, list[str]] = {}
        for type_name, label in enum_rows:
            by_type.setdefault(type_name, []).append(label)

        return [
            ColumnRef(
                table=table,
                name=r[0],
                declared_type=(r[1] or "unknown").upper(),
                nullable=(r[2] or "YES").upper() == "YES",
                primary_key=r[0] in primary,
                declared_values=tuple(by_type.get(r[3], ())),
            )
            for r in rows
        ]

    def foreign_keys(self, run: Runner, table: TableRef) -> list[ForeignKey]:
        schema = self._literal(table.schema or "public")
        name = self._literal(table.name)
        rows = run(
            "SELECT k.column_name, c.table_schema, c.table_name, c.column_name "
            "FROM information_schema.table_constraints t "
            "JOIN information_schema.key_column_usage k "
            "  ON k.constraint_name = t.constraint_name "
            " AND k.table_schema = t.table_schema "
            "JOIN information_schema.constraint_column_usage c "
            "  ON c.constraint_name = t.constraint_name "
            "WHERE t.constraint_type = 'FOREIGN KEY' "
            f"  AND t.table_schema = {schema} AND t.table_name = {name}"
        )
        return [
            ForeignKey(
                column=ColumnRef(table=table, name=r[0], declared_type="UNKNOWN"),
                target=TableRef(name=r[2], schema=r[1]),
                target_column=r[3],
                declared=True,
                inferred_by="declared",
            )
            for r in rows
        ]


class MySQLDialect:
    """MySQL and MariaDB, via `information_schema`.

    Written third, and the first that tests whether this abstraction survives a
    change of *product* rather than a change of schema. Three things differ
    from Postgres in ways the Protocol has to absorb rather than paper over:

    * **Identifiers are backtick-quoted**, escaped by doubling.
    * **There is no schema layer.** MySQL's `table_schema` *is* the database, so
      `qualify` produces `` `db`.`table` `` and a "schema" here means something
      different from the Postgres case with the same name.
    * **Enumerations are a column type, not a separate object.** `ENUM` values
      live in `column_type` as `enum('G','PG',...)` and must be parsed out of
      it. This is the first dialect that can populate `declared_values`, which
      makes `FAILURES.md` §4 — a declared value with zero rows, so a
      `NOT IN (...)` filter excludes nothing — reachable for the first time.

    `database` scopes the profile. MySQL has no cross-database catalogue view
    worth walking, and profiling every database on a server is almost never
    what the caller wants.
    """

    name = "mysql"
    prompt_note = (
        "This connection is **MySQL**. Identifiers are quoted with backticks. "
        "A schema and a database are the same thing here, so qualify as "
        "`database`.`table` rather than looking for a separate schema. Row "
        "limits use `LIMIT n` or `LIMIT offset, n`. String concatenation is "
        "`CONCAT(a, b)` — `||` is boolean OR by default. Every derived table "
        "needs its own alias."
    )

    SYSTEM_SCHEMAS = ("information_schema", "mysql", "performance_schema", "sys")

    # Spatial types have no meaningful ordering or equality for these probes.
    _SPATIAL = frozenset({
        "geometry", "point", "linestring", "polygon", "multipoint",
        "multilinestring", "multipolygon", "geometrycollection",
    })
    NO_MIN_MAX = _SPATIAL | {"json"}
    NO_EQUALITY = _SPATIAL

    def __init__(self, database: str | None = None) -> None:
        self.database = database

    def quote(self, identifier: str) -> str:
        return "`" + identifier.replace("`", "``") + "`"

    def qualify(self, table: TableRef) -> str:
        if table.schema:
            return f"{self.quote(table.schema)}.{self.quote(table.name)}"
        return self.quote(table.name)

    def limit(self, sql: str, n: int) -> str:
        return f"{sql} LIMIT {int(n)}"

    def supports_min_max(self, declared_type: str) -> bool:
        return (declared_type or "").strip().lower() not in self.NO_MIN_MAX

    def supports_distinct(self, declared_type: str) -> bool:
        return (declared_type or "").strip().lower() not in self.NO_EQUALITY

    def type_family(self, declared_type: str) -> str:
        t = (declared_type or "").strip().lower()
        if t in {"tinyint", "smallint", "mediumint", "int", "integer", "bigint"}:
            return "integer"
        if t in {"char", "varchar", "text", "tinytext", "mediumtext",
                 "longtext", "enum"}:
            return "text"
        if t in {"decimal", "numeric", "float", "double"}:
            return "numeric"
        return "other"

    def _literal(self, value: str) -> str:
        return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"

    def _scope(self) -> str:
        if self.database:
            return f"table_schema = {self._literal(self.database)}"
        excluded = ", ".join(self._literal(s) for s in self.SYSTEM_SCHEMAS)
        return f"table_schema NOT IN ({excluded})"

    def tables(self, run: Runner) -> list[TableRef]:
        # BASE TABLE excludes views. A view's contents are derived, so
        # profiling one measures the underlying tables twice and reports facts
        # about a projection nobody stores.
        rows = run(
            "SELECT table_schema, table_name FROM information_schema.tables "
            f"WHERE table_type = 'BASE TABLE' AND {self._scope()} "
            "ORDER BY table_schema, table_name"
        )
        return [TableRef(name=r[1], schema=r[0]) for r in rows]

    @staticmethod
    def parse_enum_values(column_type: str) -> tuple[str, ...]:
        """Pull the declared values out of `enum('G','PG-13',...)`.

        Only ENUM. A `SET` column stores a *combination* of its declared
        values, so "declared but absent" would be wrong for it: a value can be
        present inside a combination while never appearing as a whole cell.
        """
        text = (column_type or "").strip()
        if not text.lower().startswith("enum(") or not text.endswith(")"):
            return ()
        body = text[5:-1]
        values: list[str] = []
        current: list[str] = []
        i, inside = 0, False
        while i < len(body):
            ch = body[i]
            if not inside:
                if ch == "'":
                    inside = True
                i += 1
                continue
            if ch == "'":
                if i + 1 < len(body) and body[i + 1] == "'":
                    current.append("'")   # doubled quote is a literal quote
                    i += 2
                    continue
                values.append("".join(current))
                current = []
                inside = False
                i += 1
                continue
            if ch == "\\" and i + 1 < len(body):
                current.append(body[i + 1])
                i += 2
                continue
            current.append(ch)
            i += 1
        return tuple(values)

    def columns(self, run: Runner, table: TableRef) -> list[ColumnRef]:
        schema = self._literal(table.schema or "")
        name = self._literal(table.name)
        rows = run(
            "SELECT column_name, data_type, is_nullable, column_key, column_type "
            "FROM information_schema.columns "
            f"WHERE table_schema = {schema} AND table_name = {name} "
            "ORDER BY ordinal_position"
        )
        return [
            ColumnRef(
                table=table,
                name=r[0],
                declared_type=(r[1] or "unknown").lower(),
                nullable=(r[2] or "YES").upper() == "YES",
                primary_key=(r[3] or "") == "PRI",
                declared_values=self.parse_enum_values(r[4] or ""),
            )
            for r in rows
        ]

    def foreign_keys(self, run: Runner, table: TableRef) -> list[ForeignKey]:
        schema = self._literal(table.schema or "")
        name = self._literal(table.name)
        rows = run(
            "SELECT column_name, referenced_table_schema, referenced_table_name, "
            "referenced_column_name FROM information_schema.key_column_usage "
            f"WHERE table_schema = {schema} AND table_name = {name} "
            "AND referenced_table_name IS NOT NULL"
        )
        return [
            ForeignKey(
                column=ColumnRef(table=table, name=r[0], declared_type="unknown"),
                target=TableRef(name=r[2], schema=r[1]),
                target_column=r[3],
                declared=True,
                inferred_by="declared",
            )
            for r in rows
        ]


class SQLServerDialect:
    """Microsoft SQL Server, via `INFORMATION_SCHEMA` and `sys` catalog views.

    The most useful product to add fourth, because its differences fall on the
    parts of the Protocol that Postgres and MySQL agreed on and therefore never
    tested:

    * **`[bracket]` quoting**, escaped by doubling the closing bracket.
    * **Row limiting is a prefix, not a suffix.** `TOP (n)` goes after SELECT;
      there is no `LIMIT`. `limit()` exists as a Protocol method rather than a
      formatting constant precisely so this fits — a dialect layer that had
      assumed a trailing clause would need reworking here.
    * **Three-part names.** `database.schema.table`; `TableRef.schema` carries
      the schema and the database is the connection's own.

    `OFFSET/FETCH` is the other option for limiting, but it requires `ORDER BY`,
    which would impose a sort the profiler does not need and cannot afford on a
    large table. `TOP` has no such requirement.
    """

    name = "sqlserver"
    prompt_note = (
        "This connection is **Microsoft SQL Server** (T-SQL). Identifiers are "
        "quoted with square brackets. Tables are qualified `schema.table`, and "
        "the default schema is `dbo`, not `public` — a query filtering "
        "`table_schema = 'public'` returns nothing here. Row limits use "
        "`SELECT TOP (n) ...`, or `ORDER BY ... OFFSET n ROWS FETCH NEXT n "
        "ROWS ONLY`; there is no `LIMIT`. String concatenation is `+`. "
        "`WITH` common table expressions must start the statement."
    )

    SYSTEM_SCHEMAS = ("sys", "INFORMATION_SCHEMA", "db_owner",
                      "db_accessadmin", "db_securityadmin", "db_ddladmin",
                      "db_backupoperator", "db_datareader", "db_datawriter",
                      "db_denydatareader", "db_denydatawriter")

    # No aggregate, or no meaningful ordering.
    NO_MIN_MAX = frozenset({
        "bit", "xml", "geography", "geometry", "hierarchyid", "sql_variant",
        "image", "ntext", "text",
    })
    # Types that cannot be compared for equality, so no DISTINCT and no GROUP BY.
    NO_EQUALITY = frozenset({"xml", "geography", "geometry", "image",
                             "ntext", "text"})

    def quote(self, identifier: str) -> str:
        return "[" + identifier.replace("]", "]]") + "]"

    def qualify(self, table: TableRef) -> str:
        if table.schema:
            return f"{self.quote(table.schema)}.{self.quote(table.name)}"
        return self.quote(table.name)

    def limit(self, sql: str, n: int) -> str:
        """Insert `TOP (n)` after the leading SELECT.

        A textual splice, which is only safe because every caller passes SQL
        this module generated -- see `probe.py`. It is not a general SQL
        rewriter and must not be handed arbitrary statements.
        """
        stripped = sql.lstrip()
        prefix = sql[: len(sql) - len(stripped)]
        head = "SELECT DISTINCT " if stripped.upper().startswith("SELECT DISTINCT ") \
            else "SELECT " if stripped.upper().startswith("SELECT ") else None
        if head is None:
            raise ValueError(f"cannot apply TOP to: {sql[:60]!r}")
        return f"{prefix}{head}TOP ({int(n)}) {stripped[len(head):]}"

    def supports_min_max(self, declared_type: str) -> bool:
        return (declared_type or "").strip().lower() not in self.NO_MIN_MAX

    def supports_distinct(self, declared_type: str) -> bool:
        return (declared_type or "").strip().lower() not in self.NO_EQUALITY

    def type_family(self, declared_type: str) -> str:
        t = (declared_type or "").strip().lower()
        if t in {"tinyint", "smallint", "int", "bigint"}:
            return "integer"
        if t in {"char", "varchar", "nchar", "nvarchar"}:
            return "text"
        if t == "uniqueidentifier":
            return "uuid"
        if t in {"decimal", "numeric", "float", "real", "money", "smallmoney"}:
            return "numeric"
        return "other"

    def _literal(self, value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def tables(self, run: Runner) -> list[TableRef]:
        excluded = ", ".join(self._literal(s) for s in self.SYSTEM_SCHEMAS)
        rows = run(
            "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            f"WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA NOT IN ({excluded}) "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME"
        )
        return [TableRef(name=r[1], schema=r[0]) for r in rows]

    def columns(self, run: Runner, table: TableRef) -> list[ColumnRef]:
        schema = self._literal(table.schema or "dbo")
        name = self._literal(table.name)
        rows = run(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            f"WHERE TABLE_SCHEMA = {schema} AND TABLE_NAME = {name} "
            "ORDER BY ORDINAL_POSITION"
        )
        pk_rows = run(
            "SELECT k.COLUMN_NAME "
            "FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS t "
            "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k "
            "  ON k.CONSTRAINT_NAME = t.CONSTRAINT_NAME "
            " AND k.TABLE_SCHEMA = t.TABLE_SCHEMA "
            "WHERE t.CONSTRAINT_TYPE = 'PRIMARY KEY' "
            f"  AND t.TABLE_SCHEMA = {schema} AND t.TABLE_NAME = {name}"
        )
        primary = {r[0] for r in pk_rows}
        return [
            ColumnRef(
                table=table,
                name=r[0],
                declared_type=(r[1] or "unknown").lower(),
                nullable=(r[2] or "YES").upper() == "YES",
                primary_key=r[0] in primary,
            )
            for r in rows
        ]

    def foreign_keys(self, run: Runner, table: TableRef) -> list[ForeignKey]:
        schema = self._literal(table.schema or "dbo")
        name = self._literal(table.name)
        # sys.foreign_key_columns rather than INFORMATION_SCHEMA: the standard
        # views need three joins to pair a referencing column with its target,
        # and get it wrong on composite keys by matching on constraint name
        # alone rather than on ordinal position.
        rows = run(
            "SELECT pc.name, rs.name, rt.name, rc.name "
            "FROM sys.foreign_key_columns fkc "
            "JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id "
            " AND pc.column_id = fkc.parent_column_id "
            "JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id "
            " AND rc.column_id = fkc.referenced_column_id "
            "JOIN sys.tables pt ON pt.object_id = fkc.parent_object_id "
            "JOIN sys.schemas ps ON ps.schema_id = pt.schema_id "
            "JOIN sys.tables rt ON rt.object_id = fkc.referenced_object_id "
            "JOIN sys.schemas rs ON rs.schema_id = rt.schema_id "
            f"WHERE ps.name = {schema} AND pt.name = {name}"
        )
        return [
            ForeignKey(
                column=ColumnRef(table=table, name=r[0], declared_type="unknown"),
                target=TableRef(name=r[2], schema=r[1]),
                target_column=r[3],
                declared=True,
                inferred_by="declared",
            )
            for r in rows
        ]
