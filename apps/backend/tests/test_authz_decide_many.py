"""decide_many(): every resource decided by its policy, one row for the batch.

A listing decides every row it might show and filters; it must neither stop at
the first refusal nor file a row per row. These tests pin what a caller sees
(the decision per resource, nothing raised), what the batch leaves on record
(one ``authz.decision`` row with exact counts, committed whatever the request
does), and how the facts are found (the caller's ``attrs_for``, else the
resolver the type registered).
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.authz import (
    AUTHZ_EVENT_TYPE,
    MAX_RECORDED_REFUSALS,
    ActingContext,
    Action,
    BatchDecisionEvent,
    DecisionEvent,
    Resource,
    ResourceType,
)
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import VISIBILITY_PLATFORM
from alkera_core.models import EventOutbox
from backend.authz import decide_many, register_batch_facts
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

batch_module = importlib.import_module("backend.authz.batch")

TEAM_ID = uuid4()

#: Facts the connector policy allows a member to fetch a shared credential on.
ENTITLED: dict[str, object] = {
    "member_entitled": True,
    "enabled": True,
    "auth_mode": "shared",
    "has_shared_secret": True,
    "team_id": str(TEAM_ID),
    "owner_user_id": "",
}
#: The same facts for a connection the member is not entitled to.
NOT_ENTITLED: dict[str, object] = {**ENTITLED, "member_entitled": False}


def _request(path: str = "/api/v1/things") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"test")],
            "server": ("test", 80),
            "client": ("127.0.0.1", 12345),
        }
    )


def _ctx(org_id: UUID) -> ActingContext:
    return ActingContext.for_user(user_id=uuid4(), org_id=org_id, email="member@alkera.dev")


def _connectors(org_id: UUID, count: int) -> list[Resource]:
    return [
        Resource(ResourceType.CONNECTOR, id=f"conn-{i}", org_id=org_id, team_id=TEAM_ID)
        for i in range(count)
    ]


@dataclass
class RecordingSink:
    batches: list[BatchDecisionEvent] = field(default_factory=list)
    singles: list[DecisionEvent] = field(default_factory=list)

    async def record_allow(self, db: AsyncSession, event: DecisionEvent) -> None:
        self.singles.append(event)

    async def record_deny(self, db: AsyncSession, event: DecisionEvent) -> None:
        self.singles.append(event)

    async def record_batch(self, db: AsyncSession, event: BatchDecisionEvent) -> None:
        self.batches.append(event)


@pytest.fixture
def isolated_resolvers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registrations made by a test vanish with it."""
    monkeypatch.setattr(batch_module, "_resolvers", {})


# --------------------------------------------------------------------------- #
# what the caller gets back
# --------------------------------------------------------------------------- #


async def test_each_resource_gets_its_own_policy_decision_and_nothing_raises() -> None:
    org = uuid4()
    first, second, third = _connectors(org, 3)
    facts = {first.id: ENTITLED, second.id: NOT_ENTITLED, third.id: ENTITLED}
    sink = RecordingSink()

    decided = await decide_many(
        _request(),
        None,  # type: ignore[arg-type]  # the recording sink never touches it
        _ctx(org),
        Action.FETCH_CREDENTIAL,
        [first, second, third],
        lambda resource: facts[resource.id],
        sink=sink,
    )

    assert {rid: d.allowed for rid, d in decided.items()} == {
        "conn-0": True,
        "conn-1": False,
        "conn-2": True,
    }
    # The refusal is the policy's own: the same reason a single enforce gives.
    assert decided["conn-1"].reason == "not_entitled"


async def test_the_batch_leaves_one_summary_and_no_row_per_resource() -> None:
    org = uuid4()
    resources = _connectors(org, 5)
    refused = {"conn-1", "conn-3"}
    sink = RecordingSink()
    ctx = _ctx(org)

    await decide_many(
        _request("/api/v1/listing"),
        None,  # type: ignore[arg-type]
        ctx,
        Action.FETCH_CREDENTIAL,
        resources,
        lambda resource: NOT_ENTITLED if resource.id in refused else ENTITLED,
        sink=sink,
    )

    assert sink.singles == []
    (summary,) = sink.batches
    assert summary.outbox_payload() == {
        "batch": True,
        "effect": "partial",
        "action": "fetch_credential",
        "resource": {"type": "connector"},
        "allowed": 3,
        "refused": 2,
        "refused_ids": ["conn-1", "conn-3"],
        "method": "GET",
        "path": "/api/v1/listing",
    }
    assert summary.org_id == org
    assert summary.actor == ctx.audit_dict()


async def test_a_cross_org_resource_is_refused_in_the_batch_like_anywhere_else() -> None:
    org, other = uuid4(), uuid4()
    mine = _connectors(org, 1)[0]
    theirs = Resource(ResourceType.CONNECTOR, id="theirs", org_id=other, team_id=TEAM_ID)
    decided = await decide_many(
        _request(),
        None,  # type: ignore[arg-type]
        _ctx(org),
        Action.FETCH_CREDENTIAL,
        [mine, theirs],
        lambda _resource: ENTITLED,
        sink=RecordingSink(),
    )
    assert decided[mine.id].allowed is True
    assert decided["theirs"].allowed is False
    assert decided["theirs"].as_not_found is True


async def test_an_empty_batch_decides_and_records_nothing() -> None:
    sink = RecordingSink()
    decided = await decide_many(
        _request(),
        None,  # type: ignore[arg-type]
        _ctx(uuid4()),
        Action.READ,
        [],
        lambda _resource: {},
        sink=sink,
    )
    assert decided == {}
    assert sink.batches == []


async def test_a_batch_of_two_resource_types_is_refused_as_a_programming_error() -> None:
    org = uuid4()
    mixed = [
        _connectors(org, 1)[0],
        Resource(ResourceType.TEAM, id=str(TEAM_ID), org_id=org, team_id=TEAM_ID),
    ]
    with pytest.raises(ValueError, match="one resource type"):
        await decide_many(
            _request(),
            None,  # type: ignore[arg-type]
            _ctx(org),
            Action.READ,
            mixed,
            lambda _resource: {},
            sink=RecordingSink(),
        )


# --------------------------------------------------------------------------- #
# where the facts come from
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("isolated_resolvers")
async def test_a_registered_resolver_supplies_the_facts_for_the_whole_batch() -> None:
    org = uuid4()
    resources = _connectors(org, 3)
    asked: list[list[str]] = []

    async def resolver(
        request: Request, db: AsyncSession, ctx: ActingContext, batch: Sequence[Resource]
    ) -> Mapping[str, Mapping[str, object]]:
        asked.append([r.id for r in batch])
        # conn-2 is left out: a row the resolver cannot see is never readable.
        return {"conn-0": ENTITLED, "conn-1": NOT_ENTITLED}

    register_batch_facts(ResourceType.CONNECTOR, resolver)
    decided = await decide_many(
        _request(),
        None,  # type: ignore[arg-type]
        _ctx(org),
        Action.FETCH_CREDENTIAL,
        resources,
        sink=RecordingSink(),
    )

    assert asked == [["conn-0", "conn-1", "conn-2"]]
    assert {rid: d.allowed for rid, d in decided.items()} == {
        "conn-0": True,
        "conn-1": False,
        "conn-2": False,
    }


@pytest.mark.usefixtures("isolated_resolvers")
async def test_a_type_with_no_resolver_and_no_facts_is_a_programming_error() -> None:
    org = uuid4()
    with pytest.raises(LookupError, match="register_batch_facts"):
        await decide_many(
            _request(),
            None,  # type: ignore[arg-type]
            _ctx(org),
            Action.READ,
            _connectors(org, 1),
            sink=RecordingSink(),
        )


@pytest.mark.usefixtures("isolated_resolvers")
def test_a_type_registers_one_resolver() -> None:
    async def resolver(*_args: Any) -> Mapping[str, Mapping[str, object]]:
        return {}

    register_batch_facts(ResourceType.CONNECTOR, resolver)
    with pytest.raises(ValueError, match="already registered"):
        register_batch_facts(ResourceType.CONNECTOR, resolver)


# --------------------------------------------------------------------------- #
# the summary row
# --------------------------------------------------------------------------- #


def test_the_summary_lists_at_most_the_cap_and_counts_every_refusal() -> None:
    refused = tuple(f"id-{i}" for i in range(MAX_RECORDED_REFUSALS + 25))
    event = BatchDecisionEvent(
        org_id=uuid4(),
        actor={},
        action=Action.READ,
        resource_type="file_node",
        allowed=7,
        refused_ids=refused,
        method="GET",
        path="/p",
    )
    payload = event.outbox_payload()
    assert payload["refused"] == 125
    assert payload["allowed"] == 7
    assert payload["refused_ids"] == [f"id-{i}" for i in range(100)]


@pytest.mark.parametrize(
    ("allowed", "refused", "effect"),
    [
        pytest.param(3, (), "allow", id="nothing-refused"),
        pytest.param(0, ("a", "b"), "deny", id="everything-refused"),
        pytest.param(1, ("a",), "partial", id="some-of-each"),
    ],
)
def test_the_summary_effect_says_whether_anything_was_refused(
    allowed: int, refused: tuple[str, ...], effect: str
) -> None:
    event = BatchDecisionEvent(
        org_id=uuid4(),
        actor={},
        action=Action.READ,
        resource_type="file_node",
        allowed=allowed,
        refused_ids=refused,
        method="GET",
        path="/p",
    )
    assert event.outbox_payload()["effect"] == effect


def test_the_summary_carries_hostile_ids_and_paths_in_storable_form() -> None:
    event = BatchDecisionEvent(
        org_id=uuid4(),
        actor={},
        action=Action.READ,
        resource_type="file_node",
        allowed=0,
        refused_ids=("a\x00b",),
        method="GET",
        path="/x\x00y",
    )
    payload = event.outbox_payload()
    assert payload["refused_ids"] == ["a�b"]
    assert payload["path"] == "/x�y"


async def _decision_rows(org_id: UUID) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox).where(
                EventOutbox.org_id == org_id, EventOutbox.type == AUTHZ_EVENT_TYPE
            )
        )
        return list(rows.scalars().all())


async def test_the_summary_row_is_committed_whatever_the_request_does() -> None:
    """The real outbox: one row for the batch, filed under the platform's
    stream, and still there after the request's own transaction rolls back."""
    org = uuid4()
    resources = _connectors(org, 3)
    async with AsyncSessionLocal() as session:
        await decide_many(
            _request("/api/v1/listing"),
            session,
            _ctx(org),
            Action.FETCH_CREDENTIAL,
            resources,
            lambda resource: NOT_ENTITLED if resource.id == "conn-2" else ENTITLED,
        )
        await session.rollback()

    (row,) = await _decision_rows(org)
    assert row.visibility == VISIBILITY_PLATFORM
    assert (row.entity, row.entity_id) == ("connector", "batch")
    assert (row.payload["allowed"], row.payload["refused"], row.payload["refused_ids"]) == (
        2,
        1,
        ["conn-2"],
    )
