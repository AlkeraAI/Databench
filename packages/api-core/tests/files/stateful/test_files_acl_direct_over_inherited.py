"""The shrunk ACL sequence, replayed step by step with no randomness.

Hypothesis found two divergences between :class:`AclReferenceModel` and the real
ACL — a role the implementation reported one rung too strong, and a cached body
carrying a ``direct`` ACE the chain no longer had — and both shrank to the same
four steps: two grants on one node, revoke one of them there, then grant
somewhere else. The step that matters is the last one, because a revoke had just
freed a share handle and the harness minted the next one from how many shares
were *live*: the new grant re-pointed the older share's model row at a different
node, and the implementation was then measured against a model that had quietly
lost a grant it still holds.

These tests pin the sequence so the harness can never reuse a handle again, and
in doing so they also pin the library's side of the contract: a node keeps its
own ``direct`` ACE when an ancestor is granted to the same principal (Layer 5,
"a child can only add grants"), and the effective role stays the *maximum* over
the chain rather than the direct-most rung.

The two grants belong to two different principals, because one principal holds
one live share on one node: granting again where somebody is already shared
moves their share to the new rung rather than adding a second row, which the
second case here pins on its own.
"""

from __future__ import annotations

import pytest
from tests.files.stateful._models import AclModel
from tests.files.stateful.test_files_stateful_acl import AclReferenceModel

#: ``AclReferenceModel`` indexes ``sorted(self.model.parents)``: a, b, c, root.
NODE_A = 0
NODE_B = 1
NODE_C = 2


def test_the_model_mints_a_fresh_handle_after_a_revoke_frees_one() -> None:
    """A handle names one share for good; a revoke never hands its name on."""
    model = AclModel(parents={"root": None})
    first = model.grant("root", "admin", "reader")
    second = model.grant("root", "member", "manager")
    model.revoke(first, "root")
    third = model.grant("root", "admin", "reader")

    assert third not in {first, second}
    # The revoke took the admin's row and left the member's untouched, so the
    # third grant cannot have landed on top of it.
    assert model.grants[second].role == "manager"
    assert model.effective_role("member", "root") == "manager"


def test_a_second_grant_to_a_shared_principal_moves_the_share_they_already_hold() -> None:
    """A person has one access to one node, so a role change is one row."""
    model = AclModel(parents={"root": None})
    first = model.grant("root", "admin", "reader")
    again = model.grant("root", "admin", "manager")

    assert again == first
    assert list(model.grants) == [first]
    assert model.effective_role("admin", "root") == "manager"
    # A second row would have kept the reader alive behind the manager, so the
    # withdrawal has to leave nothing at all.
    model.revoke(first, "root")
    assert model.effective_role("admin", "root") is None


@pytest.mark.parametrize(
    ("granted_at", "read_at"),
    [
        pytest.param(NODE_C, "c", id="role-on-the-granted-node"),
        pytest.param(NODE_B, "b", id="cached-body-under-the-granted-node"),
    ],
)
def test_a_grant_elsewhere_after_a_revoke_leaves_the_surviving_share_alone(
    granted_at: int, read_at: str
) -> None:
    """The replay: two grants, a revoke, a grant on ``a``, then both invariants.

    With the handle reused, the fourth step overwrote the surviving share in the
    model — the role read back one rung too strong, and ``b``'s interned body
    kept a ``direct`` ACE the model had dropped. Both invariants run here against
    the real ACL on real Postgres, so the test also says which side was right:
    the library's.

    The member's share is the one withdrawn, so the admin's is still standing
    when the fourth grant lands: withdrawing the admin's own share instead would
    take their whole access with it and leave the fourth grant nothing to step
    on.
    """
    machine = AclReferenceModel()
    try:
        machine.build_tree()
        machine.grant(pick=granted_at, who="member", role="reader")
        machine.grant(pick=granted_at, who="admin", role="manager")
        machine.revoke(pick=0, at=granted_at)
        machine.grant(pick=NODE_A, who="admin", role="reader")

        machine.role_equals_the_chain_union()
        machine.the_cache_equals_the_union()

        # The surviving share is the admin's manager one, and it is still where
        # it was granted: nothing the fourth grant did may have moved it. The
        # member's is gone, which is what the revoke was for.
        assert machine.model.effective_role("admin", read_at) == "manager"
        assert machine.model.effective_role("member", read_at) is None
    finally:
        machine.teardown()
