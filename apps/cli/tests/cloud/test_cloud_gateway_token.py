"""The mirror hands the agent the chat's gateway token and never the box's own
bearer: minted as the machine at every session open, carried into the adapter's
config, absent from the manifest; a refused mint is a session that does not
open rather than one that runs on the device token.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.account import auth_file
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.cloud.rest import CloudApiError
from alkera_cli.harness import HarnessRuntime, PermissionBroker, gateway_session
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.gateway_session import default_gateway_config_builder
from alkera_core.authz.headers import AGENT_ID_HEADER
from alkera_core.project.directory import ProjectDirectory

CHAT_ID = "chat-gateway-token"
MACHINE_ID = "machine-gw-1"
OWNER = "00000000-0000-4000-8000-000000000001"
MODEL = {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5", "efforts": ["high"]}
MINT_PATH = f"/api/v1/chats/{CHAT_ID}/gateway-token"
EXPIRES = "2026-09-25T20:00:00+00:00"


@pytest.fixture(autouse=True)
def _chat_gateway_token_minted() -> None:
    """This module pins the mint itself: the suite's fixed-token stand-in is
    overridden here so every open meets the real call."""


def _never_read_device_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def _never() -> None:
        raise AssertionError("the device token file must not be read for a cloud chat")

    monkeypatch.setattr(auth_file, "_read_document", _never)
    monkeypatch.setattr(
        gateway_session,
        "get_settings",
        lambda: type("S", (), {"alkera_gateway_url": "https://gw.example"})(),
    )


class _Backend:
    """The one route the mirror needs here, and a record of who asked."""

    def __init__(self, *, status: int = 200) -> None:
        self.status = status
        self.mints: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == MINT_PATH:
            self.mints.append(request)
            if self.status != 200:
                return httpx.Response(
                    self.status, json={"detail": {"code": "denied", "message": "Not allowed"}}
                )
            return httpx.Response(200, json={"token": "gw-minted", "expires_at": EXPIRES})
        return httpx.Response(404, json={})


def _mirror(
    tmp_path: Path, backend: _Backend
) -> tuple[ChatMirror, HarnessRuntime, FakeAdapterFactory]:
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / "workspace" / ".alkera"),
        adapter_factory=factory,
        gateway_config_builder=default_gateway_config_builder,
    )
    runtime.project.chats().create(
        session_id=CHAT_ID, title="t", harness_type="agent", model=dict(MODEL)
    ).close()
    rest = CloudRestClient(
        api_url="http://objects.test",
        token="device-jwt",
        agent_id=CHAT_ID,
        transport=httpx.MockTransport(backend),
    )
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=rest,
        user_id=OWNER,
        owner_user_id=OWNER,
        machine_id=MACHINE_ID,
    )
    return mirror, runtime, factory


async def _allow(_request: object) -> object:
    raise AssertionError("no permission is asked here")


@pytest.mark.asyncio
async def test_the_mirror_opens_the_session_on_the_chats_gateway_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _never_read_device_token(monkeypatch)
    backend = _Backend()
    mirror, runtime, factory = _mirror(tmp_path, backend)
    assert mirror.gateway_token_expires_at is None

    session = await mirror.open_session(PermissionBroker(_allow, default_timeout_seconds=None))
    try:
        # Who asked: the box's own session, speaking as the MACHINE (the
        # publisher the route admits), for this chat.
        assert len(backend.mints) == 1
        mint = backend.mints[0]
        assert mint.headers["authorization"] == "Bearer device-jwt"
        assert mint.headers[AGENT_ID_HEADER] == MACHINE_ID
        # What the agent got: the minted token, once, and not the box's bearer.
        config = factory.configs[0]
        rendered = json.dumps(config.harness_native["agent_config"])
        assert (
            config.harness_native["agent_config"]["provider"]["alkera-anthropic"]["options"][
                "apiKey"
            ]
            == "gw-minted"
        )
        assert rendered.count("gw-minted") == 1
        assert "device-jwt" not in rendered
        assert "device-jwt" not in json.dumps(config.harness_native)
        assert mirror.gateway_token_expires_at == datetime(2026, 9, 25, 20, tzinfo=UTC)
    finally:
        await runtime.close_chat(session.session_id)

    # Nothing of it on disk.
    manifest = tmp_path / "workspace" / ".alkera" / "chats" / CHAT_ID / "manifest.json"
    text = manifest.read_text()
    assert "gw-minted" not in text and "device-jwt" not in text

    # A second open is a second mint — fresh at every open.
    session = await mirror.open_session(PermissionBroker(_allow, default_timeout_seconds=None))
    try:
        assert len(backend.mints) == 2
    finally:
        await runtime.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_a_refused_mint_opens_no_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent runs with the chat's credential or not at all: a backend that
    will not mint one leaves the chat without a session — never one that
    quietly falls back to the box's own bearer."""
    _never_read_device_token(monkeypatch)
    backend = _Backend(status=403)
    mirror, runtime, factory = _mirror(tmp_path, backend)

    with pytest.raises(CloudApiError) as refused:
        await mirror.open_session(PermissionBroker(_allow, default_timeout_seconds=None))
    assert refused.value.status == 403
    assert factory.configs == [] and factory.adapters == []
    assert runtime.open_session(CHAT_ID) is None


@pytest.mark.asyncio
async def test_a_mint_answered_without_a_token_opens_no_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _never_read_device_token(monkeypatch)

    def empty(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == MINT_PATH:
            return httpx.Response(200, json={"token": "", "expires_at": EXPIRES})
        return httpx.Response(404, json={})

    backend = _Backend()
    mirror, runtime, factory = _mirror(tmp_path, backend)
    mirror._machine_rest = CloudRestClient(
        api_url="http://objects.test",
        token="device-jwt",
        agent_id=MACHINE_ID,
        transport=httpx.MockTransport(empty),
    )
    with pytest.raises(ValueError, match="no gateway token"):
        await mirror.open_session(PermissionBroker(_allow, default_timeout_seconds=None))
    assert factory.configs == []
    assert runtime.open_session(CHAT_ID) is None
