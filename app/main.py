"""FastAPI application: chat, connections, and schema profiling.

Stage 1 per `DESIGN.md`: **binds to loopback, has no authentication.** Do not
publish this port. Auth and per-user access control are the gate before it goes
anywhere but localhost, and the settings store being encrypted does not change
that — it protects the file, not the endpoint.
"""

from __future__ import annotations

import json
import tomllib
import uuid
from decimal import Decimal
from importlib import metadata
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from fidelity.profiler import render
from fidelity.runner import FidelityRunner, ToolResult

from . import config, threads
from . import connectors
from .connectors import PREVIEW_ROWS, Connector, QueryResult
from . import providers

app = FastAPI(title="EntailDB")
HERE = __import__("pathlib").Path(__file__).parent

def _version() -> str:
    """The one version number, `pyproject.toml`'s `[project].version`.

    `pyproject.toml` wins where it exists, because in a source checkout it *is*
    the source of truth and installed metadata is a stale copy of it: an
    editable install keeps whatever version it was installed at, so every bump
    would otherwise leave the header advertising the previous build until
    someone remembered to reinstall. Distribution metadata is the fallback for a
    real deployment that ships without the source tree.

    Never re-declared in Python — two copies of a version number disagree
    eventually, and the disagreement always surfaces as a support question
    rather than a test failure.
    """
    pyproject = HERE.parent / "pyproject.toml"
    if pyproject.exists():
        try:
            return tomllib.loads(pyproject.read_text())["project"]["version"]
        except (tomllib.TOMLDecodeError, KeyError):
            pass
    try:
        return metadata.version("entaildb")
    except metadata.PackageNotFoundError:
        return "0.0.0+unknown"


VERSION = _version()

SYSTEM_PROMPT = """\
You are an analytics assistant. People ask you questions about the business and
you answer them by querying the connected database with the tools available.

Write in plain prose. Format results clearly. Be concise — the people asking are
in the middle of their working day. When a trend or a category comparison would
be clearer as a chart, use render_chart.

Every table or chart you produce is already shown to the person with copy and
CSV-download controls built into the interface, automatically, on every result
— you do not have a tool that builds a file, and you do not need one. If asked
for "a downloadable file" or "an export," tell them to use the download control
on the result already shown rather than trying to generate one yourself, and do
not paste a large result into your answer as rows of text.
"""


# The hard ceiling on `max_rows`, below. Application policy, not connector
# policy — the connector would happily fetch however many rows it's asked
# for, and "how much a model may ask for in one call" is a decision about
# what a tool-using model is, which is exactly the thing DESIGN.md keeps
# out of the library layer.
MAX_PREVIEW_ROWS = 1000


def _clamp_preview(max_rows: Any) -> int:
    """The requested preview size, bounded so "give me all rows" on a
    30M-row table can never become an unbounded fetch. Anything unusable —
    missing, non-numeric, zero, negative — falls back to the ordinary
    default rather than being treated as a request for more."""
    try:
        requested = int(max_rows)
    except (TypeError, ValueError):
        return PREVIEW_ROWS
    return min(requested, MAX_PREVIEW_ROWS) if requested > 0 else PREVIEW_ROWS


class SqlTool:
    """The one tool. Returns a bounded preview and a true total row count."""

    name = "run_sql"
    description = (
        "Run a read-only SQL query against the connected database and return "
        "the resulting rows. Large result sets come back as a bounded preview "
        "with the total number of matching rows stated separately."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "A read-only SQL SELECT statement."},
            "max_rows": {
                "type": "integer",
                "description": (
                    f"How many rows to return, up to {MAX_PREVIEW_ROWS}. "
                    f"Defaults to {PREVIEW_ROWS} if omitted. Only raise this "
                    "when the person explicitly asks for more than the "
                    "default preview, or for all of a result already known "
                    "to be larger than that — prefer pointing them at the "
                    "CSV download on the result above over pasting many "
                    "rows into your answer."
                ),
            },
        },
        "required": ["query"],
    }

    def __init__(self, connector: Connector) -> None:
        self.connector = connector
        self.calls: list[dict[str, Any]] = []

    def __call__(self, arguments: dict[str, Any]) -> ToolResult:
        args = arguments or {}
        sql = args.get("query", "")
        preview = _clamp_preview(args.get("max_rows"))
        result = self.connector.query(sql, preview=preview)
        self.calls.append({"sql": sql, "result": result})
        return ToolResult(result.to_json(), is_error=result.error is not None)


class MongoQueryTool:
    """The MongoDB equivalent of `SqlTool` — a structured operation, not a
    query string. Forcing Mongo's query language through a tool named and
    described as SQL would be actively misleading to the model, so this is
    a separate tool rather than a second thing `run_sql` accepts. Kept to
    the same shape as `SqlTool` on purpose (`self.calls` holding a `result`
    per call) so `ChartTool` works against either with no changes of its
    own — it only ever reads `entry["result"]`.
    """

    name = "mongo_query"
    description = (
        "Run a read-only MongoDB operation and return a bounded preview of "
        "the results. One of four operations: find (filter, projection, "
        "sort), aggregate (a pipeline), count, or distinct (one field). "
        "Large result sets come back as a bounded preview with the total "
        "number of matching documents stated separately."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "collection": {"type": "string",
                          "description": "The collection to query."},
            "operation": {"type": "string",
                         "enum": ["find", "aggregate", "count", "distinct"]},
            "filter": {"type": "object",
                      "description": "A MongoDB filter document. For find/count/distinct."},
            "projection": {"type": "object",
                          "description": "Which fields to return. For find only."},
            "sort": {"type": "object",
                    "description": "Field to sort direction (1 or -1). For find only."},
            "pipeline": {"type": "array",
                        "description": "An aggregation pipeline. For aggregate only."},
            "field": {"type": "string",
                     "description": "The field to collect distinct values of. For distinct only."},
            "max_rows": {
                "type": "integer",
                "description": (
                    f"How many documents to return, up to {MAX_PREVIEW_ROWS}. "
                    f"Defaults to {PREVIEW_ROWS} if omitted. Only raise this "
                    "when the person explicitly asks for more than the "
                    "default preview, or for all of a result already known "
                    "to be larger than that — prefer pointing them at the "
                    "CSV download on the result above over pasting many "
                    "documents into your answer."
                ),
            },
        },
        "required": ["collection", "operation"],
    }

    def __init__(self, connector: Connector) -> None:
        self.connector = connector
        self.calls: list[dict[str, Any]] = []

    def __call__(self, arguments: dict[str, Any]) -> ToolResult:
        args = arguments or {}
        preview = _clamp_preview(args.get("max_rows"))
        query = {k: v for k, v in args.items() if k != "max_rows"}
        result = self.connector.query(query, preview=preview)
        self.calls.append({"query": query, "result": result})
        return ToolResult(result.to_json(), is_error=result.error is not None)


def _mongo_shell_text(query: dict[str, Any]) -> str:
    """The query as `mongosh` shell syntax — `db.Deal.find({...})` — not a
    dump of this tool's own argument schema. The point of the panel this
    feeds is the same as the SQL one's: something a reader could copy and
    actually run, not a description of what ran. JSON's double-quoted keys
    are already valid inside a shell call, so no separate JS-literal
    formatting is needed for the filter/pipeline bodies themselves.
    """
    collection = query.get("collection", "?")
    operation = query.get("operation")

    def j(value: Any) -> str:
        return json.dumps(value, indent=2, default=str)

    if operation == "find":
        args = [j(query.get("filter") or {})]
        if query.get("projection"):
            args.append(j(query["projection"]))
        call = f"db.{collection}.find({', '.join(args)})"
        if query.get("sort"):
            call += f".sort({j(query['sort'])})"
        return call
    if operation == "aggregate":
        return f"db.{collection}.aggregate({j(query.get('pipeline') or [])})"
    if operation == "count":
        return f"db.{collection}.countDocuments({j(query.get('filter') or {})})"
    if operation == "distinct":
        field = j(query.get("field", ""))
        filt = query.get("filter")
        return (f"db.{collection}.distinct({field}, {j(filt)})" if filt
                else f"db.{collection}.distinct({field})")
    # An operation the schema doesn't recognize (refused before it ran) —
    # shown as what was actually asked for rather than hidden.
    return j(query)


class ChartTool:
    """Draws a chart from the rows the most recent successful `run_sql` call
    already returned this turn. The model chooses a chart type and which two
    columns map to which axis; it supplies no data of its own — this tool
    reads the real rows itself. Same reasoning as the link allowlist: a model
    may reference what a tool returned and may not invent it.

    `SqlTool` is built fresh per request (see `_answer`), so `.calls` cannot
    hold anything from an earlier turn — "most recent successful query this
    turn" is a property of that shape, not a rule this tool has to enforce.
    """

    name = "render_chart"
    description = (
        "Draw a bar or line chart from the rows returned by the most recent "
        "successful run_sql call in this turn. Provide a chart_type and the "
        "names of two columns from that result — one for x, one for y. This "
        "tool reads the rows itself; it does not accept data points, and it "
        "has no visibility into any query from an earlier turn."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "chart_type": {"type": "string", "enum": ["bar", "line"]},
            "x_column": {"type": "string",
                        "description": "A column name from the last query result."},
            "y_column": {"type": "string",
                        "description": "A numeric column name from the last query result."},
            "title": {"type": "string",
                      "description": "Optional caption. Not checked against the data."},
        },
        "required": ["chart_type", "x_column", "y_column"],
    }

    _TYPES = ("bar", "line")
    # `bool` is deliberately excluded even though it is an `int` subclass —
    # True/False are not a quantity to plot.
    _NUMERIC = (int, float, Decimal)

    def __init__(self, sql_tool: SqlTool) -> None:
        self.sql_tool = sql_tool

    def __call__(self, arguments: dict[str, Any]) -> ToolResult:
        args = arguments or {}
        chart_type = args.get("chart_type", "")
        x_column = args.get("x_column", "")
        y_column = args.get("y_column", "")
        title = args.get("title", "")

        if chart_type not in self._TYPES:
            return self._refuse(
                f"Unsupported chart type: {chart_type!r}. Use 'bar' or 'line'.")

        result = self._latest_result()
        if result is None:
            return self._refuse(
                "No successful query result to chart yet in this turn. Run a "
                "query first.")

        if x_column not in result.columns or y_column not in result.columns:
            return self._refuse(
                f"{x_column!r} and {y_column!r} must both be columns from the "
                f"last query result: {result.columns}.")

        # Walked in the rows' own order — never sorted, never reordered. The
        # accuracy instruction's "reproduce rows exactly as returned" applies
        # here just as it does to prose.
        xi, yi = result.columns.index(x_column), result.columns.index(y_column)
        points: list[dict[str, Any]] = []
        skipped_null = 0
        for row in result.rows:
            y = row[yi]
            if y is None:
                # A real NULL in the data, not a model mistake — skipped and
                # disclosed, not silently dropped.
                skipped_null += 1
                continue
            if isinstance(y, bool) or not isinstance(y, self._NUMERIC):
                # A wrong-type value is a column-selection mistake, not a
                # data-quality gap: refuse the whole call rather than plot an
                # unpredictable subset of rows for a reason nobody can see.
                return self._refuse(
                    f"Column {y_column!r} is not numeric (found a "
                    f"{type(y).__name__} value); choose a numeric column for "
                    "the y axis.")
            x = row[xi]
            points.append({"x": "NULL" if x is None else str(x), "y": float(y)})

        if not points:
            return self._refuse(f"No numeric values in {y_column!r} to chart.")

        payload: dict[str, Any] = {
            "chart_type": chart_type,
            "x_label": x_column,
            "y_label": y_column,
            "points": points,
            "rows_plotted": len(points),
        }
        if title:
            payload["title"] = title
        if result.truncated:
            payload["total_rows_matched"] = result.total_rows
        note = self._note(result, skipped_null, y_column)
        if note:
            payload["note"] = note
        return ToolResult(json.dumps(payload))

    def _latest_result(self) -> QueryResult | None:
        """The most recent call this turn whose query succeeded.

        Reads `self.sql_tool.calls` — the list `SqlTool` already accumulates —
        rather than keeping a second copy of query history.
        """
        for entry in reversed(self.sql_tool.calls):
            if entry["result"].error is None:
                return entry["result"]
        return None

    @staticmethod
    def _note(result: QueryResult, skipped_null: int, y_column: str) -> str:
        """Disclosure text: the source's own preview-boundary wording, reused
        rather than re-derived, plus how many rows had nothing to plot."""
        parts: list[str] = []
        if result.truncated:
            if result.total_rows is None:
                parts.append(
                    f"Preview only: {len(result.rows)} rows are shown and "
                    "more matched. The exact total could not be determined "
                    "for this query — do not present these rows as the "
                    "complete set.")
            else:
                parts.append(
                    f"Preview only: {len(result.rows)} of {result.total_rows} "
                    "matching rows are shown.")
        if skipped_null:
            parts.append(
                f"{skipped_null} row{'s' if skipped_null != 1 else ''} with "
                f"no value in {y_column!r} were left out.")
        return " ".join(parts)

    @staticmethod
    def _refuse(message: str) -> ToolResult:
        return ToolResult(json.dumps({"error": message}), is_error=True)


def _connection(connection_id: str) -> config.Connection:
    settings = config.load()
    for c in settings.connections:
        if c.id == connection_id:
            return c
    raise HTTPException(404, f"no such connection: {connection_id}")


# ── settings ──────────────────────────────────────────────────────────────

class ConnectionIn(BaseModel):
    label: str
    kind: str
    host: str
    port: int
    database: str
    user: str
    password: str = ""


class SettingsIn(BaseModel):
    anthropic_api_key: str = ""
    model: str = "claude-sonnet-5"


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    s = config.load()
    return {
        "connections": [c.public() for c in s.connections],
        "profiles": [p.public() for p in s.profiles],
        "default_profile_id": s.default_profile_id,
        "model": s.model,
        "api_key_set": bool(s.api_key()),
    }


@app.put("/api/settings")
def put_settings(body: SettingsIn) -> dict[str, Any]:
    s = config.load()
    s.model = body.model
    if body.anthropic_api_key and body.anthropic_api_key != config.REDACTED:
        s.anthropic_api_key = config.encrypt(body.anthropic_api_key)
    config.save(s)
    return {"ok": True}


@app.post("/api/connections")
def add_connection(body: ConnectionIn) -> dict[str, Any]:
    s = config.load()
    conn = config.Connection(
        id=uuid.uuid4().hex[:8], label=body.label, kind=body.kind,
        host=body.host, port=body.port, database=body.database, user=body.user,
        password=config.encrypt(body.password) if body.password else "",
    )
    s.connections.append(conn)
    config.save(s)
    return conn.public()


@app.delete("/api/connections/{connection_id}")
def delete_connection(connection_id: str) -> dict[str, Any]:
    s = config.load()
    s.connections = [c for c in s.connections if c.id != connection_id]
    config.save(s)
    return {"ok": True}


@app.post("/api/connections/{connection_id}/test")
def test_connection(connection_id: str) -> dict[str, Any]:
    conn = _connection(connection_id)
    c = Connector(conn.kind, conn.dsn(), conn.database)
    try:
        result = c.query("SELECT 1 AS ok")
        if result.error:
            return {"ok": False, "error": result.error}
        return {"ok": True}
    finally:
        c.close()


@app.post("/api/connections/{connection_id}/profile")
def profile_connection(connection_id: str) -> dict[str, Any]:
    """Generate the data-facts document and store it against the connection.

    Regenerated on demand rather than cached forever: the whole argument for
    pillar 2 is that generated facts regenerate and pasted facts rot.
    """
    conn = _connection(connection_id)
    c = Connector(conn.kind, conn.dsn(), conn.database)
    try:
        facts = c.facts()
        document = render(facts, database=conn.database, max_per_section=12)
    finally:
        c.close()
    s = config.load()
    for stored in s.connections:
        if stored.id == connection_id:
            stored.facts = document
    config.save(s)
    return {"facts": len(facts), "document": document}


# ── model profiles ────────────────────────────────────────────────────────

class ProfileIn(BaseModel):
    label: str
    kind: str
    model: str
    base_url: str = ""
    api_key: str = ""
    max_tokens: int = 4096


@app.get("/api/profiles")
def list_profiles() -> dict[str, Any]:
    s = config.load()
    return {"profiles": [p.public() for p in s.profiles],
            "default_profile_id": s.default_profile_id}


@app.post("/api/profiles")
def add_profile(body: ProfileIn) -> dict[str, Any]:
    if body.kind not in providers.REGISTRY:
        raise HTTPException(400, f"unsupported provider kind: {body.kind!r}")
    s = config.load()
    profile = config.ModelProfile(
        id=uuid.uuid4().hex[:8], label=body.label.strip() or body.model,
        kind=body.kind, model=body.model, base_url=body.base_url.strip(),
        api_key=config.encrypt(body.api_key) if body.api_key else "",
        max_tokens=body.max_tokens,
    )
    s.profiles.append(profile)
    if not s.default_profile_id:
        s.default_profile_id = profile.id
    config.save(s)
    return profile.public()


@app.put("/api/profiles/{profile_id}/default")
def set_default_profile(profile_id: str) -> dict[str, Any]:
    s = config.load()
    if not any(p.id == profile_id for p in s.profiles):
        raise HTTPException(404, f"no such profile: {profile_id}")
    s.default_profile_id = profile_id
    config.save(s)
    return {"ok": True}


@app.delete("/api/profiles/{profile_id}")
def remove_profile(profile_id: str) -> dict[str, Any]:
    s = config.load()
    s.profiles = [p for p in s.profiles if p.id != profile_id]
    # Connections pinned to it fall back to the default rather than pointing at
    # something gone; an unresolvable pin would fail at question time, which is
    # the worst moment to discover it.
    for c in s.connections:
        if c.model_profile_id == profile_id:
            c.model_profile_id = ""
    if s.default_profile_id == profile_id:
        s.default_profile_id = s.profiles[0].id if s.profiles else ""
    config.save(s)
    return {"ok": True}


@app.put("/api/connections/{connection_id}/profile")
def pin_connection_profile(connection_id: str, body: dict[str, str]) -> dict[str, Any]:
    """Bind one database to one model, or clear the binding."""
    s = config.load()
    wanted = (body or {}).get("model_profile_id", "")
    if wanted and not any(p.id == wanted for p in s.profiles):
        raise HTTPException(404, f"no such profile: {wanted}")
    for c in s.connections:
        if c.id == connection_id:
            c.model_profile_id = wanted
            config.save(s)
            return c.public()
    raise HTTPException(404, f"no such connection: {connection_id}")


# ── threads ───────────────────────────────────────────────────────────────

class ThreadIn(BaseModel):
    connection_id: str


class ThreadPatch(BaseModel):
    title: str


@app.get("/api/threads")
def list_threads(connection_id: str | None = None) -> list[dict[str, Any]]:
    return [t.meta() for t in threads.list_for(connection_id)]


@app.post("/api/threads")
def new_thread(body: ThreadIn) -> dict[str, Any]:
    _connection(body.connection_id)          # 404 rather than an orphan
    return threads.create(body.connection_id).public()


@app.get("/api/threads/{thread_id}")
def get_thread(thread_id: str) -> dict[str, Any]:
    thread = threads.load(thread_id)
    if thread is None:
        raise HTTPException(404, f"no such thread: {thread_id}")
    body = thread.public()
    # A thread outlives the connection it was held against. It stays readable
    # and says why it cannot be continued, rather than vanishing or offering to
    # query a database that is gone.
    body["connection_missing"] = not any(
        c.id == thread.connection_id for c in config.load().connections
    )
    return body


@app.patch("/api/threads/{thread_id}")
def rename_thread(thread_id: str, body: ThreadPatch) -> dict[str, Any]:
    thread = threads.load(thread_id)
    if thread is None:
        raise HTTPException(404, f"no such thread: {thread_id}")
    title = " ".join(body.title.split()) or threads.UNTITLED
    thread.title = title[:threads.TITLE_MAX]
    threads.save(thread)
    return thread.meta()


@app.delete("/api/threads/{thread_id}")
def remove_thread(thread_id: str) -> dict[str, Any]:
    return {"ok": threads.delete(thread_id)}


# ── chat ──────────────────────────────────────────────────────────────────

class ChatIn(BaseModel):
    thread_id: str
    message: str


@app.post("/api/chat")
def chat(body: ChatIn) -> StreamingResponse:
    """Answer one question in a thread.

    The request carries a thread id and a question — **not** a history and not
    a connection. Both are read from the stored thread, which is what makes it
    impossible for one database's results to be replayed as context for a
    question about another. That used to be a runtime check on the client; it
    is now a property of the shape of this endpoint.
    """
    settings = config.load()
    thread = threads.load(body.thread_id)
    if thread is None:
        raise HTTPException(404, f"no such thread: {body.thread_id}")

    # Claimed as soon as the thread is known, and released by the stream's
    # `finally` or by the guard below. Nothing else is set up for a request
    # that is about to be rejected, and "this conversation is busy" is a truer
    # answer than a configuration error the caller cannot act on right now.
    if not threads.begin_turn(thread.id):
        raise HTTPException(
            409, "This conversation is already answering a question. Wait for "
                 "it to finish, or start a new conversation.")
    try:
        return _answer(settings, thread, body.message)
    except BaseException:
        # Any failure before the stream owns the claim has to give it back, or
        # the conversation refuses every later question forever.
        threads.end_turn(thread.id)
        raise


def _answer(settings: config.Settings, thread: threads.Thread,
            message: str) -> StreamingResponse:
    """The turn itself. The caller holds the thread's claim."""
    conn = _connection(thread.connection_id)
    profile = settings.for_connection(conn)
    if profile is None:
        raise HTTPException(
            400, "No model configured. Add one in Settings."
        )

    connector = Connector(conn.kind, conn.dsn(), conn.database)
    # A query-language family decision (currently SQL vs Mongo), not the
    # per-database growth the DSN chain-of-ifs had before the connector
    # registry existed — every SQL product already shares SqlTool. If a
    # third query paradigm ever shows up, that's the point to generalize
    # this into a per-connector hook; not before.
    tool = MongoQueryTool(connector) if conn.kind == "mongodb" else SqlTool(connector)
    chart_tool = ChartTool(tool)

    system = SYSTEM_PROMPT
    # The dialect note goes in before anything else: which product this is
    # changes how every query in the conversation must be written.
    system += f"\n## This connection\n\n{connector.dialect().prompt_note}\n"
    if conn.facts:
        # Generated facts, clearly labelled as such. Rule 7 of the accuracy
        # instruction tells the model these are not query results -- measured:
        # a facts block does not stop recitation on its own, so it is paired
        # with the instruction rather than trusted instead of it.
        system += (
            "\n## Reference: generated data facts for this database\n\n"
            "These were measured from the database at the time stated. They "
            "describe data reliability; they are not query results and are not "
            "answers.\n\n" + conn.facts
        )

    runner = FidelityRunner(
        providers.build(profile.kind, model=profile.model, api_key=profile.key(),
                        base_url=profile.base_url, max_tokens=profile.max_tokens),
        tools=[tool, chart_tool],
        system_prompt=system,
    )

    history = thread.messages() + [{"role": "user", "content": message}]
    threads.append_event(thread.id, "user", text=message)

    def events():
        # Prose deltas are streamed as they arrive but stored consolidated:
        # the transcript is a record of what was said, not of how it was
        # chunked over the wire.
        answer: list[str] = []
        try:
            for event in runner.run(history):
                payload: dict[str, Any] = {"type": event.type}
                if event.type == "text":
                    payload["text"] = event.text
                    answer.append(event.text)
                elif event.type == "tool_call":
                    payload["name"] = event.name
                    # `run_sql` and `mongo_query` both have a real request
                    # worth showing, in their own shape; a chart request does
                    # not (the columns and type chosen are already implicit
                    # in the result that follows), so no field grows here for
                    # that one.
                    if event.name == "run_sql":
                        payload["sql"] = event.arguments.get("query", "")
                    elif event.name == "mongo_query":
                        payload["query"] = _mongo_shell_text(
                            {k: v for k, v in event.arguments.items() if k != "max_rows"})
                    threads.append_event(thread.id, "tool_call", name=event.name,
                                         sql=payload.get("sql", ""),
                                         query=payload.get("query", ""))
                elif event.type == "tool_result":
                    payload["name"] = event.name
                    payload["result"] = event.result
                    payload["is_error"] = event.is_error
                    threads.append_event(thread.id, "tool_result", name=event.name,
                                         result=event.result,
                                         is_error=event.is_error)
                elif event.type == "notice":
                    # Live status only. Not stored: a transcript is a record of
                    # what was said, not of how hard the transport worked.
                    payload["text"] = event.text
                elif event.type in ("error", "stripped_link"):
                    payload["text"] = event.text
                    threads.append_event(thread.id, event.type, text=event.text)
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            connector.close()
            # Persisted here rather than by the browser, so a tab closed
            # mid-answer still keeps the turn.
            threads.append_event(thread.id, "answer", text="".join(answer))
            # Released even if the client disconnected: closing the generator
            # runs this block, so a dropped connection cannot strand the thread
            # in a state where it refuses every later question.
            threads.end_turn(thread.id)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# The page is the application: a browser holding a stale copy is running old
# code against a current server, which during development looked exactly like
# a logic bug -- a fix verified server-side that "did not work" in the tab that
# was never reloaded. Revalidate every time; it is one request on loopback.
NO_STORE = {"Cache-Control": "no-store, must-revalidate"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(HERE / "static" / "index.html", headers=NO_STORE)


@app.get("/api/kinds")
def kinds() -> dict[str, Any]:
    """What is installed, for a settings page that builds itself.

    The alternative is a hard-coded list in the HTML, which makes "drop in a
    module" false: the plugin would work and stay invisible.
    """
    return {"databases": connectors.catalogue(),
            "providers": providers.catalogue()}


@app.get("/api/version")
def version() -> dict[str, str]:
    """What the page shows in its header, so the running build is visible."""
    return {"version": VERSION}


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
