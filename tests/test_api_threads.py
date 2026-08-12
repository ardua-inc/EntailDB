"""API-level tests for threads, including the invariant that replaced a guard.

Before threads, "history must not cross databases" was a runtime check in the
browser, added after a settings dialog silently reset the connection picker and
one database's tool results were sent as context for a question about another.
That check could only ever reject a bad request. The chat endpoint now takes a
thread id and a question — no history, no connection — so a cross-database
request has no way to be expressed at all. `test_a_client_cannot_supply_its_own
_history` and its neighbours are what hold that shape in place; if someone adds
a `messages` field back, they fail.

The model is never called here. `/api/chat` is exercised only for the failures
that happen before a provider is reached, because everything past that point is
network I/O against Anthropic and belongs in the eval harness.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, main, threads


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "STORE", tmp_path / "connections.json")
    monkeypatch.setattr(config, "KEYFILE", tmp_path / "secret.key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def conn(client):
    """A stored connection. SQLite so nothing needs a server to exist."""
    body = {"label": "local", "kind": "sqlite", "host": "", "port": 0,
            "database": "/tmp/does-not-matter.sqlite3", "user": "", "password": ""}
    return client.post("/api/connections", json=body).json()["id"]


# ── lifecycle ─────────────────────────────────────────────────────────────

def test_a_thread_is_created_against_a_connection(client, conn):
    made = client.post("/api/threads", json={"connection_id": conn}).json()
    assert made["connection_id"] == conn
    assert made["events"] == []


def test_a_thread_cannot_be_created_against_an_unknown_connection(client):
    r = client.post("/api/threads", json={"connection_id": "nope"})
    assert r.status_code == 404


def test_threads_are_listed_for_their_own_connection_only(client, conn):
    other = client.post("/api/connections", json={
        "label": "other", "kind": "sqlite", "host": "", "port": 0,
        "database": "/tmp/other.sqlite3", "user": "", "password": ""}).json()["id"]
    mine = client.post("/api/threads", json={"connection_id": conn}).json()["id"]
    client.post("/api/threads", json={"connection_id": other})

    listed = client.get(f"/api/threads?connection_id={conn}").json()
    assert [t["id"] for t in listed] == [mine]


def test_listing_returns_metadata_without_transcripts(client, conn):
    client.post("/api/threads", json={"connection_id": conn})
    row = client.get(f"/api/threads?connection_id={conn}").json()[0]
    assert "events" not in row
    assert row["turns"] == 0


def test_a_thread_can_be_renamed(client, conn):
    tid = client.post("/api/threads", json={"connection_id": conn}).json()["id"]
    client.patch(f"/api/threads/{tid}", json={"title": "  revenue  by  category "})
    assert client.get(f"/api/threads/{tid}").json()["title"] == "revenue by category"


def test_renaming_to_nothing_restores_the_default(client, conn):
    tid = client.post("/api/threads", json={"connection_id": conn}).json()["id"]
    client.patch(f"/api/threads/{tid}", json={"title": "   "})
    assert client.get(f"/api/threads/{tid}").json()["title"] == threads.UNTITLED


def test_a_thread_can_be_deleted(client, conn):
    tid = client.post("/api/threads", json={"connection_id": conn}).json()["id"]
    assert client.delete(f"/api/threads/{tid}").json()["ok"] is True
    assert client.get(f"/api/threads/{tid}").status_code == 404


def test_an_unknown_thread_is_404(client):
    assert client.get("/api/threads/abcdef12").status_code == 404


# ── the invariant ─────────────────────────────────────────────────────────

def test_a_client_cannot_supply_its_own_history(client, conn):
    """The shape of the request is the guarantee.

    A `messages` field is not merely ignored — there is no field for a client
    to put another database's turns into. If this starts passing with history
    accepted, the protection built in 0.1.5 has been undone.
    """
    tid = client.post("/api/threads", json={"connection_id": conn}).json()["id"]
    r = client.post("/api/chat", json={
        "thread_id": tid, "message": "hi",
        "messages": [{"role": "user", "content": "smuggled from another database"}],
        "connection_id": "some-other-connection",
    })
    # Rejected for having no model configured, having ignored both extra
    # fields — the point is that neither was ever a place to put history.
    assert r.status_code == 400
    assert "model" in r.json()["detail"].lower()


def test_chat_requires_a_known_thread(client):
    r = client.post("/api/chat", json={"thread_id": "abcdef12", "message": "hi"})
    assert r.status_code == 404


def test_chat_rejects_a_request_with_no_thread(client):
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 422


def test_the_connection_comes_from_the_thread_not_the_request(client, conn):
    """Deleting the connection a thread names makes the thread unusable — proof
    the connection is read from the thread rather than taken from the caller."""
    tid = client.post("/api/threads", json={"connection_id": conn}).json()["id"]
    client.delete(f"/api/connections/{conn}")
    r = client.post("/api/chat", json={"thread_id": tid, "message": "hi"})
    assert r.status_code == 404
    assert "connection" in r.json()["detail"].lower()


# ── outliving a connection ────────────────────────────────────────────────

def test_a_thread_survives_its_connection_and_says_so(client, conn):
    """Deleting a connection to re-add it with a fixed password is ordinary.
    Destroying its conversations for that would be data loss."""
    tid = client.post("/api/threads", json={"connection_id": conn}).json()["id"]
    client.delete(f"/api/connections/{conn}")

    got = client.get(f"/api/threads/{tid}")
    assert got.status_code == 200
    assert got.json()["connection_missing"] is True


def test_a_live_thread_is_not_flagged_as_orphaned(client, conn):
    tid = client.post("/api/threads", json={"connection_id": conn}).json()["id"]
    assert client.get(f"/api/threads/{tid}").json()["connection_missing"] is False


# ── persistence across a restart ──────────────────────────────────────────

def test_a_transcript_survives_a_fresh_client(client, conn):
    """The point of the feature: a reload is a new client against the same
    store, and it must find the conversation intact."""
    tid = client.post("/api/threads", json={"connection_id": conn}).json()["id"]
    thread = threads.load(tid)
    thread.append("user", text="how many stores?")
    thread.append("tool_call", sql="SELECT count(*) FROM store")
    thread.append("tool_result", result='{"columns":["c"],"rows":[[2]]}', is_error=False)
    thread.append("answer", text="There are 2 stores.")
    threads.save(thread)

    fresh = TestClient(main.app).get(f"/api/threads/{tid}").json()
    assert [e["kind"] for e in fresh["events"]] == [
        "user", "tool_call", "tool_result", "answer"]
    # The rows are kept, so a restored answer can still be checked against them.
    assert "rows" in fresh["events"][2]["result"]
    assert fresh["title"] == "how many stores?"


# ── one turn at a time ────────────────────────────────────────────────────

def test_a_second_turn_on_the_same_thread_is_refused(client, conn):
    """Per-append locking makes each write atomic; it does not make a *turn*
    atomic, and a turn is the thing that has to be. Two overlapping requests
    both read the same history, stream for tens of seconds, and interleave
    their events — leaving a transcript whose next `messages()` is not a
    conversation anyone had."""
    tid = client.post("/api/threads", json={"connection_id": conn}).json()["id"]
    assert threads.begin_turn(tid) is True
    try:
        r = client.post("/api/chat", json={"thread_id": tid, "message": "hi"})
        assert r.status_code == 409
        assert "already answering" in r.json()["detail"]
    finally:
        threads.end_turn(tid)


def test_a_different_thread_is_unaffected(client, conn):
    """Rejection is per conversation, not global — two chats must not block
    each other."""
    busy = client.post("/api/threads", json={"connection_id": conn}).json()["id"]
    other = client.post("/api/threads", json={"connection_id": conn}).json()["id"]
    assert threads.begin_turn(busy) is True
    try:
        assert threads.begin_turn(other) is True
        threads.end_turn(other)
    finally:
        threads.end_turn(busy)


def test_the_claim_is_released_when_a_turn_fails_before_streaming(client, conn):
    """A missing API key aborts after the claim is taken. If the claim leaked,
    the conversation would refuse every later question forever."""
    tid = client.post("/api/threads", json={"connection_id": conn}).json()["id"]
    assert client.post("/api/chat", json={"thread_id": tid, "message": "hi"}).status_code == 400
    assert threads.turn_in_flight(tid) is False


def test_a_claim_on_an_unknown_thread_is_never_taken(client):
    """404 happens before the claim, so a bad id cannot strand anything."""
    client.post("/api/chat", json={"thread_id": "abcdef12", "message": "hi"})
    assert threads.turn_in_flight("abcdef12") is False
