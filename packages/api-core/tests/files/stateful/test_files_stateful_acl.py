"""The ACL, driven against its reference model on real Postgres.

The model holds a set of grants per node and derives a role by walking the
chain; it has no cache and no ``acl_id``, which is precisely what lets it say
whether the implementation's *cached* body has drifted from the union it only
copies. Every rule applies the same operation to both sides. After each step two
invariants run: the role the decider hands each of a small principal set equals
the model's, and — once the background rewrite has been drained to completion —
every node's interned body equals the chain union the model computes from
scratch. A move is the interesting one: nothing about the moved node's own
grants changes, and every inherited role below it must re-derive from where it
now hangs.
"""

from __future__ import annotations

import uuid
from typing import Any

from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.principal import Principal as ActingPrincipal
from alkera_core.files import acl
from alkera_core.files.authz.decider import AccessFacts, LadderDecider, effective_role
from alkera_core.files.authz.grants import FilesGrantSource, Principal
from alkera_core.files.errors import FilesError
from alkera_core.files.ids import NodeId, OperationId
from alkera_core.files.namespace import Namespace
from alkera_core.models.files.acl import FileAcl
from alkera_core.models.files.ops import FileOp
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule
from hypothesis.strategies import integers, sampled_from
from sqlalchemy import select, text
from tests.files.stateful._models import ROLE_ORDER, AclModel, ModelRefusal
from tests.files.stateful.conftest import StatefulRig, open_rig, refusal_code

#: Two branches, one of them nested, so a grant has somewhere to descend to and
#: a move has somewhere to re-derive from.
TREE: tuple[tuple[str, str], ...] = (("a", "root"), ("b", "a"), ("c", "root"))

#: The rungs a rule may hand out. ``owner`` is left out on purpose: it is the
#: one rung the drive's own default could mask, and the point here is the union.
ROLES: tuple[str, ...] = ("reader", "commenter", "writer", "manager")

#: The two callers every role assertion is made about.
PRINCIPALS: tuple[str, ...] = ("admin", "member")


class AclReferenceModel(RuleBasedStateMachine):
    """Contract-equivalence for grant / revoke / move / read-role."""

    def __init__(self) -> None:
        super().__init__()
        self.rig: StatefulRig = open_rig(label="acl-model")
        self.model = AclModel()
        self.ids: dict[str, uuid.UUID] = {}
        self.shares: dict[str, uuid.UUID] = {}

    @initialize()
    def build_tree(self) -> None:
        self.rig.run(self._seed_principals())
        self.ids["root"] = self.rig.root_id
        self.model.parents["root"] = None
        for name, parent in TREE:
            self.ids[name] = self.rig.run(self._create(parent, name))
            self.model.parents[name] = parent

    # ---- plumbing --------------------------------------------------------

    def _ctx(self, who: str = "admin") -> ActingContext:
        return ActingContext(
            acting_principal=ActingPrincipal(
                kind=PrincipalKind.USER,
                id=str(self._uuid_of(who)),
                org_id=self.rig.org.org_team_id,
                credential=CredentialKind.JWT,
            )
        )

    def _uuid_of(self, who: str) -> uuid.UUID:
        return self.rig.org.admin_id if who == "admin" else self.rig.org.member_id

    async def _seed_principals(self) -> None:
        """Give the rig's tenant two real members.

        The rig seeds a team and a drive but no people, and a grant to a
        principal the org does not contain is refused before the ACL algebra
        gets a word in — so the two callers the invariants ask about have to
        exist as rows first.
        """
        for who, role in (("admin", "admin"), ("member", "member")):
            user_id = self._uuid_of(who)
            await self.rig.session.execute(
                text(
                    "INSERT INTO users (id, org_team_id, email, email_domain, "
                    "first_name, last_name, created_at) "
                    "VALUES (:id, :org, :email, 'files.test', :who, 'Tester', now())"
                ),
                {
                    "id": user_id,
                    "org": self.rig.org.org_team_id,
                    "email": f"{who}-{user_id.hex[:12]}@files.test",
                    "who": who,
                },
            )
            await self.rig.session.execute(
                text(
                    "INSERT INTO team_memberships (id, user_id, team_id, role, created_at) "
                    "VALUES (gen_random_uuid(), :user, :team, :role, now())"
                ),
                {"user": user_id, "team": self.rig.org.org_team_id, "role": role},
            )
        await self.rig.session.commit()

    async def _create(self, parent: str, name: str) -> uuid.UUID:
        async with self.rig.repo.transaction():
            namespace = Namespace(self.rig.repo, self._ctx(), self.rig.clock, None)
            node = await namespace.create(
                self.rig.drive_id, NodeId(self.ids[parent]), "folder", name.encode()
            )
            return node.id

    async def _node(self, handle: str) -> FileNode:
        node = await self.rig.repo.node(NodeId(self.ids[handle]))
        assert node is not None
        return node

    def _nodes(self) -> list[str]:
        return sorted(self.model.parents)

    def _movable(self) -> list[str]:
        return sorted(key for key in self.model.parents if key != "root")

    def _expect(self, call: Any, code: str) -> None:
        try:
            call()
        except FilesError as refused:
            assert refusal_code(refused) == code, (
                f"the service refused with {refusal_code(refused)}; the contract requires {code}"
            )
            return
        raise AssertionError(f"the service accepted what the contract refuses with {code}")

    # ---- rules -----------------------------------------------------------

    @rule(
        pick=integers(min_value=0),
        who=sampled_from(PRINCIPALS),
        role=sampled_from(ROLES),
    )
    def grant(self, pick: int, who: str, role: str) -> None:
        nodes = self._nodes()
        node = nodes[pick % len(nodes)]
        share = self.rig.run(self._grant(node, who, role))
        self.shares[self.model.grant(node, who, role)] = share

    async def _grant(self, node: str, who: str, role: str) -> uuid.UUID:
        async with self.rig.repo.transaction():
            share = await acl.grant(
                self.rig.repo,
                self._ctx(),
                await self._node(node),
                Principal(kind="user", id=self._uuid_of(who)),
                role,
            )
            return share.id

    @precondition(lambda self: bool(self.shares))
    @rule(pick=integers(min_value=0), at=integers(min_value=0))
    def revoke(self, pick: int, at: int) -> None:
        """Revoke a share, sometimes aimed at the wrong node on purpose.

        A revoke pointed at a node that merely *inherits* the grant is the case
        with no honest answer but a refusal, so the rule deliberately aims one
        there and holds the service to the same code the contract names.
        """
        handles = sorted(self.shares)
        handle = handles[pick % len(handles)]
        nodes = self._nodes()
        node = nodes[at % len(nodes)]

        def apply() -> Any:
            return self.rig.run(self._revoke(node, self.shares[handle]))

        try:
            self.model.revoke(handle, node)
        except ModelRefusal as refusal:
            self._expect(apply, refusal.code)
            return
        apply()
        del self.shares[handle]

    async def _revoke(self, node: str, share_id: uuid.UUID) -> None:
        async with self.rig.repo.transaction():
            await acl.revoke(self.rig.repo, self._ctx(), await self._node(node), share_id)

    @precondition(lambda self: bool(self._movable()))
    @rule(pick=integers(min_value=0), to=integers(min_value=0))
    def move(self, pick: int, to: int) -> None:
        """Re-hang a subtree; every inherited role below it must re-derive."""
        movable = self._movable()
        node = movable[pick % len(movable)]
        nodes = self._nodes()
        parent = nodes[to % len(nodes)]
        if parent == self.model.parents[node]:
            return

        def apply() -> Any:
            return self.rig.run(self._move(node, parent))

        try:
            self.model.move(node, parent)
        except ModelRefusal as refusal:
            self._expect(apply, refusal.code)
            return
        apply()

    async def _move(self, node: str, parent: str) -> None:
        async with self.rig.repo.transaction():
            namespace = Namespace(self.rig.repo, self._ctx(), self.rig.clock, None)
            target = await self._node(node)
            # No `move_rederive` here any more: the library's own move does it,
            # which is the whole point. Calling it again would be a second,
            # redundant re-derive this model does not model.
            await namespace.move(
                NodeId(target.id), NodeId(self.ids[parent]), if_match=int(target.etag)
            )

    # ---- invariants ------------------------------------------------------

    @invariant()
    def role_equals_the_chain_union(self) -> None:
        """What a caller may do is the chain's answer, cache or no cache."""
        for who in PRINCIPALS:
            for handle in self._nodes():
                got = self.rig.run(self._role_of(handle, who))
                want = self.model.effective_role(who, handle)
                assert got == want, f"{who} has {got!r} on {handle}; the chain gives {want!r}"

    async def _role_of(self, handle: str, who: str) -> str | None:
        async with self.rig.repo.transaction():
            node = await self._node(handle)
            chain = await self.rig.repo.chain(node)
            granted = await FilesGrantSource().grants_for(self.rig.repo, node, chain)
            drive = await self.rig.repo.session.get(FileDrive, self.rig.drive_uuid)
            assert drive is not None
            access = effective_role(
                self._ctx(who),
                node,
                chain,
                granted,
                drive,
                AccessFacts(team_ids=frozenset({self.rig.org.org_team_id}), org_admin=False),
                decider=LadderDecider(),
            )
            role = access.role
            return role if role in ROLE_ORDER else None

    @invariant()
    def the_cache_equals_the_union(self) -> None:
        """Drained to completion, every interned body is the chain union."""
        self.rig.run(self._drain())
        for handle in self._nodes():
            body = self.rig.run(self._body_of(handle))
            assert body == self.model.union(handle), (
                f"the cached body on {handle} is {sorted(body)}; "
                f"the chain union is {sorted(self.model.union(handle))}"
            )

    async def _drain(self) -> None:
        """Run every outstanding rewrite to completion, the way a worker would."""
        for _ in range(50):
            async with self.rig.repo.transaction():
                pending = list(
                    (
                        await self.rig.repo.execute_scoped(
                            select(FileOp).where(
                                FileOp.kind == acl.OP_ACL_REWRITE, FileOp.state != "done"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            if not pending:
                return
            for op in pending:
                async with self.rig.repo.transaction():
                    await acl.rewrite(self.rig.repo, OperationId(op.id))
        raise AssertionError("the rewrite never drained")

    async def _body_of(self, handle: str) -> set[tuple[str, str, str]]:
        async with self.rig.repo.transaction():
            node = await self._node(handle)
            if node.acl_id is None:
                return set()
            row = (
                await self.rig.repo.execute_scoped(select(FileAcl).where(FileAcl.id == node.acl_id))
            ).scalar_one()
            found: set[tuple[str, str, str]] = set()
            for ace in row.body:
                who = (
                    "admin" if uuid.UUID(ace["principal_id"]) == self.rig.org.admin_id else "member"
                )
                found.add((who, str(ace["role"]), str(ace["origin"])))
            return found

    def teardown(self) -> None:
        self.rig.close()


AclReferenceModel.TestCase.settings = hypothesis_settings(
    max_examples=5,
    stateful_step_count=8,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

TestAclReferenceModel = AclReferenceModel.TestCase
