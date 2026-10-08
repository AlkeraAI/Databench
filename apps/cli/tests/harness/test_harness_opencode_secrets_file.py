"""The agent server's password and injected config reach it by file.

An environment stays readable in ``/proc/<pid>/environ`` for the life of the
process and passes to every child, so the two secrets an agent server is
launched with (its loopback password, and its config, which carries the chat's
gateway token) are written to an owner-only file in its own state and named in
the environment instead. The vendored agent reads the file once and removes it.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import httpx
import pytest
from alkera_cli.harness.adapter import HarnessStartError, HarnessStartRefusedError, SessionConfig
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.adapters.opencode_secrets import (
    agent_secrets,
    require_agent_auth,
    write_agent_secrets,
)
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary

_GATEWAY_CONFIG: dict[str, object] = {
    "provider": {"mock": {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "http://x"}}},
    "model": "mock/mock-model",
}


def _adapter(tmp_path: Path) -> OpencodeHttpAdapter:
    config = SessionConfig(
        session_id="secrets-sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
        harness_native={"agent_config": dict(_GATEWAY_CONFIG)},
    )
    binary = ResolvedOpencodeBinary(path=Path("/usr/bin/true"), prefix_args=(), source="staged")
    return OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())


def test_agent_secrets_carry_the_password_and_only_a_given_config() -> None:
    assert agent_secrets("pw") == {"ALKERA_SERVER_PASSWORD": "pw"}
    assert json.loads(agent_secrets("pw", {"model": "m/x"})["ALKERA_CONFIG_CONTENT"]) == {
        "model": "m/x"
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_the_secrets_file_is_owner_only(tmp_path: Path) -> None:
    path = write_agent_secrets(tmp_path, {"ALKERA_SERVER_PASSWORD": "pw"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"ALKERA_SERVER_PASSWORD": "pw"}


def test_a_stale_secrets_file_is_replaced_whole(tmp_path: Path) -> None:
    """An agent that died before reading its file leaves it behind; the next
    launch's file holds only the next launch's secrets."""
    write_agent_secrets(tmp_path, {"ALKERA_SERVER_PASSWORD": "old", "ALKERA_CONFIG_CONTENT": "{}"})
    path = write_agent_secrets(tmp_path, {"ALKERA_SERVER_PASSWORD": "new"})
    assert json.loads(path.read_text()) == {"ALKERA_SERVER_PASSWORD": "new"}


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_a_link_at_the_secrets_name_is_never_written_through(tmp_path: Path) -> None:
    """The state directory is the agent's to write, so it may have left a link
    where its secrets file goes; writing through it would put the next chat's
    secrets wherever the link points."""
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "elsewhere.txt"
    target.write_text("untouched")
    (state / "launch.json").symlink_to(target)

    path = write_agent_secrets(state, {"ALKERA_SERVER_PASSWORD": "pw"})

    assert target.read_text() == "untouched"
    assert not path.is_symlink()
    assert json.loads(path.read_text()) == {"ALKERA_SERVER_PASSWORD": "pw"}


async def test_the_file_is_in_place_at_spawn_with_the_launch_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter writes the file before the spawn (so a sandbox's launch steps
    own it with the rest of the agent's state), with the password the adapter
    then authenticates with, and the environment names it."""
    adapter = _adapter(tmp_path)
    seen: dict[str, object] = {}

    async def _spawn(env: dict[str, str]) -> object:
        named = Path(env["ALKERA_SECRETS_FILE"])
        seen["secrets"] = json.loads(await asyncio.to_thread(named.read_text))
        seen["env"] = env
        raise RuntimeError("stop before a real process")

    monkeypatch.setattr(adapter, "_spawn_opencode", _spawn)
    with pytest.raises(HarnessStartError):
        await adapter._start_once()

    secrets = seen["secrets"]
    env = seen["env"]
    assert isinstance(secrets, dict) and isinstance(env, dict)
    assert secrets["ALKERA_SERVER_PASSWORD"] == adapter._state.password
    assert json.loads(secrets["ALKERA_CONFIG_CONTENT"])["model"] == "mock/mock-model"
    assert adapter._state.password not in "".join(env.values())
    assert "ALKERA_CONFIG_CONTENT" not in env


def _answering(status: int) -> httpx.AsyncClient:
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(status)

    # The adapter's own client carries the password; the probe must not.
    return httpx.AsyncClient(
        base_url="http://agent",
        auth=("opencode", "pw"),
        transport=httpx.MockTransport(_handler),
    )


async def test_an_agent_that_demands_its_password_is_accepted() -> None:
    async with _answering(401) as anonymous:
        await require_agent_auth(anonymous)


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(200, id="served-anonymously"),
        pytest.param(404, id="not-found-is-not-a-refusal"),
        pytest.param(403, id="forbidden-is-not-the-password-check"),
    ],
)
async def test_an_agent_that_answers_without_its_password_is_refused(status: int) -> None:
    """A binary that never read its secrets file serves with no password; the
    adapter refuses it rather than drive an unauthenticated shell API."""
    async with _answering(status) as anonymous:
        with pytest.raises(HarnessStartRefusedError, match="without its password"):
            await require_agent_auth(anonymous)
