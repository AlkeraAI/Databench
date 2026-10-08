"""A step after a move reads the moved subtree's rows, not what the session kept.

The ACL machine drives every step through ONE session, and a move rewrites the
subtree's ``path_ids`` with a ``text()`` statement the mapper never sees. A
``FileNode`` an earlier step loaded therefore kept its pre-move path, and a
later plain ``select`` handed that instance back: the decider walked the chain
the node had LEFT (``member has None on b; the chain gives 'reader'``) and the
drained rewrite interned that old chain's body. Whether the stale instance was
still in the weak identity map depended on when the garbage collector had last
run, so the machine went red only sometimes — and Hypothesis, replaying a
failure that then passed, reported it as inconsistent data generation.

The product opens a session per request and per worker batch, so the rig now
starts every step from an empty identity map. This test takes the garbage
collector out of it: every node the machine loads is held for the whole run,
the worst case the old rig met only by luck, and both invariants must still
hold after the move.
"""

from __future__ import annotations

from typing import Any

import pytest
from alkera_core.models.files.tree import FileNode
from tests.files.stateful.test_files_stateful_acl import AclReferenceModel

#: ``AclReferenceModel`` indexes ``sorted(self.model.parents)``: a, b, c, root.
NODE_C = 2
#: ...and ``sorted`` of the movable ones: a, b, c.
MOVABLE_A = 0


class _EveryNodeHeld(AclReferenceModel):
    """The machine with no instance it loaded ever collected."""

    def __init__(self) -> None:
        super().__init__()
        self.held: list[FileNode] = []

    async def _node(self, handle: str) -> FileNode:
        node = await super()._node(handle)
        self.held.append(node)
        return node


@pytest.mark.parametrize(
    "invariant",
    [
        pytest.param("role_equals_the_chain_union", id="role-from-the-new-chain"),
        pytest.param("the_cache_equals_the_union", id="cache-from-the-new-chain"),
    ],
)
def test_a_node_moved_under_a_grant_answers_from_where_it_now_hangs(invariant: str) -> None:
    """``a`` moves under ``c``, where the member holds ``reader``, taking ``b``.

    It is ``b`` that goes stale, not ``a``: the move reads the node it moves
    under its lock, fresh, and rewrites every descendant's path in one
    statement no instance hears about.
    """
    machine = _EveryNodeHeld()
    try:
        machine.build_tree()
        machine.grant(pick=NODE_C, who="member", role="reader")
        # Load ``b`` before the move, as the invariants after every step do.
        machine.role_equals_the_chain_union()
        machine.the_cache_equals_the_union()
        assert machine.model.effective_role("member", "b") is None

        machine.move(pick=MOVABLE_A, to=NODE_C)

        assert machine.model.chain("b") == ["root", "c", "a", "b"]
        assert machine.model.effective_role("member", "b") == "reader"
        check: Any = getattr(machine, invariant)
        check()
        # The held list is what makes this the worst case: the pre-move ``b``
        # really was still in reach when the step after the move ran.
        assert any(node.name == b"b" for node in machine.held)
    finally:
        machine.teardown()
