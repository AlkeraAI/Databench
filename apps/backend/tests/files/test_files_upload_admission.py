"""Admission of streaming upload parts: by resident memory, shared fairly.

A part PUT streams to the store, so what it costs the process is the driver's
staging buffer (``FILES_UPLOAD_PART_RESIDENT_BYTES``), never its Content-Length.
The guard used to charge the WIRE bytes of every admitted body against one
272 MiB budget, which admitted two 128 MiB parts per process for the whole
fleet and shed the third -- three people uploading at once was an outage.

Every case here drives the real middleware over a recording inner app with the
body released chunk by chunk, so a slot taken too early (at the announcement)
or too late (never) fails the test rather than the fleet.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from alkera_core.auth import encode_cli_token
from alkera_core.auth.pat_token import mint_pat_token
from alkera_core.config import FILES_UPLOAD_PART_RESIDENT_BYTES, settings
from backend.api.body_limit import GateIngestBodyLimitMiddleware

_MIB = 1 << 20
PART_PATH = "/api/v1/files/uploads/{}/parts/1"
CONTENT_PATH = "/api/v1/files/drives/d1/items/{}/content"
GATE_CAP = 33 * _MIB
#: uvicorn hands the app at most this much body per receive under flow control.
WIRE_CHUNK = 64 * 1024


#: The one org every case shares unless it is about the org tier itself.
ORG = uuid.UUID(int=1)


def _bearer(principal: uuid.UUID, org: uuid.UUID = ORG) -> bytes:
    token, _ = encode_cli_token(
        user_id=principal,
        email=f"{principal.hex[:8]}@alkera.dev",
        org_team_id=org,
        platform_role=None,
    )
    return f"Bearer {token}".encode()


class _Request:
    """One in-flight request against the middleware. The inner app reads the
    body as the test pushes it and holds its 200 until released, so a slot's
    lifetime is exactly the test's to decide."""

    def __init__(
        self,
        middleware: GateIngestBodyLimitMiddleware,
        *,
        path: str,
        declared: int,
        principal: uuid.UUID,
        org: uuid.UUID = ORG,
        bearer: bytes | None = None,
        method: str = "PUT",
    ) -> None:
        self.sent: list[dict[str, Any]] = []
        self.consumed = 0
        self.ended = False
        self.awaiting_body = asyncio.Event()
        self.progressed = asyncio.Event()
        self.release = asyncio.Event()
        self._chunks: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [
                (b"host", b"test"),
                (b"authorization", bearer or _bearer(principal, org)),
                (b"content-length", str(declared).encode()),
            ],
            "client": ("127.0.0.1", 40000),
            # How the recording inner app finds this request's events.
            "request": self,
        }
        self.task = asyncio.create_task(middleware(scope, self._receive, self._record))

    async def _receive(self) -> dict[str, Any]:
        self.awaiting_body.set()
        return await self._chunks.get()

    async def _record(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def push(self, nbytes: int, *, last: bool = False) -> None:
        """Deliver one body chunk and wait until the guard has decided on it."""
        self.progressed.clear()
        await self._chunks.put(
            {"type": "http.request", "body": b"x" * nbytes, "more_body": not last}
        )
        await asyncio.wait_for(self.progressed.wait(), 5.0)
        if last:
            self.ended = True

    async def settled(self) -> None:
        """Block until the request either cleared admission and is reading its
        body, or was answered without one."""
        waiter = asyncio.create_task(self.awaiting_body.wait())
        try:
            await asyncio.wait_for(
                asyncio.wait({waiter, self.task}, return_when=asyncio.FIRST_COMPLETED), 5.0
            )
        finally:
            waiter.cancel()

    async def finish(self) -> None:
        """Let the handler answer: end the body if it is still open, release
        the 200, and wait for the request to unwind."""
        self.release.set()
        if not self.task.done() and self.status is None:
            await self.settled()
            if not self.task.done() and self.status is None and not self.ended:
                await self.push(0, last=True)
        await asyncio.wait_for(self.task, 5.0)

    @property
    def status(self) -> int | None:
        return next(
            (int(m["status"]) for m in self.sent if m["type"] == "http.response.start"), None
        )

    def header(self, name: str) -> str | None:
        for message in self.sent:
            if message["type"] != "http.response.start":
                continue
            for key, value in message["headers"]:
                if key.decode().lower() == name.lower():
                    return value.decode()
        return None


def _middleware(clock: Callable[[], float] | None = None) -> GateIngestBodyLimitMiddleware:
    async def inner(scope: Any, receive: Any, send: Any) -> None:
        request: _Request = scope["request"]
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # The guard severed the stream; the response is already its.
                return
            request.consumed += len(message.get("body", b""))
            request.progressed.set()
            if not message.get("more_body", False):
                break
        await request.release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    kwargs: dict[str, Any] = {"max_bytes": GATE_CAP}
    if clock is not None:
        kwargs["clock"] = clock
    return GateIngestBodyLimitMiddleware(inner, **kwargs)


def _part(
    middleware: GateIngestBodyLimitMiddleware,
    principal: uuid.UUID,
    *,
    org: uuid.UUID = ORG,
    bearer: bytes | None = None,
    declared: int = settings.files_part_max_bytes,
) -> _Request:
    return _Request(
        middleware,
        path=PART_PATH.format(uuid.uuid4().hex),
        declared=declared,
        principal=principal,
        org=org,
        bearer=bearer,
    )


async def _held(
    middleware: GateIngestBodyLimitMiddleware,
    principal: uuid.UUID,
    *,
    org: uuid.UUID = ORG,
    bearer: bytes | None = None,
    declared: int | None = None,
) -> _Request:
    """A part whose first chunk has arrived and whose handler is holding it."""
    part = _part(
        middleware,
        principal,
        org=org,
        bearer=bearer,
        **({} if declared is None else {"declared": declared}),
    )
    await part.settled()
    if part.status is None:
        await part.push(WIRE_CHUNK)
    return part


def _budget(monkeypatch: pytest.MonkeyPatch, parts: int) -> None:
    monkeypatch.setattr(
        settings, "files_upload_resident_budget_bytes", parts * FILES_UPLOAD_PART_RESIDENT_BYTES
    )


# ---------------------------------------------------------------- the budget


async def test_fifty_people_can_each_stream_a_max_size_part_at_once() -> None:
    """The shipped default admits fifty 128 MiB parts from fifty principals in
    one process, and the fifty-first waits -- a 503 that names the wait and the
    share -- until one of them finishes. Under the wire-byte budget this shed
    the third."""
    middleware = _middleware()
    people = [uuid.uuid4() for _ in range(50)]
    parts = [await _held(middleware, person) for person in people]
    assert all(part.status is None for part in parts), [p.status for p in parts]

    late = await _held(middleware, uuid.uuid4())
    assert late.status == 503
    assert late.header("retry-after") == "1"
    assert late.header("x-upload-concurrency") == "1"

    await parts[0].finish()
    assert parts[0].status == 200
    assert parts[0].header("x-upload-concurrency") is not None
    after = await _held(middleware, uuid.uuid4())
    assert after.status is None, "a freed slot admits the next part"
    for part in [*parts[1:], after]:
        await part.finish()
        assert part.status == 200


async def test_three_max_size_parts_stream_their_whole_bodies_concurrently() -> None:
    """The bytes of a streaming part are forwarded, not held: three 128 MiB
    parts each push their entire body while the other two are mid-stream and
    all three land. The old budget charged the arrived bytes and shed the
    third before its first byte."""
    middleware = _middleware()
    parts = [await _held(middleware, uuid.uuid4()) for _ in range(3)]
    for _ in range(0, settings.files_part_max_bytes - WIRE_CHUNK, 8 * _MIB):
        for part in parts:
            await part.push(8 * _MIB)
    for part in parts:
        await part.push(0, last=True)
        await part.finish()
    assert [part.status for part in parts] == [200, 200, 200]


async def test_a_part_that_only_announces_itself_holds_no_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sixty announced max-size parts that send nothing occupy nothing: the
    slot is taken by the first byte that arrives, so a stalled socket -- an
    anonymous one included, whose auth refuses before the body is read -- can
    shed nobody."""
    _budget(monkeypatch, 4)
    middleware = _middleware()
    stalled = [_part(middleware, uuid.uuid4()) for _ in range(60)]
    for part in stalled:
        await part.settled()
    assert all(part.status is None for part in stalled)

    real = await _held(middleware, uuid.uuid4())
    assert real.status is None, "an announced-and-silent part reserved a slot"
    await real.finish()
    assert real.status == 200
    for part in stalled:
        await part.finish()


async def test_a_part_whose_first_byte_finds_the_slots_gone_is_shed_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admission reserves nothing, so two parts can clear it for one slot; the
    slot goes to the first byte that arrives and the other part is shed at
    its own first byte -- the same 503, its bytes never reaching the handler,
    and the handler's answer to the severed stream never reaching the wire."""
    _budget(monkeypatch, 1)
    middleware = _middleware()
    first, second = _part(middleware, uuid.uuid4()), _part(middleware, uuid.uuid4())
    await first.settled()
    await second.settled()
    assert first.status is None and second.status is None, "both cleared admission"
    await second.push(WIRE_CHUNK)
    await first._chunks.put({"type": "http.request", "body": b"x" * WIRE_CHUNK, "more_body": True})
    await asyncio.wait_for(first.task, 5.0)
    assert first.status == 503
    assert first.header("retry-after") == "1"
    assert first.consumed == 0, "the shed part's bytes reached the handler"
    assert [m["status"] for m in first.sent if m["type"] == "http.response.start"] == [503]
    await second.finish()
    assert second.status == 200


async def test_a_small_part_is_admitted_whatever_the_budget_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A part at or under the exempt ceiling is the last part of a file or a
    small file entire; it holds at most its own megabyte and is never counted
    or shed, the same as every other small body."""
    _budget(monkeypatch, 1)
    middleware = _middleware()
    holder = await _held(middleware, uuid.uuid4())
    small = await _held(middleware, uuid.uuid4(), declared=_MIB)
    assert small.status is None
    await small.push(0, last=True)
    await small.finish()
    assert small.status == 200
    await holder.finish()


async def test_the_budget_follows_the_setting_a_deployment_pins() -> None:
    """Capacity is the budget over the measured per-part cost, and the
    shipped default is the fifty concurrent parts the product asked for."""
    assert settings.files_upload_resident_budget_bytes // FILES_UPLOAD_PART_RESIDENT_BYTES == 50
    assert _middleware().parts.capacity == 50


# -------------------------------------------------------------- fairness


async def test_a_greedy_principal_is_held_to_its_share_while_another_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eight slots, one uploader holding all eight. The moment a second person
    asks, the share is four each: the newcomer is refused only until the
    greedy one's next part is -- its slots then drain to the newcomer, who
    gets four in a row and is refused the fifth like anyone over share."""
    _budget(monkeypatch, 8)
    middleware = _middleware()
    greedy, patient = uuid.uuid4(), uuid.uuid4()
    held = [await _held(middleware, greedy) for _ in range(8)]
    assert all(part.status is None for part in held)

    first_ask = await _held(middleware, patient)
    assert first_ask.status == 503
    assert first_ask.header("x-upload-concurrency") == "4"

    ninth = await _held(middleware, greedy)
    assert ninth.status == 503, "over share while another waits"
    assert ninth.header("x-upload-concurrency") == "4"

    for part in held[:4]:
        await part.finish()
    admitted = [await _held(middleware, patient) for _ in range(4)]
    assert [part.status for part in admitted] == [None] * 4
    fifth = await _held(middleware, patient)
    assert fifth.status == 503

    tenth = await _held(middleware, greedy)
    assert tenth.status == 503, "still at its share of four"
    for part in [*held[4:], *admitted]:
        await part.finish()
        assert part.status == 200


async def test_the_share_relaxes_once_the_others_go_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """A principal alone has the whole budget; one who asked and left stops
    counting after the idle window, so a lone uploader is never held to a
    share nobody else is using."""
    _budget(monkeypatch, 8)
    now = [1_000.0]
    middleware = _middleware(clock=lambda: now[0])
    alone, passerby = uuid.uuid4(), uuid.uuid4()
    held = [await _held(middleware, alone) for _ in range(4)]
    asked = await _held(middleware, passerby)
    assert asked.status is None
    await asked.finish()
    fifth = await _held(middleware, alone)
    assert fifth.status == 503, "two active principals: four each"

    now[0] += middleware.parts.idle_seconds + 1
    relaxed = [await _held(middleware, alone) for _ in range(4)]
    assert [part.status for part in relaxed] == [None] * 4
    for part in [*held, *relaxed]:
        await part.finish()


async def test_every_active_principal_keeps_at_least_one_slot_of_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More people than slots: the share floors at one, so a person is never
    told their share is zero -- they wait for a slot, not for a share."""
    _budget(monkeypatch, 2)
    middleware = _middleware()
    people = [uuid.uuid4() for _ in range(5)]
    parts = [await _held(middleware, person) for person in people]
    assert [part.status for part in parts] == [None, None, 503, 503, 503]
    assert {part.header("x-upload-concurrency") for part in parts[2:]} == {"1"}
    for part in parts[:2]:
        await part.finish()


async def test_a_crowded_org_cannot_take_the_slots_from_a_quiet_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eight slots. One org has eight people streaming a part each; another has
    one person who wants to upload. Per PERSON they are nine equals and the
    crowd wins every freed slot eight times out of nine -- which is one customer
    starving another. The org tier is what stops it: the crowd is held to half
    the budget however many people it brings, and the four slots it gives back
    go to the org that is waiting, not to the ninth colleague."""
    _budget(monkeypatch, 8)
    middleware = _middleware()
    crowd_org, quiet_org = uuid.uuid4(), uuid.uuid4()
    crowd = [uuid.uuid4() for _ in range(8)]
    quiet = uuid.uuid4()

    held = [await _held(middleware, person, org=crowd_org) for person in crowd]
    assert all(part.status is None for part in held), "an org alone fills the budget"

    first_ask = await _held(middleware, quiet, org=quiet_org)
    assert first_ask.status == 503, "every slot is taken"
    # Half the budget, not a ninth of it: the share is the org's first.
    assert first_ask.header("x-upload-concurrency") == "4"

    for part in held[:4]:
        await part.finish()
        assert part.status == 200
    # The crowd is at its share of four, so neither the people already holding
    # nor a colleague who has held nothing may take a freed slot.
    again = await _held(middleware, crowd[0], org=crowd_org)
    assert again.status == 503, "the crowd took back the slots it gave up"
    colleague = await _held(middleware, uuid.uuid4(), org=crowd_org)
    assert colleague.status == 503, "another person is not another budget"

    admitted = [await _held(middleware, quiet, org=quiet_org) for _ in range(4)]
    assert [part.status for part in admitted] == [None] * 4, "the freed half went unused"
    fifth = await _held(middleware, quiet, org=quiet_org)
    assert fifth.status == 503, "one person alone in its org is still held to the org's share"
    for part in [*held[4:], *admitted]:
        await part.finish()
        assert part.status == 200


async def test_a_credential_that_names_no_org_is_a_tenant_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A personal access token names no org this early -- nothing has read it
    yet -- so each one is its own tenant rather than all of them pooled into a
    single "no org" bucket. Six slots and three tenants: a person in their org,
    and two robots on two tokens, two slots each. Pooled, the robots would
    divide ONE tenant's share and neither could hold a second part -- two
    unrelated machines starving each other for being credentialed alike."""
    _budget(monkeypatch, 6)
    middleware = _middleware()
    person = await _held(middleware, uuid.uuid4(), org=uuid.uuid4())
    assert person.status is None
    tokens = [f"Bearer {mint_pat_token()[0]}".encode() for _ in range(2)]

    first = [await _held(middleware, uuid.uuid4(), bearer=token) for token in tokens]
    assert [part.status for part in first] == [None, None]
    second = [await _held(middleware, uuid.uuid4(), bearer=token) for token in tokens]
    assert [part.status for part in second] == [None, None], "the robots shared one share"
    for part in [person, *first, *second]:
        await part.finish()
        assert part.status == 200


# ------------------------------------------- the materialised budget still holds


async def test_materialised_bodies_keep_their_own_budget_apart_from_the_parts() -> None:
    """A JSON or content body FastAPI buffers is still charged by the bytes
    that arrive, against a budget of its own -- twice the largest materialised
    cap -- and the parts streaming alongside it spend none of it."""
    middleware = _middleware()
    assert middleware.max_inflight_bytes == 2 * 64 * _MIB
    parts = [await _held(middleware, uuid.uuid4()) for _ in range(3)]
    for part in parts:
        await part.push(32 * _MIB)

    def content(principal: uuid.UUID) -> _Request:
        return _Request(
            middleware,
            path=CONTENT_PATH.format(uuid.uuid4().hex),
            declared=64 * _MIB,
            principal=principal,
        )

    first, second = content(uuid.uuid4()), content(uuid.uuid4())
    for body in (first, second):
        await body.settled()
        await body.push(64 * _MIB, last=True)
    third = content(uuid.uuid4())
    await third.settled()
    assert third.status == 503, "the third 64 MiB materialised body is over budget"
    for body in (first, second):
        await body.finish()
        assert body.status == 200
    for part in parts:
        await part.push(0, last=True)
        await part.finish()
        assert part.status == 200
