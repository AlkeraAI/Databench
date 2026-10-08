"""A real backend for the CLI's Files tests.

The push is a *wire* client: its dedup fast path reads a version's
``contentHash`` off the server, its resume reads ``acceptedParts`` off a real
upload session, and its symlink and attrs calls carry headers the routes
enforce. None of that is provable against a mock, so these tests drive the
FastAPI app on a real socket, against the lane database, with the production
filesystem store rooted under ``tmp_path``.

The counter wrapped around the app is deliberately a *request* counter and not
a spy on the push: a second push of an unchanged tree must send no upload
request at all, and the only place that is observable is the server.

Two servers, not one. User bytes are served by a separate application whose
``HostGate`` refuses any Host that is not the content origin, and one origin
cannot be both the API and the content domain, so a
harness with a single port mints content URLs nothing can redeem. The content
app therefore gets its own ephemeral port here, exactly as the deployment gives
it its own hostname, and ``files_content_base_url`` names it so the URLs the API
mints resolve. Both servers share one event loop because the database pool is
loop-bound and a connection made on one loop is refused on another.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import uvicorn
from alkera_cli.cloud.folder import wire_path
from alkera_core.auth import encode_cli_token, register_token
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal, engine
from alkera_core.files.clock import SystemClock
from alkera_core.files.store.scoped import FilesystemScoped
from alkera_core.models import Team, TeamMembership, TeamRole, TokenType, User
from backend.content_app import CONTENT_MOUNT_PATH, build_content_app
from backend.services.files.store import set_store_factory
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from tests.files._boxes import credential_box, registered_box

if TYPE_CHECKING:
    from alkera_sdk import AlkeraClient


def home_path(api: AlkeraClient, name: str) -> str:
    """``home/<me>/<name>`` — a drive path the caller may actually write.

    The drive root, ``home/`` and ``Teams/`` are signposts: they answer a
    create under them with ``files.container_readonly``, so a test that seeds
    its folder directly at the root is refused before it has anything to work
    with. A person's own home takes the write, and is where their folders live
    anyway.
    """
    drive = api.files.drive()
    mine = wire_path(api.files.item(str(drive["id"]), str(drive["homeId"])))
    assert mine, "the drive read names no home for the caller"
    return f"{mine}/{name}"


@dataclass
class RequestLog:
    """Every request the server saw, as ``(method, path)`` pairs."""

    seen: list[tuple[str, str]] = field(default_factory=list)

    def count(self, *fragments: str) -> int:
        """How many requests carried any of these path fragments."""
        return sum(
            1 for _, target in self.seen if any(fragment in target for fragment in fragments)
        )

    def clear(self) -> None:
        self.seen.clear()


@dataclass
class LiveBackend:
    base_url: str
    content_base_url: str
    token: str
    log: RequestLog
    #: The credential a provisioned box speaks with, and the machine it asserts
    #: — present only when the harness was asked for one. The pair is what the
    #: drive checks an agent assertion against: a header naming a machine id is
    #: public and proves nothing, so a box built without this holds no lease and
    #: reads nothing out of a sealed folder. Asked for a ``machine`` box, the
    #: token is the box's own ``alk_machine_…`` credential — no user session
    #: behind it, a member of no org — and the machine is the one it holds.
    box_token: str | None = None
    box_machine: str | None = None


class _Counting:
    """An ASGI wrapper that records the method and path of every request."""

    def __init__(self, app: object, log: RequestLog) -> None:
        self._app = app
        self._log = log

    async def __call__(self, scope: dict, receive: object, send: object) -> None:  # type: ignore[type-arg]
        if scope.get("type") == "http":
            self._log.seen.append((str(scope.get("method")), str(scope.get("path"))))
        await self._app(scope, receive, send)  # type: ignore[operator]


def _fresh_pool() -> None:
    """Drop every pooled connection this loop cannot close.

    Each of these tests runs the server on its own event loop, and asyncpg
    refuses a connection created on a loop that has since closed. ``dispose()``
    would try to close them on the dead loop, so the pool is *replaced*
    instead. The sockets it drops are NOT collected with the loop they came
    from — asyncpg's abort defers the ``close()`` onto that loop, which never
    runs it again — so this leaks a live Postgres backend for every connection
    still pooled. Everything opened inside one of our own loops is therefore
    closed by ``_dispose_in_loop`` before that loop ends, and this is left only
    as the guard against connections some earlier test parked in the pool.
    """
    engine.sync_engine.pool = engine.sync_engine.pool.recreate()


async def _dispose_in_loop() -> None:
    """Close this loop's pooled connections while the loop is still running.

    The pool outlives the loop, and a connection can only be closed from the
    loop that created it. Dropping the pool afterwards would leak the backend
    on the server; the shared Postgres has a finite ``max_connections``, and
    once it is exhausted every request the in-test backend serves — the drives
    route first — fails with a 500.
    """
    await engine.dispose()


@dataclass(frozen=True)
class _Seeded:
    """The credentials one seeded org hands out."""

    token: str
    box_token: str | None
    box_machine: str | None
    org_id: uuid.UUID | None = None


async def _seed_user(*, box: bool, machine: bool) -> _Seeded:
    """One org, one member, one CLI token — the credential the SDK sends."""
    try:
        return await _seed_user_inner(box=box, machine=machine)
    finally:
        await _dispose_in_loop()


async def _seed_user_inner(*, box: bool, machine: bool) -> _Seeded:
    async with AsyncSessionLocal() as session:
        team = Team(name=f"files-push-{secrets.token_hex(4)}", is_root=True)
        session.add(team)
        await session.flush()
        user = User(
            home_org_team_id=team.id,
            email=f"push-{secrets.token_hex(6)}@alkera.dev",
            first_name="Push",
            last_name="User",
            email_verified_at=datetime.now(UTC),
        )
        session.add(user)
        await session.flush()
        # Membership, not just ``org_team_id``: the role resolver reads the
        # membership chain, and a user with none has no role on the drive root,
        # so every write would come back as the policy's 404.
        session.add(TeamMembership(user_id=user.id, team_id=team.id, role=TeamRole.ADMIN))
        await session.flush()
        token, claims = encode_cli_token(
            user_id=user.id,
            email=user.email,
            org_team_id=team.id,
            platform_role=None,
        )
        await register_token(session, claims=claims, token_type=TokenType.CLI)
        await session.commit()
        if machine:
            # A platform box on its own machine credential, dedicated to the
            # org: no user session behind its requests at all.
            box_token, box_machine = await credential_box(session, org_id=team.id, user_id=user.id)
            return _Seeded(
                token=token, box_token=box_token, box_machine=box_machine, org_id=team.id
            )
        if not box:
            return _Seeded(token=token, box_token=None, box_machine=None, org_id=team.id)
        # A second credential, with a live workspace machine registered on it:
        # that registration, not the header, is what makes a request the box.
        # Same seam the backend's lease-route suites build a box with.
        box_token, box_machine = await registered_box(
            session, user_id=user.id, email=user.email, org_id=team.id
        )
    return _Seeded(token=token, box_token=box_token, box_machine=box_machine, org_id=team.id)


def _first_answer(url: str, token: str) -> httpx.Response:
    """The first request this harness makes of the server it just started.

    ``uvicorn.Server.started`` means the socket is bound, not that the app can
    answer: the first request through it pays the import, the ORM mapper
    configuration and the first pool checkout, and on a machine running other
    suites that can outlast httpx's five-second default. A single attempt at
    that default made the runner's load decide whether the fixture came up at
    all, and the case it tore down had nothing to do with what it proved.

    Asked until the server answers instead, so what fails is a server that
    never answers rather than one that was slow once. The deadline is the
    ceiling on the former; a healthy server crosses it on the first try.
    """
    deadline = time.monotonic() + 120.0
    while True:
        try:
            response = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30.0)
            response.raise_for_status()
            return response
        except (httpx.TransportError, httpx.HTTPStatusError):
            if time.monotonic() >= deadline:
                raise
            threading.Event().wait(0.25)


def _open_the_quota(base_url: str, token: str) -> None:
    """Give the org room to store something.

    A drive is created on first touch with ``quota_bytes = 0``, which every
    upload then refuses with a 507. Production fills the quota from the org's
    plan; a test has no plan, so the row is raised directly over the sync DSN —
    a separate connection, so it cannot disturb the server's loop-bound pool.
    """
    response = _first_answer(f"{base_url}/api/v1/files/drives", token)
    sync = create_engine(settings.database_url_sync)
    try:
        with sync.begin() as connection:
            connection.execute(
                text(
                    "UPDATE file_drives SET quota_bytes = :room, quota_nodes = :nodes "
                    "WHERE id = :id"
                ),
                {"room": 1 << 34, "nodes": 100_000, "id": response.json()["id"]},
            )
    finally:
        sync.dispose()


def _content_origin(log: RequestLog) -> FastAPI:
    """The content application on its own origin, mounted where the API mounts it.

    A separate application rather than a second copy of the backend: the API's
    own middleware refuses a ``/c/`` path under any host but the content one,
    and running the whole backend twice would start its lifespan twice. The
    mount path is imported, not spelled, because the minted URL carries it.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount(CONTENT_MOUNT_PATH, build_content_app())
    return app


async def _serve(servers: tuple[uvicorn.Server, ...]) -> None:
    """Both servers on one loop, so they share the database pool."""
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        await _dispose_in_loop()


def _bound_port(server: uvicorn.Server) -> int:
    port: int = server.servers[0].sockets[0].getsockname()[1]
    return port


def _server(app: object) -> uvicorn.Server:
    return uvicorn.Server(
        uvicorn.Config(
            app,  # type: ignore[arg-type]
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="on",
        )
    )


@contextlib.contextmanager
def live_backend(
    tmp_path: Path, *, box: bool = False, machine: bool = False
) -> Iterator[LiveBackend]:
    """The backend and the content origin on two ephemeral ports, in one thread.

    A thread rather than a task because the push under test is synchronous:
    it blocks the calling thread on ``httpx``, which would deadlock an event
    loop shared with the server.

    ``box`` additionally provisions a machine on its own credential and reports
    the pair on the result, for a test whose caller is a box rather than the
    person who owns the drive. Off by default because the registration is two
    extra commits and most of these suites are a person pushing their own
    files — and it is seeded HERE, on the loop that seeds the user, because the
    connection pool is loop-bound and the servers own the loop from the next
    line on. ``machine`` provisions the platform's kind of box instead: one on
    its own ``alk_machine_…`` credential with no user session behind it.
    """
    store_root = tmp_path / "files-store"
    store_root.mkdir(parents=True, exist_ok=True)

    previous = (
        settings.files_enabled,
        settings.files_store_provider,
        settings.files_store_root,
        settings.files_inline_operations,
        settings.files_content_base_url,
        settings.files_lease_ttl_seconds,
    )
    settings.files_enabled = True
    settings.files_store_provider = "filesystem"
    settings.files_store_root = store_root
    # No Temporal worker runs beside this backend, so a queued promote would
    # never commit and every pushed file would stay invisible; the request that
    # queues it runs the same core inline instead.
    settings.files_inline_operations = True
    # These tests drive the mount library directly, so nothing beats the lease:
    # the whole take-push-release story runs inside ONE grant. At the shipped
    # 60s that makes the runner's speed the gate — a loaded machine spends
    # longer than a minute on the real uploads a case does, the grant lapses,
    # and the release is fenced as a supersession that never happened. The
    # window is widened past any run rather than raced: what a case proves is
    # what the wire answered, and a lapse would be the harness deciding it.
    settings.files_lease_ttl_seconds = 3600
    set_store_factory(FilesystemScoped(store_root, clock=SystemClock()))

    from backend.app_factory import process_app

    backend_app = process_app()

    _fresh_pool()
    seeded = asyncio.run(_seed_user(box=box, machine=machine))
    token = seeded.token
    _fresh_pool()
    log = RequestLog()
    api = _server(_Counting(backend_app, log))
    content = _server(_Counting(_content_origin(log), log))
    thread = threading.Thread(target=lambda: asyncio.run(_serve((api, content))), daemon=True)
    thread.start()
    try:
        for _ in range(1000):
            if api.started and content.started:
                break
            threading.Event().wait(0.02)
        else:  # pragma: no cover - a backend that never binds fails every test
            raise RuntimeError("the backend did not start")
        base_url = f"http://127.0.0.1:{_bound_port(api)}"
        # Set before the first request: the API mints absolute content URLs
        # from this value, and the gate on the content app compares the Host
        # against it, so a request minted before it is set is unredeemable.
        content_base_url = f"http://127.0.0.1:{_bound_port(content)}"
        settings.files_content_base_url = content_base_url
        _open_the_quota(base_url, token)
        log.clear()
        yield LiveBackend(
            base_url=base_url,
            content_base_url=content_base_url,
            token=token,
            log=log,
            box_token=seeded.box_token,
            box_machine=seeded.box_machine,
        )
    finally:
        api.should_exit = True
        content.should_exit = True
        thread.join(timeout=10)
        _fresh_pool()
        set_store_factory(None)
        (
            settings.files_enabled,
            settings.files_store_provider,
            settings.files_store_root,
            settings.files_inline_operations,
            settings.files_content_base_url,
            settings.files_lease_ttl_seconds,
        ) = previous
