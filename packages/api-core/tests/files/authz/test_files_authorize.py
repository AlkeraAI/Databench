"""``authorize`` is the only way to get a node you may act on.

Every case here runs against real rows on the lane database and the real
engine: the node, its drive, its chain and its ``file_shares`` are seeded
through the factory, and the ``enforce`` that is injected is
:func:`alkera_core.authz.authorize` itself, so an assertion about a refusal is
an assertion about the registered ``files.access`` policy rather than about a
stub that was told what to say.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from alkera_core.authz import authorize as engine_authorize
from alkera_core.authz.decision import NEVER_AUDITED_KEYS, Decision, allow, audited_attrs
from alkera_core.authz.engine import policy_for
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource
from alkera_core.files.acl_intern import ace_body, body_hash
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import Authorized, Denied, authorize
from alkera_core.files.authz.decider import AccessFacts
from alkera_core.files.authz.grants import Grant, GrantOrigin, Principal
from alkera_core.files.errors import NotFound
from alkera_core.files.ids import NodeId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.acl import FileAcl
from alkera_core.models.files.tree import FileNode
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio


def _ctx(org: FilesOrg, user_id: uuid.UUID) -> ActingContext:
    return ActingContext.for_user(user_id=user_id, org_id=org.org_team_id, email="m@files.test")


class RecordingEnforce:
    """The engine, with the attrs it decided on kept for inspection.

    It records rather than answers: every decision below is the real policy's.
    """

    def __init__(self) -> None:
        self.attrs: list[Mapping[str, object]] = []
        self.resources: list[Resource] = []
        #: What the sink was handed to record. The backend's sink turns each of
        #: these into one ``authz.decision`` outbox row, so a class that
        #: produced none here produces no row there either.
        self.decisions: list[Decision] = []

    def __call__(
        self,
        ctx: ActingContext,
        action: Action,
        resource: Resource,
        attrs: Mapping[str, object],
    ) -> Decision:
        self.attrs.append(dict(attrs))
        self.resources.append(resource)
        decision = engine_authorize(ctx, action, resource, attrs)
        self.decisions.append(decision)
        return decision


async def _run(
    repo: FilesRepo,
    ctx: ActingContext,
    node_id: NodeId,
    action: FilesAction,
    enforce: RecordingEnforce,
    facts: AccessFacts | None = None,
) -> Authorized[Any]:
    """``authorize`` where a route would call it: inside the repo transaction
    that sets the app role and the org, so RLS is in force for the load."""
    async with repo.transaction():
        return await authorize(
            ctx,
            repo,
            node_id,
            action,
            facts=facts if facts is not None else AccessFacts(),
            enforce=enforce,
        )


async def _seed_for(
    files_factory: FilesFactory, principal_id: uuid.UUID | None, role: str | None
) -> FileNode:
    drive = await files_factory.drive()
    tree = await files_factory.tree("papers/ papers/report.txt", drive=drive)
    node = tree["papers/report.txt"]
    if role is not None and principal_id is not None:
        await files_factory.grant(node, "user", principal_id, role)
    return node


async def _seed(files_factory: FilesFactory, org: FilesOrg, role: str | None) -> FileNode:
    return await _seed_for(files_factory, org.member_id, role)


@pytest.mark.parametrize(
    ("role", "action"),
    [
        pytest.param("reader", FilesAction.READ, id="reader-read"),
        pytest.param("reader", FilesAction.EXPORT, id="reader-export"),
        pytest.param("writer", FilesAction.WRITE, id="writer-write"),
        pytest.param("owner", FilesAction.DELETE, id="owner-delete"),
    ],
)
async def test_an_allowed_action_returns_a_handle_on_the_node(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    role: str,
    action: FilesAction,
) -> None:
    node = await _seed(files_factory, files_org, role)
    handle = await _run(
        repo,
        _ctx(files_org, files_org.member_id),
        NodeId(node.id),
        action,
        RecordingEnforce(),
    )
    assert isinstance(handle, Authorized)
    assert handle.node.id == node.id
    assert handle.action is action
    assert handle.access.allows(action)
    # The chain the caller needs next comes back with the handle rather than
    # being re-read: root, folder, node.
    assert [n.id for n in handle.chain][-1] == node.id
    assert len(handle.chain) == 3


@pytest.mark.parametrize(
    "action",
    [FilesAction.WRITE, FilesAction.DELETE, FilesAction.SHARE],
    ids=lambda a: a.value,
)
async def test_a_reader_asking_to_mutate_gets_a_visible_403(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, action: FilesAction
) -> None:
    """They can read the node, so the refusal names itself."""
    node = await _seed(files_factory, files_org, "reader")
    with pytest.raises(Denied) as raised:
        await _run(
            repo, _ctx(files_org, files_org.member_id), NodeId(node.id), action, RecordingEnforce()
        )
    assert raised.value.code == "files.forbidden"
    assert raised.value.status == 403


async def test_a_stranger_in_the_org_is_told_the_node_does_not_exist(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """No grant means no read, and a 403 would confirm the node is there."""
    node = await _seed(files_factory, files_org, None)
    with pytest.raises(NotFound) as raised:
        await _run(
            repo,
            _ctx(files_org, files_org.member_id),
            NodeId(node.id),
            FilesAction.READ,
            RecordingEnforce(),
        )
    assert raised.value.code == "files.not_found"


async def test_a_node_in_another_org_is_indistinguishable_from_one_that_never_existed(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_org_factory: Any,
    files_session: Any,
) -> None:
    """The whole point of the repo's scope: the two refusals are the same
    exception with the same code and the same text, so an attacker cannot use
    the difference to enumerate another tenant's ids."""
    other = await files_org_factory()
    other_factory = FilesFactory(files_session, other)
    # A real, fully granted node over there — someone's own drive, not an empty
    # one — so what refuses the read here is the scope and nothing else. (A
    # grant naming *this* org's member cannot even be written: the in-org share
    # constraint refuses it at the database.)
    foreign = await _seed_for(other_factory, other.member_id, "owner")
    foreign_id = NodeId(foreign.id)

    ctx = _ctx(files_org, files_org.member_id)
    with pytest.raises(NotFound) as cross_org:
        await _run(repo, ctx, foreign_id, FilesAction.READ, RecordingEnforce())
    with pytest.raises(NotFound) as nonexistent:
        await _run(repo, ctx, NodeId(uuid.uuid4()), FilesAction.READ, RecordingEnforce())
    assert str(cross_org.value) == str(nonexistent.value)
    assert cross_org.value.code == nonexistent.value.code
    assert type(cross_org.value) is type(nonexistent.value)


async def test_the_decision_carries_ids_and_never_the_name(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """A decision row is written for denials too, so a payload that quoted the
    filename would answer the question the denial exists to refuse."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("payroll-2026-secret.txt", drive=drive)
    node = tree["payroll-2026-secret.txt"]
    await files_factory.grant(node, "user", files_org.member_id, "reader")
    node_id, node_name = node.id, node.name_display

    recorder = RecordingEnforce()
    with pytest.raises(Denied):
        await _run(
            repo,
            _ctx(files_org, files_org.member_id),
            NodeId(node.id),
            FilesAction.WRITE,
            recorder,
        )

    (attrs,) = recorder.attrs
    (resource,) = recorder.resources
    assert resource.type is ResourceType.FILE_NODE
    assert resource.id == str(node_id)
    assert resource.org_id == files_org.org_team_id

    policy = policy_for(ResourceType.FILE_NODE)
    assert policy is not None
    recorded = audited_attrs(attrs, policy.audited_attrs)
    assert set(recorded) & NEVER_AUDITED_KEYS == set()
    assert "payroll-2026-secret.txt" not in str(recorded)
    assert node_name not in str(recorded)
    # Nothing in the whole attrs bag — audited or not — spells the node's name.
    assert node_name not in str(attrs)
    assert str(node_id) in str(attrs)


# ---------------------------------------------------------------------------
# Same work on every "not yours" path, so timing reveals nothing
# ---------------------------------------------------------------------------


@contextmanager
def _counting(session: AsyncSession) -> Iterator[list[str]]:
    """Every statement actually sent to the server while the block runs.

    ``before_cursor_execute`` fires once per statement rather than once per ORM
    call, so this counts the work the caller *caused* — the thing that becomes
    the latency and the line on the bill an attacker can read.
    """
    bind = session.get_bind()
    engine: Engine = bind.sync_engine if isinstance(bind, AsyncEngine) else bind
    seen: list[str] = []

    def record(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


@dataclass(frozen=True, slots=True)
class NotYours:
    """One of the three classes of "not yours" and the id that names it."""

    label: str
    node_id: NodeId


async def _interned_acl(
    files_session: AsyncSession, org: FilesOrg, principal_id: uuid.UUID
) -> FileAcl:
    """An interned ACL naming one principal — how a real restrictive share
    renders once the cache is warm, which is the shape every production node
    has."""
    body = ace_body(
        [
            Grant(
                principal=Principal(kind="user", id=principal_id),
                role="manager",
                origin=GrantOrigin.direct(),
            )
        ]
    )
    acl = FileAcl(
        id=uuid.uuid4(),
        org_team_id=org.org_team_id,
        body=body,
        body_hash=body_hash(body, org_team_id=org.org_team_id),
    )
    files_session.add(acl)
    await files_session.commit()
    return acl


@pytest.fixture
async def not_yours(
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_org_factory: Any,
    files_session: Any,
) -> tuple[NotYours, ...]:
    """The three ids a caller cannot have: never existed, another org's, and
    one of their own org's that names somebody else."""
    other = await files_org_factory()
    foreign = await _seed_for(FilesFactory(files_session, other), other.member_id, "owner")

    drive = await files_factory.drive()
    tree = await files_factory.tree("papers/ papers/report.txt", drive=drive)
    hidden = tree["papers/report.txt"]
    hidden.acl_id = (await _interned_acl(files_session, files_org, files_org.admin_id)).id
    await files_session.commit()

    return (
        NotYours("nonexistent", NodeId(uuid.uuid4())),
        NotYours("other_org", NodeId(foreign.id)),
        NotYours("unreadable", NodeId(hidden.id)),
    )


@pytest.mark.parametrize(
    "action",
    [FilesAction.READ, FilesAction.WRITE, FilesAction.DELETE, FilesAction.SHARE],
    ids=lambda a: a.value,
)
async def test_the_three_not_yours_classes_cost_the_same_and_each_records_one_deny(
    repo: FilesRepo,
    files_session: Any,
    files_org: FilesOrg,
    not_yours: tuple[NotYours, ...],
    action: FilesAction,
) -> None:
    """None of the three may be cheaper than the others, and none may skip the
    decision.

    An early return on "no such row" would make the nonexistent and other-org
    classes cost fewer statements than the same-org-unreadable one and would
    leave no decision row behind — two channels that answer the existence
    question the identical bodies exist to refuse. The counts are compared
    against each other rather than against a constant, so the test survives a
    change in how many statements a load takes and still fails the moment the
    three stop agreeing.
    """
    ctx = _ctx(files_org, files_org.member_id)
    counts: dict[str, int] = {}
    denials: dict[str, list[Decision]] = {}

    for case in not_yours:
        recorder = RecordingEnforce()
        with _counting(files_session) as statements:
            with pytest.raises(NotFound):
                await _run(repo, ctx, case.node_id, action, recorder)
        counts[case.label] = len(statements)
        denials[case.label] = recorder.decisions

    assert len(set(counts.values())) == 1, f"statement counts differ across classes: {counts}"

    for label, decisions in denials.items():
        assert len(decisions) == 1, f"{label} handed the sink {len(decisions)} decisions"
        (decision,) = decisions
        assert decision.allowed is False, label
        # ``as_not_found`` with no error code is what makes the row a DENY that
        # renders as the opaque 404 rather than a coded 403.
        assert decision.as_not_found is True, label
        assert decision.error_code is None, label
        assert decision.policy == "files.access", label

    reasons = {label: decisions[0].reason for label, decisions in denials.items()}
    assert reasons["nonexistent"] == reasons["other_org"] == "no_such_node", reasons
    assert reasons["unreadable"] == "action_not_allowed_unreadable", reasons


async def test_a_missing_node_never_becomes_authorized_however_the_sink_answers(
    repo: FilesRepo, files_org: FilesOrg
) -> None:
    """A sink that returns an allow for a node that is not there — a future
    engine's bug, or a policy someone reorders — still cannot produce a handle:
    there is no node to put in one."""

    class AlwaysAllows:
        def __call__(
            self,
            ctx: ActingContext,
            action: Action,
            resource: Resource,
            attrs: Mapping[str, object],
        ) -> Decision:
            return allow("test.always", "yes")

    with pytest.raises(NotFound):
        await _run(  # type: ignore[arg-type]
            repo,
            _ctx(files_org, files_org.member_id),
            NodeId(uuid.uuid4()),
            FilesAction.READ,
            AlwaysAllows(),
        )


@pytest.mark.parametrize(
    ("machine", "allowed"),
    [
        pytest.param("box-a", True, id="a-verified-box-is-served-the-bytes"),
        pytest.param(None, False, id="an-agent-nobody-verified-is-not"),
    ],
)
async def test_the_seal_yields_to_a_proven_machine_and_to_nobody_else(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    machine: str | None,
    allowed: bool,
) -> None:
    """Through the real engine, on a real row: holding the lease is what lets a
    box pull a sealed chat's bytes, and a verified machine is what counts as
    holding it.

    The agent assertion is two headers on the caller's own session, and the id
    it names is published on every chat the box serves — so without the
    verification this decision would turn on a string any member can read. The
    refusal is the one a sealed folder always gives, and the decision row says
    which fact settled it.
    """
    from alkera_core.files.authz.decider import NO_DOWNLOAD_BIT

    drive = await files_factory.drive()
    tree = await files_factory.tree("chat/ chat/notes.md", drive=drive)
    folder, node = tree["chat"], tree["chat/notes.md"]
    await files_factory.grant(folder, "user", files_org.member_id, "owner")
    node.flags |= NO_DOWNLOAD_BIT
    await files_factory._session.commit()
    agent = ActingContext.for_agent(
        user_id=files_org.member_id,
        org_id=files_org.org_team_id,
        email="m@files.test",
        session_id="box-a",
    )
    enforce = RecordingEnforce()

    facts = AccessFacts(
        is_agent=True,
        leased_subtree=folder.id,
        agent_machine_id=machine,
        held_leases=frozenset({folder.id}),
    )
    if allowed:
        handle = await _run(repo, agent, NodeId(node.id), FilesAction.EXPORT, enforce, facts=facts)
        assert handle.access.allows(FilesAction.EXPORT)
    else:
        with pytest.raises((Denied, NotFound)):
            await _run(repo, agent, NodeId(node.id), FilesAction.EXPORT, enforce, facts=facts)
    assert enforce.attrs[-1]["agent_machine_verified"] is (machine is not None)
