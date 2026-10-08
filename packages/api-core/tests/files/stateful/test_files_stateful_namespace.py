"""The namespace, driven against its reference model on real Postgres.

Every rule applies the same operation to :class:`NamespaceModel` and to the real
``Namespace``/``Trash`` on a drive of its own, then the invariants re-read the
whole tree from the rows and compare it to the model — names byte for byte,
parents, trashed state and the etag of every node the last write touched. The
refusals are held to the same bar: a rule that the contract says must fail
asserts that *both* sides refused with the *same* code, so a service that
accepts a cycle, a duplicate live sibling or a stale ``If-Match`` fails here
even though nothing crashed.
"""

from __future__ import annotations

import uuid
from typing import Any

from alkera_core.files.errors import FilesError
from alkera_core.files.ids import NodeId, TrashOpId
from alkera_core.files.namespace import Namespace
from alkera_core.files.trash import Trash
from alkera_core.models.files.tree import FileNode
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule
from hypothesis.strategies import booleans, integers, sampled_from
from sqlalchemy import Row, select
from tests.files.stateful._models import ModelRefusal, NamespaceModel
from tests.files.stateful.conftest import StatefulRig, acting_context, open_rig, refusal_code

#: Small enough that collisions and conflict renames happen constantly, and
#: byte-exact rather than casefolded so ``README`` and ``readme`` both appear.
NAMES = [b"a", b"b", b"README", b"readme", b"caf\xc3\xa9", b"\xff\xfe"]


class NamespaceReferenceModel(RuleBasedStateMachine):
    """Contract-equivalence for create / rename / move / trash / restore."""

    def __init__(self) -> None:
        super().__init__()
        self.rig: StatefulRig = open_rig(label="ns-model")
        self.model = NamespaceModel()
        # model handle -> real node id, and the reverse for the tree comparison.
        self.ids: dict[str, uuid.UUID] = {}
        self.handles: dict[uuid.UUID, str] = {}
        self.trash_ops: dict[str, uuid.UUID] = {}

    @initialize()
    def bind_root(self) -> None:
        self.ids[self.model.root] = self.rig.root_id
        self.handles[self.rig.root_id] = self.model.root
        # The drive root is seeded by the drive, not by a namespace write, so
        # its starting version is the drive's, not the model's default.
        self._resync_etags()

    # ---- the services under test -----------------------------------------

    def _namespace(self) -> Namespace:
        return Namespace(self.rig.repo, acting_context(), self.rig.clock, None)

    def _trash(self) -> Trash:
        return Trash(self.rig.repo, acting_context(), self.rig.clock, None)

    def _folders(self) -> list[str]:
        return sorted(key for key, node in self.model.live().items() if node.kind == "folder")

    def _movable(self) -> list[str]:
        return sorted(key for key in self.model.live() if key != self.model.root)

    # ---- rules -----------------------------------------------------------

    @rule(
        parent=integers(min_value=0),
        name=sampled_from(NAMES),
        folder=booleans(),
        rename_on_conflict=booleans(),
    )
    def create(self, parent: int, name: bytes, folder: bool, rename_on_conflict: bool) -> None:
        folders = self._folders()
        chosen = folders[parent % len(folders)]
        kind = "folder" if folder else "file"
        conflict = "rename" if rename_on_conflict else "fail"

        def apply() -> Any:
            return self.rig.run(self._create(chosen, kind, name, conflict))

        expected: str | None
        try:
            handle = self.model.create(chosen, kind, name, conflict=conflict)
        except ModelRefusal as refusal:
            expected = refusal.code
            self._expect_refusal(apply, expected)
            return
        made = apply()
        assert made.name == self.model.nodes[handle].name, (
            f"the service named it {made.name!r}, the contract says "
            f"{self.model.nodes[handle].name!r}"
        )
        assert made.etag == 1, f"a fresh node starts at etag 1, not {made.etag}"
        self.ids[handle] = made.id
        self.handles[made.id] = handle

    async def _create(self, parent: str, kind: str, name: bytes, conflict: str) -> FileNode:
        async with self.rig.repo.transaction():
            return await self._namespace().create(
                self.rig.drive_id,
                NodeId(self.ids[parent]),
                kind,
                name,
                conflict=conflict,  # type: ignore[arg-type]
            )

    @precondition(lambda self: bool(self._movable()))
    @rule(
        pick=integers(min_value=0),
        name=sampled_from(NAMES),
        stale=booleans(),
        rename_on_conflict=booleans(),
    )
    def rename(self, pick: int, name: bytes, stale: bool, rename_on_conflict: bool) -> None:
        movable = self._movable()
        handle = movable[pick % len(movable)]
        if_match = self.model.nodes[handle].etag - (1 if stale else 0)
        conflict = "rename" if rename_on_conflict else "fail"

        def apply() -> Any:
            return self.rig.run(self._rename(handle, name, if_match, conflict))

        try:
            expected_name = self.model.rename(handle, name, if_match=if_match, conflict=conflict)
        except ModelRefusal as refusal:
            self._expect_refusal(apply, refusal.code)
            return
        renamed = apply()
        assert renamed.name == expected_name, f"{renamed.name!r} vs {expected_name!r}"
        assert renamed.etag == self.model.nodes[handle].etag

    async def _rename(self, handle: str, name: bytes, if_match: int, conflict: str) -> FileNode:
        async with self.rig.repo.transaction():
            return await self._namespace().rename(
                NodeId(self.ids[handle]),
                name,
                if_match=if_match,
                conflict=conflict,  # type: ignore[arg-type]
            )

    @precondition(lambda self: bool(self._movable()))
    @rule(
        pick=integers(min_value=0),
        into=integers(min_value=0),
        stale=booleans(),
        rename_on_conflict=booleans(),
    )
    def move(self, pick: int, into: int, stale: bool, rename_on_conflict: bool) -> None:
        movable = self._movable()
        handle = movable[pick % len(movable)]
        # Every live node is a candidate parent — that is what generates the
        # cycles and the "a file may not hold children" refusals.
        candidates = sorted(self.model.live())
        parent = candidates[into % len(candidates)]
        if_match = self.model.nodes[handle].etag - (1 if stale else 0)
        conflict = "rename" if rename_on_conflict else "fail"

        def apply() -> Any:
            return self.rig.run(self._move(handle, parent, if_match, conflict))

        try:
            self.model.move(handle, parent, if_match=if_match, conflict=conflict)
        except ModelRefusal as refusal:
            self._expect_refusal(apply, refusal.code)
            return
        moved = apply()
        assert moved.parent_id == self.ids[parent]
        assert moved.name == self.model.nodes[handle].name
        assert moved.etag == self.model.nodes[handle].etag

    async def _move(self, handle: str, parent: str, if_match: int, conflict: str) -> FileNode:
        async with self.rig.repo.transaction():
            return await self._namespace().move(
                NodeId(self.ids[handle]),
                NodeId(self.ids[parent]),
                if_match=if_match,
                conflict=conflict,  # type: ignore[arg-type]
            )

    @precondition(lambda self: bool(self._movable()))
    @rule(pick=integers(min_value=0), stale=booleans())
    def trash(self, pick: int, stale: bool) -> None:
        movable = self._movable()
        handle = movable[pick % len(movable)]
        if_match = self.model.nodes[handle].etag - (1 if stale else 0)

        def apply() -> Any:
            return self.rig.run(self._trash_node(handle, if_match))

        try:
            op = self.model.trash(handle, if_match=if_match)
        except ModelRefusal as refusal:
            self._expect_refusal(apply, refusal.code)
            return
        self.trash_ops[op] = apply().id
        self._resync_etags()

    async def _trash_node(self, handle: str, if_match: int) -> Any:
        async with self.rig.repo.transaction():
            return await self._trash().trash(NodeId(self.ids[handle]), if_match=if_match)

    @precondition(lambda self: bool(self.trash_ops))
    @rule(pick=integers(min_value=0))
    def restore(self, pick: int) -> None:
        ops = sorted(self.trash_ops)
        op = ops[pick % len(ops)]
        expected_name = self.model.restore(op)
        restored = self.rig.run(self._restore(self.trash_ops.pop(op)))
        assert restored.name == expected_name, (
            f"restore named it {restored.name!r}; the contract says {expected_name!r}"
        )
        self._resync_etags()

    async def _restore(self, op_id: uuid.UUID) -> FileNode:
        async with self.rig.repo.transaction():
            return await self._trash().restore(TrashOpId(op_id))

    # ---- invariants ------------------------------------------------------

    @invariant()
    def the_tree_equals_the_model(self) -> None:
        rows = self.rig.run(self._read_tree())
        real = {
            self.handles[row.id]: (
                row.name,
                self.handles[row.parent_id] if row.parent_id is not None else None,
                row.kind,
                row.trashed_at is not None,
            )
            for row in rows
            if row.id in self.handles
        }
        expected = {
            key: (node.name, node.parent, node.kind, node.trash_op is not None)
            for key, node in self.model.nodes.items()
        }
        assert real == expected, f"tree diverged: rows {real} vs model {expected}"

    @invariant()
    def every_etag_matches_the_model(self) -> None:
        rows = self.rig.run(self._read_tree())
        real = {self.handles[row.id]: row.etag for row in rows if row.id in self.handles}
        expected = {key: node.etag for key, node in self.model.nodes.items()}
        assert real == expected, f"etags diverged: rows {real} vs model {expected}"

    @invariant()
    def live_siblings_are_byte_unique(self) -> None:
        rows = self.rig.run(self._read_tree())
        seen: set[tuple[uuid.UUID | None, bytes]] = set()
        for row in rows:
            if row.trashed_at is not None:
                continue
            key = (row.parent_id, row.name)
            assert key not in seen, f"two live siblings named {row.name!r}"
            seen.add(key)

    async def _read_tree(self) -> list[Row]:
        """Read the tree as plain columns.

        Deliberately not ORM entities: the session's identity map keeps the
        attributes it already loaded, so a ``select(FileNode)`` would hand the
        invariant the pre-trash ``trashed_at`` and the pre-rename ``etag`` and
        pass while the rows on disk disagreed.
        """
        async with self.rig.repo.transaction() as repo:
            result = await repo.execute_scoped(
                select(
                    FileNode.id,
                    FileNode.name,
                    FileNode.parent_id,
                    FileNode.kind,
                    FileNode.trashed_at,
                    FileNode.etag,
                ).where(FileNode.drive_id == self.rig.drive_uuid)
            )
            return list(result.all())

    # ---- helpers ---------------------------------------------------------

    def _expect_refusal(self, apply: Any, expected: str) -> None:
        try:
            apply()
        except FilesError as error:
            assert refusal_code(error) == expected, (
                f"the service refused with {refusal_code(error)}; the contract requires {expected}"
            )
            return
        raise AssertionError(f"the service accepted what the contract refuses with {expected}")

    def _resync_etags(self) -> None:
        rows = self.rig.run(self._read_tree())
        self.model.resync_etags(
            {self.handles[row.id]: row.etag for row in rows if row.id in self.handles}
        )

    def teardown(self) -> None:
        self.rig.close()


TestNamespaceReferenceModel = NamespaceReferenceModel.TestCase
TestNamespaceReferenceModel.settings = hypothesis_settings(  # type: ignore[attr-defined]
    max_examples=30,
    stateful_step_count=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    print_blob=True,
)
