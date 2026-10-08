"""Tests for the daemon's `preferences.get` / `preferences.set` methods.

Drives the daemon in-process over piped streams (same plumbing as
``test_daemon_auth.py``) and isolates ``~/.alkera/`` to a tmp dir so the
real user's preferences file is never touched.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.daemon import (
    JsonRpcServer,
    PipeWriter,
    connect_pipe_reader,
    connect_pipe_writer,
    read_frame_async,
    write_frame_async,
)
from alkera_cli.daemon import methods as _register_methods  # noqa: F401
from alkera_cli.host import paths
from alkera_core.schemas.preferences import Preferences

#: How long a test waits for one daemon reply or for shutdown. Well above the
#: preferences / instructions writers' own patience: a 2 s lock retry plus the
#: NTFS sharing-violation backoff behind every atomic write, which a loaded
#: Windows shard does spend. The suite-wide 90 s timeout still catches a hang.
_REPLY_BUDGET_S = 20.0


def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "alkera-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setattr(paths, "PREFERENCES_FILE_PATH", home / "preferences.yml")
    monkeypatch.setattr(paths, "PREFERENCES_LOCK_PATH", home / ".preferences.lock")
    return home


async def _connected_streams() -> tuple[
    asyncio.StreamReader,
    PipeWriter,
    asyncio.StreamReader,
    PipeWriter,
]:
    # Through ``daemon.pipes`` so each platform runs its production transport
    # (raw connect_read_pipe never delivers on the Windows ProactorEventLoop).
    c2s_r, c2s_w = os.pipe()
    server_reader = await connect_pipe_reader(os.fdopen(c2s_r, "rb", buffering=0))
    client_writer = await connect_pipe_writer(os.fdopen(c2s_w, "wb", buffering=0))
    s2c_r, s2c_w = os.pipe()
    client_reader = await connect_pipe_reader(os.fdopen(s2c_r, "rb", buffering=0))
    server_writer = await connect_pipe_writer(os.fdopen(s2c_w, "wb", buffering=0))
    return server_reader, server_writer, client_reader, client_writer


async def _send(writer: PipeWriter, envelope: dict[str, Any]) -> None:
    await write_frame_async(writer, json.dumps(envelope).encode("utf-8"))


async def _recv_until(
    reader: asyncio.StreamReader,
    predicate,
    *,
    timeout: float = _REPLY_BUDGET_S,  # noqa: ASYNC109
) -> dict[str, Any]:
    async def _recv() -> dict[str, Any]:
        return json.loads((await read_frame_async(reader)).decode("utf-8"))

    return await asyncio.wait_for(_drain_until(reader, predicate, _recv), timeout=timeout)


async def _drain_until(reader, predicate, recv):
    while True:
        msg = await recv()
        if predicate(msg):
            return msg


@pytest.fixture
async def daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _isolate_home(monkeypatch, tmp_path)
    sr, sw, cr, cw = await _connected_streams()
    server = JsonRpcServer(reader=sr, writer=sw)
    task = asyncio.create_task(server.serve())

    async def stop() -> None:
        server.request_shutdown()
        cw.close()
        await asyncio.wait_for(task, timeout=_REPLY_BUDGET_S)

    try:
        yield server, cw, cr, stop, tmp_path
    finally:
        if not task.done():
            await stop()


@pytest.mark.asyncio
async def test_preferences_get_defaults(daemon):
    _server, cw, cr, stop, _home = daemon
    await _send(cw, {"jsonrpc": "2.0", "id": 1, "method": "preferences.get", "params": {}})
    resp = await _recv_until(cr, lambda m: m.get("id") == 1)
    assert resp["result"]["preferences"]["show_banner"] is True
    assert "show_thinking" not in resp["result"]["preferences"]
    await stop()


@pytest.mark.asyncio
async def test_preferences_set_then_get(daemon):
    _server, cw, cr, stop, _home = daemon
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "preferences.set",
            "params": {"preferences": {"show_banner": False}},
        },
    )
    set_resp = await _recv_until(cr, lambda m: m.get("id") == 2)
    assert set_resp["result"]["preferences"]["show_banner"] is False

    await _send(cw, {"jsonrpc": "2.0", "id": 3, "method": "preferences.get", "params": {}})
    get_resp = await _recv_until(cr, lambda m: m.get("id") == 3)
    assert get_resp["result"]["preferences"]["show_banner"] is False
    await stop()


@pytest.mark.asyncio
async def test_preferences_set_reconciles_sentry(daemon, monkeypatch: pytest.MonkeyPatch):
    """Toggling telemetry via `preferences.set` re-gates the daemon's OWN Sentry
    live — the daemon is long-running, so the opt-out must take effect mid-session
    (not only on its next start). Asserts the set handler routes through the gate."""
    _server, cw, cr, stop, _home = daemon
    components: list[str] = []
    monkeypatch.setattr(
        "alkera_cli.daemon.methods.preferences.reconcile_sentry",
        lambda component: components.append(component),
    )
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "preferences.set",
            "params": {"preferences": {"telemetry_enabled": False}},
        },
    )
    resp = await _recv_until(cr, lambda m: m.get("id") == 5)
    assert resp["result"]["preferences"]["telemetry_enabled"] is False
    assert components == ["daemon"]  # the gate ran for the daemon after the write
    await stop()


@pytest.mark.asyncio
async def test_preferences_set_sanitizes_current_rpc_input_without_dropping_future_field(
    daemon,
):
    """Catch an RPC merge that preserves retired show_thinking as an ordinary
    extra, or removes every unknown field while trying to filter it."""
    _server, cw, cr, stop, _home = daemon
    update = {
        "schema_version": Preferences.SCHEMA_VERSION,
        "show_banner": False,
        "show_thinking": True,
        "future_pref": "keep",
    }
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "preferences.set",
            "params": {"preferences": update},
        },
    )
    resp = await _recv_until(cr, lambda m: m.get("id") == 4)
    prefs = resp["result"]["preferences"]
    assert prefs["show_banner"] is False
    assert "show_thinking" not in prefs
    assert prefs["future_pref"] == "keep"
    await stop()


# --- the catalog-scoped keys are read and written as the project's org ----------


async def _rpc(cw: Any, cr: Any, rid: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    await _send(cw, {"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    resp = await _recv_until(cr, lambda m: m.get("id") == rid)
    assert "result" in resp, resp
    result: dict[str, Any] = resp["result"]
    return result


@pytest.fixture
def pinned_to_a(tmp_path: Path) -> str:
    """A project pinned to org A while the daemon's current sign-in is org B."""
    from _profiles import ORG_A, ORG_B, store
    from alkera_cli.account.binding import pin_project
    from alkera_core.project.directory import ProjectDirectory

    project = ProjectDirectory(tmp_path / "workspace" / ".alkera")
    a = store(ORG_A)
    store(ORG_B, current=True)
    pin_project(project, a)
    return str(project.path.parent)


@pytest.mark.asyncio
async def test_a_model_effort_set_for_a_pinned_project_stays_in_its_org(daemon, pinned_to_a):
    """The window's project is pinned to A: what it saves is A's, and the
    daemon's current sign-in (B) does not read it, while a shared preference
    set in the same call is read by both."""
    _server, cw, cr, stop, _home = daemon
    await _rpc(
        cw,
        cr,
        11,
        "preferences.set",
        {
            "preferences": {"model_efforts": {"gpt-5": "high"}, "show_banner": False},
            "project_path": pinned_to_a,
        },
    )
    in_a = await _rpc(cw, cr, 12, "preferences.get", {"project_path": pinned_to_a})
    in_b = await _rpc(cw, cr, 13, "preferences.get", {})
    assert in_a["preferences"]["model_efforts"] == {"gpt-5": "high"}
    assert in_b["preferences"]["model_efforts"] == {}
    assert in_a["preferences"]["show_banner"] is False
    assert in_b["preferences"]["show_banner"] is False
    assert "orgs" not in in_a["preferences"]
    await stop()


@pytest.mark.asyncio
async def test_the_saved_default_resolves_from_the_projects_org(
    daemon, pinned_to_a, monkeypatch: pytest.MonkeyPatch
):
    """With the gateway unreachable the saved default comes back as saved: the
    project pinned to A gets A's pick, the daemon's own sign-in (B) gets B's."""
    from _profiles import ORG_A, ORG_B
    from alkera_cli.gateway.client import GatewayUnavailableError
    from alkera_cli.preferences import user as preferences_file

    async def _down(**_kwargs: Any) -> Any:
        raise GatewayUnavailableError("down")

    monkeypatch.setattr("alkera_cli.daemon.methods.preferences.fetch_models", _down)
    preferences_file.set_preference("default_chat_model", "claude-a", org_id=ORG_A)
    preferences_file.set_preference("default_chat_model", "gpt-b", org_id=ORG_B)
    _server, cw, cr, stop, _home = daemon
    for_a = await _rpc(
        cw, cr, 21, "preferences.resolve_chat_defaults", {"project_path": pinned_to_a}
    )
    for_b = await _rpc(cw, cr, 22, "preferences.resolve_chat_defaults", {})
    assert for_a["model"] == "claude-a"
    assert for_b["model"] == "gpt-b"
    await stop()
