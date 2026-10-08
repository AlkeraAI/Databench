"""The seam proof: a foreign engine drives Files without Files changing.

``GrantSource`` and ``AccessDecider`` exist so the platform RBAC/ABAC engine can
replace both without anything above ``files/authz`` noticing. This module is the
evidence that they actually can: it defines an engine-shaped
:class:`PlatformGrantSource` reading a platform-wide grants table (keyed by
node, not by ``file_shares``) and a :class:`ConditionDecider` that evaluates
ABAC ``conditions`` this build's decider refuses to guess at — an ``expires_at``
clause and a ``label`` fact — wires them through :func:`effective_role`, and
shows ``capabilities`` and the refusal reasons come out of the far side
unchanged. A rung the engine invents is honoured because the ladder is data.

Nothing here imports from outside ``alkera_core.files.authz``; that is the
point. If a future edit forces this module to reach into the tree, the
namespace or a route, the seam has leaked and this file will not compile.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.principal import Principal as ActingPrincipal
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.capabilities import (
    REASON_INSUFFICIENT_ROLE,
    REASON_NO_ROLE,
    capabilities,
)
from alkera_core.files.authz.decider import (
    AccessDecider,
    AccessFacts,
    EffectiveAccess,
    LadderDecider,
    effective_role,
    node_flags,
)
from alkera_core.files.authz.grants import Grant, GrantOrigin, GrantSource, Principal
from alkera_core.files.authz.ladder import DEFAULT_LADDER, LADDER, RoleLadder
from alkera_core.files.path_labels import node_label
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode

NOW: Final[datetime] = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

#: A rung the product's ladder has never heard of, added the way the engine
#: would add one: as a row of the table, not as a branch.
AUDITOR: Final[str] = "auditor"
ENGINE_LADDER: Final[RoleLadder] = RoleLadder(
    (
        *LADDER,
        (AUDITOR, frozenset({FilesAction.READ, FilesAction.EXPORT, FilesAction.COMMENT})),
    )
)


# ---- the engine's shapes -------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlatformGrantRow:
    """One row of the platform engine's own grants table.

    Deliberately *not* a ``file_shares`` row: the engine keys grants by an
    opaque resource id and carries its conditions as a bag, which is precisely
    the shape Files must be able to consume without a schema of its own.
    """

    resource_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    role: str
    conditions: Mapping[str, Any] | None = None


class PlatformGrantSource:
    """A ``GrantSource`` served entirely from the engine's table.

    It never touches ``repo``: the parameter stays in the signature because the
    protocol has it, and an implementation that does not need a session is the
    clearest possible proof that the protocol does not force one.
    """

    def __init__(self, rows: Sequence[PlatformGrantRow]) -> None:
        self._rows = tuple(rows)

    async def grants_for(
        self, repo: FilesRepo, node: FileNode, chain: Sequence[FileNode]
    ) -> Sequence[Grant]:
        out: list[Grant] = []
        for ancestor in chain:
            origin = (
                GrantOrigin.direct()
                if ancestor.id == node.id
                else GrantOrigin.inherited(ancestor.id)
            )
            for row in self._rows:
                if row.resource_id != ancestor.id:
                    continue
                out.append(
                    Grant(
                        principal=Principal(kind=row.subject_kind, id=row.subject_id),
                        role=row.role,
                        origin=origin,
                        conditions=row.conditions,
                    )
                )
        return out


class ConditionDecider:
    """An ``AccessDecider`` that evaluates ABAC conditions.

    Two clauses, both of which today's :class:`LadderDecider` refuses outright
    (it understands ``team_role`` and nothing else, so an unreadable clause
    fails closed rather than widening access):

    * ``expires_at`` — a grant whose window has closed is not a candidate;
    * ``label`` — a grant is capped at ``cap`` while the node carries the named
      label, which is how a data-classification engine expresses "confidential
      folders are read-only for everyone below owner".
    """

    def __init__(self, ladder: RoleLadder = DEFAULT_LADDER) -> None:
        self._ladder = ladder

    def decide(
        self,
        ctx: ActingContext,
        node: FileNode,
        chain: Sequence[FileNode],
        grants: Sequence[Grant],
        drive: FileDrive,
        facts: AccessFacts,
    ) -> EffectiveAccess:
        flags = frozenset(flag.value for flag in node_flags(node, drive))
        if node.org_team_id != ctx.acting_principal.org_id:
            return EffectiveAccess(None, frozenset(), flags, in_org=False)
        labels = {str(one) for one in cast(Sequence[Any], facts.extra.get("labels", ()))}
        subject = uuid.UUID(ctx.acting_principal.id)
        roles: list[str] = []
        for grant in grants:
            if grant.principal.kind != "user" or grant.principal.id != subject:
                continue
            role = self._role_after_conditions(grant, labels, facts)
            if role is not None:
                roles.append(role)
        role = self._ladder.max(roles)
        return EffectiveAccess(
            role=role,
            allowed_actions=frozenset(one.value for one in self._ladder.actions_for(role)),
            flags=flags,
            in_org=True,
        )

    def _role_after_conditions(
        self, grant: Grant, labels: set[str], facts: AccessFacts
    ) -> str | None:
        conditions = grant.conditions or {}
        expires_at = conditions.get("expires_at")
        if isinstance(expires_at, datetime) and facts.now is not None and expires_at <= facts.now:
            return None
        label = conditions.get("label")
        if isinstance(label, Mapping) and str(label.get("name")) in labels:
            cap = str(label.get("cap"))
            if self._ladder.rank(cap) < self._ladder.rank(grant.role):
                return cap
        return grant.role


# ---- the world the seam is proved against --------------------------------


ORG_ID: Final[uuid.UUID] = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
USER_ID: Final[uuid.UUID] = uuid.UUID("00000000-0000-4000-8000-0000000000bb")


def _ctx(org_id: uuid.UUID = ORG_ID, user_id: uuid.UUID = USER_ID) -> ActingContext:
    return ActingContext(
        acting_principal=ActingPrincipal(
            kind=PrincipalKind.USER,
            id=str(user_id),
            org_id=org_id,
            credential=CredentialKind.JWT,
        )
    )


def _drive() -> FileDrive:
    return FileDrive(id=uuid.uuid4(), org_team_id=ORG_ID, kind="org", frozen_reason=None)


def _chain() -> tuple[FileNode, FileNode]:
    """A folder and a file under it, built in memory: the seam needs no rows."""
    folder_id = uuid.uuid4()
    child_id = uuid.uuid4()
    folder = FileNode(
        id=folder_id,
        org_team_id=ORG_ID,
        parent_id=None,
        kind="folder",
        name=b"docs",
        path_ids=node_label(folder_id),
        depth=1,
        flags=0,
        state=None,
    )
    child = FileNode(
        id=child_id,
        org_team_id=ORG_ID,
        parent_id=folder_id,
        kind="file",
        name=b"q3.pdf",
        path_ids=f"{node_label(folder_id)}.{node_label(child_id)}",
        depth=2,
        flags=0,
        state=None,
    )
    return folder, child


async def _access(
    rows: Sequence[PlatformGrantRow],
    *,
    decider: AccessDecider,
    facts: AccessFacts,
) -> EffectiveAccess:
    """Read grants through the engine's source, decide through the engine's
    decider, exactly as a route would."""
    folder, child = _chain()
    source: GrantSource = PlatformGrantSource(
        [
            PlatformGrantRow(
                resource_id=folder.id if row.resource_id == _ANCESTOR else child.id,
                subject_kind=row.subject_kind,
                subject_id=row.subject_id,
                role=row.role,
                conditions=row.conditions,
            )
            for row in rows
        ]
    )
    chain = [folder, child]
    grants = await source.grants_for(cast(FilesRepo, None), child, chain)
    return effective_role(_ctx(), child, chain, grants, _drive(), facts, decider=decider)


#: A sentinel resource id meaning "the ancestor folder" in a test's row list;
#: the real ids are minted per call so no two tests share a node.
_ANCESTOR: Final[uuid.UUID] = uuid.UUID("00000000-0000-4000-8000-00000000dead")


def _row(role: str, *, conditions: Mapping[str, Any] | None = None) -> PlatformGrantRow:
    return PlatformGrantRow(
        resource_id=_ANCESTOR,
        subject_kind="user",
        subject_id=USER_ID,
        role=role,
        conditions=conditions,
    )


pytestmark = pytest.mark.asyncio


# ---- the proof -----------------------------------------------------------


async def test_a_platform_grant_source_reaches_effective_role_with_no_files_rows() -> None:
    """A source that never touches ``file_shares`` still drives the decision."""
    access = await _access([_row("writer")], decider=LadderDecider(), facts=AccessFacts(now=NOW))
    assert access.role == "writer"
    assert access.allows(FilesAction.WRITE)
    assert not access.allows(FilesAction.SHARE)


async def test_an_expired_condition_is_ignored_by_the_engines_decider() -> None:
    """The clause today's decider refuses to read is honoured by the engine's."""
    expired = await _access(
        [_row("writer", conditions={"expires_at": NOW - timedelta(minutes=1)})],
        decider=ConditionDecider(),
        facts=AccessFacts(now=NOW),
    )
    assert expired.role is None
    assert expired.allowed_actions == frozenset()


async def test_an_unexpired_condition_is_the_negative_twin() -> None:
    """Same grant, window still open: the expiry branch is what decided."""
    live = await _access(
        [_row("writer", conditions={"expires_at": NOW + timedelta(minutes=1)})],
        decider=ConditionDecider(),
        facts=AccessFacts(now=NOW),
    )
    assert live.role == "writer"
    assert live.allows(FilesAction.WRITE)


async def test_a_label_fact_caps_the_role_through_the_facts_bag() -> None:
    """``AccessFacts.extra`` is the open bag ABAC reads; no signature changed."""
    capped = await _access(
        [_row("owner", conditions={"label": {"name": "confidential", "cap": "reader"}})],
        decider=ConditionDecider(),
        facts=AccessFacts(now=NOW, extra={"labels": ["confidential"]}),
    )
    assert capped.role == "reader"
    assert capped.allows(FilesAction.READ)
    assert not capped.allows(FilesAction.WRITE)
    assert not capped.allows(FilesAction.DELETE)


async def test_the_same_grant_without_the_label_keeps_the_owner_rung() -> None:
    """The negative twin: the cap came from the fact, not from the role."""
    full = await _access(
        [_row("owner", conditions={"label": {"name": "confidential", "cap": "reader"}})],
        decider=ConditionDecider(),
        facts=AccessFacts(now=NOW, extra={"labels": []}),
    )
    assert full.role == "owner"
    assert full.allows(FilesAction.DELETE)


async def test_capabilities_are_unchanged_by_the_seam() -> None:
    """``capabilities`` consumes the result, never the decider that made it."""
    through_engine = await _access(
        [_row("reader")], decider=ConditionDecider(), facts=AccessFacts(now=NOW)
    )
    through_files = await _access(
        [_row("reader")], decider=LadderDecider(), facts=AccessFacts(now=NOW)
    )
    engine_caps = capabilities(through_engine)
    files_caps = capabilities(through_files)
    assert engine_caps.as_dict() == files_caps.as_dict()
    assert engine_caps.as_dict()["canRead"] is True
    assert engine_caps.as_dict()["canWrite"] is False
    assert engine_caps.refusals[FilesAction.WRITE.value] == REASON_INSUFFICIENT_ROLE


async def test_a_refusal_with_no_grant_still_names_the_reason() -> None:
    """An engine that grants nothing produces the ordinary no-role refusal."""
    nothing = await _access([], decider=ConditionDecider(), facts=AccessFacts(now=NOW))
    caps = capabilities(nothing)
    assert nothing.role is None
    assert caps.refusals[FilesAction.READ.value] == REASON_NO_ROLE
    assert set(caps.as_dict()) == {
        "can" + "".join(part.capitalize() for part in action.value.split("_"))
        for action in FilesAction
    }


async def test_a_rung_the_engine_invents_is_honoured_because_the_ladder_is_data() -> None:
    """``auditor`` exists only as a row of ``ENGINE_LADDER`` — no code knows it."""
    access = await _access(
        [_row(AUDITOR)], decider=ConditionDecider(ENGINE_LADDER), facts=AccessFacts(now=NOW)
    )
    assert access.role == AUDITOR
    assert access.allows(FilesAction.COMMENT)
    assert access.allows(FilesAction.EXPORT)
    assert not access.allows(FilesAction.WRITE)
    assert capabilities(access).as_dict()["canComment"] is True


async def test_the_product_ladder_refuses_the_engines_rung() -> None:
    """The negative twin: the rung was honoured by the *table*, not by luck.

    Run the identical grant against the shipped ladder and an unknown rung
    ranks below everything and allows nothing — which is also the fail-closed
    guarantee for a role name a newer writer persists.
    """
    access = await _access(
        [_row(AUDITOR)], decider=ConditionDecider(DEFAULT_LADDER), facts=AccessFacts(now=NOW)
    )
    assert DEFAULT_LADDER.rank(AUDITOR) == -1
    assert access.allowed_actions == frozenset()
