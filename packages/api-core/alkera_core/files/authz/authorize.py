"""The one way to get a node you are allowed to act on.

``authorize`` loads the node **through the repo** (so row-level security and
the cross-org gate apply before anything else), resolves the effective access,
and hands the decision to ``enforce()``. What comes back is not a boolean but a
:class:`Authorized` handle carrying the node, its chain and the access that
justified it — and the handle is *typed by the action*, so a service method
that mutates can spell its parameter ``Authorized[Write]`` and forgetting to
authorize becomes a ``mypy`` error rather than a missing line of code.

``enforce`` is injected. The backend's ``backend.authz.enforce`` writes the
decision row (allow in the request transaction, deny in its own committed one);
this library never imports the backend, and a test can pass the pure
:func:`alkera_core.authz.authorize` instead.

Both refusals collapse to the same shape a stranger sees: a node in another org
is not there as far as the repo is concerned, and a policy denial that asks for
an opaque answer raises the identical :class:`NotFound`. A caller can tell "you
may not" from "it is not there" only when they can already read the node.

The same is true of the *work*. There is no early return on "no such row": a
node that is missing (or in another org, which the repo's scope makes the same
thing) still performs the loads a present node performs, still reaches
``enforce`` — with ``exists=False`` as one more fact — and still leaves a DENY
decision row behind. Otherwise a caller who cannot read the body could count
queries, time the request, or read their own bill and recover exactly the
existence answer the identical bodies refuse them. Every path does the same
work.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, Literal, NoReturn, Protocol, TypeVar

from alkera_core.authz.decision import Decision
from alkera_core.authz.engine import authorize as decide
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.decider import (
    DEFAULT_DECIDER,
    AccessDecider,
    AccessFacts,
    EffectiveAccess,
    effective_role,
    rung_within,
)
from alkera_core.files.authz.grants import FilesGrantSource, GrantSource
from alkera_core.files.authz.policy_attrs import policy_attrs
from alkera_core.files.errors import FilesError, NotFound
from alkera_core.files.ids import AclId, DriveId, NodeId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion

A = TypeVar("A", bound=str)

#: The fact that says whether the node the caller named is there at all. The
#: policy — not a branch in this module — decides what its absence means.
EXISTS = "exists"


class Denied(FilesError):  # noqa: N818 - the route layer maps these names; the Error suffix is noise here
    """The caller may read this node but not do this to it (403).

    Raised only where a 403 is safe — the policy has already decided that a
    caller who cannot read the node gets an opaque :class:`NotFound` instead.
    """

    code = "files.forbidden"
    status = 403


@dataclass(frozen=True, slots=True)
class Authorized(Generic[A]):
    """Proof that ``action`` was allowed on ``node`` for one caller.

    The type parameter is the action's literal name, which is what makes
    ``Authorized[Write]`` and ``Authorized[Read]`` different types to ``mypy``
    while being the same class at runtime. ``chain`` and ``access`` ride along
    because every caller that just authorized a node needs them next (the
    ancestor chain for a move or a quota walk, the access for ``capabilities``)
    and re-reading them would be a second, racier answer. ``head`` and
    ``starred`` ride along for the same reason: the wire item names the head
    version's hash, mime type and scan state and whether *this* caller starred
    the node, and both came out of the statement that loaded the node.
    """

    node: FileNode
    chain: Sequence[FileNode]
    access: EffectiveAccess
    action: FilesAction
    head: FileVersion | None = None
    starred: bool = False
    #: Of the rungs the caller asked about (``share_rungs``), the ones a share
    #: of this node may grant or withdraw: what a share dialog renders its
    #: per-row controls from, decided by the policy that enforces the write.
    shareable: frozenset[str] = frozenset()


Read = Authorized[Literal["read"]]
Write = Authorized[Literal["write"]]
Share = Authorized[Literal["share"]]
Delete = Authorized[Literal["delete"]]
Lease_ = Authorized[Literal["lease"]]
LeaseRequest = Authorized[Literal["lease_request"]]
LeaseForce = Authorized[Literal["lease_force"]]
Snapshot = Authorized[Literal["snapshot"]]
Restore = Authorized[Literal["restore"]]
Export = Authorized[Literal["export"]]
Copy = Authorized[Literal["copy"]]


class Enforcer(Protocol):
    """The decision sink: ``backend.authz.enforce`` with its request and session
    already bound, or the pure engine in a library test. Either may be
    synchronous, so :func:`authorize` awaits only what is awaitable."""

    def __call__(
        self,
        ctx: ActingContext,
        action: Action,
        resource: Resource,
        attrs: Mapping[str, object],
    ) -> Decision | Awaitable[Decision]: ...


def platform_action(action: FilesAction) -> Action:
    """The platform verb for a Files verb. The two enums share their values on
    purpose, so this is a lookup rather than a mapping table that could drift."""
    return Action(action.value)


async def authorize(
    ctx: ActingContext,
    repo: FilesRepo,
    node_id: NodeId,
    action: FilesAction,
    *,
    facts: AccessFacts,
    enforce: Enforcer,
    grants: GrantSource | None = None,
    decider: AccessDecider = DEFAULT_DECIDER,
    grant_role: str | None = None,
    share_rungs: Iterable[str] = (),
) -> Authorized[Any]:
    """Authorize ``action`` on ``node_id``, or raise.

    Raises :class:`NotFound` for a node that does not exist, one in another org,
    and one the caller may not know about — the same exception with the same
    code and text in all three cases — and :class:`Denied` only when the policy
    says a visible refusal is safe.

    ``grant_role`` is the rung a share is about to grant or withdraw, so the
    policy can hold it to the caller's own: a ``SHARE`` that names none is a
    read of the roster and has no ceiling to check.

    ``share_rungs`` asks, on top of the decision, which of those rungs a
    ``SHARE`` of the node would be allowed to grant or withdraw. Each is the
    same policy over the same facts with only the ceiling varied, run through
    the pure engine: it is a preview for rendering controls, not a decision
    the caller acted on, so it leaves no decision row.
    """
    row = await repo.node_row(node_id, starred_for=ctx.effective_user_id)
    if row is None:
        # Not there, or in another org — the repo's scope makes those one
        # answer, and the policy gives it, not a branch here.
        return await _absent(ctx, repo, node_id, action, facts=facts, enforce=enforce)
    node = row.node
    drive = await repo.drive(DriveId(node.drive_id))
    if drive is None:
        # A node whose drive is gone. The foreign key makes this unreachable,
        # so it is answered as absence rather than as a fourth kind of refusal.
        return await _absent(ctx, repo, node_id, action, facts=facts, enforce=enforce)

    chain = await repo.chain(node)
    source = grants if grants is not None else FilesGrantSource()
    grant_rows = await source.grants_for(repo, node, chain)
    access = effective_role(ctx, node, chain, grant_rows, drive, facts, decider=decider)

    resource = node_resource(node, drive)
    attrs = present_attrs(access, node, drive, ctx, facts, grant_role=grant_role)
    decision = await _decide(enforce, ctx, action, resource, attrs)

    if not decision.allowed:
        if decision.as_not_found:
            raise NotFound()
        raise Denied(decision.error_code or Denied.code, decision.message or "Not allowed")
    shareable = frozenset(
        rung
        for rung in share_rungs
        if decide(
            ctx,
            platform_action(FilesAction.SHARE),
            resource,
            {
                **policy_attrs(
                    access,
                    node,
                    drive,
                    ctx,
                    facts,
                    grant_within_rung=rung_within(access, rung),
                ),
                EXISTS: True,
            },
        ).allowed
    )
    return Authorized(
        node=node,
        chain=chain,
        access=access,
        action=action,
        head=row.head,
        starred=row.starred,
        shareable=shareable,
    )


def node_resource(node: FileNode, drive: FileDrive) -> Resource:
    """The decision engine's handle on a node that is there."""
    return Resource(
        type=ResourceType.FILE_NODE,
        id=str(node.id),
        org_id=node.org_team_id,
        team_id=drive.org_team_id,
    )


def present_attrs(
    access: EffectiveAccess,
    node: FileNode,
    drive: FileDrive,
    ctx: ActingContext,
    facts: AccessFacts,
    *,
    grant_role: str | None = None,
) -> dict[str, object]:
    """The facts the ``files.access`` policy decides over for a node that is
    there: one decision and a batch of them read the same bag."""
    return {
        **policy_attrs(
            access,
            node,
            drive,
            ctx,
            facts,
            grant_within_rung=grant_role is None or rung_within(access, grant_role),
        ),
        EXISTS: True,
    }


async def _decide(
    enforce: Enforcer,
    ctx: ActingContext,
    action: FilesAction,
    resource: Resource,
    attrs: Mapping[str, object],
) -> Decision:
    """Hand the facts to the sink, awaiting it only when it is awaitable."""
    outcome = enforce(ctx, platform_action(action), resource, attrs)
    return await outcome if inspect.isawaitable(outcome) else outcome


def _absent_attrs(ctx: ActingContext, node_id: NodeId, facts: AccessFacts) -> dict[str, object]:
    """The facts for a node that is not there.

    Every axis a present node varies on is at its fail-closed value — no
    actions, no standing in the org, no flags, no drive kind — so ``exists`` is
    the only thing left for the policy to decide on, and a reordering of the
    policy's branches still produces an opaque refusal rather than an allow.
    The bag mirrors the shape :func:`policy_attrs` builds so a decision row for
    a missing node has the same columns as one for a node that is there, with
    the id the caller named and nothing else.
    """
    bag: dict[str, object] = {
        "node_id": str(node_id),
        "drive_id": None,
        "trust": None,
        "state": None,
        "kind": None,
        "traversal_only": False,
        "leased_subtree": (str(facts.leased_subtree) if facts.leased_subtree is not None else None),
        "held_leases": sorted(str(root) for root in facts.held_leases),
    }
    bag.update(facts.extra)
    return {
        EXISTS: False,
        "org_id": str(ctx.org_id),
        "allowed_actions": frozenset[str](),
        "flags": frozenset[str](),
        "is_agent": facts.is_agent,
        "agent_machine_verified": facts.agent_machine_id is not None,
        "chat_subtree": False,
        "chat_bound_elsewhere": False,
        "workspace_subtree": False,
        "workspace_bound_elsewhere": False,
        "holds_lease": False,
        "machine_runs_it": False,
        "is_link": facts.is_link,
        "in_org": False,
        "drive_kind": "",
        "held": False,
        "locked": False,
        "trashed": False,
        "seal_self_only": False,
        "no_reshare_chain": False,
        "read_via_conditional_grant": False,
        "grant_within_rung": False,
        "facts": bag,
    }


async def _absent(
    ctx: ActingContext,
    repo: FilesRepo,
    node_id: NodeId,
    action: FilesAction,
    *,
    facts: AccessFacts,
    enforce: Enforcer,
) -> NoReturn:
    """Refuse a node that is not there, having done the work a present node costs.

    The three reads the present path still owes — the drive, the chain, the
    interned ACL — are issued here against the id the caller named, which
    names none of those things, so each returns nothing and the caller learns
    nothing; what stays equal is the number of statements, which is the
    latency and the line on the bill. Then the policy decides and the DENY row
    is written exactly as it is for a node the caller may not read.

    Always raises: an absent node can never be authorized.
    """
    await repo.drive(DriveId(node_id))
    await repo.nodes([node_id])
    await repo.acl(AclId(node_id))

    resource = Resource(type=ResourceType.FILE_NODE, id=str(node_id))
    await _decide(enforce, ctx, action, resource, _absent_attrs(ctx, node_id, facts))
    # A sink that answers rather than raising still gets the opaque refusal:
    # the policy has no allow for a node that is not there, and this module
    # will not invent one.
    raise NotFound()


__all__ = [
    "EXISTS",
    "Authorized",
    "Copy",
    "Delete",
    "Denied",
    "Enforcer",
    "Export",
    "LeaseForce",
    "LeaseRequest",
    "Lease_",
    "Read",
    "Restore",
    "Share",
    "Snapshot",
    "Write",
    "authorize",
    "node_resource",
    "platform_action",
    "present_attrs",
]
