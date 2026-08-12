"""Conversation storage: one JSON file per thread.

A thread is a conversation bound to one connection. That binding is the point:
before threads existed, the client held the history and the active database was
a separate piece of UI state, so a settings dialog that quietly reset the
picker sent one database's tool results as context for a question about
another. The fix at the time was a runtime check on the client. This module
removes the need for one — history is read from the thread, the thread names
its connection, and the client has no way to supply history at all.

**Layout.** `<data>/threads/<id>.json`, `0600`, in a `0700` directory, matching
`config.py`'s posture. One file per thread rather than one large file: a turn
appends without rewriting every other conversation, and deleting is an unlink.

Listing reads every file. That is fine for the tens-to-hundreds of threads a
single-user tool accumulates, and it avoids a separate index that can disagree
with the files it describes. Past a few hundred threads it should become an
index; it is called out here so that is a decision rather than a discovery.

**Events are the record.** The messages sent to the model are derived from
them, so there is no second copy of the conversation to drift out of step.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

TITLE_MAX = 60
UNTITLED = "New conversation"

# Thread ids reach this module straight from a URL path. Anything that is not a
# plain hex token is refused rather than joined onto a path, because `..` in an
# id would otherwise read and overwrite files outside the data directory.
_ID = re.compile(r"^[0-9a-f]{6,32}$")


# One lock per thread id, so two turns in the same conversation serialise and
# two different conversations do not. Writes used to share a single
# `<id>.json.tmp` name per thread: concurrent saves renamed it out from under
# each other and `os.replace` raised FileNotFoundError, which is the loud
# failure. The quiet one was worse — two overlapping load/append/save cycles
# lose whichever events the slower one had not read.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def lock_for(thread_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(thread_id, threading.Lock())


# Per-thread locking makes each *append* atomic; it does not make a whole turn
# atomic, and a turn is the thing that has to be. A request reads the history,
# then streams for tens of seconds, then writes. Two overlapping turns both
# answer from the same stale history and interleave their events, leaving a
# transcript whose next `messages()` is not a conversation anyone had.
#
# Rejected rather than queued. A conversation is sequential by nature: the
# second question was asked without its author having seen the first answer, so
# serialising would produce an answer to a question that no longer makes sense,
# several seconds late and looking like a hang.
_IN_FLIGHT: set[str] = set()


def begin_turn(thread_id: str) -> bool:
    """Claim a thread for one turn. False if a turn is already running."""
    with _LOCKS_GUARD:
        if thread_id in _IN_FLIGHT:
            return False
        _IN_FLIGHT.add(thread_id)
        return True


def end_turn(thread_id: str) -> None:
    with _LOCKS_GUARD:
        _IN_FLIGHT.discard(thread_id)


def turn_in_flight(thread_id: str) -> bool:
    with _LOCKS_GUARD:
        return thread_id in _IN_FLIGHT


def _now() -> str:
    """Microsecond precision, deliberately.

    Second resolution reads fine and sorts badly: two conversations touched in
    the same second order arbitrarily, and the sidebar is a list people
    navigate by recency. The extra digits cost nothing and are never shown —
    the browser renders a time, a weekday, or a date.
    """
    return datetime.now(timezone.utc).isoformat()


def _dir() -> Path:
    """Resolved on every call so tests can redirect `config.DATA_DIR`."""
    return config.DATA_DIR / "threads"


def _path(thread_id: str) -> Path:
    if not _ID.match(thread_id or ""):
        raise ValueError(f"invalid thread id: {thread_id!r}")
    return _dir() / f"{thread_id}.json"


def derive_title(text: str) -> str:
    """A thread's name is its opening question, which is what people recognise."""
    flat = " ".join((text or "").split())
    if not flat:
        return UNTITLED
    if len(flat) <= TITLE_MAX:
        return flat
    # Break on a word boundary where there is one close enough to the limit.
    clipped = flat[:TITLE_MAX]
    spaced = clipped.rsplit(" ", 1)[0]
    return (spaced if len(spaced) > TITLE_MAX - 15 else clipped).rstrip() + "…"


@dataclass
class Thread:
    id: str
    connection_id: str
    title: str = UNTITLED
    created_at: str = ""
    updated_at: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    # ── what the model sees ───────────────────────────────────────────────

    def messages(self) -> list[dict[str, str]]:
        """Model history, derived from the stored events.

        Only `user` and `answer` events become turns: this preserves the
        existing behaviour where a follow-up sees prior prose but not prior
        tool results, and it means the derivation has exactly one input.

        Consecutive same-role turns are joined rather than emitted separately.
        A question that produced an error has no answer, so without this a
        second question would put two user turns back to back, which the
        Messages API rejects. Leading assistant turns are dropped for the same
        reason — a conversation has to begin with a user.
        """
        out: list[dict[str, str]] = []
        for event in self.events:
            kind = event.get("kind")
            if kind == "user":
                role, text = "user", event.get("text", "")
            elif kind == "answer":
                role, text = "assistant", event.get("text", "")
            else:
                continue
            if not text:
                continue
            if out and out[-1]["role"] == role:
                out[-1]["content"] += "\n\n" + text
            else:
                out.append({"role": role, "content": text})
        while out and out[0]["role"] != "user":
            out.pop(0)
        return out

    # ── what the browser sees ─────────────────────────────────────────────

    def meta(self) -> dict[str, Any]:
        """Sidebar row: everything but the transcript."""
        d = asdict(self)
        d.pop("events")
        d["turns"] = sum(1 for e in self.events if e.get("kind") == "user")
        return d

    def public(self) -> dict[str, Any]:
        return asdict(self)

    # ── mutation ──────────────────────────────────────────────────────────

    def append(self, kind: str, **payload: Any) -> None:
        self.events.append({"kind": kind, **payload})
        self.updated_at = _now()
        if kind == "user" and self.title == UNTITLED:
            self.title = derive_title(payload.get("text", ""))


def create(connection_id: str) -> Thread:
    thread = Thread(
        id=uuid.uuid4().hex[:12],
        connection_id=connection_id,
        created_at=_now(),
        updated_at=_now(),
    )
    save(thread)
    return thread


def save(thread: Thread) -> None:
    """Write via a temporary file and rename, so an interrupted write cannot
    truncate an existing conversation.

    Callers doing read-modify-write should use `append_event`, or hold
    `lock_for(id)` across the whole cycle; this only makes the write itself
    atomic, not the sequence around it.
    """
    directory = _dir()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    final = _path(thread.id)
    # A unique temp name per write. Sharing one name per thread meant a
    # concurrent save could rename it away mid-write.
    handle, tmp_name = tempfile.mkstemp(dir=directory, prefix=f"{thread.id}.",
                                        suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w") as out:
            out.write(json.dumps(asdict(thread), indent=2))
        tmp.chmod(0o600)
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def append_event(thread_id: str, kind: str, **payload: Any) -> Thread | None:
    """Add one event to a stored thread, atomically.

    Load-append-save as three separate steps loses events when two turns
    overlap: each reads the thread, adds to its own copy, and the later write
    silently discards whatever the earlier one added. Re-reading inside the
    lock is what makes the write reflect everything already there.
    """
    with lock_for(thread_id):
        thread = load(thread_id)
        if thread is None:
            return None
        thread.append(kind, **payload)
        save(thread)
        return thread


def load(thread_id: str) -> Thread | None:
    try:
        path = _path(thread_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        return Thread(**json.loads(path.read_text()))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def list_for(connection_id: str | None = None) -> list[Thread]:
    """Threads, newest activity first. Unreadable files are skipped.

    A single corrupt file must not make the sidebar unreachable — losing one
    conversation is recoverable, losing the list of all of them is not.
    """
    directory = _dir()
    if not directory.exists():
        return []
    out: list[Thread] = []
    for path in directory.glob("*.json"):
        if path.name.endswith(".tmp"):
            continue
        thread = load(path.stem)
        if thread is None:
            continue
        if connection_id is None or thread.connection_id == connection_id:
            out.append(thread)
    return sorted(out, key=lambda t: t.updated_at, reverse=True)


def delete(thread_id: str) -> bool:
    try:
        path = _path(thread_id)
    except ValueError:
        return False
    if path.exists():
        path.unlink()
        return True
    return False


# Deliberately absent: deleting a connection does *not* delete its threads.
# Removing a connection to re-add it with a corrected password is an ordinary
# thing to do, and it would silently destroy every conversation held against
# it. Orphaned threads stay on disk and stop being listed, which is clutter;
# the alternative is data loss, which is worse. If reaching them again matters,
# that is an "orphaned threads" view later, not an unlink now.
