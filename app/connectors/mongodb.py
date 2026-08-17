"""MongoDB.

No SQL, no shared cursor, no driver-level read-only mode — three separate
ways this connector cannot reuse what `base.py` already built for the other
four. There is no SQL string a Mongo driver executes, so `refuse_reason()`
below validates a structured operation instead of parsing text, and
`MongoConnector.query()` is a full override rather than an implementation of
`BaseConnector._query()`'s DB-API cursor protocol.

**The one real asymmetry against the other four drivers.** Postgres opens
with `conn.read_only = True`; SQLite opens `mode=ro`. Pymongo's `MongoClient`
has no equivalent — there is no driver-level backstop under the allowlist
below. For a connection without a database role scoped to `read` only, this
allowlist is not defense-in-depth; it is the only control. That is mitigated
structurally, not compensated for: the tool this connector is paired with
never accepts free text, so there is no "detect a disguised write" step to
get wrong — the code below literally never calls anything but
`find`/`aggregate`/`count_documents`/`distinct` on the driver.
"""

from __future__ import annotations

import itertools
from typing import Any

from fidelity.profiler import Dialect, Fact, profile_mongo_database

from .base import BaseConnector, PREVIEW_ROWS, QueryResult, register

_READ_OPERATIONS = {"find", "aggregate", "count", "distinct"}

# Aggregation stages that only shape or filter output. An allowlist, not a
# blocklist, for the same reason `base.py`'s own SQL gate is one: a blocklist
# was bypassed three different ways on a real SQL Server. `$out` and `$merge`
# — the two stages that write results to a collection — are exactly what
# this list exists to keep out; anything unrecognized refuses rather than
# passing through.
_ALLOWED_STAGES = {
    "$match", "$project", "$group", "$sort", "$limit", "$skip", "$unwind",
    "$lookup", "$count", "$addFields", "$set", "$replaceRoot", "$replaceWith",
    "$facet", "$bucket", "$bucketAuto", "$sample", "$sortByCount", "$unset",
}

# Never permitted, wherever they appear. `$where`/`$function`/`$accumulator`
# execute arbitrary JavaScript on the server; `$out`/`$merge` write to a
# collection. All five can appear nested inside an otherwise-allowed stage —
# a `$where` inside a `$match`, an `$out` some future stage's sub-pipeline —
# so the stage allowlist above does not catch them on its own. This is
# walked over the whole query structure, at any depth, as the belt to that
# allowlist's braces.
_FORBIDDEN_ANYWHERE = {"$where", "$function", "$accumulator", "$out", "$merge"}


def _forbidden_operator(value: Any) -> str | None:
    """The first forbidden key found anywhere in this structure, or None."""
    if isinstance(value, dict):
        for key, sub in value.items():
            if key in _FORBIDDEN_ANYWHERE:
                return key
            found = _forbidden_operator(sub)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _forbidden_operator(item)
            if found:
                return found
    return None


def refuse_reason(query: dict[str, Any]) -> str | None:
    """Why this query may not run, or None if it is a plain read.

    Validates a structured operation rather than parsing text — there is no
    SQL string here to parse. See the module docstring for why that makes
    this the only enforcement for a connection with no scoped database role,
    not a second layer under a driver-level read-only mode.
    """
    operation = query.get("operation")
    if operation not in _READ_OPERATIONS:
        return (
            f"Unsupported operation: {operation!r}. Only find, aggregate, "
            "count and distinct are permitted; this connection is read-only."
        )

    collection = query.get("collection")
    if not isinstance(collection, str) or not collection or collection.startswith("$"):
        return "A valid collection name is required."

    if operation == "aggregate":
        pipeline = query.get("pipeline")
        if not isinstance(pipeline, list):
            return "aggregate requires a pipeline (a list of stages)."
        for stage in pipeline:
            if not isinstance(stage, dict) or len(stage) != 1:
                return "Each pipeline stage must be a single-key object."
            (stage_name,) = stage.keys()
            if stage_name not in _ALLOWED_STAGES:
                return (
                    f"`{stage_name}` is not a permitted aggregation stage; "
                    "this connection is read-only. `$out` and `$merge` write "
                    "results to a collection and are never allowed."
                )

    for field in ("filter", "pipeline", "projection"):
        found = _forbidden_operator(query.get(field))
        if found:
            return (
                f"`{found}` is not permitted in a query on this connection; "
                "it either executes server-side JavaScript or writes to a "
                "collection, and this connection is read-only."
            )

    return None


class _MongoDialect:
    """Satisfies only what `_answer()` reads off any connector's dialect:
    `prompt_note`. The rest of the `Dialect` protocol is SQL-shaped
    (`quote`, `tables`, `foreign_keys`, ...) and is never called for a Mongo
    connection — `MongoConnector.query()` overrides SQL execution outright,
    and `MongoConnector.facts()` uses `profile_mongo_database()`, never
    `profile_database()`."""

    name = "mongodb"
    prompt_note = (
        "This is a MongoDB database. Use the mongo_query tool, not SQL: give "
        "it a collection, an operation (find, aggregate, count, or "
        "distinct), and a filter or pipeline as a JSON object. Field names "
        "are whatever the documents actually use — if you are unsure of a "
        "collection's shape, run a find with no filter first, or check the "
        "generated data facts."
    )


@register
class MongoConnector(BaseConnector):
    kind = "mongodb"
    label = "MongoDB"
    default_port = 27017

    @staticmethod
    def dsn_for(f: dict[str, Any]) -> dict[str, Any]:
        dsn: dict[str, Any] = {"host": f["host"], "port": f["port"]}
        if f.get("user"):
            # Blank credentials must never reach MongoClient as empty
            # strings — pymongo treats that as "authenticate with nothing"
            # rather than "no authentication requested", and fails loudly on
            # a database (like this session's eval instance) that has no
            # auth enabled at all.
            dsn["username"] = f["user"]
            dsn["password"] = f.get("password", "")
            dsn["authSource"] = f["database"]
        return dsn

    def open(self) -> Any:
        import pymongo
        return pymongo.MongoClient(**self.dsn, serverSelectionTimeoutMS=5000)

    def dialect(self) -> Dialect:
        return _MongoDialect()

    def facts(self) -> list[Fact]:
        """Overrides `BaseConnector.facts()`'s SQL default — there is no
        `information_schema` here. `profile_mongo_database()` samples
        documents instead and already returns finished, derived facts."""
        return profile_mongo_database(self._db())

    def ping(self) -> None:
        """Overrides `BaseConnector.ping()`'s `cursor().execute("SELECT 1")`
        — there is no cursor here either. `admin.command("ping")` is
        Mongo's own zero-cost reachability check, raising the same way a
        bad host or bad credential would raise from a real query."""
        self._connect().admin.command("ping")

    def _db(self) -> Any:
        return self._connect()[self.database]

    # ── query ─────────────────────────────────────────────────────────────
    def query(self, query: dict[str, Any], preview: int = PREVIEW_ROWS) -> QueryResult:
        """Run one read-only Mongo operation and return a bounded preview.

        Mirrors the rule `BaseConnector._query()` states for SQL: the query
        is never rewritten to bound it. Appending a second limit to a
        pipeline that already ends in one is the exact class of bug that
        produced `LIMIT 5 LIMIT 51` there; bounding happens on what is
        consumed from the cursor, never on the query itself.
        """
        refusal = refuse_reason(query)
        if refusal:
            return QueryResult([], [], 0, False, error=refusal)

        operation = query["operation"]
        try:
            collection = self._db()[query["collection"]]

            if operation == "count":
                n = collection.count_documents(query.get("filter") or {})
                return QueryResult(["count"], [[n]], 1, False)

            if operation == "distinct":
                field = query.get("field")
                if not isinstance(field, str) or not field:
                    return QueryResult(
                        [], [], 0, False,
                        error="distinct requires a field name.")
                # pymongo returns this fully materialized — there is no
                # server-side cap to lean on, unlike find/aggregate cursors.
                values = collection.distinct(field, query.get("filter") or {})
                truncated = len(values) > preview
                return QueryResult(
                    [field], [[v] for v in values[:preview]],
                    len(values), truncated)

            if operation == "find":
                cursor = collection.find(
                    query.get("filter") or {}, query.get("projection") or None)
                sort = query.get("sort")
                if sort:
                    cursor = cursor.sort(list(sort.items()))
            else:  # aggregate
                cursor = collection.aggregate(query.get("pipeline") or [])

            docs = list(itertools.islice(cursor, preview + 1))
            truncated = len(docs) > preview
            docs = docs[:preview]
            # The union of keys actually present, not a fixed schema —
            # documents in one collection can have different shapes, which
            # is the reason this connector reports columns per result rather
            # than per collection.
            columns = sorted({k for d in docs for k in d.keys()})
            rows = [[d.get(c) for c in columns] for d in docs]

            total = len(docs)
            if truncated:
                total = self._exact_total(collection, query, operation)
            return QueryResult(columns, rows, total, truncated)
        except Exception as exc:  # noqa: BLE001 — reported to the model verbatim
            return QueryResult([], [], 0, False, error=f"{type(exc).__name__}: {exc}")

    def _exact_total(self, collection: Any, query: dict[str, Any],
                     operation: str) -> int | None:
        """Exact count of matching documents, or None if it cannot be
        obtained — reusing the same "total could not be determined" wording
        `QueryResult.to_json()` already carries for SQL Server's CTE case.
        Not hypothetical here either: one collection in real test data has a
        malformed `_id` that raises on aggregation-based counting, which is
        exactly the case this fallback exists for."""
        try:
            if operation == "find":
                return collection.count_documents(query.get("filter") or {})
            # A pipeline may reshape or filter rows in ways a plain filter
            # count cannot reproduce, so count via the same pipeline.
            pipeline = list(query.get("pipeline") or []) + [{"$count": "n"}]
            result = list(collection.aggregate(pipeline))
            return result[0]["n"] if result else 0
        except Exception:
            return None
