"""The ``GrantSource`` seam, driven over a second concrete implementation.

Future caller this stands for: the **platform RBAC/ABAC engine** that replaces
both ``GrantSource`` and the grant half of the decider. It keeps its grants in
its own store, not in ``file_shares``, and it names principals in its own terms
— here ``engine:role``, a role assignment the Files ladder had never heard of
when these rows were written.

Two claims, and the second is the one with teeth:

* the source is a *source*: :class:`EngineGrantSource` is asked for a node and
  its chain and answers with :class:`~alkera_core.files.authz.grants.Grant`
  values, so nothing above ``files/authz`` learns which store answered;
* the kind is admitted **by registration**, through
  :func:`~alkera_core.files.authz.grants.register_principal_kind` — the
  registration point ``Principal.kind``'s "open registry" needs, where ``user``,
  ``team`` and ``org`` are themselves registered. The negative twin is the same
  grant with the same registration removed: the decider then ignores it, which
  is what proves the registration is doing the work.

Everything runs through the real entry point,
:func:`~alkera_core.files.authz.decider.effective_role`, and the assertion is a
comparison against what a plain ``user`` grant of the same role resolves to on
the same node — an independently-computed expectation, not a restatement.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.principal import Principal as ActingPrincipal
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.decider import AccessFacts, effective_role
from alkera_core.files.authz.grants import (
    CallerIdentity,
    Grant,
    GrantOrigin,
    GrantSource,
    Principal,
    principal_kinds,
    principal_matcher,
    register_principal_kind,
    unregister_principal_kind,
)
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

#: The engine's own principal kind. Nothing in the library mentions it.
ENGINE_KIND = "engine:role"
#: The engine's id for "whoever holds the data-platform role assignment".
ROLE_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
#: Where the engine keeps the fact that this caller holds that role.
ROLE_FACT = "engine_role_ids"


# ---- the second implementation ---------------------------------------------


class EngineGrantSource:
    """Grants read out of the engine's own store, keyed by its own principals.

    It never touches ``file_shares`` or the interned ACL cache, and it answers
    for a whole chain in one call — which is what a platform-wide grants table
    would do.
    """

    def __init__(self, assignments: dict[uuid.UUID, tuple[uuid.UUID, str]]) -> None:
        #: ``node id -> (engine principal id, role)``.
        self._assignments = assignments
        self.asked: list[uuid.UUID] = []

    async def grants_for(
        self, repo: FilesRepo, node: FileNode, chain: Sequence[FileNode]
    ) -> Sequence[Grant]:
        self.asked.append(node.id)
        grants: list[Grant] = []
        for ancestor in chain:
            found = self._assignments.get(ancestor.id)
            if found is None:
                continue
            principal_id, role = found
            grants.append(
                Grant(
                    principal=Principal(kind=ENGINE_KIND, id=principal_id),
                    role=role,
                    origin=(
                        GrantOrigin.direct()
                        if ancestor.id == node.id
                        else GrantOrigin.inherited(ancestor.id)
                    ),
                )
            )
        return grants


@pytest.fixture
def engine_kind() -> Iterator[None]:
    """Register the engine's principal kind for the length of one test.

    The matcher reads only the :class:`CallerIdentity` it is handed — the
    engine's role ids ride in the open ``extra`` bag, which is the hook
    ``AccessFacts`` carries for exactly this.
    """

    def holds_the_role(principal: Principal, caller: CallerIdentity) -> bool:
        held = caller.extra.get(ROLE_FACT, ())
        return principal.id in held

    register_principal_kind(ENGINE_KIND, holds_the_role)
    try:
        yield
    finally:
        unregister_principal_kind(ENGINE_KIND)


# ---- the scene -------------------------------------------------------------


@pytest.fixture
async def scene(
    files_factory: FilesFactory, files_org: FilesOrg
) -> tuple[FileDrive, dict[str, FileNode]]:
    drive = await files_factory.drive()
    made = await files_factory.tree("team/ team/plans/ team/plans/q3.txt", drive=drive)
    return drive, made


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=ActingPrincipal(
            kind=PrincipalKind.USER,
            id=str(org.member_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


def _facts(*, holds_role: bool) -> AccessFacts:
    return AccessFacts(extra={ROLE_FACT: (ROLE_ID,)} if holds_role else {})


def _resolve(
    org: FilesOrg,
    drive: FileDrive,
    made: dict[str, FileNode],
    grants: Sequence[Grant],
    *,
    holds_role: bool = True,
) -> frozenset[str]:
    node = made["team/plans/q3.txt"]
    chain = (made["team"], made["team/plans"], node)
    access = effective_role(_ctx(org), node, chain, grants, drive, _facts(holds_role=holds_role))
    assert access.in_org
    return access.allowed_actions


# ---- the registration point ------------------------------------------------


async def test_the_three_builtin_kinds_are_themselves_registered() -> None:
    """The engine's kind is admitted the same way ``user`` is — one registry,
    no privileged branch for the kinds that shipped first."""
    assert set(principal_kinds()) >= {"user", "team", "org"}
    assert principal_matcher("engine:role") is None


async def test_registering_a_kind_twice_is_refused_at_wiring_time(engine_kind: None) -> None:
    """A second matcher would silently change who a live grant matches."""
    with pytest.raises(ValueError, match="already registered"):
        register_principal_kind(ENGINE_KIND, lambda principal, caller: True)


async def test_an_empty_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="may not be empty"):
        register_principal_kind("", lambda principal, caller: True)


async def test_unregistering_a_kind_nobody_registered_is_not_an_error() -> None:
    unregister_principal_kind("engine:never-existed")
    assert principal_matcher("engine:never-existed") is None


# ---- the seam's claims -----------------------------------------------------


async def test_the_engine_source_satisfies_the_grant_source_protocol() -> None:
    source: GrantSource = EngineGrantSource({})
    assert source is not None


async def test_an_engine_principal_resolves_the_same_actions_as_a_user(
    scene: tuple[FileDrive, dict[str, FileNode]], files_org: FilesOrg, engine_kind: None
) -> None:
    """The claim: a kind the ladder did not know resolves through the SAME
    decider to the SAME access a ``user`` grant of that role gives."""
    drive, made = scene
    node = made["team/plans/q3.txt"]

    as_user = _resolve(
        files_org,
        drive,
        made,
        [Grant(principal=Principal(kind="user", id=files_org.member_id), role="writer")],
        holds_role=False,
    )
    source = EngineGrantSource({node.id: (ROLE_ID, "writer")})
    as_engine = _resolve(files_org, drive, made, await source.grants_for(None, node, (node,)))  # type: ignore[arg-type]

    assert as_engine == as_user
    assert FilesAction.WRITE.value in as_engine
    assert source.asked == [node.id]


async def test_the_same_engine_grant_is_ignored_without_the_registration(
    scene: tuple[FileDrive, dict[str, FileNode]], files_org: FilesOrg
) -> None:
    """The negative twin, with ``engine_kind`` deliberately NOT requested: an
    unregistered kind is data to ignore, so the identical grant grants nothing."""
    drive, made = scene
    node = made["team/plans/q3.txt"]
    source = EngineGrantSource({node.id: (ROLE_ID, "writer")})

    assert principal_matcher(ENGINE_KIND) is None
    assert (
        _resolve(files_org, drive, made, await source.grants_for(None, node, (node,)))
        == frozenset()
    )  # type: ignore[arg-type]


async def test_a_caller_without_the_engines_role_fact_gets_nothing(
    scene: tuple[FileDrive, dict[str, FileNode]], files_org: FilesOrg, engine_kind: None
) -> None:
    """The matcher is the gate, not the registration: registering the kind does
    not hand the grant to everyone."""
    drive, made = scene
    node = made["team/plans/q3.txt"]
    source = EngineGrantSource({node.id: (ROLE_ID, "manager")})

    granted = await source.grants_for(None, node, (node,))  # type: ignore[arg-type]
    assert _resolve(files_org, drive, made, granted, holds_role=False) == frozenset()


async def test_an_engine_grant_on_an_ancestor_trickles_down(
    scene: tuple[FileDrive, dict[str, FileNode]], files_org: FilesOrg, engine_kind: None
) -> None:
    """Descent is the decider's rule and applies to the engine's grants too —
    the source only reports where a grant sits."""
    drive, made = scene
    node = made["team/plans/q3.txt"]
    chain = (made["team"], made["team/plans"], node)
    source = EngineGrantSource({made["team"].id: (ROLE_ID, "reader")})

    granted = await source.grants_for(None, node, chain)  # type: ignore[arg-type]
    assert [grant.origin.kind for grant in granted] == ["inherited"]
    actions = _resolve(files_org, drive, made, granted)
    assert FilesAction.READ.value in actions
    assert FilesAction.WRITE.value not in actions


@pytest.mark.parametrize(
    ("role", "writes"),
    [
        pytest.param("reader", False, id="reader"),
        pytest.param("commenter", False, id="commenter"),
        pytest.param("writer", True, id="writer"),
        pytest.param("manager", True, id="manager"),
        pytest.param("owner", True, id="owner"),
    ],
)
async def test_every_rung_of_the_ladder_reaches_the_engine_principal(
    scene: tuple[FileDrive, dict[str, FileNode]],
    files_org: FilesOrg,
    engine_kind: None,
    role: str,
    writes: bool,
) -> None:
    """The ladder is data: the engine's principal climbs all of it, and the
    role it was given is the role it gets — not a floor and not a ceiling."""
    drive, made = scene
    node = made["team/plans/q3.txt"]
    source = EngineGrantSource({node.id: (ROLE_ID, role)})

    granted = await source.grants_for(None, node, (node,))  # type: ignore[arg-type]
    engine_actions = _resolve(files_org, drive, made, granted)
    user_actions = _resolve(
        files_org,
        drive,
        made,
        [Grant(principal=Principal(kind="user", id=files_org.member_id), role=role)],
        holds_role=False,
    )
    assert engine_actions == user_actions
    assert (FilesAction.WRITE.value in engine_actions) is writes
