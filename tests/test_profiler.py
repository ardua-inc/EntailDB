"""Tests for the schema profiler.

The centrepiece is `TestDesignExamples`: a real SQLite database built to
contain neutral analogues of all five facts `DESIGN.md` says the profiler
would have found automatically — each of which was, in the source system,
discovered by hand after a production incident. If the profiler cannot
rediscover them from data alone, pillar 2's claim is not supported.

SQLite is used deliberately: it is in the standard library, so these are real
end-to-end tests against a real database with no new dependency, and its
introspection (`sqlite_master`, `PRAGMA`) is maximally unlike
`information_schema` — an abstraction that survives it is unlikely to be
secretly Postgres-shaped.
"""

from __future__ import annotations

import sqlite3

import pytest

from fidelity.profiler import (
    ColumnProfile,
    ColumnRef,
    Fact,
    ForeignKey,
    JoinProfile,
    SQLiteDialect,
    TableProfile,
    TableRef,
    derive,
    dominant_format,
    infer_soft_keys,
    profile_database,
    render,
    shape_of,
)
from fidelity.profiler.facts import column_facts, join_facts, table_facts


def runner_for(conn: sqlite3.Connection):
    def run(sql: str):
        return conn.execute(sql).fetchall()
    return run


@pytest.fixture
def warehouse():
    """A database carrying a neutral analogue of each DESIGN.md example."""
    conn = sqlite3.connect(":memory:")
    c = conn.executescript

    c("""
    CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT);

    -- (1) `cost` mostly NULL and semantically abandoned
    -- (5) `full_name` follows a convention most of the time, not always
    CREATE TABLE products (
        id INTEGER PRIMARY KEY, sku TEXT, cost REAL, category TEXT
    );

    -- (3) `status` NULL on every row: WHERE status='ACTIVE' returns nothing
    CREATE TABLE employees (
        id INTEGER PRIMARY KEY, full_name TEXT, status TEXT
    );

    -- (4) exists in the schema, zero rows
    CREATE TABLE receipts (id INTEGER PRIMARY KEY, total REAL);

    -- (2) soft key: `client_id` resolves to a customer only sometimes
    CREATE TABLE service_requests (
        id INTEGER PRIMARY KEY, client_id INTEGER, channel TEXT
    );
    """)

    conn.executemany("INSERT INTO customers (id, email) VALUES (?, ?)",
                     [(i, f"user{i}@example.test") for i in range(1, 51)])
    # cost populated on 1 in 4
    conn.executemany(
        "INSERT INTO products (id, sku, cost, category) VALUES (?, ?, ?, ?)",
        [(i, f"HW-{4000+i}", (i * 1.5 if i % 4 == 0 else None),
          "hardware" if i % 2 else "garden") for i in range(1, 101)])
    # full_name: 87 of 100 follow "Last (N), First"; the rest do not
    names = ([f"Doe ({i}), Jane" for i in range(87)]
             + [f"unformatted name {i}" for i in range(13)])
    conn.executemany(
        "INSERT INTO employees (id, full_name, status) VALUES (?, ?, NULL)",
        [(i + 1, n) for i, n in enumerate(names)])
    # client_id resolves ~30% of the time
    conn.executemany(
        "INSERT INTO service_requests (id, client_id, channel) VALUES (?, ?, ?)",
        [(i, (i if i <= 30 else 9000 + i), "phone" if i % 3 else "web")
         for i in range(1, 101)])
    conn.commit()
    yield conn
    conn.close()


class TestDesignExamples:
    """All five DESIGN.md examples, rediscovered from data alone."""

    @pytest.fixture
    def facts(self, warehouse):
        tables, joins = profile_database(runner_for(warehouse), SQLiteDialect())
        return derive(tables, joins)

    def _kinds(self, facts, subject):
        return {f.kind for f in facts if f.subject == subject}

    def test_mostly_null_abandoned_column(self, facts):
        """`cost` is 75% NULL and semantically abandoned."""
        assert "mostly_null" in self._kinds(facts, "products.cost")
        f = next(f for f in facts if f.subject == "products.cost")
        assert f.evidence["null_rate"] == pytest.approx(0.75, abs=0.01)

    def test_soft_key_that_usually_misses(self, facts):
        """`client_id` resolves to a customer row only ~30% of the time."""
        f = next(f for f in facts if f.kind == "join_miss")
        assert f.subject == "service_requests.client_id"
        assert f.evidence["hit_rate"] == pytest.approx(0.30, abs=0.02)
        assert f.evidence["declared"] is False

    def test_all_null_column_is_blocking(self, facts):
        """`status` is NULL on every row, so a filter on it matches nothing."""
        f = next(f for f in facts if f.subject == "employees.status")
        assert f.kind == "always_null"
        assert f.severity == "blocking"
        assert "matches nothing" in f.statement

    def test_empty_table_is_blocking(self, facts):
        """A table that exists in the schema and has never had a row."""
        f = next(f for f in facts if f.subject == "receipts")
        assert f.kind == "empty_table"
        assert f.severity == "blocking"

    def test_format_convention_that_mostly_holds(self, facts):
        """`full_name` matches a shape 87% of the time — a convention."""
        f = next(f for f in facts
                 if f.subject == "employees.full_name" and f.kind == "format")
        assert f.evidence["coverage"] == pytest.approx(0.87, abs=0.02)
        assert "convention, not a constraint" in f.statement

    def test_document_stays_short(self, facts):
        """Selectivity is the product. A profiler that emits every statistic
        replaces one bloated prompt with a longer generated one."""
        doc = render(facts, database="warehouse")
        assert len(doc.splitlines()) < 40
        # Nothing about the well-behaved columns.
        assert "customers.email" not in doc
        assert "products.id" not in doc

    def test_document_declares_itself_generated(self, facts):
        doc = render(facts, database="warehouse")
        assert "do not edit" in doc.lower()
        assert "Digest:" in doc and "Generated:" in doc

    def test_digest_tracks_content(self, facts):
        a = render(facts, database="w")
        b = render(facts[:-1], database="w")
        digest = lambda d: [l for l in d.splitlines() if l.startswith("Digest")][0]
        assert digest(a) != digest(b)


class TestShapeInference:
    @pytest.mark.parametrize("value,shape", [
        ("Doe (3), Jane", "Aa (9), Aa"),
        ("HW-4021", "A-9"),
        ("2026-08-11", "9-9-9"),
        ("", ""),
    ])
    def test_shape_of(self, value, shape):
        assert shape_of(value) == shape

    def test_too_few_samples_infers_nothing(self):
        assert dominant_format(["Doe (3), Jane"] * 5) is None

    def test_uniform_shape_is_not_reported(self):
        """All-identical shape usually means a typed column, which tells a
        reader nothing they did not already know from the type."""
        assert dominant_format(["2026-01-0%d" % (i % 9 + 1) for i in range(40)]) is None

    def test_mixed_shape_below_threshold_infers_nothing(self):
        samples = [f"{i}" for i in range(20)] + [f"x{i}" for i in range(20)]
        assert dominant_format(samples) is None


class TestSoftKeyInference:
    def test_infers_by_naming_convention(self):
        orders = TableRef("orders")
        customers = TableRef("customers")
        col = ColumnRef(orders, "customer_id", "INTEGER")
        pk = ColumnRef(customers, "id", "INTEGER", primary_key=True)
        keys = infer_soft_keys(SQLiteDialect(), [orders, customers],
                               {"orders": [col], "customers": [pk]}, [])
        assert len(keys) == 1
        assert keys[0].target == customers
        assert keys[0].target_column == "id"
        assert keys[0].declared is False

    def test_skips_columns_a_declared_key_already_covers(self):
        orders, customers = TableRef("orders"), TableRef("customers")
        col = ColumnRef(orders, "customer_id", "INTEGER")
        pk = ColumnRef(customers, "id", "INTEGER", primary_key=True)
        declared = [ForeignKey(col, customers, "id", declared=True)]
        assert infer_soft_keys(SQLiteDialect(), [orders, customers],
                               {"orders": [col], "customers": [pk]},
                               declared) == []

    def test_skips_own_primary_key(self):
        orders = TableRef("orders")
        col = ColumnRef(orders, "id", "INTEGER")
        assert infer_soft_keys(SQLiteDialect(), [orders], {"orders": [col]}, []) == []

    def test_skips_when_no_matching_table(self):
        orders = TableRef("orders")
        col = ColumnRef(orders, "widget_id", "INTEGER")
        assert infer_soft_keys(SQLiteDialect(), [orders], {"orders": [col]}, []) == []


class TestThresholds:
    def _col(self, **kw):
        c = ColumnRef(TableRef("t"), "c", "TEXT")
        return ColumnProfile(column=c, **kw)

    def test_well_behaved_column_produces_no_facts(self):
        p = self._col(rows=100, nulls=0, distinct=100)
        assert column_facts(p) == []

    def test_null_rate_just_below_threshold_is_silent(self):
        assert column_facts(self._col(rows=100, nulls=49, distinct=50)) == []

    def test_null_rate_at_threshold_reports(self):
        facts = column_facts(self._col(rows=100, nulls=50, distinct=50))
        assert [f.kind for f in facts] == ["mostly_null"]

    def test_constant_column(self):
        p = self._col(rows=100, nulls=0, distinct=1,
                      observed_values=(("ACTIVE", 100),))
        assert [f.kind for f in column_facts(p)] == ["constant"]

    def test_declared_value_with_zero_rows(self):
        """FAILURES.md §4: a NOT IN (...) built on an empty set is a no-op."""
        c = ColumnRef(TableRef("t"), "kind", "TEXT",
                      declared_values=("web", "phone", "kiosk"))
        p = ColumnProfile(column=c, rows=100, nulls=0, distinct=2,
                          observed_values=(("web", 60), ("phone", 40)))
        kinds = [f.kind for f in column_facts(p)]
        assert "declared_but_absent" in kinds
        f = next(f for f in column_facts(p) if f.kind == "declared_but_absent")
        assert f.evidence["unseen"] == ["kiosk"]

    def test_declared_key_at_full_hit_rate_is_silent(self):
        fk = ForeignKey(ColumnRef(TableRef("a"), "b_id", "INT"),
                        TableRef("b"), "id", declared=True)
        assert join_facts(JoinProfile(fk, non_null=100, matched=100)) == []

    def test_soft_key_slightly_below_perfect_reports(self):
        """A soft key is a remembered convention, so near-misses matter."""
        fk = ForeignKey(ColumnRef(TableRef("a"), "b_id", "INT"),
                        TableRef("b"), "id", declared=False)
        facts = join_facts(JoinProfile(fk, non_null=100, matched=94))
        assert [f.kind for f in facts] == ["join_miss"]

    def test_empty_table_short_circuits_column_facts(self):
        t = TableProfile(table=TableRef("t"), rows=0)
        assert [f.kind for f in table_facts(t)] == ["empty_table"]


class TestRendering:
    def test_clean_database_says_so(self):
        doc = render([], database="w")
        assert "No notable facts" in doc

    def test_blocking_facts_come_first(self):
        facts = derive([], [])
        info = Fact("a", "format", "shape thing", severity="info")
        blocking = Fact("b", "empty_table", "empty thing", severity="blocking")
        ordered = derive([], [])
        doc = render(sorted([info, blocking],
                            key=lambda f: {"blocking": 0, "info": 2}[f.severity]),
                     database="w")
        assert doc.index("empty thing") < doc.index("shape thing")


class TestLibraryBoundary:
    """DESIGN.md: the library never learns what a connection or a user is."""

    def test_profiler_imports_no_driver(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent / "src" / "fidelity"
        for path in (root / "profiler").rglob("*.py"):
            source = path.read_text()
            for banned in ("import sqlite3", "import psycopg", "os.environ",
                           "getenv", "import requests"):
                assert banned not in source, f"{path.name} contains {banned!r}"

    def test_profiler_issues_no_writes(self):
        """Read-only as a property of the code, not a promise in a docstring.

        Checked over string *literals* rather than raw text, and skipping
        docstrings. The original was a text grep and produced false positives
        on its own documentation — "Insert `TOP (n)` after the leading SELECT"
        and a `createdb` line in a usage example. A guard that fires on prose
        gets weakened or deleted the first time it is inconvenient, so it is
        worth making precise rather than loud.
        """
        import ast
        import pathlib
        import re

        dml = re.compile(
            r"^\s*(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|GRANT|"
            r"REVOKE|MERGE)\b",
            re.IGNORECASE,
        )
        root = pathlib.Path(__file__).resolve().parent.parent / "src" / "fidelity"
        for path in (root / "profiler").rglob("*.py"):
            tree = ast.parse(path.read_text())
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    body = getattr(node, "body", None)
                    if (body and isinstance(body[0], ast.Expr)
                            and isinstance(body[0].value, ast.Constant)
                            and isinstance(body[0].value.value, str)):
                        docstrings.add(id(body[0].value))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and id(node) not in docstrings):
                    assert not dml.match(node.value), (
                        f"{path.name}:{node.lineno} builds SQL that writes: "
                        f"{node.value[:60]!r}"
                    )


class TestKeyProvenance:
    """How a join was found changes what the statement means."""

    def test_value_inferred_key_says_the_schema_does_not_record_it(self, warehouse):
        tables, joins = profile_database(runner_for(warehouse), SQLiteDialect())
        f = next(f for f in derive(tables, joins) if f.kind == "join_miss")
        assert f.evidence["inferred_by"] == "value_overlap"
        assert "nothing in the schema records it" in f.statement

    def test_name_inferred_key_says_so(self):
        fk = ForeignKey(ColumnRef(TableRef("orders"), "customer_id", "INT"),
                        TableRef("customers"), "id",
                        declared=False, inferred_by="name")
        f = join_facts(JoinProfile(fk, non_null=100, matched=40))[0]
        assert "inferred from the column name" in f.statement

    def test_declared_key_says_so(self):
        fk = ForeignKey(ColumnRef(TableRef("a"), "b_id", "INT"),
                        TableRef("b"), "id", declared=True)
        f = join_facts(JoinProfile(fk, non_null=100, matched=40))[0]
        assert "declared foreign key" in f.statement


class TestHeaderCarriesNoInstruction:
    """The document must not smuggle in behavioural guidance.

    If the header tells the model anything about how to treat figures, the
    profiler stops being separable from a prompt intervention and its measured
    effect cannot be attributed to the facts it carries.
    """

    def test_header_is_provenance_only(self):
        doc = render([], database="w")
        header = doc.split("---")[0].lower()
        for hint in ("recite", "recited", "fabricat", "do not state",
                     "verify", "stale", "current fact"):
            assert hint not in header, f"header hints at behaviour: {hint!r}"

    def test_header_still_carries_provenance(self):
        doc = render([], database="w")
        for required in ("Database:", "Generated:", "Digest:", "do not edit"):
            assert required in doc


class TestNoMagnitudesInStatements:
    """Measured correction: a generated document carrying counts becomes a new
    source of prompt-embedded figures a model will recite when it cannot query.

    Observed directly — with an earlier version in the system prompt, an answer
    read "based on the documented data facts, `service_requests` contains 120
    rows". Counts now live in `evidence`, which never reaches a prompt.
    """

    def test_always_null_statement_omits_the_row_count(self):
        c = ColumnRef(TableRef("t"), "status", "TEXT")
        p = ColumnProfile(column=c, rows=120, nulls=120)
        f = column_facts(p)[0]
        assert "120" not in f.statement
        assert "every row" in f.statement
        assert f.evidence["rows"] == 120  # still available to tooling

    def test_enumerated_statement_omits_per_value_counts(self):
        c = ColumnRef(TableRef("t"), "category", "TEXT")
        p = ColumnProfile(column=c, rows=120, nulls=0, distinct=3,
                          observed_values=(("repair", 40), ("install", 40),
                                           ("inspection", 40)))
        f = next(f for f in column_facts(p) if f.kind == "enumerated")
        assert "40" not in f.statement
        assert "repair, install, inspection" in f.statement
        assert f.evidence["values"] == {"repair": 40, "install": 40,
                                        "inspection": 40}

    def test_reliability_rates_are_kept(self):
        """A null rate is not an answer to a business question, and dropping it
        would remove what makes the fact actionable."""
        c = ColumnRef(TableRef("t"), "cost", "REAL")
        f = column_facts(ColumnProfile(column=c, rows=100, nulls=75,
                                       distinct=25))[0]
        assert "75%" in f.statement

    def test_rendered_document_contains_no_bare_counts(self, warehouse):
        tables, joins = profile_database(runner_for(warehouse), SQLiteDialect())
        doc = render(derive(tables, joins), database="w")
        body = doc.split("---", 1)[1]
        import re
        # Percentages are fine; standalone integers of 2+ digits are not.
        stripped = re.sub(r"\d+%", "", body)
        stripped = re.sub(r"`[^`]*`", "", stripped)   # identifiers, shapes
        assert not re.search(r"\b\d{2,}\b", stripped), stripped


class TestPostgresDialect:
    """Unit-level only. Postgres has not been run against a live server, and
    these tests verify SQL shape and row parsing — not that a real server
    accepts the SQL. The first live run is the actual acceptance test.
    """

    def _recorder(self, responses):
        """Returns rows by matching a substring of the query, and records SQL."""
        seen = []

        def run(sql):
            seen.append(sql)
            for needle, rows in responses.items():
                if needle in sql:
                    return rows
            return []

        return run, seen

    def _dialect(self):
        from fidelity.profiler import PostgresDialect
        return PostgresDialect()

    def test_qualifies_with_schema(self):
        d = self._dialect()
        assert d.qualify(TableRef("orders", "sales")) == '"sales"."orders"'
        assert d.qualify(TableRef("orders")) == '"orders"'

    def test_quoting_escapes_embedded_quotes(self):
        assert self._dialect().quote('we"ird') == '"we""ird"'

    def test_system_schemas_are_excluded(self):
        run, seen = self._recorder({"information_schema.tables": [("public", "orders")]})
        tables = self._dialect().tables(run)
        assert tables == [TableRef("orders", "public")]
        assert "pg_catalog" in seen[0] and "information_schema'" in seen[0]

    def test_catalog_values_are_escaped_into_sql(self):
        """Catalog reads are still interpolated, and a quote in a table name
        is legal. A profiler that trusted its own catalog reads would break."""
        run, seen = self._recorder({})
        self._dialect().columns(run, TableRef("we'ird", "public"))
        assert "'we''ird'" in seen[0]

    def test_columns_carry_nullability_pk_and_enum_values(self):
        run, _ = self._recorder({
            "information_schema.columns": [
                ("id", "integer", "NO", "int4"),
                ("channel", "USER-DEFINED", "YES", "channel_kind"),
            ],
            "PRIMARY KEY": [("id",)],
            "pg_enum": [("channel_kind", "web"), ("channel_kind", "phone")],
        })
        cols = self._dialect().columns(run, TableRef("t", "public"))
        by_name = {c.name: c for c in cols}
        assert by_name["id"].primary_key and not by_name["id"].nullable
        assert by_name["channel"].nullable
        assert by_name["channel"].declared_values == ("web", "phone")
        assert by_name["id"].declared_values == ()

    def test_foreign_keys_are_schema_qualified(self):
        run, _ = self._recorder({
            "FOREIGN KEY": [("customer_id", "sales", "customers", "id")],
        })
        fks = self._dialect().foreign_keys(run, TableRef("orders", "sales"))
        assert fks[0].target == TableRef("customers", "sales")
        assert fks[0].declared and fks[0].inferred_by == "declared"

    def test_probes_use_the_qualified_name(self):
        """The bug `qualify()` exists to prevent: probing `orders` instead of
        `sales.orders` silently profiles the wrong table, or errors."""
        from fidelity.profiler import profile_table
        run, seen = self._recorder({"count(*)": [(0,)]})
        table = TableRef("orders", "sales")
        profile_table(run, self._dialect(), table, [])
        assert '"sales"."orders"' in seen[0]


class TestRealSchemaKeyNames:
    """Regression from the first live Postgres run.

    `infer_soft_keys` assumed every table's primary key was named `id` — true
    of every fixture written until then, false of real schemas. dvdrental uses
    `store_id`, `film_id`, `customer_id`; every inferred join failed with
    "column t.id does not exist".
    """

    def test_target_key_is_read_from_the_schema(self):
        store = TableRef("store")
        customer = TableRef("customer")
        cols = {
            "store": [ColumnRef(store, "store_id", "INTEGER", primary_key=True)],
            "customer": [ColumnRef(customer, "store_id", "INTEGER")],
        }
        keys = infer_soft_keys(SQLiteDialect(), [store, customer], cols, [])
        assert len(keys) == 1
        assert keys[0].target_column == "store_id"

    def test_composite_key_target_is_skipped_not_guessed(self):
        pair = TableRef("pair")
        child = TableRef("child")
        cols = {
            "pair": [ColumnRef(pair, "a_id", "INT", primary_key=True),
                     ColumnRef(pair, "b_id", "INT", primary_key=True)],
            "child": [ColumnRef(child, "pair_id", "INT")],
        }
        assert infer_soft_keys(SQLiteDialect(), [pair, child], cols, []) == []

    def test_target_without_a_primary_key_is_skipped(self):
        log = TableRef("log")
        child = TableRef("child")
        cols = {"log": [ColumnRef(log, "note", "TEXT")],
                "child": [ColumnRef(child, "log_id", "INT")]}
        assert infer_soft_keys(SQLiteDialect(), [log, child], cols, []) == []


class TestSelectivityAgainstRealSchemas:
    """Regressions from the first live run against dvdrental.

    The SQLite fixture was tuned to yield exactly the five facts DESIGN.md
    names, so it validated the derivation logic and nothing about restraint.
    A real 15-table sample database produced **50 facts**, most of them noise.
    Each test below pins one cause.
    """

    def _col(self, rows, **kw):
        c = ColumnRef(TableRef("t"), "c", "TEXT",
                      primary_key=kw.pop("primary_key", False))
        return ColumnProfile(column=c, rows=rows, **kw)

    def test_tiny_table_yields_no_distribution_facts(self):
        """A two-row table makes every column constant and every column
        enumerated. That is the table, not a finding."""
        p = self._col(2, nulls=0, distinct=1, observed_values=(("Mike", 2),))
        assert column_facts(p) == []

    def test_primary_key_is_never_enumerated(self):
        """All-distinct by definition; listing its values is a data dump."""
        p = self._col(200, nulls=0, distinct=2, primary_key=True,
                      observed_values=(("1", 100), ("2", 100)))
        assert column_facts(p) == []

    def test_uniform_shape_is_not_a_convention(self):
        """Rounded display turned 99.5% into "matches in 100% of values — the
        remainder does not", which is self-contradictory."""
        samples = ["Smith"] * 199 + ["O'Neill"]
        assert dominant_format(samples) is None

    def test_shape_convention_in_the_middle_still_reports(self):
        samples = ["12 Oak Street"] * 74 + [f"PO Box {i}" for i in range(26)]
        assert dominant_format(samples) is not None

    def test_repeated_findings_roll_up(self):
        facts = [Fact(f"t.col{i}", "constant", f"col{i} is constant")
                 for i in range(14)]
        rolled = derive([], [])
        from fidelity.profiler.facts import _rollup
        out = _rollup(facts)
        assert len(out) == 1
        assert "14 columns" in out[0].statement
        assert out[0].evidence["columns"] == [f"t.col{i}" for i in range(14)]

    def test_few_findings_are_not_rolled_up(self):
        from fidelity.profiler.facts import _rollup
        facts = [Fact(f"t.col{i}", "constant", f"col{i} is constant")
                 for i in range(3)]
        assert len(_rollup(facts)) == 3

    def test_blocking_facts_never_roll_up(self):
        """An empty table or an always-NULL column is individually actionable;
        burying it in a list is how it gets skimmed past."""
        from fidelity.profiler.facts import _rollup
        facts = [Fact(f"t{i}", "empty_table", f"t{i} is empty",
                      severity="blocking") for i in range(9)]
        assert len(_rollup(facts)) == 9


class TestBoundedDistinctCount:
    """Measured against 78M rows: exact `count(DISTINCT)` was 78% of runtime,
    and no derivation reads the exact value — only whether it is small enough
    to enumerate."""

    def test_high_cardinality_is_capped_not_counted(self):
        from fidelity.profiler.probe import ENUM_MAX_DISTINCT, profile_table
        seen = []

        def run(sql):
            seen.append(sql)
            if "IS NULL" in sql:
                return [(0,)]                       # no nulls
            if sql.startswith("SELECT count(*) FROM (SELECT DISTINCT"):
                return [(ENUM_MAX_DISTINCT + 1,)]
            if sql.startswith("SELECT count(*) FROM"):
                return [(1_000_000,)]               # row count
            return [(None, None)]                   # min/max

        table = TableRef("t")
        col = ColumnRef(table, "email", "TEXT")
        p = profile_table(run, SQLiteDialect(), table, [col])
        assert p.columns[0].distinct_capped
        # The probe must bound the scan, not count everything.
        assert any("SELECT DISTINCT" in s and "LIMIT" in s for s in seen)
        assert not any("count(DISTINCT" in s for s in seen)

    def test_low_cardinality_count_is_exact(self):
        from fidelity.profiler.probe import profile_table

        def run(sql):
            if "IS NULL" in sql:
                return [(0,)]
            if sql.startswith("SELECT count(*) FROM (SELECT DISTINCT"):
                return [(3,)]
            if sql.startswith("SELECT count(*) FROM"):
                return [(500,)]
            if "GROUP BY" in sql:
                return [("a", 300), ("b", 150), ("c", 50)]
            return [(None, None)]

        table = TableRef("t")
        p = profile_table(run, SQLiteDialect(), table,
                          [ColumnRef(table, "status", "TEXT")])
        c = p.columns[0]
        assert c.distinct == 3 and not c.distinct_capped
        assert c.observed_values[0] == ("a", 300)

    def test_capping_never_changes_a_fact(self):
        """The cap (25) sits above the reporting threshold (12), so a capped
        count can only ever mean "too many to enumerate" — which is the same
        conclusion an exact count would reach."""
        from fidelity.profiler.probe import ENUM_MAX_DISTINCT
        from fidelity.profiler.facts import ENUM_MAX_DISTINCT_REPORTED
        assert ENUM_MAX_DISTINCT > ENUM_MAX_DISTINCT_REPORTED


class TestDistinctExcludesNull:
    """Regression from optimising `count(DISTINCT)` into a bounded scan.

    `count(DISTINCT c)` excludes NULL; `SELECT DISTINCT c` returns it as a row.
    The rewrite inflated distinct by one on every nullable column, silently
    reclassifying constant columns as enumerated. Found by reading MySQL output
    — the unit-test fakes did not model NULL.
    """

    def test_bounded_probe_excludes_nulls(self):
        from fidelity.profiler.probe import profile_table
        seen = []

        def run(sql):
            seen.append(sql)
            if "IS NULL" in sql:
                return [(4,)]                     # 4 of 603 are NULL
            if sql.startswith("SELECT count(*) FROM (SELECT DISTINCT"):
                return [(1,)]
            if sql.startswith("SELECT count(*) FROM"):
                return [(603,)]
            if "GROUP BY" in sql:
                return [("", 599)]
            return [(None, None)]

        table = TableRef("address")
        profile_table(run, SQLiteDialect(), table,
                      [ColumnRef(table, "address2", "TEXT")])
        distinct_sql = next(s for s in seen if "SELECT DISTINCT" in s)
        assert "IS NOT NULL" in distinct_sql

    def test_constant_column_with_nulls_is_still_constant(self):
        c = ColumnRef(TableRef("address"), "address2", "TEXT")
        p = ColumnProfile(column=c, rows=603, nulls=4, distinct=1,
                          observed_values=(("", 599),))
        kinds = [f.kind for f in column_facts(p)]
        assert "constant" in kinds and "enumerated" not in kinds

    def test_empty_string_is_rendered_visibly(self):
        c = ColumnRef(TableRef("t"), "c", "TEXT")
        p = ColumnProfile(column=c, rows=600, nulls=0, distinct=1,
                          observed_values=(("", 600),))
        assert "empty string" in column_facts(p)[0].statement

    def test_whitespace_value_is_rendered_visibly(self):
        c = ColumnRef(TableRef("t"), "c", "TEXT")
        p = ColumnProfile(column=c, rows=600, nulls=0, distinct=1,
                          observed_values=(("   ", 600),))
        assert "whitespace" in column_facts(p)[0].statement


class TestHonestRounding:
    """Observed on AdventureWorksDW: `DimCustomer.Suffix is 100% NULL.
    Aggregates over it describe the populated minority` — self-contradictory,
    because a genuinely 100%-NULL column is the blocking `always_null` fact."""

    def _col(self, rows, nulls):
        c = ColumnRef(TableRef("t"), "c", "TEXT")
        return ColumnProfile(column=c, rows=rows, nulls=nulls, distinct=2)

    def test_almost_all_null_does_not_render_as_100(self):
        f = column_facts(self._col(1000, 997))[0]
        assert ">99%" in f.statement and "100%" not in f.statement
        assert f.kind == "mostly_null"

    def test_genuinely_all_null_is_blocking(self):
        p = ColumnProfile(column=ColumnRef(TableRef("t"), "c", "TEXT"),
                          rows=1000, nulls=1000)
        f = column_facts(p)[0]
        assert f.kind == "always_null" and f.severity == "blocking"

    def test_exact_percentages_still_render_plainly(self):
        assert "75%" in column_facts(self._col(100, 75))[0].statement


class TestSectionCap:
    def _facts(self, n, severity="info"):
        return [Fact(f"t.c{i}", "enumerated", f"c{i} takes values", severity)
                for i in range(n)]

    def test_uncapped_by_default(self):
        doc = render(self._facts(30), database="w")
        assert "omitted for length" not in doc
        assert doc.count("takes values") == 30

    def test_cap_states_what_it_omitted(self):
        doc = render(self._facts(30), database="w", max_per_section=10)
        assert doc.count("takes values") == 10
        assert "…and 20 further info facts, omitted for length" in doc

    def test_singular_omission_reads_correctly(self):
        doc = render(self._facts(11), database="w", max_per_section=10)
        assert "1 further info fact," in doc


class TestProbeBudgetOrdering:
    """A finite budget makes probe *order* decide what a truncated search finds.

    Measured on AdventureWorks OLTP: 156 of ~356 candidate pairs went untested.
    In arbitrary order that is a coin flip on whether the real relationship is
    among the ones probed.
    """

    def _schema(self, n_decoys):
        from fidelity.profiler import ColumnRef, TableRef
        orders = TableRef("orders")
        customers = TableRef("customers")
        tables = [orders, customers]
        cols = {
            "orders": [ColumnRef(orders, "customer_id", "INTEGER")],
            "customers": [ColumnRef(customers, "id", "INTEGER",
                                    primary_key=True)],
        }
        # Decoys sort before "customers" alphabetically, so an unordered search
        # would burn the budget on them first.
        for i in range(n_decoys):
            t = TableRef(f"aaa_decoy_{i}")
            tables.append(t)
            cols[t.name] = [ColumnRef(t, "id", "INTEGER", primary_key=True)]
        return tables, cols

    def test_plausible_target_is_probed_before_decoys(self):
        from fidelity.profiler import SQLiteDialect, infer_by_value_overlap
        tables, cols = self._schema(20)
        probed = []

        def run(sql):
            probed.append(sql)
            return [(0,)]

        _, skipped = infer_by_value_overlap(
            run, SQLiteDialect(), tables, cols, (), probe_budget=2)
        assert skipped > 0, "test needs the budget to actually bind"
        assert any('"customers"' in s for s in probed), (
            "budget was spent on decoys before the name-matching target")

    def test_full_budget_reaches_everything(self):
        from fidelity.profiler import SQLiteDialect, infer_by_value_overlap
        tables, cols = self._schema(5)
        _, skipped = infer_by_value_overlap(
            run=lambda sql: [(0,)], dialect=SQLiteDialect(), tables=tables,
            columns_by_table=cols, covered=(), probe_budget=1000)
        assert skipped == 0
