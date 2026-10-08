"""Bytes on demand through the real content routes.

A folder is held live by a registered box whose socket holds its own machine
channel on a served app; the holder's tree report has minted rows ahead of
their bytes. A reader opens one of those files. What is asserted is what the
reader is answered and what the machine is asked — over the real socket, the
real ``pg_notify`` lane, the real listener and hub, and the real fenced upload
that lands the bytes:

* a file with no bytes yet asks the machine exactly once, parks, and is served
  from the store the moment the holder's upload lands; a second reader joins
  the same wait;
* no answer in time is ``files.live_pending`` with ``Retry-After`` and a detail
  naming the machine, what asking it came to and how far its upload has got;
* a file the drive holds an older copy of is served from the store, marked as
  such, when the newer one does not arrive;
* a file the drive already holds, and a folder whose machine is not serving,
  ask nothing;
* a parked reader holds no database connection.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import pytest
import pytest_asyncio
from _files_kit import NOT_FOUND, refusal
from _promote_world import (
    FRAME_BACKSTOP,
    World,
    drained,
    held_world,
    reported,
    requests_heard,
)
from alkera_core.config import settings
from alkera_core.db.session import engine
from alkera_core.files.promotion import PromoteOutcome, Promoter
from httpx import AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import app_client, login

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.usefixtures("files_on"),
    # Each case serves a real app and waits on real frames over a real socket;
    # they run in series on one worker so a wait measures the socket, not the
    # queue of other served apps in front of it.
    pytest.mark.xdist_group("content_promote"),
]

CONTENT_HOST = "files.localhost:8000"
CONTENT_ORIGIN = f"http://{CONTENT_HOST}"

V1 = b"id,total\n1,10\n"
V2 = b"id,total\n1,10\n2,20\n"
#: How long a case lets the machine take to answer, and a reader to wait for
#: the bytes, when the case is about the answer not coming. Short, because the
#: claim is WHICH answer a reader gets, and the deadline is the premise.
ACK_SECONDS = 0.5
WAIT_SECONDS = 1.5


@pytest.fixture(autouse=True)
def _content_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "files_content_base_url", CONTENT_ORIGIN)


@pytest.fixture
def short_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "files_promote_ack_seconds", ACK_SECONDS)
    monkeypatch.setattr(settings, "files_promote_wait_seconds", WAIT_SECONDS)
    monkeypatch.setattr(settings, "files_promote_page_wait_seconds", WAIT_SECONDS)


@pytest_asyncio.fixture
async def world(
    uvicorn_server: str,
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    files_org: Any,
) -> World:
    """``/Shared/project`` held live by the box; the served app is running, so
    a reader's request on the shared app waits on its hub."""
    return await held_world(
        files_client,
        fx,
        real_session,
        idem,
        org_id=files_org.org.org_id,
        admin_id=files_org.org.admin_id,
        admin_email=files_org.org.admin_email,
    )


async def reader(files_org: Any) -> AsyncClient:
    """Another browser tab of a person who may read the folder."""
    client = app_client()
    return await login(client, files_org.org.admin_email, files_org.org.admin_password)


async def _redeem(client: AsyncClient, url: str) -> bytes:
    served = await client.get(url.removeprefix(CONTENT_ORIGIN), headers={"Host": CONTENT_HOST})
    assert served.status_code == 200, served.text
    return served.content


# ---------------------------------------------------------------------------
# A file with no bytes yet
# ---------------------------------------------------------------------------


async def test_an_unlanded_file_asks_the_machine_once_and_is_served_once_its_bytes_land(
    uvicorn_server: str, world: World, files_org: Any
) -> None:
    await world.report(reported("data/report.csv", V1))
    node_id = await world.node_at(b"report.csv")
    tab = await reader(files_org)
    async with world.box(uvicorn_server) as box:
        with requests_heard() as heard:
            opening = asyncio.create_task(tab.post(world.grants(node_id), json={"kind": "file"}))
            request = await box.request()
            assert (
                request["kind"],
                request["path"],
                request["node_id"],
                request["lease_node_id"],
                request["epoch"],
                request["expected"],
            ) == (
                "promote",
                "data/report.csv",
                str(node_id),
                str(world.project_id),
                world.holder.epoch,
                {"size": len(V1), "mtime_ns": reported("x", V1)["mtime_ns"]},
            )
            await box.ack(request, "accepted")
            await asyncio.sleep(0.2)
            assert not opening.done(), "an accepted request waits for the bytes"

            await world.land(node_id, V1)
            answer = await asyncio.wait_for(opening, FRAME_BACKSTOP)
            assert answer.status_code == 201, answer.text
            body = answer.json()
            assert body["contentState"] == "on_drive"
            assert body["asOf"] is None
            assert "x-alkera-content-state" not in answer.headers
            assert await _redeem(tab, body["url"]) == V1
            assert len(drained(heard)) == 1, "the machine was asked exactly once"
        await box.nothing_asked(0.5)
    await tab.aclose()


async def test_a_second_reader_joins_the_first_readers_wait(
    uvicorn_server: str, world: World, files_org: Any
) -> None:
    await world.report(reported("report.csv", V1))
    node_id = await world.node_at(b"report.csv")
    first, second = await reader(files_org), await reader(files_org)
    async with world.box(uvicorn_server) as box:
        with requests_heard() as heard:
            one = asyncio.create_task(first.post(world.grants(node_id), json={"kind": "file"}))
            request = await box.request()
            two = asyncio.create_task(second.get(world.content(node_id)))
            await box.ack(request, "accepted")
            await asyncio.sleep(0.3)
            assert not two.done(), "the second reader is parked on the same landing"
            await world.land(node_id, V1)
            granted, redirected = await asyncio.wait_for(asyncio.gather(one, two), FRAME_BACKSTOP)
            assert granted.status_code == 201, granted.text
            assert redirected.status_code == 302, redirected.text
            assert await _redeem(second, redirected.headers["location"]) == V1
            assert len(drained(heard)) == 1, "two readers, one request"
        await box.nothing_asked(0.5)
    await first.aclose()
    await second.aclose()


@pytest.mark.usefixtures("short_waits")
async def test_no_landing_in_time_is_live_pending_naming_the_machine_and_the_outcome(
    uvicorn_server: str, world: World, files_org: Any
) -> None:
    await world.report(reported("report.csv", V1))
    node_id = await world.node_at(b"report.csv")
    tab = await reader(files_org)
    async with world.box(uvicorn_server) as box:
        opening = asyncio.create_task(tab.post(world.grants(node_id), json={"kind": "file"}))
        await box.ack(await box.request(), "accepted")
        answer = await asyncio.wait_for(opening, FRAME_BACKSTOP)
    assert answer.status_code == 409, answer.text
    assert answer.headers["retry-after"] == "2"
    assert refusal(answer) == {
        "type": "urn:alkera:error:files.live_pending",
        "code": "files.live_pending",
        "status": 409,
        "message": "The machine holding this folder has not synced these bytes yet",
        "details": {"holder": world.machine_id, "outcome": "accepted", "landing": None},
    }
    await tab.aclose()


@pytest.mark.usefixtures("short_waits")
async def test_live_pending_names_the_machine_by_the_name_the_listing_shows(
    world: World, files_org: Any
) -> None:
    """The lease names a registered box by its allocation id; a person knows it
    by the allocation's name, which is what every listing of the folder shows.
    The refusal a reader is waiting behind names it the same way — and a
    machine with no name keeps the lease's own word for it."""
    await world.session.execute(
        text("UPDATE compute_allocations SET name = 'alkera-demo-box' WHERE id = :id"),
        {"id": uuid.UUID(world.machine_id)},
    )
    await world.session.commit()
    await world.report(reported("report.csv", V1))
    node_id = await world.node_at(b"report.csv")
    tab = await reader(files_org)
    answer = await tab.get(world.content(node_id))
    assert answer.status_code == 409, answer.text
    assert answer.json()["detail"]["holder"] == "alkera-demo-box"
    await tab.aclose()


@pytest.mark.usefixtures("short_waits")
async def test_a_machine_that_never_answers_ends_the_wait_at_the_ack_window(
    world: World, files_org: Any
) -> None:
    """No socket holds the machine's channel: the request goes nowhere, and the
    reader is answered when the ack window closes, not at the deadline."""
    await world.report(reported("report.csv", V1))
    node_id = await world.node_at(b"report.csv")
    tab = await reader(files_org)
    loop = asyncio.get_running_loop()
    started = loop.time()
    answer = await tab.get(world.content(node_id))
    elapsed = loop.time() - started
    assert answer.status_code == 409, answer.text
    assert answer.json()["detail"]["outcome"] == "timed_out"
    assert ACK_SECONDS * 0.9 <= elapsed < WAIT_SECONDS
    await tab.aclose()


@pytest.mark.parametrize("outcome", ["not_holder", "missing", "busy"])
async def test_a_machine_refusing_the_request_is_live_pending_with_its_answer_at_once(
    uvicorn_server: str, world: World, files_org: Any, outcome: str
) -> None:
    await world.report(reported("report.csv", V1))
    node_id = await world.node_at(b"report.csv")
    tab = await reader(files_org)
    async with world.box(uvicorn_server) as box:
        opening = asyncio.create_task(tab.get(world.content(node_id)))
        await box.ack(await box.request(), outcome)
        answer = await asyncio.wait_for(opening, FRAME_BACKSTOP)
    assert answer.status_code == 409, answer.text
    assert answer.json()["detail"]["outcome"] == outcome
    await tab.aclose()


# ---------------------------------------------------------------------------
# The holder reading its own files
# ---------------------------------------------------------------------------


async def test_the_holder_reading_its_own_unlanded_file_is_answered_at_once_and_asks_nothing(
    uvicorn_server: str, world: World
) -> None:
    """The box downloads what the drive lists under its own lease (an inbound
    pass, a re-take). A file whose bytes are still on that same box must not
    send the box a request for them and park this request until the deadline:
    the request IS the box, waiting on itself."""
    await world.report(reported("report.csv", V1))
    node_id = await world.node_at(b"report.csv")
    loop = asyncio.get_running_loop()
    async with world.box(uvicorn_server) as box:
        started = loop.time()
        answer = await asyncio.wait_for(world.box_http.get(world.content(node_id)), FRAME_BACKSTOP)
        elapsed = loop.time() - started
        await box.nothing_asked()
    assert answer.status_code == 409, answer.text
    assert answer.json()["detail"]["outcome"] == "requester"
    assert elapsed < settings.files_promote_ack_seconds


async def test_the_holder_reading_a_file_it_has_rewritten_gets_the_stores_copy_and_asks_nothing(
    uvicorn_server: str, world: World
) -> None:
    await world.report(reported("report.csv", V1))
    node_id = await world.node_at(b"report.csv")
    await world.land(node_id, V1)
    await world.report(reported("report.csv", V2, mtime=reported("x", V1)["mtime_ns"] + 1))
    async with world.box(uvicorn_server) as box:
        answer = await asyncio.wait_for(world.box_http.get(world.content(node_id)), FRAME_BACKSTOP)
        await box.nothing_asked()
    assert answer.status_code == 302, answer.text
    assert answer.headers["x-alkera-content-state"] == "behind"


@pytest.mark.usefixtures("short_waits")
async def test_live_pending_says_how_far_the_holders_upload_of_the_file_has_got(
    uvicorn_server: str, world: World, files_org: Any
) -> None:
    from blake3 import blake3
    from tests.files._live_holder import PREFIX

    await world.report(reported("report.csv", V1))
    node_id = await world.node_at(b"report.csv")
    opened = await world.holder._client.post(
        f"{PREFIX}/uploads",
        json={"declaredSize": len(V1), "name": "report.csv", "parentId": str(world.project_id)},
        headers={**world.idem(), **world.holder.fence},
    )
    assert opened.status_code == 201, opened.text
    part = await world.holder._client.put(
        f"{PREFIX}/uploads/{opened.json()['uploadId']}/parts/1",
        content=V1,
        headers={**world.idem(), "X-Part-Checksum": blake3(V1).digest().hex()},
    )
    assert part.status_code == 200, part.text
    tab = await reader(files_org)
    answer = await tab.get(world.content(node_id))
    assert answer.status_code == 409, answer.text
    assert answer.json()["detail"]["landing"] == {"done_bytes": len(V1), "total_bytes": len(V1)}
    await tab.aclose()


# ---------------------------------------------------------------------------
# A file the drive holds an older copy of
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("short_waits")
async def test_a_behind_file_whose_newer_copy_does_not_arrive_is_served_from_the_store_marked(
    uvicorn_server: str, world: World, files_org: Any
) -> None:
    await world.report(reported("report.csv", V1))
    node_id = await world.node_at(b"report.csv")
    await world.land(node_id, V1)
    await world.report(reported("report.csv", V2, mtime=reported("x", V1)["mtime_ns"] + 1))
    last_sync = (
        await world.session.execute(
            text("SELECT last_sync_at FROM file_leases WHERE node_id = :node"),
            {"node": world.project_id},
        )
    ).scalar_one()
    await world.session.commit()

    tab = await reader(files_org)
    async with world.box(uvicorn_server) as box:
        with requests_heard() as heard:
            granting = asyncio.create_task(tab.post(world.grants(node_id), json={"kind": "file"}))
            request = await box.request()
            assert request["expected"]["size"] == len(V2)
            granted = await asyncio.wait_for(granting, FRAME_BACKSTOP)
            redirected = await tab.get(world.content(node_id))
            assert len(drained(heard)) == 2, "each reader of a behind file asks again"

    assert granted.status_code == 201, granted.text
    body = granted.json()
    assert body["contentState"] == "behind"
    assert body["asOf"] is not None
    assert granted.headers["x-alkera-content-state"] == "behind"
    assert granted.headers["x-alkera-content-as-of"] == last_sync.isoformat()
    assert await _redeem(tab, body["url"]) == V1, "the store's older copy is what is served"

    assert redirected.status_code == 302, redirected.text
    assert redirected.headers["x-alkera-content-state"] == "behind"
    assert await _redeem(tab, redirected.headers["location"]) == V1
    await tab.aclose()


async def test_a_behind_file_whose_newer_copy_lands_is_served_new(
    uvicorn_server: str, world: World, files_org: Any
) -> None:
    await world.report(reported("report.csv", V1))
    node_id = await world.node_at(b"report.csv")
    await world.land(node_id, V1)
    await world.report(reported("report.csv", V2, mtime=reported("x", V1)["mtime_ns"] + 1))
    tab = await reader(files_org)
    async with world.box(uvicorn_server) as box:
        granting = asyncio.create_task(tab.post(world.grants(node_id), json={"kind": "file"}))
        await box.ack(await box.request(), "accepted")
        await world.land(node_id, V2)
        granted = await asyncio.wait_for(granting, FRAME_BACKSTOP)
    assert granted.status_code == 201, granted.text
    assert granted.json()["contentState"] == "on_drive"
    assert "x-alkera-content-state" not in granted.headers
    assert await _redeem(tab, granted.json()["url"]) == V2
    await tab.aclose()


# ---------------------------------------------------------------------------
# Nothing to ask
# ---------------------------------------------------------------------------


async def test_a_file_the_drive_already_holds_asks_the_machine_nothing(
    uvicorn_server: str, world: World, files_org: Any
) -> None:
    await world.report(reported("report.csv", V1))
    node_id = await world.node_at(b"report.csv")
    await world.land(node_id, V1)
    tab = await reader(files_org)
    async with world.box(uvicorn_server) as box:
        with requests_heard() as heard:
            granted = await tab.post(world.grants(node_id), json={"kind": "file"})
            redirected = await tab.get(world.content(node_id))
            await box.nothing_asked(1.0)
            assert drained(heard) == []
    assert granted.status_code == 201, granted.text
    assert granted.json()["contentState"] == "none"
    assert redirected.status_code == 302
    assert await _redeem(tab, granted.json()["url"]) == V1
    await tab.aclose()


async def test_a_folder_whose_machine_stopped_beating_asks_nothing_and_answers_at_once(
    uvicorn_server: str, world: World, files_org: Any
) -> None:
    await world.report(reported("report.csv", V1))
    node_id = await world.node_at(b"report.csv")
    await world.session.execute(
        text(
            "UPDATE file_leases SET heartbeat_at = now() - interval '10 minutes' "
            "WHERE node_id = :node"
        ),
        {"node": world.project_id},
    )
    await world.session.commit()
    tab = await reader(files_org)
    loop = asyncio.get_running_loop()
    async with world.box(uvicorn_server) as box:
        with requests_heard() as heard:
            started = loop.time()
            answer = await tab.get(world.content(node_id))
            elapsed = loop.time() - started
            await box.nothing_asked(0.5)
            assert drained(heard) == []
    assert answer.status_code == 409, answer.text
    assert answer.json()["detail"] == {
        "holder": world.machine_id,
        "outcome": "offline",
        "landing": None,
    }
    assert elapsed < settings.files_promote_ack_seconds, "nothing was waited for"
    await tab.aclose()


# ---------------------------------------------------------------------------
# A parked reader
# ---------------------------------------------------------------------------


class _Checkouts:
    """Which task holds each connection checked out of the app engine's pool.

    A pool-wide count is not what a reader holds: the served app's own socket
    handlers borrow from the same pool in the same process (the box's ack is
    published on a connection of its own), so a slow runner that is still
    committing one of those at the moment of the count reads it as a reader's.
    A request's statements run in the task that sent it, so attributing each
    checkout to its task scopes the count to the readers alone."""

    def __init__(self) -> None:
        self._held: dict[int, asyncio.Task[Any] | None] = {}

    def held_by(self, tasks: list[asyncio.Task[Any]]) -> int:
        return sum(1 for holder in self._held.values() if holder in tasks)

    def _checkout(self, _dbapi: Any, record: Any, _proxy: Any) -> None:
        try:
            self._held[id(record)] = asyncio.current_task()
        except RuntimeError:
            self._held[id(record)] = None

    def _checkin(self, _dbapi: Any, record: Any) -> None:
        self._held.pop(id(record), None)

    def __enter__(self) -> _Checkouts:
        event.listen(engine.sync_engine, "checkout", self._checkout)
        event.listen(engine.sync_engine, "checkin", self._checkin)
        return self

    def __exit__(self, *_exc: object) -> None:
        event.remove(engine.sync_engine, "checkout", self._checkout)
        event.remove(engine.sync_engine, "checkin", self._checkin)


async def test_a_parked_reader_holds_no_database_connection(
    uvicorn_server: str, world: World, files_org: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.report(reported("report.csv", V1))
    node_id = await world.node_at(b"report.csv")
    tabs = [await reader(files_org) for _ in range(3)]
    # A reader is parked once it is inside the promoter's wait: the route has
    # done all its database work by then. "Not answered yet" is not the same
    # thing -- a reader still authorizing on a slow runner is not answered
    # either, and holds its connection legitimately.
    waiting: list[asyncio.Task[Any] | None] = []
    promote = Promoter.promote

    async def entered(self: Promoter, *args: Any, **kwargs: Any) -> PromoteOutcome:
        waiting.append(asyncio.current_task())
        return await promote(self, *args, **kwargs)

    monkeypatch.setattr(Promoter, "promote", entered)
    async with world.box(uvicorn_server) as box:
        with _Checkouts() as checkouts:
            opening = [
                asyncio.create_task(tab.post(world.grants(node_id), json={"kind": "file"}))
                for tab in tabs
            ]
            request = await box.request()
            await box.ack(request, "accepted")
            loop = asyncio.get_running_loop()
            give_up = loop.time() + FRAME_BACKSTOP
            while not set(opening) <= set(waiting):
                assert loop.time() < give_up, f"{len(waiting)} of three readers parked"
                await asyncio.sleep(0.01)
            assert not any(task.done() for task in opening), "all three readers are parked"
            held = checkouts.held_by(opening)
            assert held == 0, f"three parked readers hold {held} connection(s) between them"
        await world.land(node_id, V1)
        answers = await asyncio.wait_for(asyncio.gather(*opening), FRAME_BACKSTOP)
    assert [answer.status_code for answer in answers] == [201, 201, 201]
    for tab in tabs:
        await tab.aclose()


async def test_a_node_nobody_may_see_is_the_opaque_404_and_asks_nothing(
    uvicorn_server: str, world: World, files_org: Any
) -> None:
    """The three "not yours" classes answer before the lease is read, so none of
    them can make a machine do work — the no-oracle table in
    ``test_files_no_oracle_items`` holds them to identical answers and costs."""
    tab = await reader(files_org)
    async with world.box(uvicorn_server) as box:
        with requests_heard() as heard:
            answer = await tab.post(world.grants(uuid.uuid4()), json={"kind": "file"})
            await box.nothing_asked(0.5)
            assert drained(heard) == []
    assert answer.status_code == 404
    assert refusal(answer) == NOT_FOUND
    await tab.aclose()


async def test_a_reported_file_a_member_may_not_read_is_the_opaque_404_and_asks_nothing(
    uvicorn_server: str, world: World, files_org: Any
) -> None:
    """A file under the live lease with no bytes yet, behind a share that
    names only the org admin. A member who may not read it gets exactly the
    404 a node that does not exist gets — not ``files.live_pending``, whose
    detail would confirm the file and name the machine — and the machine is
    never asked, so the member cannot make a box do work for a file they may
    not see."""
    from alkera_core.files.acl_intern import ace_body, body_hash
    from alkera_core.files.authz.grants import Grant, GrantOrigin, Principal
    from alkera_core.models.files.acl import FileAcl

    await world.report(reported("secret.csv", V1))
    node_id = await world.node_at(b"secret.csv")
    body = ace_body(
        [
            Grant(
                principal=Principal(kind="user", id=files_org.org.admin_id),
                role="manager",
                origin=GrantOrigin.direct(),
            )
        ]
    )
    acl = FileAcl(
        id=uuid.uuid4(),
        org_team_id=files_org.org.org_id,
        body=body,
        body_hash=body_hash(body, org_team_id=files_org.org.org_id),
    )
    world.session.add(acl)
    await world.session.flush()
    await world.session.execute(
        text("UPDATE file_nodes SET acl_id = :acl WHERE id = :node"),
        {"acl": acl.id, "node": node_id},
    )
    await world.session.commit()
    member = await login(app_client(), files_org.member.email, files_org.member_password)
    async with world.box(uvicorn_server) as box:
        with requests_heard() as heard:
            hidden = await member.post(world.grants(node_id), json={"kind": "file"})
            nowhere = await member.post(world.grants(uuid.uuid4()), json={"kind": "file"})
            hidden_get = await member.get(world.content(node_id))
            await box.nothing_asked(0.5)
            assert drained(heard) == []
    assert (hidden.status_code, refusal(hidden)) == (nowhere.status_code, refusal(nowhere))
    assert hidden.status_code == 404
    assert "retry-after" not in hidden.headers
    assert hidden_get.status_code == 404
    await member.aclose()


def test_the_wire_names_every_outcome_a_promotion_can_come_to() -> None:
    """The ``409`` detail's ``outcome`` is typed on the wire; a promoter outcome
    the schema does not name would be a body the SDK refuses to parse."""
    from alkera_core.files.promotion import PromoteOutcome
    from backend.api.routes.files.content import PROMOTE_OUTCOME_NAMES

    assert set(PROMOTE_OUTCOME_NAMES) == {outcome.value for outcome in PromoteOutcome}
