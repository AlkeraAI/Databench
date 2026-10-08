"""Trace retention sweep: expiry boundary, subtree rule, disable switch."""

from __future__ import annotations

import json
from pathlib import Path

from alkera_cli.observability.trace_store import sweep_expired_traces
from alkera_core.project.directory import ProjectDirectory
from freezegun import freeze_time


def _store(tmp_path: Path):
    return ProjectDirectory(tmp_path / ".alkera").chats()


def _amend_manifest(store, session_id: str, **fields: object) -> None:
    path = store.path / session_id / "manifest.json"
    data = json.loads(path.read_text())
    data.update(fields)
    path.write_text(json.dumps(data))


def test_sweep_deletes_only_past_the_boundary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with freeze_time("2026-06-18T00:00:00+00:00") as frozen:
        old = store.create(harness_type="agent").manifest.session_id
        frozen.move_to("2026-06-20T00:00:00+00:00")
        fresh = store.create(harness_type="agent").manifest.session_id
        frozen.move_to("2026-07-19T00:00:00+00:00")
        removed = sweep_expired_traces(store, retention_days=30)
    assert removed == 1
    assert store.list_session_ids() == [fresh]
    assert old not in store.list_session_ids()


def test_fresh_child_keeps_its_expired_parent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with freeze_time("2026-05-01T00:00:00+00:00") as frozen:
        parent = store.create(harness_type="agent").manifest.session_id
        frozen.move_to("2026-07-18T00:00:00+00:00")
        child = store.create(harness_type="agent").manifest.session_id
        _amend_manifest(store, child, parent_session_id=parent)
        frozen.move_to("2026-07-19T00:00:00+00:00")
        removed = sweep_expired_traces(store, retention_days=30)
    assert removed == 0
    assert set(store.list_session_ids()) == {parent, child}


def test_expired_subtree_goes_together(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with freeze_time("2026-05-01T00:00:00+00:00") as frozen:
        parent = store.create(harness_type="agent").manifest.session_id
        child = store.create(harness_type="agent").manifest.session_id
        _amend_manifest(store, child, parent_session_id=parent)
        frozen.move_to("2026-07-19T00:00:00+00:00")
        removed = sweep_expired_traces(store, retention_days=30)
    assert removed == 1
    assert store.list_session_ids() == []


def test_running_background_job_blocks_deletion(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with freeze_time("2026-05-01T00:00:00+00:00") as frozen:
        sid = store.create(harness_type="agent").manifest.session_id
        _amend_manifest(store, sid, background_jobs_running=1)
        frozen.move_to("2026-07-19T00:00:00+00:00")
        assert sweep_expired_traces(store, retention_days=30) == 0
    assert store.list_session_ids() == [sid]


def test_zero_retention_disables_the_sweep(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with freeze_time("2020-01-01T00:00:00+00:00"):
        store.create(harness_type="agent")
    assert sweep_expired_traces(store, retention_days=0) == 0
    assert len(store.list_session_ids()) == 1
