"""A chat whose pinned agent session is not in the store it opens on.

On a cloud box the agent's session store travels with the chat's folder, and
a box restarted mid-turn can let the folder go before the store's bytes land
on the drive. The next box then opens a store that does not hold the session
the chat's manifest pins. The transcript is the chat's record, so that box
opens a fresh session and serves the chat; refusing (the local behaviour,
where a missing pin means a wiped store to repair) refused the chat on every
box, every retry, for good.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from _mocks.adapter_seam import opencode_adapter
from alkera_cli.harness import HarnessRuntime, PathFence
from alkera_cli.harness.adapter import HarnessUnavailableError
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_core.project.directory import ProjectDirectory

PINNED = "ses_pinned_by_the_last_box"


class _Agent:
    """The agent's session listing and creation, recorded."""

    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        self.sessions = sessions
        self.created: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/session":
            return httpx.Response(200, json=self.sessions)
        if request.method == "POST" and request.url.path == "/session":
            sid = f"ses_fresh_{len(self.created) + 1}"
            self.created.append(sid)
            return httpx.Response(200, json={"id": sid})
        return httpx.Response(404, json={})


def _adapter(tmp_path: Path, agent: _Agent, *, store_is_cache: bool) -> OpencodeHttpAdapter:
    adapter = opencode_adapter(tmp_path, session_id="chat-1", wired=False)
    adapter._config = dataclasses.replace(adapter._config, store_is_cache=store_is_cache)
    adapter._config.harness_native["agent_session_id"] = PINNED
    adapter._state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(agent.handler), base_url="http://agent"
    )
    return adapter


OLDER = {"id": "ses_older", "time": {"created": 1, "updated": 2}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sessions",
    [
        pytest.param([], id="the-store-did-not-land-at-all"),
        pytest.param([OLDER], id="an-older-store-landed"),
    ],
)
async def test_a_box_opens_a_fresh_session_when_the_pin_is_not_in_the_store(
    tmp_path: Path, sessions: list[dict[str, Any]]
) -> None:
    agent = _Agent(sessions)
    adapter = _adapter(tmp_path, agent, store_is_cache=True)

    opened = await adapter._ensure_opencode_session()

    # A fresh session, never an older one the store happens to hold: that
    # would carry a context the transcript has moved past.
    assert agent.created == ["ses_fresh_1"]
    assert opened == "ses_fresh_1"
    assert "agent_session_id" not in adapter._config.harness_native


@pytest.mark.asyncio
async def test_locally_a_missing_pin_is_still_refused_for_repair(tmp_path: Path) -> None:
    agent = _Agent([OLDER])
    adapter = _adapter(tmp_path, agent, store_is_cache=False)

    with pytest.raises(HarnessUnavailableError, match="no longer exists") as refused:
        await adapter._ensure_opencode_session()

    # The refusal names a step the person can take, not a command that does not exist.
    assert "Start a new chat to continue." in str(refused.value)
    assert "repair" not in str(refused.value)

    assert agent.created == [], "nothing re-targeted or created behind the person's back"
    assert adapter._config.harness_native["agent_session_id"] == PINNED


@pytest.mark.asyncio
@pytest.mark.parametrize("store_is_cache", [True, False])
async def test_a_pin_the_store_holds_is_attached_to(tmp_path: Path, store_is_cache: bool) -> None:
    agent = _Agent([OLDER, {"id": PINNED}])
    adapter = _adapter(tmp_path, agent, store_is_cache=store_is_cache)

    assert await adapter._ensure_opencode_session() == PINNED
    assert agent.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fenced", "store_is_cache"),
    [
        pytest.param(True, True, id="a-cloud-box-session-treats-its-store-as-a-cache"),
        pytest.param(False, False, id="a-local-session-keeps-its-store-authoritative"),
    ],
)
async def test_the_runtime_marks_a_cloud_sessions_store_as_a_cache(
    tmp_path: Path, fenced: bool, store_is_cache: bool
) -> None:
    factory = FakeAdapterFactory(available=True)
    runtime = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)
    fence = PathFence(escape=lambda request: False, reason="outside") if fenced else None
    session = await runtime.open_chat(create=True, harness_type="agent", path_fence=fence)
    try:
        assert factory.configs[-1].store_is_cache is store_is_cache
    finally:
        await runtime.close_chat(session.session_id)
