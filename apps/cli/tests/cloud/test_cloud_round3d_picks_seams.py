"""The three picks a browser chat carries — model, effort, stance — from the
composer's create to the turn the adapter admits, every hop real.

A brand-new chat opened from the browser was refused on its first turn with
"This chat has no model configured" while the composer showed the model the
reader had picked. The row carried the pin (the server resolves one for every
create); the box read it; and then lost it: the folder lease pulls the chat's
files down into the chat's own directory BEFORE the mirror starts, the mirror
decided "already created" by that directory alone, skipped writing the local
record, and the runtime opened the chat on a reconstructed manifest naming no
model — so no gateway config was injected and the adapter, rightly, refused.

These cases run the browser's real create, the real backend, the real
``ChatMirror`` on a real ``HarnessRuntime`` with the production gateway config
builder, and read what the harness was opened with; the admission itself is
then checked through the real opencode adapter's gate on that very config.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import ChatMirror, CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import pinned_model
from alkera_cli.cloud.service import _stored_mode
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.adapter import PromptInput, SessionConfig
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.gateway_session import default_gateway_config_builder
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import WorkspaceObject
from alkera_core.project.directory import ProjectDirectory
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login, mint_cli_token, served_client

pytestmark = pytest.mark.asyncio

WAIT = 10.0
#: The gateway provider the box files an Anthropic-wire model under.
PROVIDER = "alkera-anthropic"
#: The one model the suite's default gateway catalog serves (backend conftest).
MODEL = "claude-opus-4.5"


async def _no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


async def _wait_for(predicate: Callable[[], bool], *, seconds: float = WAIT) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def _box_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[HarnessRuntime, FakeAdapterFactory]:
    """The box's runtime as ``build_mirror_runtime`` wires it — the production
    single-model gateway builder — with the harness faked. The builder renders
    the chat's own gateway token (the suite mints one per chat)."""
    factory = FakeAdapterFactory(lambda: FakeAdapter(reply_text="Answer: 42"), available=True)
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=factory,
        gateway_config_builder=default_gateway_config_builder,
    )
    return runtime, factory


@contextlib.asynccontextmanager
async def _mirror_from_row(
    *,
    row: dict[str, Any],
    runtime: HarnessRuntime,
    rest: CloudRestClient,
    org_admin: OrgWithAdmin,
    directory_already_there: bool,
) -> AsyncIterator[ChatMirror]:
    """The mirror the service builds for a chat row (``_default_mirror``'s
    translation of the row's picks), started the way the box starts it —
    optionally after the folder lease has already made the chat's directory."""
    chat_id = str(row["id"])
    if directory_already_there:
        # Exactly what ``ChatFolders.take`` leaves behind before ``mirror.start``:
        # the chat's directory under ``.alkera/chats``, with no record in it.
        (runtime.project.chats().path / chat_id).mkdir(parents=True, exist_ok=True)
    socket = CloudSocket(rest, sleep=_no_sleep)
    await socket.start()
    mirror = ChatMirror(
        chat_id=chat_id,
        runtime=runtime,
        socket=socket,
        rest=rest,
        user_id=str(org_admin.admin_id),
        owner_user_id=str(org_admin.admin_id),
        model=pinned_model(row.get("model")),
        permission_mode=_stored_mode(row.get("permission_mode")),
        chunk_interval=0.05,
    )
    await mirror.start()
    try:
        yield mirror
    finally:
        await mirror.stop()
        await socket.stop()
        await runtime.close_all()


async def _box_rest(uvicorn_server: str, org_admin: OrgWithAdmin, chat_id: str) -> CloudRestClient:
    token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    return CloudRestClient(api_url=f"http://{uvicorn_server}", token=token, agent_id=chat_id)


async def _admitted(config: SessionConfig, prompt: PromptInput) -> dict[str, str] | None:
    """What the real opencode adapter, opened on ``config``, sends opencode for
    ``prompt`` — or the refusal it raises. The fake harness recorded the config;
    the gate is the adapter's own."""
    binary = ResolvedOpencodeBinary(path=Path("/usr/bin/true"), prefix_args=(), source="staged")
    adapter = OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())
    return await adapter._admit_turn_model(prompt)


async def _strip_pin(chat_id: str) -> None:
    """Leave the row the way a writer from before the pin existed left it."""
    async with AsyncSessionLocal() as session:
        row = await session.get(WorkspaceObject, UUID(chat_id))
        assert row is not None
        row.spec = {**row.spec, "model": None}
        await session.commit()


async def _post(browser: AsyncClient, chat_id: str, text: str, client_id: str) -> None:
    posted = await browser.post(
        f"/api/v1/chats/{chat_id}/messages", json={"text": text, "client_id": client_id}
    )
    assert posted.status_code == 201, posted.text


async def test_a_browser_chats_picks_reach_the_harness_after_the_folder_lease_made_its_directory(
    uvicorn_server: str, org_admin: OrgWithAdmin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composer's first Send: model (server-resolved when the composer names
    none), effort and stance ride the create; the box reads the row; the
    session opens ON them — the pin as the gateway config, the stance as the
    session's mode — even though the chat's directory was already there; the
    first turn carries the effort; and the adapter's gate admits it, naming the
    provider and the effort variant opencode is handed."""
    async with served_client(uvicorn_server) as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        created = await browser.post(
            "/api/v1/chats",
            json={"title": "Why are prompts down?", "effort": "high", "permission_mode": "plan"},
        )
        assert created.status_code == 201, created.text
        chat_id = str(created.json()["id"])

        runtime, factory = _box_runtime(tmp_path, monkeypatch)
        rest = await _box_rest(uvicorn_server, org_admin, chat_id)
        row = await rest.get_chat(chat_id)
        async with _mirror_from_row(
            row=row, runtime=runtime, rest=rest, org_admin=org_admin, directory_already_there=True
        ):
            config = factory.configs[0]
            assert config.model == {"provider_id": PROVIDER, "model_id": MODEL}
            assert config.harness_native["agent_config"]["model"] == f"{PROVIDER}/{MODEL}::high"
            assert PROVIDER in config.harness_native["agent_config"]["provider"]
            assert config.permission_mode == "plan"

            await _post(browser, chat_id, "what is 6*7?", "web-1")
            adapter = factory.adapters[0]
            await _wait_for(lambda: bool(adapter.sent_prompts))
            sent = adapter.sent_prompts[0]
            assert sent.variant == "high"

    assert await _admitted(config, sent) == {"provider_id": PROVIDER, "model_id": f"{MODEL}::high"}


async def test_an_effort_switched_on_the_row_rides_the_next_turn(
    uvicorn_server: str, org_admin: OrgWithAdmin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-chat switch through the per-prompt seam: the route writes the new
    pin on the row and relays it; the running mirror repins its manifest; the
    NEXT turn carries the new effort as its variant, and the adapter composes
    the variant onto the model the session was opened on."""
    async with served_client(uvicorn_server) as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        created = await browser.post("/api/v1/chats", json={"title": "Ops", "effort": "high"})
        assert created.status_code == 201, created.text
        chat_id = str(created.json()["id"])

        runtime, factory = _box_runtime(tmp_path, monkeypatch)
        rest = await _box_rest(uvicorn_server, org_admin, chat_id)
        row = await rest.get_chat(chat_id)
        async with _mirror_from_row(
            row=row, runtime=runtime, rest=rest, org_admin=org_admin, directory_already_there=True
        ) as mirror:
            adapter = factory.adapters[0]
            await _post(browser, chat_id, "first", "web-1")
            await _wait_for(lambda: len(adapter.sent_prompts) == 1)
            assert adapter.sent_prompts[0].variant == "high"

            switched = await browser.put(
                f"/api/v1/chats/{chat_id}/model", json={"model": MODEL, "effort": "low"}
            )
            assert switched.status_code == 200, switched.text
            assert mirror.session is not None
            await _wait_for(lambda: mirror.session.manifest.model.get("effort") == "low")

            await _post(browser, chat_id, "second", "web-2")
            await _wait_for(lambda: len(adapter.sent_prompts) == 2)
            second = adapter.sent_prompts[1]
            assert second.variant == "low"
            config = factory.configs[0]

    assert await _admitted(config, second) == {"provider_id": PROVIDER, "model_id": f"{MODEL}::low"}


async def test_a_legacy_chat_the_box_discovers_opens_on_the_pin_the_list_resolved(
    uvicorn_server: str, org_admin: OrgWithAdmin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row from before every chat carried a model, found by the box the way
    it finds every chat — through the list — comes back pinned by the server
    on that read, and the session opens on it; a chat that simply predates the
    field is no longer refused at its first turn."""
    async with served_client(uvicorn_server) as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        created = await browser.post("/api/v1/chats", json={"title": "Old"})
        assert created.status_code == 201, created.text
        chat_id = str(created.json()["id"])
        await _strip_pin(chat_id)

        runtime, factory = _box_runtime(tmp_path, monkeypatch)
        rest = await _box_rest(uvicorn_server, org_admin, chat_id)
        page = await rest.list_chats()
        row = next(item for item in page["items"] if str(item["id"]) == chat_id)
        assert row["model"] is not None, "the discovery read pins a legacy row"
        async with _mirror_from_row(
            row=row, runtime=runtime, rest=rest, org_admin=org_admin, directory_already_there=True
        ):
            config = factory.configs[0]
            assert config.model == {"provider_id": PROVIDER, "model_id": MODEL}
            assert config.harness_native["agent_config"]["model"] == f"{PROVIDER}/{MODEL}::medium"
            # Only the model is stripped: the row keeps the stance the chat was
            # created with (a new chat starts in Default), and the box opens on it.
            assert config.permission_mode == "default"

            await _post(browser, chat_id, "still there?", "web-1")
            adapter = factory.adapters[0]
            await _wait_for(lambda: bool(adapter.sent_prompts))
            sent = adapter.sent_prompts[0]

    assert await _admitted(config, sent) == {
        "provider_id": PROVIDER,
        "model_id": f"{MODEL}::medium",
    }


async def test_a_record_this_box_already_holds_is_moved_onto_the_rows_pin(
    uvicorn_server: str, org_admin: OrgWithAdmin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is the durable word: a local record left by an earlier open on
    this box (unpinned, or pinned to what the row no longer says) opens on
    the row's pin and stance, not on the stale manifest."""
    async with served_client(uvicorn_server) as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        created = await browser.post(
            "/api/v1/chats", json={"title": "Ops", "effort": "low", "permission_mode": "default"}
        )
        assert created.status_code == 201, created.text
        chat_id = str(created.json()["id"])

        runtime, factory = _box_runtime(tmp_path, monkeypatch)
        # An earlier open on this box: a record that names no model, opened
        # read-only — what every browser chat's record looked like before.
        stale = runtime.project.chats().create(session_id=chat_id, title="Ops", model=None)
        stale.close()
        rest = await _box_rest(uvicorn_server, org_admin, chat_id)
        row = await rest.get_chat(chat_id)
        async with _mirror_from_row(
            row=row, runtime=runtime, rest=rest, org_admin=org_admin, directory_already_there=False
        ):
            config = factory.configs[0]
            assert config.model == {"provider_id": PROVIDER, "model_id": MODEL}
            assert config.harness_native["agent_config"]["model"] == f"{PROVIDER}/{MODEL}::low"
            assert config.permission_mode == "default"
