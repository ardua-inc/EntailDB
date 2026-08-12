"""Connector tests, run against a real SQLite database rather than a fake.

The choice of a real engine is deliberate and was bought with a defect. While
the profiler was being verified against MySQL, an optimisation replaced a
`SELECT DISTINCT` with `count(DISTINCT c)` — which excludes NULL, where the
former returns it as a row. Every nullable column's distinct count was wrong.
The full suite passed the whole time, because the fakes it ran against did not
model NULL. A fake only knows the behaviours its author already thought of,
which is precisely the set of behaviours that do not produce bugs.

SQLite costs nothing to stand up, so there is no reason to guess here. The
three server products still need containers, and their differences (T-SQL row
limits, MySQL's schema/database conflation) are covered by the dialect tests
and by live verification recorded in `MEASUREMENT.md`.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.connectors import PREVIEW_ROWS, Connector, QueryResult


@pytest.fixture
def db(tmp_path):
    """A small database with a known shape: 120 rows, some NULLs, one string."""
    path = tmp_path / "fixture.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, label TEXT, qty INTEGER)")
    conn.executemany(
        "INSERT INTO item (id, label, qty) VALUES (?, ?, ?)",
        [(i, f"item-{i:03d}", None if i % 10 == 0 else i * 2) for i in range(1, 121)],
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def connector(db):
    return Connector("sqlite", {"path": str(db)})


# ── the read-only gate ────────────────────────────────────────────────────

@pytest.mark.parametrize("sql", [
    "DELETE FROM item",
    "  delete from item",
    "UPDATE item SET qty = 0",
    "INSERT INTO item VALUES (999, 'x', 1)",
    "DROP TABLE item",
    "TRUNCATE TABLE item",
    "ALTER TABLE item ADD COLUMN x INT",
    "CREATE TABLE other (id INT)",
])
def test_writes_are_refused(connector, sql):
    result = connector.query(sql)
    assert result.error is not None
    assert "read-only" in result.error


def test_write_refusal_does_not_reach_the_database(connector, db):
    connector.query("DELETE FROM item")
    surviving = connector.query("SELECT count(*) FROM item").rows[0][0]
    assert surviving == 120


def test_driver_refuses_writes_even_if_the_gate_is_bypassed(connector):
    """The `mode=ro` connection is a second line, below the statement gate.

    Called directly, sidestepping `query()` entirely — the point is that the
    regex is not the only thing standing between a model and a DELETE.
    """
    run = connector.runner()
    with pytest.raises(sqlite3.OperationalError):
        run("DELETE FROM item")


def test_select_is_permitted(connector):
    result = connector.query("SELECT id, label FROM item WHERE id = 3")
    assert result.error is None
    assert result.rows == [[3, "item-003"]]


# ── bounded previews ──────────────────────────────────────────────────────

def test_preview_is_bounded_and_total_is_reported(connector):
    result = connector.query("SELECT id FROM item ORDER BY id")
    assert result.truncated is True
    assert len(result.rows) == PREVIEW_ROWS
    assert result.total_rows == 120


def test_small_result_is_not_truncated(connector):
    result = connector.query("SELECT id FROM item WHERE id <= 5 ORDER BY id")
    assert result.truncated is False
    assert len(result.rows) == 5


def test_result_at_exactly_the_preview_size_is_not_truncated(connector):
    """The N+1 fetch exists to tell "exactly N" from "N and more" apart."""
    result = connector.query(
        f"SELECT id FROM item ORDER BY id LIMIT {PREVIEW_ROWS}"
    )
    assert len(result.rows) == PREVIEW_ROWS
    assert result.truncated is False


def test_truncation_is_stated_in_the_payload_the_model_sees(connector):
    body = connector.query("SELECT id FROM item ORDER BY id").to_json()
    assert "120" in body
    assert "Preview only" in body


def test_untruncated_payload_makes_no_partial_claim(connector):
    body = connector.query("SELECT id FROM item WHERE id <= 5").to_json()
    assert "Preview only" not in body


# ── the double-LIMIT regression ───────────────────────────────────────────

def test_a_model_supplied_limit_is_preserved(connector):
    """Regression: the first real end-to-end question produced a syntax error.

    `query()` used to append the dialect's row limit to whatever SQL it was
    given, so a model that wrote its own `LIMIT 5` — which is most of them —
    got `LIMIT 5 LIMIT 51`. The fix stopped rewriting SQL altogether and bounds
    the cursor instead. Wrapping in a subquery would have traded this bug for a
    worse one: SQL Server does not allow `WITH` inside a derived table.
    """
    result = connector.query("SELECT id FROM item ORDER BY id LIMIT 5")
    assert result.error is None
    assert len(result.rows) == 5
    assert result.rows == [[1], [2], [3], [4], [5]]


def test_a_model_supplied_limit_larger_than_the_preview_still_truncates(connector):
    result = connector.query("SELECT id FROM item ORDER BY id LIMIT 100")
    assert len(result.rows) == PREVIEW_ROWS
    assert result.truncated is True


def test_a_cte_survives(connector):
    """The case that ruled out subquery wrapping as the fix."""
    result = connector.query(
        "WITH big AS (SELECT id FROM item WHERE qty > 200) "
        "SELECT count(*) FROM big"
    )
    assert result.error is None
    # 20 rows have id > 100, but `qty` is NULL on every tenth row and
    # `NULL > 200` is not true, so 110 and 120 are excluded.
    assert result.rows[0][0] == 18


def test_trailing_semicolon_is_tolerated(connector):
    assert connector.query("SELECT id FROM item WHERE id = 1;").rows == [[1]]


# ── failures are reported, not swallowed ──────────────────────────────────

def test_a_broken_query_returns_the_error(connector):
    result = connector.query("SELECT nonexistent FROM item")
    assert result.error is not None
    assert result.rows == []


def test_an_error_payload_says_so(connector):
    body = connector.query("SELECT nonexistent FROM item").to_json()
    assert "error" in body.lower()


def test_an_unknown_total_is_reported_as_unknown_not_as_the_preview_size():
    """A total that cannot be counted must never be reported as the row count.

    Saying "50 rows matched" when 4,312 did is the exact failure this project
    exists to prevent, so the unknown case is stated rather than guessed.
    """
    result = QueryResult(
        columns=["id"], rows=[[i] for i in range(PREVIEW_ROWS)],
        total_rows=None, truncated=True,
    )
    body = result.to_json()
    assert "could not be determined" in body
    assert "do not present these rows as the complete set" in body


# ── dialects ──────────────────────────────────────────────────────────────

def test_every_supported_kind_resolves_to_a_dialect():
    for kind in ("sqlite", "postgres", "mysql", "sqlserver"):
        assert Connector(kind, {}).dialect() is not None


def test_an_unknown_kind_is_refused_rather_than_defaulted():
    """It used to fall through to SQL Server, which would have written T-SQL
    against whatever the connection actually was."""
    with pytest.raises(ValueError):
        Connector("oracle", {}).dialect()


def test_each_dialect_names_its_own_product_in_its_prompt_note():
    for kind, expected in (
        ("sqlite", "SQLite"),
        ("postgres", "PostgreSQL"),
        ("mysql", "MySQL"),
        ("sqlserver", "SQL Server"),
    ):
        assert expected in Connector(kind, {}).dialect().prompt_note


def test_the_sqlserver_note_corrects_the_mistake_that_prompted_it():
    """A model opened with Postgres' `table_schema = 'public'` against SQL
    Server, got nothing, and had to recover. The note names `dbo`."""
    note = Connector("sqlserver", {}).dialect().prompt_note
    assert "dbo" in note
    assert "public" in note


# ── the served page ───────────────────────────────────────────────────────

def test_the_page_is_served_with_no_store():
    """A browser holding a stale copy runs old code against a current server,
    which is indistinguishable from a logic bug — it cost a full round of
    false diagnosis once already."""
    from app.main import NO_STORE
    assert "no-store" in NO_STORE["Cache-Control"]


def test_the_version_is_read_from_pyproject_not_redeclared():
    """One source of truth: `pyproject.toml`'s `[project].version`.

    Read from `pyproject.toml` first rather than from installed metadata: an
    editable install keeps the version it was installed at, so every bump would
    otherwise leave the running app advertising the previous build.
    """
    import tomllib
    from pathlib import Path

    from app.main import VERSION

    root = Path(__file__).resolve().parent.parent
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert VERSION == declared, (
        f"served version {VERSION!r} does not match pyproject {declared!r}"
    )


# ── the gate is an allowlist, not a keyword blocklist ─────────────────────
#
# The blocklist that shipped matched a leading keyword, and a CTE-backed DELETE
# walked past it and emptied a table on a live SQL Server through this
# connector. SQL Server was the exposed one because Postgres, MySQL and SQLite
# each hold a server- or driver-level read-only mode — but a gate that relies on
# another layer catching it is not a gate.

@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "  select a, b from t where c = 1  ",
    "SELECT 1;",
    "WITH x AS (SELECT 1 AS n) SELECT * FROM x",
    "WITH a AS (SELECT 1 AS n), b AS (SELECT 2 AS n) SELECT * FROM a JOIN b ON 1=1",
    "SELECT * FROM (SELECT id FROM t) s",
    "select 'delete from t' as literal",
    "SELECT col AS inserted_at FROM t",          # keyword as a substring
    "-- a comment\nSELECT 1",
])
def test_a_read_is_permitted(connector, sql):
    from app.connectors.base import refuse_reason

    assert refuse_reason(sql) is None, sql


@pytest.mark.parametrize("sql", [
    "WITH d AS (SELECT * FROM item) DELETE FROM d",
    "WITH d AS (SELECT * FROM item) DELETE FROM d OUTPUT deleted.id",
    "SELECT id INTO copy_of_item FROM item",
    "/* hidden */ DROP TABLE item",
    "-- hidden\nDELETE FROM item",
    "SELECT 1; DROP TABLE item",
    "DELETE FROM item",
    "EXEC sp_who",
    "EXECUTE some_proc",
    "TRUNCATE TABLE item",
    "MERGE item USING other ON 1=1 WHEN MATCHED THEN DELETE",
    "BACKUP DATABASE x TO DISK = 'y'",
    "",
])
def test_a_write_is_refused(connector, sql):
    from app.connectors.base import refuse_reason

    assert refuse_reason(sql) is not None, sql


def test_a_cte_backed_delete_is_refused_end_to_end(connector):
    """The demonstrated exploit, through the public entry point."""
    result = connector.query(
        "WITH d AS (SELECT * FROM item) DELETE FROM d")
    assert result.error is not None
    assert "read-only" in result.error
    assert connector.query("SELECT count(*) FROM item").rows[0][0] == 120


def test_select_into_is_named_specifically():
    """A generic refusal would leave the author guessing which clause offended."""
    from app.connectors.base import refuse_reason

    assert "INTO" in refuse_reason("SELECT a INTO t2 FROM t")


def test_only_sql_server_needs_the_rollback_belt():
    """The others enforce read-only below the SQL layer; rolling back every
    query there would be cost without benefit."""
    from app import connectors

    needs = {k: connectors.REGISTRY[k].rollback_after_query
             for k in connectors.kinds()}
    assert needs == {"sqlserver": True, "postgres": False,
                     "mysql": False, "sqlite": False}
