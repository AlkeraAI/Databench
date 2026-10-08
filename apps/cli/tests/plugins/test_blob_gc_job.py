"""The ``blob_gc`` scheduler job — registers + runs the blob mark-and-sweep.

Mirrors the kb_prune job test: register on a real Scheduler over a tmp store,
run it, and assert it reclaims an unreferenced blob while keeping a referenced one.
Uses a real past mtime (the sweep is mtime/grace-keyed, not freezegun-able).
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from alkera_cli.plugins.plugin_base.blob_gc_job import BLOB_GC_KIND, schedule_blob_gc
from alkera_cli.plugins.plugin_base.scheduler import Scheduler, SchedulerStore
from alkera_core.project.directory import ProjectDirectory

_PAST = 48 * 3600  # seconds — comfortably older than the 24h GC grace


def _age(path: Path) -> None:
    old = time.time() - _PAST
    os.utime(path, (old, old))


async def _run_job(project: ProjectDirectory) -> None:
    scheduler = Scheduler(SchedulerStore(project.scheduler_path))
    job_id = schedule_blob_gc(scheduler, project, now=datetime.now(UTC))
    assert job_id == BLOB_GC_KIND
    assert BLOB_GC_KIND in scheduler.registered_kinds()
    assert await scheduler.run_now(BLOB_GC_KIND) is True
    await scheduler._tasks[BLOB_GC_KIND]  # await the spawned run


async def test_blob_gc_job_reclaims_an_orphan(tmp_path: Path) -> None:
    project = ProjectDirectory(tmp_path / ".alkera")
    blobs = project.blobs()
    sha, _ = blobs.write(b"orphan - referenced by no chat")
    _age(blobs.path(sha))

    await _run_job(project)

    assert not blobs.exists(sha)


async def test_blob_gc_job_keeps_a_referenced_blob(tmp_path: Path) -> None:
    project = ProjectDirectory(tmp_path / ".alkera")
    blobs = project.blobs()
    sha, _ = blobs.write(b"referenced by a live chat")
    _age(blobs.path(sha))
    # A chat references it via a spilled-result handle → must survive the sweep.
    chat_dir = project.chats_path / "c1"
    chat_dir.mkdir(parents=True)
    event = {
        "event_type": "part.created",
        "part": {"type": "tool", "output": {"blob": {"sha256": sha, "size": 1, "media_type": "x"}}},
    }
    (chat_dir / "chat.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    await _run_job(project)

    assert blobs.exists(sha)
