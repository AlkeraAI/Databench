"""Fifty people uploading at once, over real TCP, against the real guard.

The unit cases prove the admission rule with a scripted ASGI receive; this
proves the deployment shape: a real uvicorn on a loopback port, the real
:class:`GateIngestBodyLimitMiddleware` with the shipped budget in front of a
handler that streams its part into a store the way ``put_part`` does (each wire
chunk hashed and forwarded, never held), and real sockets. What must hold:
fifty people spread over five orgs are all admitted, every part lands whole,
the process's resident set grows by a bounded amount rather than by the bytes
that crossed it, and an org that piles on cannot hold more than its share while
another org is uploading.

The store is fake on purpose -- the S3 driver's staging cost has its own
measurement -- so what is measured here is the guard, the streaming and the
sockets, with 1.2 GB of parts crossing them in a test that must itself stay
small: bodies are produced 64 KiB at a time and never assembled.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import psutil
import pytest
from alkera_core.auth import encode_cli_token
from alkera_core.config import FILES_UPLOAD_PART_RESIDENT_BYTES, settings
from backend.api.body_limit import GateIngestBodyLimitMiddleware
from blake3 import blake3
from fastapi import FastAPI, Request
from httpx import AsyncClient
from tests.conftest import serve_controlled

pytestmark = pytest.mark.asyncio

_MIB = 1 << 20
WIRE_CHUNK = 64 * 1024
PART_BYTES = 8 * _MIB
#: How often the fairness case reads the guard's live holdings. uvicorn runs on
#: this very loop, so a sample is a consistent snapshot rather than a guess.
SAMPLE_SECONDS = 0.002


def _bearer(principal: uuid.UUID, org: uuid.UUID) -> str:
    token, _ = encode_cli_token(
        user_id=principal,
        email=f"{principal.hex[:8]}@alkera.dev",
        org_team_id=org,
        platform_role=None,
    )
    return f"Bearer {token}"


async def _body(total: int) -> AsyncIterator[bytes]:
    sent = 0
    while sent < total:
        size = min(WIRE_CHUNK, total - sent)
        yield b"\x5a" * size
        sent += size


def _digest(total: int) -> str:
    hasher = blake3()
    sent = 0
    while sent < total:
        size = min(WIRE_CHUNK, total - sent)
        hasher.update(b"\x5a" * size)
        sent += size
    return hasher.hexdigest()


class _FakeStore:
    """What the store keeps of a streamed part: its length and its digest."""

    def __init__(self) -> None:
        self.parts: dict[tuple[str, int], tuple[int, str]] = {}


def _app(store: _FakeStore) -> tuple[FastAPI, GateIngestBodyLimitMiddleware]:
    app = FastAPI()

    @app.put("/api/v1/files/uploads/{session_id}/parts/{part_no}")
    async def put_part(request: Request, session_id: str, part_no: int) -> dict[str, int]:
        hasher = blake3()
        size = 0
        async for chunk in request.stream():
            hasher.update(chunk)
            size += len(chunk)
        store.parts[(session_id, part_no)] = (size, hasher.hexdigest())
        return {"partNo": part_no, "size": size}

    guard = GateIngestBodyLimitMiddleware(app, max_bytes=33 * _MIB)
    served = FastAPI()
    served.mount("/", guard)
    return served, guard


def _rss() -> int:
    """This process's resident set right now -- not the peak, so the bytes a
    drop left behind are visible as a difference rather than hidden under some
    earlier test's high-water mark."""
    return int(psutil.Process().memory_info().rss)


class _Uploader:
    """One person's browser: parts one after another per file, files in
    parallel lanes, waiting out a shed for the time the server named."""

    def __init__(
        self, client: AsyncClient, principal: uuid.UUID, org: uuid.UUID, *, retry_after_cap: float
    ):
        self.client = client
        self.headers = {"Authorization": _bearer(principal, org)}
        self.retry_after_cap = retry_after_cap
        self.sheds = 0
        self.landed = 0
        #: Set by the first part to land, and by the end of the whole drop.
        self.started = asyncio.Event()
        self.done = asyncio.Event()

    async def part(self, session: str, part_no: int, size: int) -> None:
        for _ in range(200):
            response = await self.client.put(
                f"/api/v1/files/uploads/{session}/parts/{part_no}",
                content=_body(size),
                headers={**self.headers, "Content-Length": str(size)},
            )
            if response.status_code == 200:
                self.landed += 1
                self.started.set()
                return
            assert response.status_code == 503, response.text
            self.sheds += 1
            wait = float(response.headers.get("retry-after", "1"))
            await asyncio.sleep(min(wait, self.retry_after_cap))
        raise AssertionError("a part was shed two hundred times in a row")

    async def file(self, parts: int, size: int) -> None:
        session = uuid.uuid4().hex
        for part_no in range(1, parts + 1):
            await self.part(session, part_no, size)

    async def drop(self, files: int, *, parts: int, size: int, lanes: int) -> None:
        gate = asyncio.Semaphore(lanes)

        async def one() -> None:
            async with gate:
                await self.file(parts, size)

        try:
            await asyncio.gather(*(one() for _ in range(files)))
        finally:
            self.done.set()


async def test_fifty_uploaders_over_five_orgs_stream_at_once_with_no_shed_and_bounded_memory() -> (
    None
):
    """Fifty principals in five orgs, three 8 MiB parts each, all at once over
    TCP: zero 503s, every part whole, and the process grown by a small fraction
    of the 1.2 GB that crossed it -- the bytes went through, they were not kept.

    The numbers are the shipped ones and they divide: fifty slots over five
    active orgs is ten each, which is exactly the ten people each org brought
    holding one part apiece. Under the wire-byte budget this shed the third
    person in the whole fleet.
    """
    store = _FakeStore()
    served, guard = _app(store)
    orgs = [uuid.uuid4() for _ in range(5)]
    before = _rss()
    async with serve_controlled(served) as backend:
        async with AsyncClient(base_url=f"http://{backend.addr}", timeout=120.0) as client:
            uploaders = [
                _Uploader(client, uuid.uuid4(), org, retry_after_cap=0.05)
                for org in orgs
                for _ in range(10)
            ]
            await asyncio.gather(
                *(up.drop(1, parts=3, size=PART_BYTES, lanes=1) for up in uploaders)
            )
    grown = _rss() - before
    assert sum(up.sheds for up in uploaders) == 0, [up.sheds for up in uploaders]
    assert sum(up.landed for up in uploaders) == 150
    assert len(store.parts) == 150
    expected = _digest(PART_BYTES)
    assert all(size == PART_BYTES and digest == expected for size, digest in store.parts.values())
    # Zero sheds is only worth something if the parts really were concurrent:
    # fifty uploaders that happened to queue up one at a time would also never
    # be refused, and would prove nothing about admitting fifty.
    assert guard.parts.peak >= 20, f"only {guard.parts.peak} parts were ever in flight at once"
    assert guard.parts.peak <= guard.parts.capacity
    assert guard.parts.total == 0, "a slot was not given back"
    assert guard.parts.held_by_tenant == {}
    # 1200 MiB crossed the process. The staging the guard admits at once is
    # fifty parts' worth, and a fake store stages nothing, so anything near the
    # bytes that crossed means a body was held rather than forwarded.
    assert grown < 50 * FILES_UPLOAD_PART_RESIDENT_BYTES // 4, f"RSS grew by {grown} bytes"


async def test_an_org_that_piles_on_is_held_to_its_share_while_another_uploads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eight slots, two orgs, over real sockets. One org has eight people each
    running two lanes; the other has one person sending four parts one at a
    time. The crowd is never allowed past half the budget for as long as the
    quiet org is uploading -- and it does take that half, so the ceiling is a
    share and not an idle process.

    Per person the two orgs are nine equals and the crowd would hold eight
    slots of eight; that is the starvation the org tier exists to stop, and it
    is what the sampled high-water mark below would show.
    """
    monkeypatch.setattr(
        settings, "files_upload_resident_budget_bytes", 8 * FILES_UPLOAD_PART_RESIDENT_BYTES
    )
    store = _FakeStore()
    served, guard = _app(store)
    assert guard.parts.capacity == 8
    crowd_org, quiet_org = uuid.uuid4(), uuid.uuid4()
    crowd = str(crowd_org)
    crowd_held: list[int] = []

    async def watch(until: asyncio.Event) -> None:
        while not until.is_set():
            crowd_held.append(
                next((held for key, held in guard.parts.held_by_tenant.items() if crowd in key), 0)
            )
            await asyncio.sleep(SAMPLE_SECONDS)

    async with serve_controlled(served) as backend:
        async with AsyncClient(base_url=f"http://{backend.addr}", timeout=120.0) as client:
            quiet = _Uploader(client, uuid.uuid4(), quiet_org, retry_after_cap=0.02)
            crowders = [
                _Uploader(client, uuid.uuid4(), crowd_org, retry_after_cap=0.02) for _ in range(8)
            ]
            # The quiet org is uploading BEFORE the crowd arrives, so the crowd
            # is held to a share from its very first part and the reading below
            # needs no warm-up window to be read around.
            quiet_run = asyncio.create_task(quiet.drop(1, parts=8, size=4 * _MIB, lanes=1))
            await quiet.started.wait()
            sampler = asyncio.create_task(watch(quiet.done))
            await asyncio.gather(
                *(up.drop(2, parts=4, size=4 * _MIB, lanes=2) for up in crowders),
                quiet_run,
            )
            await sampler

    assert quiet.landed == 8, "the quiet org did not finish"
    assert sum(up.landed for up in crowders) == 64
    assert crowd_held, "the crowd never ran alongside the quiet org"
    assert max(crowd_held) == guard.parts.capacity // 2, (
        f"the crowd held {max(crowd_held)} of {guard.parts.capacity} slots while one org uploaded"
    )
    assert quiet.sheds < sum(up.sheds for up in crowders), "the one waiting was the one refused"
    assert guard.parts.total == 0
