"""The machine-failure drill at the seam the browser watches.

The box is killed mid-turn and the supervisor brings the mirror back within a
couple of seconds — well inside the 45 s heartbeat window, so the machine row
never reads ``unreachable`` and no banner ever says anything. The ONLY thing
that can tell the reader their turn died is the chat document itself: when it
was killed the mirror had published ``turn_state.state == "working"``, and the
harness session it reopens is idle. A mirror that comes back and says nothing
leaves the reader with a turn that never ends: the composer sits in its
working state until the 60 s stall watchdog gives up on it, and the document's
meta says ``working`` for as long as nobody asks another question.

So: a mirror that starts on a chat whose document says a turn is in flight,
while its own session runs none, settles that turn on the transcript — a meta
transition off ``working`` and a system note the reader can see.

Against the real realtime gateway (uvicorn + Postgres), the same fixtures as
``test_cloud_gateway.py``; the helpers are copied rather than imported because
a test module is not a package.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import ChatMirror, CloudRestClient, CloudSocket
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_READER
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.realtime_doc import RealtimeDoc
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import SessionStatusChanged
from backend.services.chats import chat_service
from backend.services.realtime.chat_lookup import ChatDocScope
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_declarations import DECLARED
from tests.chat_shares import files_on, share_chat_with  # noqa: F401
from tests.conftest import OrgWithAdmin, mint_cli_token
from tests.test_ws_gateway import Socket, connect, member_client

pytestmark = pytest.mark.usefixtures("files_on")

_T = datetime(2026, 9, 6, tzinfo=UTC)
WAIT = 10.0


async def _no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


async def _daemon_rest(addr: str, org_admin: OrgWithAdmin, *, agent_id: str) -> CloudRestClient:
    token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    return CloudRestClient(api_url=f"http://{addr}", token=token, agent_id=agent_id)


async def _shared_chat(db: AsyncSession, org_admin: OrgWithAdmin) -> str:
    """A chat created the way ``POST /chats`` creates it — the workspace
    object with the Files node its shares hang from — declared to the socket
    with the same owner and audience. A chat is private until its node is
    shared, so the second peer ``_viewer`` opens reads it through a "Can view"
    grant, the way a colleague does; the declaration alone admits nobody but
    the owner and the bound machine."""
    owner = await db.get(User, org_admin.admin_id)
    assert owner is not None
    chat, _ = await chat_service.create_chat(
        db,
        owner=owner,
        title="Ops",
        client_id=None,
        machine_id=None,
        machine_status="none",
        org_id=owner.home_org_team_id,
    )
    await db.commit()
    chat_id = str(chat.id)
    DECLARED[(org_admin.org_id, chat_id)] = ChatDocScope(
        owner_user_id=org_admin.admin_id, team_id=None, visibility_scope="org"
    )
    return chat_id


def _fake_runtime(
    tmp_path: Path, make: Callable[[], FakeAdapter] = FakeAdapter
) -> tuple[HarnessRuntime, FakeAdapterFactory]:
    factory = FakeAdapterFactory(make, available=True)
    return HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory), factory


@contextlib.asynccontextmanager
async def _viewer(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin, channel: str
) -> AsyncIterator[tuple[Socket, dict[str, Any]]]:
    _user, viewer_client = await member_client(real_session, org_admin.org_id)
    chat = await real_session.get(WorkspaceObject, UUID(channel.split(":", 2)[2]))
    assert chat is not None and chat.owner_user_id is not None
    owner = await real_session.get(User, chat.owner_user_id)
    assert owner is not None
    await share_chat_with(
        real_session,
        chat=chat,
        owner=owner,
        principal=Principal(kind="user", id=_user.id),
        role=ROLE_READER,
    )
    try:
        async with connect(uvicorn_server, viewer_client) as viewer:
            assert await viewer.subscribe(channel) is False
            snapshot = await viewer.hello(channel)
            yield viewer, dict(snapshot)
    finally:
        await viewer_client.aclose()


async def _next_op(viewer: Socket, intent: str, *, seconds: float = WAIT) -> dict[str, Any]:
    frame = await viewer.recv_until(
        lambda f: (
            f["t"] == "doc"
            and f["envelope"]["kind"] == "op"
            and f["envelope"]["payload"].get("intent") == intent
        ),
        seconds,
    )
    return dict(frame["envelope"])


async def _next_note(viewer: Socket, *, seconds: float = WAIT) -> str:
    def is_note(f: dict[str, Any]) -> bool:
        if f["t"] != "doc" or f["envelope"]["kind"] != "op":
            return False
        payload = f["envelope"]["payload"]
        if payload.get("intent") != "append":
            return False
        entry = payload["events"][0]
        part = entry.get("payload", {}).get("part", {})
        return (
            entry.get("kind") == "part.created"
            and entry.get("role") == "system"
            and (part.get("type") == "text")
        )

    frame = await viewer.recv_until(is_note, seconds)
    return str(frame["envelope"]["payload"]["events"][0]["payload"]["part"]["text"])


def _turn_state(snapshot: dict[str, Any]) -> str | None:
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else snapshot
    meta = ((payload or {}).get("state") or {}).get("meta") or {}
    turn_state = meta.get("turn_state") or {}
    state = turn_state.get("state")
    return state if isinstance(state, str) else None


async def _killed_mid_turn(
    uvicorn_server: str, org_admin: OrgWithAdmin, chat_id: str, workspace: Path
) -> None:
    """The first life of the box: a turn starts, ``working`` is published,
    and the process is gone before the turn ends (no idle, no stopped)."""
    runtime, factory = _fake_runtime(workspace)
    rest = await _daemon_rest(uvicorn_server, org_admin, agent_id=chat_id)
    socket = CloudSocket(rest, sleep=_no_sleep)
    await socket.start()
    mirror = ChatMirror(
        chat_id=chat_id,
        runtime=runtime,
        socket=socket,
        rest=rest,
        user_id=str(org_admin.admin_id),
        chunk_interval=0.05,
    )
    await mirror.start()
    try:
        adapter = factory.adapters[0]
        # The pump subscribes to the session's live bus on its first loop turn;
        # an event fed before that is not replayed to it. In production the
        # prompt arrives long after start — give the pumps that turn here.
        await asyncio.sleep(0.2)
        await adapter.feed(
            SessionStatusChanged(
                event_id="run-1", time=_T, session_id=chat_id, status="running", turn_id="t1"
            )
        )
        deadline = asyncio.get_running_loop().time() + WAIT
        while mirror.published_count < 1:
            if asyncio.get_running_loop().time() >= deadline:
                doc = mirror.doc
                raise AssertionError(
                    "working never published: "
                    f"turn_running={mirror.turn_running} state={mirror.state} "
                    f"queue={mirror._outbound.qsize()} "
                    f"live={doc.live.is_set() if doc else None} "
                    f"outstanding={[o.op_id for o in doc.outstanding] if doc else None} "
                    f"socket={socket.state} adapters={len(factory.adapters)}"
                )
            await asyncio.sleep(0.02)
        assert mirror.turn_running
    finally:
        # A kill: the tasks stop, the socket goes, nothing more is said.
        await mirror.stop()
        await socket.stop()
        await runtime.close_all()


async def test_a_mirror_that_restarts_mid_turn_settles_the_turn_it_cannot_finish(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin, tmp_path: Path
) -> None:
    chat_id = await _shared_chat(real_session, org_admin)
    channel = f"doc:chat:{chat_id}"
    workspace = tmp_path / "box"
    workspace.mkdir()

    await _killed_mid_turn(uvicorn_server, org_admin, chat_id, workspace)

    # The document the reader is looking at when the box comes back.
    async with _viewer(uvicorn_server, real_session, org_admin, channel) as (viewer, before):
        assert _turn_state(before) == "working", before

        # The second life: the supervisor restarted the mirror on the same
        # workspace; its harness session has no turn.
        runtime, _factory = _fake_runtime(workspace)
        rest = await _daemon_rest(uvicorn_server, org_admin, agent_id=chat_id)
        socket = CloudSocket(rest, sleep=_no_sleep)
        await socket.start()
        mirror = ChatMirror(
            chat_id=chat_id,
            runtime=runtime,
            socket=socket,
            rest=rest,
            user_id=str(org_admin.admin_id),
            chunk_interval=0.05,
        )
        await mirror.start()
        try:
            assert not mirror.turn_running
            settled = await _next_op(viewer, "set_meta")
            assert settled["payload"]["meta"]["turn_state"]["state"] != "working", settled
            note = await _next_note(viewer)
            assert note, "the reader is told the turn did not survive the restart"
            # A reload reads what the live reader was shown: the server holds
            # the turn as over, in the snapshot and in the column every
            # listing reads.
            async with _viewer(uvicorn_server, real_session, org_admin, channel) as (_, after):
                assert _turn_state(after) == "idle", after
            real_session.expire_all()
            doc = (
                await real_session.execute(
                    select(RealtimeDoc).where(
                        RealtimeDoc.doc_type == "chat", RealtimeDoc.doc_id == chat_id
                    )
                )
            ).scalar_one()
            assert doc.turn_state == "idle"
        finally:
            await mirror.stop()
            await socket.stop()
            await runtime.close_all()
