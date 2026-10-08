"""PluginStore: typed, race-safe, on the existing .alkera plumbing."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alkera_cli.plugins.plugin_base import PluginState
from alkera_core.project.directory import ProjectDirectory
from pydantic import Field


class _DemoState(PluginState):
    SCHEMA_VERSION = "1.0.0"

    counter: int = 0
    discovered: list[str] = Field(default_factory=list)
    watermark: int | None = None


def _store(tmp_path: Path):  # type: ignore[no-untyped-def]
    project = ProjectDirectory(tmp_path / ".alkera")
    return project.plugins("demo", _DemoState)


async def test_load_defaults_on_first_run(tmp_path: Path) -> None:
    state = await _store(tmp_path).load()
    assert isinstance(state, _DemoState)
    assert state.counter == 0
    assert state.discovered == []


async def test_save_then_load_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.save(_DemoState(counter=7, discovered=["a", "b"], watermark=123))
    loaded = await store.load()
    assert loaded.counter == 7
    assert loaded.discovered == ["a", "b"]
    assert loaded.watermark == 123


async def test_state_persists_across_handles(tmp_path: Path) -> None:
    # A fresh handle to the same project sees prior writes (state is on disk,
    # not in the in-memory registry).
    await _store(tmp_path).save(_DemoState(counter=3))
    assert (await _store(tmp_path).load()).counter == 3


async def test_update_is_race_safe_no_lost_updates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.save(_DemoState(counter=0))

    def _inc(s: _DemoState) -> _DemoState:
        return s.model_copy(update={"counter": s.counter + 1})

    n = 25
    await asyncio.gather(*[store.update(_inc) for _ in range(n)])
    assert (await store.load()).counter == n


async def test_for_connection_namespaces_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.for_connection("snow_prod").save(_DemoState(watermark=100))
    await store.for_connection("snow_dev").save(_DemoState(watermark=200))

    assert (await store.for_connection("snow_prod").load()).watermark == 100
    assert (await store.for_connection("snow_dev").load()).watermark == 200
    # The plugin-level state is independent of any connection's.
    assert (await store.load()).watermark is None


async def test_append_and_read_log_preserve_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.append("refresh", _DemoState(counter=1))
    await store.append("refresh", _DemoState(counter=2))
    await store.append("refresh", _DemoState(counter=3))

    counters = [rec["counter"] for rec in store.read_log("refresh")]
    assert counters == [1, 2, 3]


def test_read_log_missing_is_empty(tmp_path: Path) -> None:
    assert list(_store(tmp_path).read_log("never_written")) == []


def test_blobs_dedup_by_content(tmp_path: Path) -> None:
    blobs = _store(tmp_path).blobs()
    sha1, size1 = blobs.write(b"INFORMATION_SCHEMA dump")
    sha2, size2 = blobs.write(b"INFORMATION_SCHEMA dump")
    assert sha1 == sha2
    assert size1 == size2
    assert blobs.read(sha1) == b"INFORMATION_SCHEMA dump"


def test_invalid_log_name_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="invalid log name"):
        list(store.read_log("../escape"))
