"""Profiling MongoDB — document sampling instead of `information_schema`.

Not an implementation of `Dialect`. `probe.py`'s functions assemble literal
SQL strings (`f"SELECT count(*) FROM {name} WHERE {col} IS NULL"`) and hand
them to a `Runner`; there is no SQL string a Mongo driver could execute, so
that whole path is unimplementable here, not merely inconvenient. What *is*
reusable is downstream of it: `derive()` and `render()` (`facts.py`) operate
purely on `TableProfile`/`ColumnProfile`/`JoinProfile`/`Fact` — already-
abstracted dataclasses with no SQL left in them. This module builds those
same dataclasses from document samples instead of SQL aggregates, and gets
the entire fact-derivation and rendering pipeline for free.

**Exact where SQL is exact, sampled where SQL is sampled.** SQL's null rate
is an exact `count(*) WHERE col IS NULL` over the whole table; its enum
values and format-shape detection come from a bounded sample. This module
holds the same split: presence and null counts are exact
(`count_documents`/`$exists`/`$type`), because `column_facts()`'s most
consequential thresholds — `always_null` is blocking severity — depend on
that number being real, not estimated from 200 documents. Types, enum
values and format samples come from one `$sample` pass, same as SQL's own
sample-based format detection. Thresholds (`SAMPLE_SIZE`, `ENUM_MAX_DISTINCT`)
are the same module-level constants `probe.py` uses, imported rather than
duplicated — SQL's version does not expose them as per-call parameters
either, and there is no reason this one should diverge from that precedent.

**Missing is not the same as null, and the wording says so.** A SQL column
is either NULL or it has a value. A Mongo field can be absent from a
document entirely — a different, and common, source of surprise for a model
writing a filter. Folding the two into one "null rate" would make
`column_facts()`'s "NULL on every row" fire — and be worded — inaccurately
for a field that's simply missing from most documents rather than
explicitly null on them. So `ColumnProfile.nulls` here counts *only*
explicit BSON null — `column_facts()`'s existing wording stays literally
true — and presence is reported as its own fact, in its own words, below.

**Scope, stated rather than silently assumed.** Nested subdocuments are
flattened one level (`profile.name`), not recursively; arrays are reported
as an opaque `array`-typed field, not profiled element by element. Joins are
inferred by naming convention only (a `dealId` field matched against a
`Deal` collection) — not the value-overlap search `probe.py` also does for
SQL. Both are real gaps a wider pass could close; neither is silent here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .facts import derive
from .model import (
    ColumnProfile,
    ColumnRef,
    Fact,
    ForeignKey,
    JoinProfile,
    TableProfile,
    TableRef,
)
from .probe import ENUM_MAX_DISTINCT, SAMPLE_SIZE

# A field present in less than this fraction of sampled documents gets its
# own fact — worded around "present", never "NULL", since it may never have
# existed on most documents rather than having been cleared.
MOSTLY_MISSING = 0.50

_OBJECT_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")
# Nested subdocuments are flattened this many levels for field discovery.
# Beyond this, a subdocument is reported as an opaque `object`-typed field
# rather than expanded further — see the module docstring.
_FLATTEN_DEPTH = 1
_ENUM_TYPES = (str, int, float, bool)


@dataclass
class _FieldStats:
    types: set = field(default_factory=set)
    string_samples: list = field(default_factory=list)
    enum_counts: dict = field(default_factory=dict)
    minimum: Any = None
    maximum: Any = None
    is_object_id_shaped: bool = True  # narrowed to False on the first non-match


def _bson_type(value: Any) -> str:
    # bool before int: bool is an int subclass in Python.
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return {"ObjectId": "objectId", "datetime": "date",
            "Decimal128": "decimal"}.get(type(value).__name__, type(value).__name__)


def _looks_like_object_id(value: Any) -> bool:
    return type(value).__name__ == "ObjectId" or (
        isinstance(value, str) and bool(_OBJECT_ID_RE.match(value)))


def _collect_fields(doc: dict, prefix: str, depth: int,
                    into: dict[str, _FieldStats]) -> None:
    for key, value in doc.items():
        path = f"{prefix}.{key}" if prefix else key
        stats = into.setdefault(path, _FieldStats())

        if value is None:
            continue  # counted via $type in the exact pass, not from the sample

        stats.types.add(_bson_type(value))
        if not _looks_like_object_id(value):
            stats.is_object_id_shaped = False

        if not isinstance(value, bool) and (
                isinstance(value, (int, float)) or type(value).__name__ == "datetime"):
            stats.minimum = value if stats.minimum is None else min(stats.minimum, value)
            stats.maximum = value if stats.maximum is None else max(stats.maximum, value)

        if isinstance(value, str) and len(stats.string_samples) < SAMPLE_SIZE:
            stats.string_samples.append(value)

        if isinstance(value, _ENUM_TYPES) and (
                value in stats.enum_counts or len(stats.enum_counts) <= ENUM_MAX_DISTINCT):
            # Bounded growth: an existing key's count always updates; a *new*
            # key stops being admitted once the cap is reached, so a
            # high-cardinality field is correctly read as "too many to
            # enumerate" rather than silently truncated to whichever values
            # happened to sort first.
            stats.enum_counts[value] = stats.enum_counts.get(value, 0) + 1

        if depth < _FLATTEN_DEPTH and isinstance(value, dict):
            _collect_fields(value, prefix=path, depth=depth + 1, into=into)


def _exact_presence(collection: Any, path: str) -> tuple[int, int]:
    """(present, explicitly_null) — exact counts, not sampled.

    `{field: null}` as a plain equality filter is the classic Mongo trap: it
    matches a *missing* field too, not only an explicit null. `$type: "null"`
    is the operator that means only the latter.
    """
    present = collection.count_documents({path: {"$exists": True}})
    explicit_null = collection.count_documents({path: {"$type": "null"}})
    return present, explicit_null


def _declared_type(stats: _FieldStats) -> str:
    if len(stats.types) == 1:
        return next(iter(stats.types))
    return "mixed" if stats.types else "null"


def _profile_collection(
    db: Any, name: str,
) -> tuple[TableProfile, list[Fact], dict[str, list[Any]]]:
    collection = db[name]
    row_count = collection.estimated_document_count()

    sample = list(collection.aggregate([{"$sample": {"size": SAMPLE_SIZE}}]))
    field_stats: dict[str, _FieldStats] = {}
    for doc in sample:
        _collect_fields(doc, prefix="", depth=0, into=field_stats)

    table = TableRef(name=name)
    columns: list[ColumnProfile] = []
    extra_facts: list[Fact] = []
    # Top-level, ObjectId-shaped `*Id` fields only — candidates for join
    # inference, kept alongside their raw sampled values so
    # `_soft_key_joins` doesn't need a second pass over the documents.
    join_candidates: dict[str, list[Any]] = {}

    for path in sorted(field_stats):
        stats = field_stats[path]
        present, explicit_null = (0, 0) if row_count == 0 else _exact_presence(collection, path)

        column = ColumnRef(table=table, name=path, declared_type=_declared_type(stats),
                           primary_key=(path == "_id"))
        cp = ColumnProfile(column=column, rows=row_count, nulls=explicit_null)

        if present > explicit_null:
            distinct_enum = len(stats.enum_counts)
            if 0 < distinct_enum <= ENUM_MAX_DISTINCT:
                cp.distinct = distinct_enum
                cp.observed_values = tuple(
                    sorted(((str(v), n) for v, n in stats.enum_counts.items()),
                          key=lambda vn: -vn[1]))
            cp.minimum, cp.maximum = stats.minimum, stats.maximum
            cp.samples = tuple(stats.string_samples)
        columns.append(cp)

        missing_rate = (row_count - present) / row_count if row_count else 0.0
        if row_count > 0 and missing_rate >= MOSTLY_MISSING:
            extra_facts.append(Fact(
                subject=f"{name}.{path}", kind="mostly_missing",
                statement=(
                    f"`{name}.{path}` is present in only "
                    f"{round((1 - missing_rate) * 100)}% of documents; the "
                    "rest simply do not have this field, rather than having "
                    "it set to null. Aggregates over it describe the "
                    "documents that have it, not the collection."),
                severity="caution",
                evidence={"missing_rate": round(missing_rate, 4)}))
        if len(stats.types) > 1:
            extra_facts.append(Fact(
                subject=f"{name}.{path}", kind="mixed_type",
                statement=(
                    f"`{name}.{path}` was observed with more than one type "
                    f"in a sample of the data: {', '.join(sorted(stats.types))}. "
                    "A comparison or sort against it may not behave "
                    "consistently across documents."),
                severity="caution",
                evidence={"types_observed": sorted(stats.types)}))

        if "." not in path and path != "_id" and path.endswith("Id") and \
           stats.is_object_id_shaped and stats.types:
            join_candidates[path] = [d.get(path) for d in sample]

    return (TableProfile(table=table, rows=row_count, columns=columns),
            extra_facts, join_candidates)


def _soft_key_joins(
    db: Any, tables: list[TableProfile],
    candidates_by_table: dict[str, dict[str, list[Any]]],
) -> list[JoinProfile]:
    """Naming-convention soft keys, measured the same way `probe.py`'s
    `profile_join` measures a SQL one: sample the referencing values, check
    how many resolve against the target's real `_id`s."""
    from bson import ObjectId

    collection_names = {t.table.name for t in tables}
    joins: list[JoinProfile] = []

    for table in tables:
        for path, sampled in candidates_by_table.get(table.table.name, {}).items():
            prefix = path[: -len("Id")]
            target_name = next(
                (n for n in collection_names if n.lower() == prefix.lower()), None)
            if not target_name or target_name == table.table.name:
                continue
            column = next(c.column for c in table.columns if c.column.name == path)
            fk = ForeignKey(column=column, target=TableRef(name=target_name),
                            target_column="_id", declared=False, inferred_by="name")

            non_null = [v for v in sampled if v is not None]
            if not non_null:
                joins.append(JoinProfile(foreign_key=fk, non_null=0, matched=0))
                continue

            ids = []
            for v in non_null:
                if type(v).__name__ == "ObjectId":
                    ids.append(v)
                elif isinstance(v, str) and _OBJECT_ID_RE.match(v):
                    try:
                        ids.append(ObjectId(v))
                    except Exception:  # noqa: BLE001 — an unparsable id just fails to match
                        ids.append(v)
                else:
                    ids.append(v)

            resolved = {
                d["_id"] for d in db[target_name].find(
                    {"_id": {"$in": list({*ids})}}, {"_id": 1})
            }
            matched = sum(1 for v in ids if v in resolved)
            joins.append(JoinProfile(foreign_key=fk, non_null=len(non_null), matched=matched))

    return joins


def profile_mongo_database(db: Any) -> list[Fact]:
    """Profile every collection in `db` and derive its facts.

    `db` is a `pymongo.database.Database` — the driver's own handle, not a
    generic callable. `probe.py`'s `Runner` abstraction (a callable taking
    SQL, returning rows) has no Mongo equivalent to abstract over; a real
    document database object is what every operation here actually needs.

    One collection failing to profile does not abort the run — caught, and
    the gap is stated as a fact rather than silently dropped, the same rule
    `render()` already applies to a truncated document.
    """
    tables: list[TableProfile] = []
    all_extra: list[Fact] = []
    candidates_by_table: dict[str, dict[str, list[Any]]] = {}

    for name in sorted(db.list_collection_names()):
        try:
            table, extra, join_candidates = _profile_collection(db, name)
        except Exception as exc:  # noqa: BLE001 — one bad collection must not sink the rest
            all_extra.append(Fact(
                subject=name, kind="profiling_failed",
                statement=(
                    f"`{name}` could not be profiled ({type(exc).__name__}: "
                    f"{exc}). It exists but nothing below is measured for it."),
                severity="caution", evidence={"error": str(exc)}))
            continue
        tables.append(table)
        all_extra.extend(extra)
        candidates_by_table[name] = join_candidates

    joins = _soft_key_joins(db, tables, candidates_by_table)

    facts = derive(tables, joins) + all_extra
    order = {"blocking": 0, "caution": 1, "info": 2}
    facts.sort(key=lambda f: (order.get(f.severity, 3), f.subject))
    return facts
