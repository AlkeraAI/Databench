"""A socket that keeps feeding the validator updates that crash it is closed.

One update that kills a worker is a refusal the sender sees; three inside ten
minutes is somebody probing for the crash, and the socket goes with ``4429``.
A timeout is never a strike: it says the host was slow, not the sender.
Driven through the real :class:`CrdtSocket` with the store's verdict faked —
the store's own crash handling is covered in ``test_crdt_docs``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from alkera_core.schemas.realtime import CRDT_PROTOCOL, DocEnvelope, encode_b64
from backend.services.crdt.docs import CrdtError, Sync
from backend.services.crdt.gateway import (
    MAX_VALIDATOR_CRASHES,
    VALIDATOR_CRASH_WINDOW_SECONDS,
    CrdtSocket,
)
from backend.services.crdt.registry import Access, ChatWorkspaceType
from backend.services.realtime import close_codes
from backend.services.realtime.channels import Channel, ChannelGrant
from backend.services.realtime.filters import EntitlementSnapshot
from backend.services.sharing.node_role import SharedRungCache

pytestmark = pytest.mark.asyncio


@dataclass
class _User:
    id: Any = field(default_factory=uuid4)
    org_team_id: Any = field(default_factory=uuid4)
    email: str = "dana@acme.test"


@dataclass
class _Host:
    peer_id: str = "p:1"
    user: Any = field(default_factory=_User)
    agent_id: str | None = None
    machine_id: str | None = None
    rungs: SharedRungCache = field(default_factory=SharedRungCache)
    sent: list[Any] = field(default_factory=list)
    closed: tuple[int, str] | None = None

    @property
    def org_id(self) -> Any:
        return self.user.org_team_id

    @property
    def ent(self) -> EntitlementSnapshot:
        return EntitlementSnapshot(
            org_id=self.org_id, team_ids=frozenset(), org_admin=False, platform=False
        )

    @property
    def actor(self) -> dict[str, Any]:
        return {}

    async def send(self, frame: Any) -> None:
        self.sent.append(frame)

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)

    def display_name(self) -> str:
        return "Dana"


class _Docs:
    """The store, answering every update with a validator crash."""

    def type_of(self, ref: Any) -> ChatWorkspaceType:
        return ChatWorkspaceType()

    @contextlib.asynccontextmanager
    async def admitted(self) -> AsyncIterator[None]:
        yield

    async def access(self, *args: Any, **kwargs: Any) -> Access:
        return Access(can_read=True, can_write=True)

    async def claim_peer(self, *args: Any, **kwargs: Any) -> int:
        return 5000

    async def peers_of(self, *args: Any, **kwargs: Any) -> frozenset[int]:
        return frozenset({5000})

    async def sync(self, *args: Any, **kwargs: Any) -> Sync:
        return Sync(mode="snapshot", data=b"", vv=b"", epoch=1, doc_schema=1)

    failure = CrdtError("crdt_rejected", "crashed", reason="validator_crash")

    async def apply(self, *args: Any, **kwargs: Any) -> Any:
        raise self.failure

    async def release_peer(self, *args: Any, **kwargs: Any) -> None:
        return None


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _envelope(kind: str, payload: dict[str, Any], epoch: int) -> DocEnvelope:
    return DocEnvelope(
        doc_id="sess-1",
        doc_type="chat_draft",
        epoch=epoch,
        peer_id="p:1",
        seq=0,
        kind=kind,
        payload=payload,
    )


async def _socket(
    clock: _Clock, failure: CrdtError | None = None
) -> tuple[CrdtSocket, _Host, ChannelGrant]:
    host = _Host()
    docs = _Docs()
    if failure is not None:
        docs.failure = failure
    sock = CrdtSocket(host=host, docs=docs, clock=clock)  # type: ignore[arg-type]
    channel = Channel(doc_type="chat_draft", doc_id="sess-1")
    grant = ChannelGrant(
        channel=channel,
        org_id=host.user.org_team_id,
        can_write=True,
        owner_user_id=None,
        team_id=None,
    )
    await sock.handle(
        grant, _envelope("hello", {"proto": CRDT_PROTOCOL, "loro": "1", "doc_schema": 1}, 0)
    )
    return sock, host, grant


async def _crash(sock: CrdtSocket, grant: ChannelGrant) -> None:
    await sock.handle(
        grant, _envelope("crdt", {"t": "update", "update_id": "u", "data_b64": encode_b64(b"x")}, 1)
    )


async def test_the_third_crash_in_the_window_closes_the_socket() -> None:
    """An update that kills the worker on a fresh worker too costs the
    sender a strike; three in the window close the socket."""
    clock = _Clock()
    sock, host, grant = await _socket(clock)
    for _ in range(MAX_VALIDATOR_CRASHES - 1):
        await _crash(sock, grant)
        clock.now += 1
    assert host.closed is None
    errors = [f.envelope.payload for f in host.sent if f.envelope.kind == "error"]
    assert [e["reason"] for e in errors] == ["validator_crash"] * (MAX_VALIDATOR_CRASHES - 1)
    await _crash(sock, grant)
    assert host.closed is not None and host.closed[0] == close_codes.TOO_MANY


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            CrdtError("crdt_busy", "slow", reason="validator_timeout", retry_after_ms=1000),
            id="the-validator-timed-out",
        ),
        pytest.param(
            CrdtError("crdt_busy", "slow", reason="load_timeout", retry_after_ms=1000),
            id="loading-timed-out",
        ),
        pytest.param(
            CrdtError("crdt_rejected", "rules", reason="container"),
            id="a-rule-refusal",
        ),
    ],
)
async def test_a_timeout_or_a_refusal_is_never_a_strike(failure: CrdtError) -> None:
    """A timeout says the host was slow: the sender is told to wait and send
    again, and however often it happens the socket stays open. A first SQL
    cell on a cold worker timed out three times and closed the socket."""
    clock = _Clock()
    sock, host, grant = await _socket(clock, failure)
    for _ in range(MAX_VALIDATOR_CRASHES * 3):
        await _crash(sock, grant)
        clock.now += 1
    assert host.closed is None
    errors = [f.envelope.payload for f in host.sent if f.envelope.kind == "error"]
    assert [e["code"] for e in errors] == [failure.code] * (MAX_VALIDATOR_CRASHES * 3)


async def test_crashes_spread_beyond_the_window_never_close_it() -> None:
    clock = _Clock()
    sock, host, grant = await _socket(clock)
    for _ in range(MAX_VALIDATOR_CRASHES * 3):
        await _crash(sock, grant)
        clock.now += VALIDATOR_CRASH_WINDOW_SECONDS / (MAX_VALIDATOR_CRASHES - 1) + 1
    assert host.closed is None
