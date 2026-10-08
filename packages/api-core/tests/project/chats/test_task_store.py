"""Tests for the per-chat ``TaskStore`` (tasks.json persistence).

Covers the load-bearing invariants: round-trip, graceful degradation on a
missing/corrupt file, a rejected mutation leaving the file byte-identical, and —
critically — that concurrent same-turn mutations serialize without a lost update
(the OpenCode parallel-dispatch hazard the ``asyncio.Lock`` exists to close).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alkera_core.project.chats.tasks import TaskStore
from alkera_core.schemas.chat.tasks import TaskValidationError

NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


async def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    assert (await store.load()).is_empty()


async def test_apply_persists_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    await store.apply(upsert=[{"id": "a", "title": "Do A"}], now=NOW)
    # A fresh instance reads it back from disk (no in-memory cheating).
    reloaded = await TaskStore(path).load()
    assert [t.id for t in reloaded.tasks] == ["a"]
    assert reloaded.by_id()["a"].title == "Do A"


async def test_apply_returns_full_updated_list(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    await store.apply(upsert=[{"id": "a", "title": "A"}], now=NOW)
    out = await store.apply(upsert=[{"id": "b", "title": "B", "depends_on": ["a"]}], now=NOW)
    assert [t.id for t in out.tasks] == ["a", "b"]
    assert out.blocked_by("b") == ["a"]


async def test_corrupt_file_loads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    path.write_text("{ this is not json")
    assert (await TaskStore(path).load()).is_empty()


async def test_rejected_mutation_leaves_file_byte_identical(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    await store.apply(
        upsert=[{"id": "a", "title": "A"}, {"id": "b", "title": "B", "depends_on": ["a"]}],
        now=NOW,
    )
    before = path.read_bytes()
    # Introducing a cycle must be rejected and write nothing.
    with pytest.raises(TaskValidationError, match="cycle"):
        await store.apply(upsert=[{"id": "a", "depends_on": ["b"]}], now=NOW)
    assert path.read_bytes() == before


async def test_partial_corrupt_load_salvages_valid_tasks(tmp_path: Path) -> None:
    """One unparseable task must NOT discard the rest — the valid tasks load, only
    the bad node is dropped (the catastrophic 'whole list wiped' bug)."""
    path = tmp_path / "tasks.json"
    # Two valid tasks + one whose status is a value a newer writer added.
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "tasks": [
                    {"id": "a", "title": "A", "status": "pending", "depends_on": []},
                    {"id": "b", "title": "B", "status": "deferred", "depends_on": []},
                    {"id": "c", "title": "C", "status": "completed", "depends_on": []},
                ],
            }
        )
    )
    loaded = await TaskStore(path).load()
    assert [t.id for t in loaded.tasks] == ["a", "c"]  # valid kept, bad 'b' dropped


async def test_salvaged_tasks_survive_the_next_mutation(tmp_path: Path) -> None:
    """The downstream of the salvage fix: a subsequent mutation builds on the
    salvaged list, so the user's real tasks are NOT persisted away."""
    path = tmp_path / "tasks.json"
    path.write_text(
        json.dumps(
            {
                "tasks": [
                    {"id": "a", "title": "A", "status": "pending", "depends_on": []},
                    {"id": "bad", "title": "Bad", "status": "deferred", "depends_on": []},
                ]
            }
        )
    )
    store = TaskStore(path)
    await store.apply(upsert=[{"id": "new", "title": "New"}], now=NOW)
    final = await TaskStore(path).load()
    assert sorted(t.id for t in final.tasks) == ["a", "new"]  # 'a' preserved, not clobbered


async def test_unreadable_file_loads_as_empty(tmp_path: Path) -> None:
    """A non-FileNotFound OSError on read (here: a directory where the file should
    be) degrades to empty — the store's 'never raises' promise holds."""
    path = tmp_path / "tasks.json"
    path.mkdir()  # read_bytes() raises IsADirectoryError (an OSError)
    assert (await TaskStore(path).load()).is_empty()


async def test_concurrent_applies_have_no_lost_update(tmp_path: Path) -> None:
    """Fire N concurrent single-task adds; the lock must serialize the
    read-modify-write so every task lands (no clobber)."""
    store = TaskStore(tmp_path / "tasks.json")
    n = 25
    await asyncio.gather(
        *(store.apply(upsert=[{"id": f"t{i:02d}", "title": f"T{i}"}], now=NOW) for i in range(n))
    )
    final = await store.load()
    assert sorted(t.id for t in final.tasks) == sorted(f"t{i:02d}" for i in range(n))
