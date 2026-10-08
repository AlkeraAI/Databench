"""What one socket may cost the CRDT lane, driven through the real
:class:`CrdtSocket` with the store faked: every hello opens a transaction that
locks the document row, so a socket repeating them is held to a burst and then
one a second, per channel, and told how long to wait."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from alkera_core.schemas.realtime import CRDT_PROTOCOL, DocEnvelope, DocFrame
from backend.services.crdt.docs import Sync
from backend.services.crdt.gateway import HELLO_BURST, HELLO_PER_SECOND, CrdtSocket
from backend.services.crdt.registry import Access, ChatWorkspaceType
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
        raise AssertionError("the socket is never closed for hellos")

    def display_name(self) -> str:
        return "Dana"


class _Docs:
    """A store that serves every hello an empty document."""

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

    async def release_peer(self, *args: Any, **kwargs: Any) -> None:
        return None


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _grant(doc_id: str, org_id: Any) -> ChannelGrant:
    return ChannelGrant(
        channel=Channel(doc_type="chat_draft", doc_id=doc_id),
        org_id=org_id,
        can_write=True,
        owner_user_id=None,
        team_id=None,
    )


def _hello(doc_id: str) -> DocEnvelope:
    return DocEnvelope(
        doc_id=doc_id,
        doc_type="chat_draft",
        epoch=0,
        peer_id="p:1",
        seq=0,
        kind="hello",
        payload={"proto": CRDT_PROTOCOL, "loro": "1", "doc_schema": 1},
    )


async def _answer(sock: CrdtSocket, host: _Host, doc_id: str) -> DocEnvelope:
    host.sent.clear()
    await sock.handle(_grant(doc_id, host.user.org_team_id), _hello(doc_id))
    frames = [f for f in host.sent if isinstance(f, DocFrame)]
    assert len(frames) == 1, frames
    return frames[0].envelope


async def test_hellos_past_the_burst_wait_for_the_bucket_to_refill() -> None:
    clock = _Clock()
    host = _Host()
    sock = CrdtSocket(host=host, docs=_Docs(), clock=clock)  # type: ignore[arg-type]
    for _ in range(HELLO_BURST):
        assert (await _answer(sock, host, "chat-a")).kind == "snapshot"

    refused = await _answer(sock, host, "chat-a")
    assert (refused.kind, refused.payload["code"]) == ("error", "crdt_busy")
    wait = refused.payload["retry_after_ms"]
    assert 0 < wait <= 1000 / HELLO_PER_SECOND

    # Another document on the same socket has a bucket of its own.
    assert (await _answer(sock, host, "chat-b")).kind == "snapshot"

    # Not yet...
    clock.now += wait / 1000 / 2
    assert (await _answer(sock, host, "chat-a")).kind == "error"
    # ...and once the wait it was told has passed, one hello goes through.
    clock.now += wait / 1000
    assert (await _answer(sock, host, "chat-a")).kind == "snapshot"
    assert (await _answer(sock, host, "chat-a")).kind == "error"


@pytest.mark.parametrize(
    ("pool_size", "slots"),
    [
        pytest.param(20, 10, id="default-pool"),
        pytest.param(7, 3, id="odd-pool-rounds-down"),
        pytest.param(1, 1, id="one-connection-still-admits-one"),
    ],
)
def test_admission_takes_half_the_pool(pool_size: int, slots: int) -> None:
    from backend.services.crdt.docs import admission_slots_for

    assert admission_slots_for(pool_size=pool_size) == slots
