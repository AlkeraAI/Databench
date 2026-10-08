"""A chat whose agent database came from another box: the agent starts but
cannot list its sessions. The transcript is the record and the store a cache,
so the adapter sets the store aside and opens a fresh session once — never a
retry loop on the same unreadable file, and never a second set-aside when a
fresh store refuses too."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from _mocks.adapter_seam import opencode_adapter
from alkera_cli.harness.adapter import (
    HarnessStartRefusedError,
    HarnessStoreUnreadableError,
)
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.opencode_db import set_aside_agent_store


def _store(adapter: OpencodeHttpAdapter) -> Path:
    db = adapter._harness_dir / "agent" / "agent.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"from another box")
    db.with_name("agent.db-wal").write_bytes(b"wal")
    db.with_name("agent.db-shm").write_bytes(b"shm")
    return db


@pytest.mark.asyncio
async def test_an_unreadable_store_is_set_aside_once_and_the_chat_starts_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = opencode_adapter(tmp_path, session_id="foreign-store", wired=False)
    adapter._config.harness_native["agent_session_id"] = "ses_from_the_other_box"
    db = _store(adapter)
    attempts: list[str | None] = []

    async def _start_once() -> None:
        attempts.append(adapter._config.harness_native.get("agent_session_id"))
        if len(attempts) == 1:
            raise HarnessStoreUnreadableError("agent /session list failed: 500")
        adapter._state.started = True

    monkeypatch.setattr(adapter, "_start_once", _start_once)
    await adapter.start()

    # One refusal, one fresh attempt with the pin dropped — no third.
    assert attempts == ["ses_from_the_other_box", None]
    assert not db.exists() and not db.with_name("agent.db-wal").exists()
    aside = sorted(db.parent.glob("agent.db.unreadable-*"))
    stamp = aside[0].name.removeprefix("agent.db.unreadable-").split("-")[0]
    assert [p.name for p in aside] == [
        f"agent.db.unreadable-{stamp}",
        f"agent.db.unreadable-{stamp}-shm",
        f"agent.db.unreadable-{stamp}-wal",
    ]
    assert aside[0].read_bytes() == b"from another box"


@pytest.mark.asyncio
async def test_a_fresh_store_that_refuses_too_is_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = opencode_adapter(tmp_path, session_id="broken-agent", wired=False)
    _store(adapter)
    attempts = 0

    async def _start_once() -> None:
        nonlocal attempts
        attempts += 1
        raise HarnessStoreUnreadableError("agent /session list failed: 500")

    monkeypatch.setattr(adapter, "_start_once", _start_once)
    with pytest.raises(HarnessStoreUnreadableError):
        await adapter.start()
    assert attempts == 2


@pytest.mark.asyncio
async def test_a_refusal_with_no_store_to_set_aside_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = opencode_adapter(tmp_path, session_id="no-store", wired=False)
    attempts = 0

    async def _start_once() -> None:
        nonlocal attempts
        attempts += 1
        raise HarnessStoreUnreadableError("agent /session list failed: 500")

    monkeypatch.setattr(adapter, "_start_once", _start_once)
    with pytest.raises(HarnessStoreUnreadableError):
        await adapter.start()
    assert attempts == 1


@pytest.mark.asyncio
async def test_any_other_refusal_leaves_the_store_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = opencode_adapter(tmp_path, session_id="plain-refusal", wired=False)
    adapter._config.harness_native["agent_session_id"] = "ses_pinned"
    db = _store(adapter)

    async def _start_once() -> None:
        raise HarnessStartRefusedError("agent /session list failed: 404")

    monkeypatch.setattr(adapter, "_start_once", _start_once)
    with pytest.raises(HarnessStartRefusedError):
        await adapter.start()
    assert db.read_bytes() == b"from another box"
    assert adapter._config.harness_native["agent_session_id"] == "ses_pinned"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "store_unreadable"),
    [
        pytest.param(500, True, id="500-is-the-store"),
        pytest.param(503, True, id="503-is-the-store"),
        pytest.param(404, False, id="404-is-a-plain-refusal"),
        pytest.param(401, False, id="401-is-a-plain-refusal"),
    ],
)
async def test_the_session_listing_tells_a_broken_store_from_a_refusal(
    tmp_path: Path, status: int, store_unreadable: bool
) -> None:
    adapter = opencode_adapter(tmp_path, session_id="listing", wired=False)

    def _answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "boom"})

    adapter._state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_answer), base_url="http://agent"
    )
    with pytest.raises(HarnessStartRefusedError) as raised:
        await adapter._ensure_opencode_session()
    assert isinstance(raised.value, HarnessStoreUnreadableError) is store_unreadable


def test_set_aside_moves_the_database_and_its_side_files_only(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    db.write_bytes(b"db")
    db.with_name("agent.db-wal").write_bytes(b"wal")
    (tmp_path / "agent.db-walx").write_bytes(b"not sqlite's")
    aside = set_aside_agent_store(db, stamp="T")
    assert aside == tmp_path / "agent.db.unreadable-T"
    assert aside.read_bytes() == b"db"
    assert aside.with_name("agent.db.unreadable-T-wal").read_bytes() == b"wal"
    assert (tmp_path / "agent.db-walx").exists()
    assert set_aside_agent_store(db, stamp="U") is None
