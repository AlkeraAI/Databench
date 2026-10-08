"""A co-edited file over real sockets on a real server.

Two tabs (Loro documents in this process) open one file in a chat's working
folder, type into it and see each other; the file on the drive receives what
they typed without anybody saving; a reader is told it may not write; a
writer demoted mid-session loses writing at once; and carets travel in the
file's text stamped with who moved them.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import pytest
from alkera_core.config import settings
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_READER
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from alkera_core.schemas.realtime import decode_b64, encode_b64
from backend.services import files
from backend.services.realtime.runtime import runtime_of
from httpx import AsyncClient
from loro import EphemeralStore, Side
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app
from tests.chat_shares import files_on, share_chat_with  # noqa: F401
from tests.conftest import OrgWithAdmin
from tests.crdt.crdt_world import Person
from tests.crdt.file_world import SOURCE, FileWorld, drive_text, file_world, node_of
from tests.crdt.test_crdt_file_docs import _holder, _idem
from tests.crdt.test_crdt_gateway import Tab
from tests.test_ws_gateway import connect, logged_in

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


@asynccontextmanager
async def _tabs(server: str, fw: FileWorld, *people: Person) -> AsyncIterator[list[Tab]]:
    async with AsyncExitStack() as stack:
        tabs = []
        for person in people:
            client = await logged_in(person.user.email, person.password)
            stack.push_async_callback(client.aclose)
            sock = await stack.enter_async_context(connect(server, client))
            tabs.append(Tab(sock, fw.ref.channel, container="content"))
        yield tabs


async def _until_written(
    db: AsyncSession, fw: FileWorld, expected: str, seconds: float = 20
) -> str:
    """The file's text once it reads ``expected``, or the last text seen."""
    seen = ""
    for _ in range(int(seconds / 0.25)):
        seen = await drive_text(db, fw)
        if seen == expected:
            return seen
        await asyncio.sleep(0.25)
    return seen


async def test_two_people_edit_one_file_and_the_drive_gets_it_without_a_save(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    async with _tabs(uvicorn_server, fw, fw.world.owner, fw.world.writer) as (a, b):
        assert await a.sock.subscribe(fw.ref.channel) is True
        assert await b.sock.subscribe(fw.ref.channel) is True
        sync = await a.hello()
        assert sync["payload"]["limits"]["max_text_bytes"] == 1024 * 1024
        await b.hello()
        assert a.peer is not None and b.peer is not None
        assert a.peer.text == b.peer.text == SOURCE

        assert (await a.type(0, "# shared\n"))["kind"] == "ack"
        await b.receive_update()
        assert (await b.type(len(b.peer.text), "# end\n"))["kind"] == "ack"
        await a.receive_update()
        expected = "# shared\n" + SOURCE + "# end\n"
        assert a.peer.text == b.peer.text == expected
        # Written back while the session is still open, a beat after typing.
        assert await _until_written(real_session, fw, expected) == expected

        await b.sock.ws.close()
        assert (await a.type(0, "!"))["kind"] == "ack"
    # The last writer left: what they typed last is on the drive.
    assert await _until_written(real_session, fw, "!" + expected) == "!" + expected


async def test_a_reader_is_told_it_may_not_write_and_its_update_is_refused(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    async with _tabs(uvicorn_server, fw, fw.world.reader) as (tab,):
        assert await tab.sock.subscribe(fw.ref.channel) is False
        await tab.hello()
        refused = await tab.type(0, "x")
        assert refused["payload"]["code"] == "forbidden"
    assert await drive_text(real_session, fw) == SOURCE


async def test_a_member_the_chat_was_never_shared_with_is_refused_the_channel(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    async with _tabs(uvicorn_server, fw, fw.world.stranger) as (tab,):
        assert (await tab.sock.subscribe_error(fw.ref.channel))["code"] == "not_found"


async def test_a_writer_demoted_mid_session_loses_writing_the_file_at_once(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    async with _tabs(uvicorn_server, fw, fw.world.writer) as (tab,):
        assert await tab.sock.subscribe(fw.ref.channel) is True
        await tab.hello()
        chat = await real_session.get(WorkspaceObject, uuid.UUID(fw.world.ref.doc_id))
        owner = await real_session.get(User, fw.world.owner.user.id)
        assert chat is not None and owner is not None
        await share_chat_with(
            real_session,
            chat=chat,
            owner=owner,
            principal=Principal(kind="user", id=fw.world.writer.user.id),
            role=ROLE_READER,
        )
        told = await tab.sock.recv_until(lambda f: f["t"] == "subscribed")
        assert (told["channel"], told["can_write"]) == (fw.ref.channel, False)
        refused = await tab.type(0, "x")
        assert refused["payload"]["code"] == "forbidden"


async def test_a_caret_in_the_file_reaches_the_other_tab_stamped_with_who_moved_it(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    async with _tabs(uvicorn_server, fw, fw.world.owner, fw.world.reader) as (a, b):
        await a.sock.subscribe(fw.ref.channel)
        await b.sock.subscribe(fw.ref.channel)
        await a.hello()
        await b.hello()
        assert a.peer is not None
        cursor = a.peer.doc.get_text("content").get_cursor(4, Side.Middle)
        assert cursor is not None
        store = EphemeralStore(60_000)
        store.set(
            str(a.peer.peer), {"anchor": bytes(cursor.encode()), "focus": bytes(cursor.encode())}
        )
        await a.sock.send(
            a.envelope(
                "crdt", {"t": "ephemeral", "data_b64": encode_b64(bytes(store.encode_all()))}
            )
        )
        frame = await b.sock.recv_until(
            lambda f: f["t"] == "doc" and f["envelope"]["payload"].get("t") == "ephemeral"
        )
        caret = frame["envelope"]["payload"]
        assert (caret["user_id"], caret["loro_peer"]) == (str(fw.world.owner.user.id), a.peer.peer)
        relayed = EphemeralStore(60_000)
        relayed.apply(decode_b64(caret["data_b64"]))
        assert set(relayed.get_all_states()) == {str(a.peer.peer)}


async def _seen(
    tab: Tab, matches: Callable[[dict[str, Any]], bool], seconds: float = 20
) -> dict[str, Any]:
    """The first frame matching ``matches``, whether it already arrived while
    the test waited for another or arrives next: two frames one change sends
    (a notice and the grant it moved) reach the socket in either order."""
    for frame in tab.sock.skipped:
        if matches(frame):
            tab.sock.skipped.remove(frame)
            return frame
    return await tab.sock.recv_until(matches, seconds=seconds)


def _saving(frame: dict[str, Any]) -> bool:
    envelope = frame.get("envelope") or {}
    payload = envelope.get("payload") or {}
    return frame.get("t") == "doc" and payload.get("t") == "saving"


async def test_a_machine_taking_the_folder_pauses_saving_and_editing_until_it_lets_go(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typed, then a machine that takes no outside write (``alkera files
    mount``) takes the folder before the write back: the tab is told saving is
    paused and why, and that it may not type, at once. When the machine lets
    go, the tab is told it may type again (on the socket's next tick) and the
    edit it had already made reaches the drive."""
    monkeypatch.setattr(settings, "realtime_sse_keepalive_seconds", 1)
    runtime = runtime_of(app)
    assert runtime is not None
    # The write back waits past the take, so it meets the machine's lease.
    monkeypatch.setattr(runtime.crdt, "write_back_delay", 5.0)
    fw = await file_world(real_session, org_admin)
    async with _tabs(uvicorn_server, fw, fw.world.owner) as (tab,):
        assert await tab.sock.subscribe(fw.ref.channel) is True
        await tab.hello()
        assert (await tab.type(0, "# typed\n"))["kind"] == "ack"
        holder = await _holder(real_session, client, fw, purpose="mount", inbound=False)
        paused = (await _seen(tab, _saving))["envelope"]["payload"]
        assert (paused["state"], paused["reason"]) == ("paused", "leased")
        told = await _seen(tab, lambda f: f["t"] == "subscribed")
        assert (told["channel"], told["can_write"]) == (fw.ref.channel, False)
        released = await holder.release(real_session, _idem)
        assert released.status_code in (200, 204), released.text
        told = await _seen(tab, lambda f: f["t"] == "subscribed" and f["can_write"] is True)
        resumed = (await _seen(tab, _saving))["envelope"]["payload"]
        assert resumed["state"] == "ok"
        assert await _until_written(real_session, fw, "# typed\n" + SOURCE) == "# typed\n" + SOURCE


async def test_three_people_typing_hold_up_no_other_request(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three tabs type into one file for several seconds, its session writing
    back as they go to a slow drive, while someone else keeps reading the chat
    list. Every keystroke is acknowledged, no request is refused for a lock
    and none waits long: nothing a write back holds is anything they need."""
    real_write = files.write_back

    async def slow_drive(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(4.0)
        return await real_write(*args, **kwargs)

    monkeypatch.setattr(files, "write_back", slow_drive)
    fw = await file_world(real_session, org_admin)
    people = (fw.world.owner, fw.world.writer, fw.world.owner)
    reader = await logged_in(fw.world.owner.user.email, fw.world.owner.password)
    statuses: list[int] = []
    waits: list[float] = []
    acks: list[float] = []
    loop = asyncio.get_running_loop()
    async with _tabs(uvicorn_server, fw, *people) as tabs:
        for tab in tabs:
            assert await tab.sock.subscribe(fw.ref.channel) is True
            await tab.hello()
        stop = loop.time() + 12.0

        async def typist(tab: Tab, mark: str) -> int:
            acked = 0
            while loop.time() < stop:
                started = loop.time()
                frame = await tab.type(0, mark)
                assert frame["kind"] == "ack", frame
                acks.append(loop.time() - started)
                acked += 1
                await asyncio.sleep(0.05)
            return acked

        async def reading() -> None:
            while loop.time() < stop:
                started = loop.time()
                answered = await reader.get("/api/v1/chats")
                waits.append(loop.time() - started)
                statuses.append(answered.status_code)
                await asyncio.sleep(0.1)

        typed = await asyncio.gather(
            *(typist(tab, mark) for tab, mark in zip(tabs, "abc", strict=True)), reading()
        )
    await reader.aclose()
    # Every keystroke was taken, none of them after a wait on a lock (the
    # database gives up on one after five seconds; a write back held the
    # row for that long while it wrote the drive).
    assert all(count > 0 for count in typed[:3]), typed
    assert max(acks) < 3.0, sorted(acks)[-5:]
    assert statuses and set(statuses) == {200}, statuses
    assert max(waits) < 3.0, max(waits)
    # The session wrote back while they typed.
    assert await drive_text(real_session, fw) != SOURCE


PLAN = "".join(f"{n}. item {n}\n" for n in range(1, 13))


async def test_an_agent_edit_made_while_two_people_type_is_kept_with_no_copy(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    client: AsyncClient,
) -> None:
    """The walkthrough's scenario. Two people type into a twelve line plan, at
    the end of line 1 and at the end of line 12, while the session writes back
    as they go. The box holding the chat folder sends up the agent's edit
    (line 6 changed, line 13 added), made on the version it last agreed, which
    the write backs have long overtaken. Everything ends up in the file: both
    people's typing and both agent edits, with no conflicted copy beside it."""
    fw = await file_world(real_session, org_admin, content=PLAN.encode(), name="plan.txt")
    holder = await _holder(real_session, client, fw, purpose="chat", inbound=True)
    agreed = (await node_of(real_session, fw.node_id)).etag
    agent = PLAN.replace("6. item 6\n", "6. item SIX (agent)\n") + "13. agent added\n"
    loop = asyncio.get_running_loop()
    async with _tabs(uvicorn_server, fw, fw.world.owner, fw.world.writer) as (a, b):
        for tab in (a, b):
            assert await tab.sock.subscribe(fw.ref.channel) is True
            await tab.hello()
        stop = loop.time() + 6.0

        async def typing(tab: Tab, line: int, mark: str) -> str:
            typed = ""
            while loop.time() < stop:
                assert tab.peer is not None
                lines = tab.peer.text.split("\n")
                at = sum(len(one) + 1 for one in lines[:line]) - 1
                piece = f"{mark}{len(typed)}"
                assert (await tab.type(at, piece))["kind"] == "ack"
                typed += piece
                await asyncio.sleep(0.05)
            return typed

        async def box() -> None:
            await asyncio.sleep(2.5)
            sent = await holder.push(real_session, _idem, fw.node_id, agent.encode(), base=agreed)
            assert sent.status_code in (200, 201), sent.text

        first, twelfth, _ = await asyncio.gather(typing(a, 1, "a"), typing(b, 12, "b"), box())
    expected_lines = {
        f"1. item 1{first}",
        "6. item SIX (agent)",
        f"12. item 12{twelfth}",
        "13. agent added",
    }
    seen = ""
    for _ in range(80):
        seen = await drive_text(real_session, fw)
        if expected_lines <= set(seen.split("\n")):
            break
        await asyncio.sleep(0.25)
    assert expected_lines <= set(seen.split("\n")), seen
    names = (
        await real_session.execute(
            select(FileNode.name).where(
                FileNode.parent_id == (await node_of(real_session, fw.node_id)).parent_id,
                FileNode.trashed_at.is_(None),
            )
        )
    ).scalars()
    assert [bytes(n) for n in names if b"conflicted copy" in bytes(n)] == []
