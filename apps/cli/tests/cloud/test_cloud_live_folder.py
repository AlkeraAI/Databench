"""The live lease end to end: a box writes, and the person watching sees it.

Everything here runs against the REAL backend — a uvicorn instance on the test
loop, real Postgres, the real lease, upload, listing and content routes — and a
REAL holder: :class:`alkera_cli.cloud.folder.ChatFolders` takes the chat's
folder over HTTP, and its live sync watches the working directory with the
watcher production uses. Nothing in the chain is stubbed, because the claim
being made is a chain claim: a file written on the box exists on the drive,
appears on the watcher's event stream naming the folder it landed in, and reads
back byte for byte, all inside the window a person would call "live".

The four directions this pins, and why each needs the real wire:

* **out** — the agent writes, the member sees it. The frame the member's stream
  carries has to name the FOLDER, or a client watching one listing cannot tell
  a file of its own from any other file in the org, and refreshes everything;
* **in** — the member uploads while the box holds the folder. The write is
  admitted rather than refused, recorded for the holder, and applied onto the
  box's disk, which is the only way the agent can read what a person just
  dropped into the chat;
* **the window the drive opens before the bytes exist** — the upload creates
  the node and records it for the holder in one transaction and lands its bytes
  in the next, so a holder that drains in between meets a node with no version.
  It must come away with nothing rather than an empty file;
* **silence** — a box that stops beating loses the folder, and what it was
  half-way through writing must not be left on the drive as a file that never
  opens.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from _live_window import Window, live_window
from alkera_cli.cloud.box_auth import BearerAuth
from alkera_cli.cloud.folder import ChatFolders
from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.files.authz.ladder import ROLE_WRITER
from alkera_core.files.clock import SystemClock
from alkera_core.files.ids import OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.files.sweepers import LeaseReaper, SweepDeps
from alkera_core.models import User
from backend.api.routes.files import PREFIX
from backend.services.chats import chat_service
from blake3 import blake3
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._wait_ceiling import wait_ceiling
from tests.chat_shares import files_on, share_chat_with  # noqa: F401
from tests.conftest import OrgWithAdmin, make_member, mint_cli_token, serve_app
from tests.files._boxes import registered_box

pytestmark = pytest.mark.usefixtures("files_on")

UPLOADS = f"{PREFIX}/uploads"

#: How long a write on the box may take to become a row the member can read.
#: The number is what separates "live" from "saved at a checkpoint when the
#: turn ends": the served cadence debounces for 300 ms and batches every
#: 500 ms, and the upload and its commit follow, so a healthy chain lands in
#: about a second and a chain that needs more than this is not live in any
#: sense a person watching the panel would accept.
#:
#: Five seconds rather than the two the shape was drawn with, deliberately and
#: permanently: the chain went past two seconds on a lane database shared with
#: everything else running on the machine, and a deadline that measures the
#: host's load measures nothing about the plane. What it defends is the
#: distinction — live versus saved when the turn ends — and five seconds
#: defends that as well as two while staying green on a busy host.
#:
#: What a run asserts is that window times its allowance. The gate runs this
#: suite sixteen workers deep on one Postgres and one disk, where what a
#: deadline measures is mostly the machine: the shard that carries it ran at
#: twice its usual length one evening and took the claim down with it, on a
#: plane nothing had touched. So a shared gate asserts three times the window
#: and a quiet run asserts the window itself, the allowance the Files
#: performance budgets already publish, off the scale knob they already read
#: (``_live_window``).
LIVE_WINDOW = live_window(5.0)

#: Longer deadlines for the things that are not the live window itself — a
#: lease being taken, a tree being pulled, a background promote landing. Under
#: the same allowance: a cold chain's first write is what a loaded machine
#: slows most.
SETUP_WAIT = live_window(30.0)

#: How long one HTTP request to the served instance may take. A transport
#: timeout on a fixture's client rather than a claim about the plane — it
#: bounds a request that never answers, and no test asserts it — so it is not
#: scaled with the windows above.
HTTP_TIMEOUT = 30.0

REPORT = b"<!doctype html><title>Q3</title><h1>Q3 revenue</h1>"
REWRITE = b"<!doctype html><title>Q3</title><h1>Q3 revenue, revised</h1>"
DROPPED = b"seat,region\n1,emea\n2,apac\n"

#: The names in the redirect a download answers with have to be reachable: the
#: box follows it to fetch a drop, and so does a browser fetching a preview. So
#: user bytes get a SECOND served instance rather than a second name for the
#: first one — the app refuses an API path arriving under the content origin
#: and ``/c/*`` under the API's, which is the whole point of the two origins.


# --------------------------------------------------------------------------- #
# the event stream, read over real TCP
# --------------------------------------------------------------------------- #


class Frames:
    """Every named frame an open ``text/event-stream`` has delivered so far.

    Collected rather than awaited one at a time: the stream carries the whole
    org's traffic, so a test that read the *next* frame would be asserting on
    whatever else happened to commit first.
    """

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def pump(self, response: httpx.Response) -> None:
        event: str | None = None
        data: str | None = None
        async for line in response.aiter_lines():
            if not line.strip():
                if event is not None and data is not None:
                    with contextlib.suppress(json.JSONDecodeError):
                        self.seen.append({"event": event, "data": json.loads(data)})
                event, data = None, None
                continue
            field, _, value = line.partition(":")
            if field == "event":
                event = value.strip()
            elif field == "data":
                data = value.strip()

    def matching(self, predicate: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
        return [frame for frame in self.seen if predicate(frame)]

    async def until(
        self, predicate: Callable[[dict[str, Any]], bool], *, window: Window
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + window.seconds
        while True:
            found = self.matching(predicate)
            if found:
                return found[0]
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(
                    f"no frame matched within {window}; saw "
                    f"{[frame['event'] for frame in self.seen]}"
                )
            await asyncio.sleep(0.02)


def in_folder(
    parent_id: str, *, besides: frozenset[str] = frozenset()
) -> Callable[[dict[str, Any]], bool]:
    """A ``file_node.changed`` naming a node that sits in ``parent_id``, other
    than the nodes in ``besides``.

    The folder is the whole point of matching on it: the stream carries the
    org's traffic, and a client holding one listing open can only tell that
    THIS listing moved if the frame says where the node sits.

    The folder's own frame is not one: a tree batch announces each folder it
    touched as ``{id: folder, parent_id: folder}`` (``live_batch``), which says
    the listing moved but names no node in it, so a case that reads the node
    off the frame would read the folder.

    ``besides`` is for the fixture's own ``ready.txt``. The fixture offers that
    write until it lands, so on a loaded box a second, identical offer is still
    on its way when the first one settles, and its save reaches the stream after
    the case has started watching: the first frame in the folder then names
    ``ready.txt``, not the file the case wrote.
    """

    def matches(frame: dict[str, Any]) -> bool:
        data = frame["data"]
        node = str(data.get("entity_id"))
        return (
            frame["event"] == "file_node.changed"
            and data.get("parent_id") == parent_id
            and node != parent_id
            and node not in besides
        )

    return matches


@contextlib.asynccontextmanager
async def watching(addr: str, token: str) -> AsyncIterator[Frames]:
    """One person's open event stream, as the portal holds it."""
    frames = Frames()
    async with httpx.AsyncClient(base_url=f"http://{addr}", timeout=HTTP_TIMEOUT) as client:
        async with client.stream(
            "GET", "/api/v1/events", headers={"Authorization": f"Bearer {token}"}
        ) as response:
            assert response.status_code == 200, await response.aread()
            pump = asyncio.create_task(frames.pump(response))
            try:
                yield frames
            finally:
                pump.cancel()
                with contextlib.suppress(BaseException):
                    await pump


# --------------------------------------------------------------------------- #
# the chat, the box, and the person watching it
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LiveChat:
    """One chat whose folder a box is holding with the live plane running."""

    addr: str
    org_id: uuid.UUID
    chat_id: str
    drive_id: str
    chat_node_id: str
    scratch_node_id: str
    #: The fixture's own ``ready.txt``, whose trailing saves are not the case's.
    ready_node_id: str
    working_dir: Path
    folders: ChatFolders
    member: User
    member_token: str
    box_token: str
    box_machine: str
    #: Stop the heartbeat this chat's box is running. Only the case that is
    #: ABOUT a silent box calls it; every other case wants the beat running,
    #: because a real box never stops beating while it holds a folder.
    stop_beating: Callable[[], None]

    def url(self, suffix: str) -> str:
        return f"{PREFIX}/drives/{self.drive_id}/items/{suffix}"


async def _node_of(db: AsyncSession, object_id: uuid.UUID) -> Any:
    row = (
        await db.execute(
            text(
                "SELECT id, drive_id FROM file_nodes "
                "WHERE target_object_id = :object AND trashed_at IS NULL"
            ),
            {"object": object_id},
        )
    ).one()
    return row


async def _child(db: AsyncSession, parent_id: Any, name: bytes) -> Any:
    return (
        await db.execute(
            text(
                "SELECT id, head_version_id, etag, size FROM file_nodes "
                "WHERE parent_id = :parent AND name = :name AND trashed_at IS NULL"
            ),
            {"parent": parent_id, "name": name},
        )
    ).one_or_none()


def _settled(db: AsyncSession, parent_id: str, name: bytes, size: int) -> Callable[[], Any]:
    """A predicate that holds once ``name`` is on the drive AT ``size``.

    Waiting for the node alone would pass the instant the holder minted it,
    before any bytes moved — and every deadline here is about the bytes having
    arrived, not about a row having appeared.
    """

    async def settled() -> Any:
        row = await _child(db, uuid.UUID(parent_id), name)
        if row is None or row.head_version_id is None:
            return None
        return row if int(row.size) == size else None

    return settled


async def _until(predicate: Callable[[], Any], *, window: Window, what: str) -> Any:
    """Wait for ``predicate``, and say what window a failure was measured against.

    The window is reported rather than the bare deadline: a reader told the
    write did not arrive in fifteen seconds has to go looking for a
    fifteen-second claim, and there is none — there is a five-second one and the
    allowance this run gave it.
    """
    deadline = asyncio.get_running_loop().time() + window.seconds
    while True:
        found = predicate()
        if asyncio.iscoroutine(found):
            found = await found
        if found:
            return found
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"{what} did not happen within {window}")
        await asyncio.sleep(0.05)


#: How long the fixture keeps offering its readiness write before it calls the
#: plane broken. A safety net rather than a measurement — the wait below ends
#: when the drive has the file, not when a clock says it should — so the number
#: is taken from the run's own per-test budget instead of an idle box's.
SETUP_CEILING = wait_ceiling()

#: How long one offer is given before the next one is made. Several flush
#: cadences (the served one debounces 300 ms and batches every 500 ms), so a
#: watch that IS up settles the first offer and no second one is ever written.
OFFER_EVERY = 3.0


async def _offered_until_it_lands(
    path: Path, body: bytes, settled: Callable[[], Any], *, what: str
) -> Any:
    """Write ``body`` at ``path`` until the drive holds it, and answer the row.

    Not a retry around a slow plane: a lost write. ``ChatFolders.live`` returns
    as soon as the sync's thread is STARTED, and the OS watch that thread puts
    on the directory is in place some time after that — so a single write made
    the moment ``live()`` returns can land on disk before anything is watching
    it. A watcher reports changes, not what it found already there, so that
    write is not late, it is gone: nothing will ever carry it to the drive, and
    every second of waiting is spent on an event that is never coming. That is
    what a ninety-second setup error on a loaded box was, and why widening the
    window made it slower rather than greener.

    Offering the write again is what ends. The first offer the watch is up for
    settles, so the fixture leaves the moment the boundary it depends on is
    actually reached — and on a quiet box that is still the first offer, with
    nothing else written.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SETUP_CEILING
    offers = 0
    while True:
        await asyncio.to_thread(path.write_bytes, body)
        offers += 1
        next_offer = loop.time() + OFFER_EVERY
        while loop.time() < next_offer:
            found = settled()
            if asyncio.iscoroutine(found):
                found = await found
            if found:
                return found
            await asyncio.sleep(0.05)
        if loop.time() > deadline:
            raise AssertionError(
                f"{what} did not happen within {SETUP_CEILING:.0f}s, over {offers} offer(s)"
            )


async def _beating(folders: ChatFolders, chat_id: str, *, every: float) -> None:
    """Keep the holder's lease alive for as long as the box holds the folder.

    A real box runs exactly this loop in ``CloudService._beat_folders``, and
    the live plane cannot run without it: :class:`SelfFence` closes after two
    heartbeat periods of silence, and a closed fence makes ``LiveSync.flush``
    return without sending anything. A holder nobody beats therefore goes
    permanently deaf about thirty seconds after taking the lease — with no
    error anywhere, because a holder that cannot prove the folder is its own
    is SUPPOSED to stop writing.

    A suite without this beat is green only while it finishes inside that
    grace, so it reads as a machine-speed flake — the write "did not arrive in
    time" — when what actually happened is that the write was never going to
    arrive. Widening the deadline makes it worse, not better: the test waits
    longer in a state that cannot resolve.

    Beating twice per period rather than once for the reason a box does: one
    slow beat must not be able to close the fence.
    """
    while True:
        await asyncio.sleep(every)
        if not await asyncio.to_thread(folders.beat, chat_id):
            return


@pytest_asyncio.fixture
async def content_server() -> AsyncIterator[str]:
    """A second served instance, and the origin user bytes are minted onto.

    Its own origin rather than a second name for the API's, because that is the
    deployment the code enforces: the app answers an opaque 404 to an API path
    that arrives under the content origin and to ``/c/*`` under the API's, so a
    single-origin test would be testing a shape production refuses to boot.
    """
    async with serve_app() as addr:
        yield addr


@pytest_asyncio.fixture
async def live_chat(
    uvicorn_server: str,
    content_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[LiveChat]:
    """A chat, a second member who can read it, and a box holding its folder.

    The upload's commit runs in the request that queued it, because there is
    no worker here to hand it to and what is under test is what the drive does
    with the bytes, not which process moves them.
    """
    monkeypatch.setattr(settings, "files_content_base_url", f"http://{content_server}")
    monkeypatch.setattr(settings, "files_inline_operations", True)

    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    chat, _ = await chat_service.create_chat(
        real_session,
        owner=owner,
        org_id=owner.home_org_team_id,
        title="Ops",
        client_id=None,
        machine_id=None,
        machine_status="none",
    )
    await real_session.commit()

    node = await _node_of(real_session, chat.id)
    scratch = await _child(real_session, node.id, b"scratch")
    assert scratch is not None, "a chat is born with its working directory"

    member, password = await make_member(real_session, org_id=org_admin.org_id)
    assert password is not None
    await share_chat_with(
        real_session,
        chat=chat,
        owner=owner,
        principal=_principal(member),
        role=ROLE_WRITER,
    )
    member_token = await mint_cli_token(
        user_id=member.id, email=member.email, org_team_id=org_admin.org_id
    )
    # The drive admits an agent assertion only when the id names a live
    # workspace machine of this org, registered by this operator, on the very
    # credential the request carries — an id alone is public and proves
    # nothing. Built by the same seam the route suites use, so a box here and
    # a box there are the same shape.
    box_token, box_machine = await registered_box(
        real_session,
        user_id=org_admin.admin_id,
        email=org_admin.admin_email,
        org_id=org_admin.org_id,
    )

    folders = ChatFolders.for_box(
        api_url=f"http://{uvicorn_server}",
        auth=BearerAuth(lambda: box_token),
        chats_root=tmp_path / "chats",
    )
    # The box asserts the machine it registered as. A chat folder is
    # NO_DOWNLOAD for every reader that is not the box running the chat, and
    # the drive tells the two apart by this assertion checked against the
    # registration — so without it the holder cannot read its own folder down.
    folders.bind_machine(box_machine)
    chat_id = str(chat.id)
    held = await asyncio.to_thread(
        folders.take, chat_id, {"files_node_id": str(node.id)}, instance="box-1"
    )
    assert held is not None, "the box could not take the chat's folder"
    working_dir = held.root / "scratch"
    working_dir.mkdir(parents=True, exist_ok=True)
    sync = await asyncio.to_thread(folders.live, chat_id, working_dir)
    assert sync is not None, "the lease was granted without the live plane"
    held_now = folders.held(chat_id)
    assert held_now is not None
    # The box's own heartbeat, on the cadence the grant published. Without it
    # the holder's self-fence closes partway through the module and every
    # later write is dropped in silence (see ``_beating``).
    beating = asyncio.create_task(
        _beating(folders, chat_id, every=held_now.record.heartbeat_every / 2)
    )
    # One write through the whole chain before the deadlines start. Everything
    # a first write pays for once — the watcher's first OS event, the upload
    # session, the store's first open — is paid here, so what the live window
    # measures below is how fast the plane runs, not how long this process
    # took to warm up.
    ready = await _offered_until_it_lands(
        working_dir / "ready.txt",
        b"ready\n",
        _settled(real_session, str(scratch.id), b"ready.txt", 6),
        what="the live plane's first write reaching the drive",
    )
    try:
        yield LiveChat(
            addr=uvicorn_server,
            org_id=org_admin.org_id,
            chat_id=chat_id,
            drive_id=str(node.drive_id),
            chat_node_id=str(node.id),
            scratch_node_id=str(scratch.id),
            ready_node_id=str(ready.id),
            working_dir=working_dir,
            folders=folders,
            member=member,
            member_token=member_token,
            box_token=box_token,
            box_machine=box_machine,
            stop_beating=beating.cancel,
        )
    finally:
        beating.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beating
        await asyncio.to_thread(folders.stop_live, chat_id)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(folders.release, chat_id)


def _principal(user: User) -> Any:
    from alkera_core.files.authz.grants import Principal

    return Principal(kind="user", id=user.id)


@contextlib.asynccontextmanager
async def _as_member(live: LiveChat) -> AsyncIterator[httpx.AsyncClient]:
    """The member's own HTTP client against the served instance."""
    async with httpx.AsyncClient(
        base_url=f"http://{live.addr}",
        timeout=HTTP_TIMEOUT,
        headers={"Authorization": f"Bearer {live.member_token}"},
        follow_redirects=True,
    ) as client:
        yield client


async def _listing(client: httpx.AsyncClient, live: LiveChat) -> list[dict[str, Any]]:
    answer = await client.get(live.url(f"{live.scratch_node_id}/children"))
    assert answer.status_code == 200, answer.text
    body: Any = answer.json()
    rows = body.get("value") if isinstance(body, dict) else body
    assert isinstance(rows, list), body
    return [row for row in rows if isinstance(row, dict)]


async def _served_bytes(live: LiveChat, client: httpx.AsyncClient, node_id: str) -> httpx.Response:
    """Fetch a node's bytes the way a browser does: mint a single-use grant on
    the API origin, then redeem it on the content origin.

    Redeemed against the same served instance with the content Host spelled on
    the request, because that is what the deployment shape here is — one
    process answering two hostnames — and it keeps the signed URL the route
    really minted as the thing under test rather than a path composed here.
    """
    minted = await client.post(
        live.url(f"{node_id}/content-grants"),
        json={"kind": "file"},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert minted.status_code == 201, minted.text
    url = str(minted.json()["url"])
    assert not url.startswith(f"http://{live.addr}"), "bytes were minted onto the API origin"
    # Followed verbatim and with no session of its own: the grant IS the whole
    # credential, and a cookie riding along would say nothing about whether it
    # works — the content origin must never see one.
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as anonymous:
        return await anonymous.get(url)


def _row(rows: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for row in rows:
        if row.get("name") == name or row.get("nameDisplay") == name:
            return row
    return None


# --------------------------------------------------------------------------- #
# out: the agent writes, the member sees it
# --------------------------------------------------------------------------- #


async def test_a_file_written_on_the_box_reaches_the_member_inside_the_live_window(
    live_chat: LiveChat, real_session: AsyncSession
) -> None:
    """The whole outbound claim in one run.

    The file is written into the directory the box is watching — the same thing
    a turn's ``write`` tool does — and nothing else is touched. Within the live
    window the member's stream carries a frame naming the node AND the folder it
    landed in (without the folder a client watching one listing has to refresh
    every listing it holds), the listing shows the row as a settled file rather
    than one still in flight, and the bytes read back are the bytes on the box.
    """
    async with (
        _as_member(live_chat) as member,
        watching(live_chat.addr, live_chat.member_token) as frames,
    ):
        (live_chat.working_dir / "report.html").write_bytes(REPORT)
        # The frame is what is on the clock: the whole chain — watcher, batch,
        # upload, version — has to have run for one to exist at all, and the
        # member learns from it alone that a listing they are showing moved.
        framed = await frames.until(
            in_folder(live_chat.scratch_node_id, besides=frozenset({live_chat.ready_node_id})),
            window=LIVE_WINDOW,
        )
        node_id = str(framed["data"]["entity_id"])

        row = await _until(
            _settled(real_session, live_chat.scratch_node_id, b"report.html", len(REPORT)),
            window=LIVE_WINDOW,
            what="the write reaching the drive",
        )
        assert node_id == str(row.id), "the frame named a different node than the one that landed"

        listed = _row(await _listing(member, live_chat), "report.html")
        assert listed is not None, "the member's listing does not show the file"
        assert listed.get("live") is None, "the row is still reported in flight after it landed"
        assert listed["file"]["size"] == len(REPORT)

        served = await _served_bytes(live_chat, member, node_id)
        assert served.status_code == 200, served.text
        assert served.content == REPORT


async def test_the_plane_keeps_running_after_the_turn_s_first_file(
    live_chat: LiveChat, real_session: AsyncSession
) -> None:
    """A turn writes more than one file, and every one of them is live.

    The second file is the interesting one: the watcher has already delivered a
    batch and the plane has already flushed, so this is what says the stream
    keeps up with a working agent rather than catching only whatever happened
    to be on disk when it started. Each file is its own row with its own bytes.
    """
    async with _as_member(live_chat) as member:
        (live_chat.working_dir / "report.html").write_bytes(REPORT)
        first = await _until(
            _settled(real_session, live_chat.scratch_node_id, b"report.html", len(REPORT)),
            window=LIVE_WINDOW,
            what="the first write reaching the drive",
        )

        (live_chat.working_dir / "appendix.html").write_bytes(REWRITE)
        second = await _until(
            _settled(real_session, live_chat.scratch_node_id, b"appendix.html", len(REWRITE)),
            window=LIVE_WINDOW,
            what="the second write reaching the drive",
        )
        assert str(second.id) != str(first.id)

        for node, expected in ((first, REPORT), (second, REWRITE)):
            served = await _served_bytes(live_chat, member, str(node.id))
            assert served.status_code == 200, served.text
            assert served.content == expected


# --------------------------------------------------------------------------- #
# in: the member uploads into a folder somebody else is holding
# --------------------------------------------------------------------------- #


async def _upload(client: httpx.AsyncClient, live: LiveChat, *, name: str, payload: bytes) -> str:
    """Put ``payload`` into the chat's working directory through the real
    upload routes, and answer the operation id."""
    opened = await client.post(
        UPLOADS,
        json={"declaredSize": len(payload), "name": name, "parentId": live.scratch_node_id},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert opened.status_code == 201, opened.text
    upload_id = opened.json()["uploadId"]
    sent = await client.put(
        f"{UPLOADS}/{upload_id}/parts/1",
        content=payload,
        headers={
            "Idempotency-Key": uuid.uuid4().hex,
            "X-Part-Checksum": blake3(payload).digest().hex(),
        },
    )
    assert sent.status_code == 200, sent.text
    finished = await client.post(
        f"{UPLOADS}/{upload_id}/complete",
        json={
            "parts": [
                {"partNo": 1, "size": len(payload), "checksum": blake3(payload).digest().hex()}
            ]
        },
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert finished.status_code == 202, finished.text
    return str(finished.json()["id"])


async def _live_row(db: AsyncSession, node_id: Any) -> Any:
    return (
        await db.execute(
            text("SELECT state FROM file_lease_live_entries WHERE node_id = :node"),
            {"node": node_id},
        )
    ).one_or_none()


async def test_a_members_upload_is_handed_to_the_box_and_clears_once_applied(
    live_chat: LiveChat, real_session: AsyncSession
) -> None:
    """A person drops a file into the chat while the machine is running it.

    The write is admitted rather than refused — the lease takes drops — and is
    recorded as owed to the holder. The box drains it, the bytes are on its
    disk where the agent will read them, and the row is gone: an entry the
    holder already applied that was offered again would be downloaded a second
    time over a file the agent may have edited since.
    """
    async with _as_member(live_chat) as member:
        await _upload(member, live_chat, name="seats.csv", payload=DROPPED)

        async def landed() -> Any:
            row = await _child(real_session, uuid.UUID(live_chat.scratch_node_id), b"seats.csv")
            return row if row is not None and row.head_version_id is not None else None

        row = await _until(landed, window=SETUP_WAIT, what="the upload landing its bytes")
        owed = await _live_row(real_session, row.id)
        assert owed is not None and owed.state == "inbound", (
            "the drive did not record the drop for the machine holding the folder"
        )

        held = live_chat.folders.held(live_chat.chat_id)
        assert held is not None and held.live is not None
        reported = await asyncio.to_thread(held.live.pull_inbound)
        assert [entry.state for entry in reported if entry.node_id == str(row.id)] == ["applied"], (
            reported
        )

        assert (live_chat.working_dir / "seats.csv").read_bytes() == DROPPED
        real_session.expire_all()
        assert await _live_row(real_session, row.id) is None, "the applied entry was not cleared"


async def test_a_drain_in_the_window_before_the_bytes_exist_writes_nothing(
    live_chat: LiveChat, real_session: AsyncSession
) -> None:
    """The upload opens the node before it fills it, and the holder must wait.

    The landing creates the node and records it as owed to the holder in one
    transaction, and writes the bytes in the next — so a holder woken by the
    first commit meets a node with no version at all. The drive says so
    (``files.live_pending``) rather than serving nothing, and the holder comes
    away with no file and no report: an empty ``seats.csv`` on the box would be
    what the agent read, and an ``applied`` would retire the entry that is the
    only record the drop ever happened.
    """
    held = live_chat.folders.held(live_chat.chat_id)
    assert held is not None and held.live is not None
    node_id = await _headless_node(live_chat, real_session, name=b"seats.csv")

    async with _as_member(live_chat) as member:
        refused = await member.get(live_chat.url(f"{node_id}/content"))
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "files.live_pending", refused.text

    reported = await asyncio.to_thread(held.live.pull_inbound)
    assert [entry for entry in reported if entry.node_id == str(node_id)] == [], (
        "the holder reported on a node whose bytes do not exist yet"
    )
    assert not (live_chat.working_dir / "seats.csv").exists()
    owed = await _live_row(real_session, node_id)
    assert owed is not None and owed.state == "inbound", "the drop stopped being owed"


async def _headless_node(live: LiveChat, db: AsyncSession, *, name: bytes) -> uuid.UUID:
    """A node the drive has opened for a drop whose bytes have not landed.

    Built through the library the upload's landing uses, under the same fence
    an ordinary member's upload presents (none of its own), so what it produces
    is the state the upload route really passes through — not a row written by
    hand into a shape nothing else makes.
    """
    from alkera_core.files.ids import DriveId, NodeId
    from alkera_core.files.namespace import Namespace

    ctx = ActingContext.for_user(
        user_id=live.member.id, org_id=live.org_id, email=live.member.email
    )
    repo = FilesRepo(db, OrgScope(org_team_id=live.org_id))
    async with repo.transaction():
        made = await Namespace(repo, ctx, SystemClock(), None).create(
            DriveId(uuid.UUID(live.drive_id)),
            NodeId(uuid.UUID(live.scratch_node_id)),
            "file",
            name,
            conflict="fail",
        )
    await db.commit()
    return uuid.UUID(str(made.id))


# --------------------------------------------------------------------------- #
# silence: the box stops beating
# --------------------------------------------------------------------------- #


async def test_a_box_that_stops_beating_loses_the_facet_but_not_its_half_written_file(
    live_chat: LiveChat, real_session: AsyncSession
) -> None:
    """Silence ends the lease; it does not end the box's custody of a file.

    While the lease is live the folder carries the facet a reader turns into
    "Live — … is working on …". Once it has expired the facet is gone from the
    very next read, with no sweeper in between, so the reader falls back to
    "Last saved" on its own. The reaper then retracts what the plane claimed
    but hides nothing: the node the machine opened and never filled has its
    only bytes on that machine, which may be back once an outage ends, so it
    stays listed rather than vanishing with no trash entry to restore it from.
    """
    node_id = await _headless_node(live_chat, real_session, name=b"half-written.bin")
    async with _as_member(live_chat) as member:
        item = await member.get(live_chat.url(live_chat.chat_node_id))
        assert item.status_code == 200, item.text
        facet = item.json().get("lease")
        assert facet is not None and facet["live"] is True

        await _expire_lease(real_session, live_chat)

        after = await member.get(live_chat.url(live_chat.chat_node_id))
        assert after.status_code == 200, after.text
        assert after.json().get("lease") is None, "an expired lease still claims to be live"

    await _reap(real_session, live_chat)
    trashed = (
        await real_session.execute(
            text("SELECT trashed_at FROM file_nodes WHERE id = :id"), {"id": node_id}
        )
    ).scalar_one()
    assert trashed is None, "the reaper hid a file whose only copy is on the box"
    async with _as_member(live_chat) as member:
        assert _row(await _listing(member, live_chat), "half-written.bin") is not None


async def _expire_lease(db: AsyncSession, live: LiveChat) -> None:
    """Stop the beat and move the lease's deadline into the past.

    Waiting out a real TTL would put a lease-length sleep in the suite; what is
    under test is what an expired lease means, not how long one lasts.
    """
    live.stop_beating()
    await asyncio.to_thread(live.folders.stop_live, live.chat_id)
    await db.execute(
        text("UPDATE file_leases SET expires_at = :past WHERE node_id = :node"),
        {"past": datetime.now(UTC) - timedelta(seconds=1), "node": uuid.UUID(live.chat_node_id)},
    )
    await db.commit()


async def _reap(db: AsyncSession, live: LiveChat) -> None:
    """One pass of the reaper, run directly — the janitor's sweeper, not a
    reimplementation of what it does."""
    ctx = ActingContext.for_user(
        user_id=live.member.id, org_id=live.org_id, email=live.member.email
    )
    repo = FilesRepo(db, OrgScope(org_team_id=live.org_id))
    await LeaseReaper(SweepDeps(repo=repo, ctx=ctx)).run(datetime.now(UTC))
    await db.commit()


async def test_a_rewrite_on_the_box_lands_as_a_new_version_of_the_same_node(
    live_chat: LiveChat, real_session: AsyncSession
) -> None:
    """The agent saves the same file twice, and the drive keeps up.

    An agent's own work is the repeated-write case: it drafts a report and
    rewrites it, and every save after the first has to land as a new VERSION of
    the node the name already has. A push that sent the rewrite as a create is
    refused by the drive ("that name is taken in this folder") while the holder
    records the new digest as sent — so the second save and every save after it
    is lost in silence, which is the whole of the agent's output.
    """
    async with _as_member(live_chat) as member:
        (live_chat.working_dir / "report.html").write_bytes(REPORT)
        first = await _until(
            _settled(real_session, live_chat.scratch_node_id, b"report.html", len(REPORT)),
            window=LIVE_WINDOW,
            what="the first save reaching the drive",
        )

        (live_chat.working_dir / "report.html").write_bytes(REWRITE)
        rewritten = await _until(
            _settled(real_session, live_chat.scratch_node_id, b"report.html", len(REWRITE)),
            window=LIVE_WINDOW,
            what="the rewrite reaching the drive",
        )

        assert str(rewritten.id) == str(first.id), "the rewrite minted a second node"
        assert str(rewritten.etag) != str(first.etag), "the rewrite did not move the etag"
        assert str(rewritten.head_version_id) != str(first.head_version_id)

        listed = _row(await _listing(member, live_chat), "report.html")
        assert listed is not None and listed["file"]["size"] == len(REWRITE)
        served = await _served_bytes(live_chat, member, str(rewritten.id))
        assert served.status_code == 200, served.text
        assert served.content == REWRITE, "the member is still served the superseded draft"


async def test_the_boxs_own_writes_are_not_offered_back_to_it_as_drops(
    live_chat: LiveChat, real_session: AsyncSession
) -> None:
    """A file the box wrote is settled, not queued for the box to fetch.

    The live plane writes through the folder's own fenced client, namespace
    included. Opened unfenced, the drive cannot tell the holder's own save from
    a stranger's upload: it admits the write as an inbound DROP for this box to
    download back over the file it just wrote, and the lease's ``last_sync_at``
    never moves — so the member's row says the file is still on its way to the
    workspace that produced it.
    """
    (live_chat.working_dir / "report.html").write_bytes(REPORT)
    row = await _until(
        _settled(real_session, live_chat.scratch_node_id, b"report.html", len(REPORT)),
        window=LIVE_WINDOW,
        what="the write reaching the drive",
    )
    real_session.expire_all()
    owed = await _live_row(real_session, row.id)
    assert owed is None or owed.state != "inbound", (
        "the drive recorded the holder's own write as a drop for the holder to fetch"
    )

    held = live_chat.folders.held(live_chat.chat_id)
    assert held is not None and held.live is not None
    assert [entry.node_id for entry in await asyncio.to_thread(held.live.pull_inbound)] == [], (
        "the box was handed its own write back"
    )

    synced = (
        await real_session.execute(
            text("SELECT last_sync_at FROM file_leases WHERE node_id = :node"),
            {"node": uuid.UUID(live_chat.chat_node_id)},
        )
    ).scalar_one_or_none()
    assert synced is not None, "the lease never recorded that the holder had saved"


async def test_a_second_box_resuming_the_chat_pulls_what_its_scratch_already_holds(
    live_chat: LiveChat, real_session: AsyncSession, tmp_path: Path
) -> None:
    """Resume anywhere: the next box starts with the chat's work on its disk.

    The chat folder carries ``NO_DOWNLOAD`` — a chat's bytes never leave the
    platform as a file — and that governs readers who are not the holder. The
    box running the chat is the one reader it must not govern: it pulls the
    folder it leases, under the fence the lease just granted. Without the pull
    a chat resumed on a second machine opens on an empty working directory and
    loses sight of everything the last turn produced.
    """
    (live_chat.working_dir / "report.html").write_bytes(REPORT)
    await _until(
        _settled(real_session, live_chat.scratch_node_id, b"report.html", len(REPORT)),
        window=LIVE_WINDOW,
        what="the first box's work reaching the drive",
    )
    await asyncio.to_thread(live_chat.folders.stop_live, live_chat.chat_id)
    await asyncio.to_thread(live_chat.folders.release, live_chat.chat_id)

    second = ChatFolders.for_box(
        api_url=f"http://{live_chat.addr}",
        auth=BearerAuth(lambda: live_chat.box_token),
        chats_root=tmp_path / "second-box",
    )
    second.bind_machine(live_chat.box_machine)
    held = await asyncio.to_thread(
        second.take,
        live_chat.chat_id,
        {"files_node_id": live_chat.chat_node_id},
        instance="box-2",
    )
    try:
        assert held is not None, "the second box could not take the chat's folder"
        assert held.pull.undownloadable == 0, held.pull.warnings
        assert (held.root / "scratch" / "report.html").read_bytes() == REPORT, (
            "the resumed box did not get the chat's existing work"
        )
    finally:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(second.release, live_chat.chat_id)


async def _scratch_names(db: AsyncSession, live: LiveChat) -> list[bytes]:
    db.expire_all()
    rows = await db.execute(
        text("SELECT name FROM file_nodes WHERE parent_id = :parent AND trashed_at IS NULL"),
        {"parent": uuid.UUID(live.scratch_node_id)},
    )
    return sorted(bytes(row.name) for row in rows)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param("save", id="a-save"),
        "restore",
        pytest.param("restore-unwritten", id="restore-before-the-map-is-written"),
    ],
)
async def test_a_change_a_person_makes_after_the_box_agreed_a_file_is_never_written_over(
    live_chat: LiveChat, real_session: AsyncSession, change: str
) -> None:
    """The box holds ``notes.txt`` as agreed with the drive; a person then
    saves new text, or restores an older version, through the API. Before the
    box takes that change, the agent's turn ends and the checkpoint push runs
    over the stale copy. The person's version stays the head, no conflicted
    copy appears and no version is added; the box then takes their bytes.
    It used to file its copy over theirs on the base it last agreed, which
    kept their version only as a conflicted copy beside the file.

    The live sync writes the node map the push reads its bases from at most
    once per debounce. ``restore-before-the-map-is-written`` holds that write
    back for the whole test, so the push meets the box's second save landed on
    the drive but not yet in the map, as a loaded box does for a moment after
    each upload; the push still fences on what the sync agreed."""
    notes = live_chat.working_dir / "notes.txt"
    first, second = b"base\n", b"base, the agent's second line\n"
    notes.write_bytes(first)
    await _until(
        _settled(real_session, live_chat.scratch_node_id, b"notes.txt", len(first)),
        window=LIVE_WINDOW,
        what="the first save reaching the drive",
    )
    if change == "restore-unwritten":
        held_now = live_chat.folders.held(live_chat.chat_id)
        assert held_now is not None and held_now.live is not None
        live = held_now.live
        live.cadence = dataclasses.replace(live.cadence, debounce_ms=600_000)
    if change.startswith("restore"):
        notes.write_bytes(second)
        await _until(
            _settled(real_session, live_chat.scratch_node_id, b"notes.txt", len(second)),
            window=LIVE_WINDOW,
            what="the second save reaching the drive",
        )
    row = await _child(real_session, uuid.UUID(live_chat.scratch_node_id), b"notes.txt")
    names = await _scratch_names(real_session, live_chat)

    async with _as_member(live_chat) as member:
        theirs = b"what the person saved\n" if change == "save" else first
        listed = await member.get(live_chat.url(f"{row.id}/versions"))
        assert listed.status_code == 200, listed.text
        oldest = min(listed.json()["versions"], key=lambda version: version["seq"])
        for _attempt in range(3):
            # The box's metadata reports move the etag without touching the
            # bytes, so the precondition is read right before each try.
            real_session.expire_all()
            fresh = await _child(real_session, uuid.UUID(live_chat.scratch_node_id), b"notes.txt")
            key = {"Idempotency-Key": uuid.uuid4().hex, "If-Match": str(fresh.etag)}
            if change == "save":
                written = await member.put(
                    live_chat.url(f"{row.id}/content"),
                    content=theirs,
                    headers={**key, "Content-Type": "application/octet-stream"},
                )
            else:
                written = await member.post(
                    live_chat.url(f"{row.id}/versions/{oldest['id']}/restore"),
                    json={},
                    headers=key,
                )
            if written.status_code != 412:
                break
        assert written.status_code in (200, 201), written.text
        real_session.expire_all()
        head = await _child(real_session, uuid.UUID(live_chat.scratch_node_id), b"notes.txt")

        assert await asyncio.to_thread(live_chat.folders.push, live_chat.chat_id) is not None

        real_session.expire_all()
        after = await _child(real_session, uuid.UUID(live_chat.scratch_node_id), b"notes.txt")
        assert after.head_version_id == head.head_version_id, "the box's copy became the head"
        assert await _scratch_names(real_session, live_chat) == names, "a copy appeared"
        served = await _served_bytes(live_chat, member, str(row.id))
        assert served.content == theirs

    held = live_chat.folders.held(live_chat.chat_id)
    assert held is not None and held.live is not None
    reported = await asyncio.to_thread(held.live.pull_inbound)
    assert [entry.state for entry in reported if entry.node_id == str(row.id)] == ["applied"]
    assert notes.read_bytes() == theirs
