"""Tests for the conversation store.

Two properties carry most of the weight. The first is that `messages()` derives
model history from the stored events and nothing else, so there is no second
copy of a conversation to drift. The second is that a thread names its
connection, which is what makes cross-database history impossible to express
rather than merely rejected — see `test_api_threads.py`.

The rest is the unglamorous half of a store: a corrupt file must not take the
sidebar down with it, an interrupted write must not truncate a conversation,
and an id arriving from a URL must not be able to name a path outside the data
directory.
"""

from __future__ import annotations

import json
import stat

import pytest

from app import config, threads


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


# ── round trips ───────────────────────────────────────────────────────────

def test_a_thread_round_trips():
    made = threads.create("conn-a")
    made.append("user", text="how many stores?")
    made.append("answer", text="There are 2.")
    threads.save(made)

    back = threads.load(made.id)
    assert back is not None
    assert back.connection_id == "conn-a"
    assert [e["kind"] for e in back.events] == ["user", "answer"]


def test_an_unknown_thread_is_none_not_an_error():
    assert threads.load("deadbeef") is None


def test_a_new_thread_is_stored_immediately():
    made = threads.create("conn-a")
    assert threads.load(made.id) is not None


# ── the model's view ──────────────────────────────────────────────────────

def test_history_is_derived_from_events():
    t = threads.create("c")
    t.append("user", text="first")
    t.append("tool_call", sql="SELECT 1")
    t.append("tool_result", result="{}", is_error=False)
    t.append("answer", text="one")
    assert t.messages() == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "one"},
    ]


def test_tool_results_are_not_replayed_to_the_model():
    """Preserves existing behaviour: a follow-up sees prior prose, not prior
    rows. Changing that is a real decision, not a side effect of persistence."""
    t = threads.create("c")
    t.append("user", text="q")
    t.append("tool_result", result='{"rows": [[1]]}', is_error=False)
    t.append("answer", text="a")
    assert all("rows" not in m["content"] for m in t.messages())


def test_a_turn_that_produced_no_answer_does_not_break_alternation():
    """A failed turn leaves a user event with no answer. Emitted naively that
    puts two user turns back to back, which the Messages API rejects."""
    t = threads.create("c")
    t.append("user", text="first")
    t.append("answer", text="")
    t.append("user", text="second")
    roles = [m["role"] for m in t.messages()]
    assert roles == ["user"]
    assert t.messages()[0]["content"] == "first\n\nsecond"


def test_history_always_begins_with_a_user_turn():
    t = threads.create("c")
    t.append("answer", text="stray")
    t.append("user", text="q")
    t.append("answer", text="a")
    assert [m["role"] for m in t.messages()] == ["user", "assistant"]


def test_empty_events_are_skipped():
    t = threads.create("c")
    t.append("user", text="q")
    t.append("answer", text="")
    assert t.messages() == [{"role": "user", "content": "q"}]


# ── titles ────────────────────────────────────────────────────────────────

def test_the_title_comes_from_the_first_question():
    t = threads.create("c")
    t.append("user", text="How many stores are there?")
    assert t.title == "How many stores are there?"


def test_the_title_does_not_change_on_later_questions():
    t = threads.create("c")
    t.append("user", text="first question")
    t.append("user", text="second question")
    assert t.title == "first question"


def test_a_long_title_is_truncated_on_a_word_boundary():
    t = threads.create("c")
    t.append("user", text="show me every customer who rented more than thirty films last year please")
    assert len(t.title) <= threads.TITLE_MAX + 1
    assert t.title.endswith("…")
    assert not t.title.rstrip("…").endswith(" ")


def test_a_single_long_word_is_still_truncated():
    assert len(threads.derive_title("x" * 200)) <= threads.TITLE_MAX + 1


def test_whitespace_is_collapsed_in_a_title():
    assert threads.derive_title("  how   many\n\nstores? ") == "how many stores?"


def test_an_empty_question_leaves_the_default_title():
    assert threads.derive_title("   ") == threads.UNTITLED


# ── listing ───────────────────────────────────────────────────────────────

def test_listing_filters_by_connection():
    a = threads.create("conn-a")
    b = threads.create("conn-b")
    ids = {t.id for t in threads.list_for("conn-a")}
    assert ids == {a.id} and b.id not in ids


def test_listing_without_a_connection_returns_everything():
    threads.create("conn-a")
    threads.create("conn-b")
    assert len(threads.list_for()) == 2


def test_listing_is_newest_activity_first():
    first = threads.create("c")
    second = threads.create("c")
    first.append("user", text="later activity")   # bumps updated_at
    threads.save(first)
    assert [t.id for t in threads.list_for("c")] == [first.id, second.id]


def test_listing_is_empty_before_anything_is_stored():
    assert threads.list_for("c") == []


def test_a_corrupt_file_is_skipped_rather_than_breaking_the_list(store):
    good = threads.create("c")
    (store / "threads" / "bad0bad0.json").write_text("{ this is not json")
    listed = [t.id for t in threads.list_for("c")]
    assert listed == [good.id]


def test_metadata_omits_the_transcript():
    t = threads.create("c")
    t.append("user", text="q")
    t.append("answer", text="a")
    meta = t.meta()
    assert "events" not in meta
    assert meta["turns"] == 1


# ── deletion ──────────────────────────────────────────────────────────────

def test_delete_removes_the_file():
    t = threads.create("c")
    assert threads.delete(t.id) is True
    assert threads.load(t.id) is None
    assert threads.list_for("c") == []


def test_deleting_an_unknown_thread_is_false_not_an_error():
    assert threads.delete("abcdef") is False


# ── the file on disk ──────────────────────────────────────────────────────

def test_files_are_not_world_readable(store):
    t = threads.create("c")
    mode = stat.S_IMODE((store / "threads" / f"{t.id}.json").stat().st_mode)
    assert mode == 0o600


def test_the_directory_is_not_world_readable(store):
    threads.create("c")
    assert stat.S_IMODE((store / "threads").stat().st_mode) == 0o700


def test_a_save_leaves_no_temporary_file_behind(store):
    t = threads.create("c")
    t.append("user", text="q")
    threads.save(t)
    assert list((store / "threads").glob("*.tmp")) == []


def test_the_stored_file_is_readable_json(store):
    t = threads.create("c")
    t.append("user", text="q")
    threads.save(t)
    raw = json.loads((store / "threads" / f"{t.id}.json").read_text())
    assert raw["connection_id"] == "c"
    assert raw["events"][0]["text"] == "q"


# ── ids arriving from a URL ───────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "../../etc/passwd",
    "../secret",
    "a/b",
    "",
    "NOTHEX!",
    "x" * 64,
])
def test_a_malicious_id_cannot_escape_the_data_directory(bad):
    """Ids reach the store straight from a URL path. `..` joined onto a path
    would read and overwrite files outside the data directory."""
    assert threads.load(bad) is None
    assert threads.delete(bad) is False


def test_a_traversal_id_does_not_create_a_file(store, tmp_path):
    victim = tmp_path / "victim.json"
    victim.write_text("original")
    assert threads.delete("../victim") is False
    assert victim.read_text() == "original"


# ── concurrent writes ─────────────────────────────────────────────────────

def test_concurrent_appends_keep_every_event():
    """Two turns in one conversation used to lose events.

    Each request loaded the thread, appended to its own copy and saved, so the
    later write discarded whatever the earlier had added. The loud half of the
    same bug was a shared `<id>.json.tmp`: concurrent saves renamed it out from
    under each other and `os.replace` raised FileNotFoundError.
    """
    import threading as _threading

    thread = threads.create("c")
    errors: list[str] = []

    def hammer(tag: str) -> None:
        try:
            for i in range(25):
                threads.append_event(thread.id, "user", text=f"{tag}-{i}")
        except Exception as exc:            # noqa: BLE001 — reported below
            errors.append(f"{type(exc).__name__}: {exc}")

    workers = [_threading.Thread(target=hammer, args=(f"w{n}",)) for n in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    assert errors == []
    assert len(threads.load(thread.id).events) == 100


def test_a_save_leaves_no_temporary_file_behind_under_contention(store):
    import threading as _threading

    thread = threads.create("c")
    workers = [_threading.Thread(
        target=lambda: [threads.append_event(thread.id, "user", text="x")
                        for _ in range(10)]) for _ in range(3)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert list((store / "threads").glob("*.tmp")) == []


def test_appending_to_an_unknown_thread_is_none_not_an_error():
    assert threads.append_event("abcdef12", "user", text="x") is None


def test_a_stray_temp_file_is_not_listed_as_a_thread(store):
    threads.create("c")
    (store / "threads" / "deadbeef.abc.tmp").write_text("{}")
    assert len(threads.list_for("c")) == 1
