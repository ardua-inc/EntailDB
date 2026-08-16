"""What every database driver shares: bounded queries and the registry.

A driver is a self-contained module in this package. It subclasses
`BaseConnector`, declares its `kind`, `label`, `default_port`, how to open a
connection and what shape of DSN it wants, and calls `register`. Discovery is
automatic, so dropping a file in this directory is the whole installation step.

Everything database-agnostic stays here: the read-only gate, the bounded
preview, and the exact-total rule. A driver that reimplemented those would be
free to get them subtly wrong, and "wrong in a way that still returns rows" is
the failure this project exists to prevent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from fidelity.profiler import Dialect, Fact, derive, profile_database

PREVIEW_ROWS = 50

# Statements are admitted by an allowlist, not refused by a blocklist.
#
# The blocklist that stood here matched a leading keyword — INSERT, UPDATE,
# DELETE and friends — and three things walked straight past it:
#
#   WITH d AS (SELECT * FROM t) DELETE FROM d OUTPUT deleted.id
#   SELECT col INTO new_table FROM t
#   /* comment */ DROP TABLE t
#
# The first was demonstrated against a live SQL Server through this connector
# and emptied the table. Postgres, MySQL and SQLite each hold a server- or
# driver-level read-only mode, so only SQL Server was exposed — but a gate that
# depends on another layer catching it is not a gate.
#
# So: a statement must *be* a SELECT. One statement, no trailing semicolon
# games, optional leading CTEs, and no data-modifying keyword or INTO at the
# top level. Comments and string literals are removed before any of that is
# judged, because both can hide a keyword from a pattern.

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING = re.compile(r"'(?:[^']|'')*'")
_BRACKET_IDENT = re.compile(r"\[[^\]]*\]")
_QUOTED_IDENT = re.compile(r'"(?:[^"]|"")*"')

# Anything that changes state. `EXEC` is here because a procedure can do
# anything at all, which makes it unreviewable rather than merely risky.
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|MERGE|"
    r"REPLACE|CALL|EXEC|EXECUTE|BACKUP|RESTORE|SHUTDOWN|RECONFIGURE|"
    r"BULK|OPENROWSET|OPENDATASOURCE|INTO)\b",
    re.IGNORECASE,
)


def _strip_literals(sql: str) -> str:
    """Remove comments, strings and quoted identifiers.

    A keyword hidden inside any of them is not a keyword, and a keyword hidden
    behind a comment was one of the demonstrated bypasses.
    """
    for pattern in (_BLOCK_COMMENT, _LINE_COMMENT, _STRING, _BRACKET_IDENT,
                    _QUOTED_IDENT):
        sql = pattern.sub(" ", sql)
    return sql


def _top_level(sql: str) -> str:
    """The statement with everything inside parentheses removed.

    A subquery cannot modify anything, so `INTO` or `DELETE` at depth zero is
    the thing worth judging. Removing nested content keeps a legitimate
    `SELECT ... FROM (SELECT ...)` from being read as a compound statement.
    """
    out, depth = [], 0
    for ch in sql:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
        else:
            continue
        if depth == 0 and ch in "()":
            out.append(" ")
    return "".join(out)


def refuse_reason(sql: str) -> str | None:
    """Why this statement may not run, or None if it is a plain SELECT."""
    bare = _strip_literals(sql or "").strip()
    if not bare:
        return "Empty statement."

    # One statement only. A trailing `;` is fine; a second statement is not.
    if ";" in bare.rstrip().rstrip(";"):
        return ("Only one statement may be run at a time; this connection is "
                "read-only.")

    bare = bare.rstrip().rstrip(";")
    first = re.match(r"\s*([A-Za-z_]+)", bare)
    if not first or first.group(1).upper() not in ("SELECT", "WITH"):
        return ("This connection is read-only; only SELECT statements are "
                "permitted.")

    outer = _top_level(bare)
    hit = _FORBIDDEN.search(outer)
    if hit:
        word = hit.group(1).upper()
        if word == "INTO":
            return ("`SELECT ... INTO` creates a table; this connection is "
                    "read-only.")
        return (f"`{word}` modifies data; this connection is read-only. A "
                "common-table expression does not make it a read.")

    # `WITH x AS (...) DELETE FROM x` reaches here only if the CTE bodies hid
    # the keyword, which `_top_level` has already removed — so the statement
    # after the CTE list must itself begin with SELECT.
    if first.group(1).upper() == "WITH":
        after = re.sub(r"^\s*WITH\b", "", outer, flags=re.IGNORECASE)
        tail = re.search(r"\b(SELECT)\b", after, re.IGNORECASE)
        if not tail:
            return ("A common-table expression must end in a SELECT; this "
                    "connection is read-only.")
    return None


REGISTRY: dict[str, type["BaseConnector"]] = {}


def register(cls: type["BaseConnector"]) -> type["BaseConnector"]:
    """Announce a driver. Used as a decorator on the class itself."""
    if not getattr(cls, "kind", ""):
        raise ValueError(f"{cls.__name__} must declare a `kind`")
    if cls.kind in REGISTRY:
        raise ValueError(f"duplicate database kind: {cls.kind!r}")
    REGISTRY[cls.kind] = cls
    return cls


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    total_rows: int | None
    truncated: bool
    error: str | None = None

    def to_json(self) -> str:
        if self.error:
            return json.dumps({"error": self.error}, indent=2, default=str)
        body: dict[str, Any] = {
            "columns": self.columns,
            "rows": self.rows,
            "rows_returned": len(self.rows),
        }
        if self.truncated:
            # Stated whenever it is true, so "these are all of them" is never
            # an inference the model has to make on its own.
            if self.total_rows is None:
                body["total_rows_matched"] = None
                body["note"] = (
                    f"Preview only: {len(self.rows)} rows are shown and more "
                    "matched. The exact total could not be determined for this "
                    "query — do not present these rows as the complete set."
                )
            else:
                body["total_rows_matched"] = self.total_rows
                body["note"] = (
                    f"Preview only: {len(self.rows)} of {self.total_rows} "
                    "matching rows are shown."
                )
        return json.dumps(body, indent=2, default=str)


class BaseConnector:
    """One configured database. Owns its driver; hands the library a callable."""

    # Identity and what the settings page shows. A new driver supplies these
    # and appears in the UI without any existing file being edited.
    kind: str = ""
    label: str = ""
    default_port: int = 0
    wants_credentials: bool = True
    # True where the product has no server- or driver-level read-only mode and
    # the only remaining enforcement is to undo whatever ran. See
    # `SQLServerConnector`.
    rollback_after_query: bool = False

    def __init__(self, kind: str = "", dsn: dict[str, Any] | None = None,
                 database: str | None = None):
        # `kind` is accepted for the historical call signature and ignored:
        # the class already knows what it is.
        self.dsn = dsn or {}
        self.database = database
        self._conn: Any = None

    # ── what a driver supplies ────────────────────────────────────────────

    @staticmethod
    def dsn_for(fields: dict[str, Any]) -> dict[str, Any]:
        """Build this driver's connection arguments from stored fields."""
        raise NotImplementedError

    def open(self) -> Any:
        """Open a connection, read-only where the driver can enforce it."""
        raise NotImplementedError

    def dialect(self) -> Dialect:
        raise NotImplementedError

    def facts(self) -> list[Fact]:
        """Profile this connection and derive its facts.

        Default is the SQL profiler, which is what every product but Mongo
        actually is — so this is the one place `profile_database()` and
        `.dialect()` get called for the four SQL drivers, and no driver has
        to repeat it. A connector whose query language isn't SQL (Mongo)
        overrides this outright with its own profiling strategy, still
        finishing through the same `derive()` — the part of the pipeline
        that has nothing SQL-specific in it.
        """
        tables, joins = profile_database(self.runner(), self.dialect())
        return derive(tables, joins)

    # ── connection ────────────────────────────────────────────────────────
    def _connect(self) -> Any:
        if self._conn is None:
            self._conn = self.open()
        return self._conn
        if self.kind == "sqlite":
            import sqlite3
            # `mode=ro` is enforced by the driver, below the SQL layer, so it
            # holds even if the statement gate above is ever wrong. The other
            # products have no equivalent that a plain connection string can
            # express, which is why they rely on session settings instead.
            path = self.dsn["path"]
            self._conn = sqlite3.connect(
                f"file:{path}?mode=ro", uri=True, check_same_thread=False
            )
        elif self.kind == "postgres":
            import psycopg
            self._conn = psycopg.connect(**self.dsn)
            self._conn.read_only = True
        elif self.kind == "mysql":
            import pymysql
            self._conn = pymysql.connect(**self.dsn)
            with self._conn.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
        elif self.kind == "sqlserver":
            import pymssql
            self._conn = pymssql.connect(**self.dsn)
        else:
            raise ValueError(f"unsupported database kind: {self.kind!r}")
        return self._conn

    def runner(self) -> Callable[[str], list[tuple]]:
        """A `run(sql) -> rows` callable, as the profiler expects."""
        def run(sql: str) -> list[tuple]:
            conn = self._connect()
            cur = conn.cursor()
            try:
                cur.execute(sql)
                return list(cur.fetchall())
            finally:
                cur.close()
        return run

    # ── query ─────────────────────────────────────────────────────────────
    def query(self, sql: str, preview: int = PREVIEW_ROWS) -> QueryResult:
        """Run one read-only statement, undoing anything that was not."""
        try:
            return self._query(sql, preview)
        finally:
            if self.rollback_after_query:
                # Belt to the allowlist's braces. If a statement this gate
                # judged to be a SELECT turns out not to be, it does not
                # persist. Harmless for an actual read.
                self._rollback()

    def _query(self, sql: str, preview: int = PREVIEW_ROWS) -> QueryResult:
        """Run one read-only statement and return a bounded preview.

        **The query is executed verbatim.** Bounding happens on the cursor, not
        by rewriting SQL. An earlier version appended the dialect's row limit,
        which produced `LIMIT 5 LIMIT 51` the first time a model wrote its own
        `LIMIT` — caught on the first real end-to-end question. Wrapping in a
        subquery instead would have broken CTEs on SQL Server, where `WITH` is
        not allowed inside a derived table. Fetching N+1 rows from the cursor
        has neither problem and needs no dialect involvement at all.

        The total is counted separately, and only when the preview was actually
        truncated, so a question against a 30M-row table costs a count and 50
        rows rather than a 30M-row transfer.
        """
        refusal = refuse_reason(sql)
        if refusal:
            return QueryResult([], [], 0, False, error=refusal)
        dialect = self.dialect()
        conn = self._connect()
        try:
            cur = conn.cursor()
            try:
                stripped = sql.strip().rstrip(";")
                cur.execute(stripped)
                fetched = [list(r) for r in cur.fetchmany(preview + 1)]
                columns = [d[0] for d in (cur.description or [])]
            finally:
                cur.close()

            truncated = len(fetched) > preview
            rows = fetched[:preview]
            total = len(rows)
            if truncated:
                total = self._total(conn, stripped, fallback=len(rows))
            return QueryResult(columns, rows, total, truncated)
        except Exception as exc:  # noqa: BLE001 — reported to the model verbatim
            self._rollback()
            return QueryResult([], [], 0, False, error=f"{type(exc).__name__}: {exc}")

    def _total(self, conn: Any, sql: str, fallback: int) -> int | None:
        """Exact count of matching rows, or None if it cannot be obtained.

        Wrapping the statement in `count(*)` fails on SQL Server when the query
        opens with a CTE — `WITH` is not permitted inside a derived table. That
        is a real limit, so it returns None and the result says the total is
        unknown rather than quietly reporting the preview size as the whole
        set. Reporting 50 when the answer is 4,312 is exactly the failure this
        project exists to prevent.
        """
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT count(*) FROM ({sql}) AS _fidelity_total")
            got = cur.fetchall()
            return int(got[0][0]) if got else fallback
        except Exception:
            self._rollback()
            return None
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _rollback(self) -> None:
        # Postgres aborts a transaction on a failed statement; without this the
        # next query fails with a confusing "current transaction is aborted"
        # rather than its own error.
        try:
            if self._conn is not None:
                self._conn.rollback()
        except Exception:
            self._conn = None

    def close(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        finally:
            self._conn = None
