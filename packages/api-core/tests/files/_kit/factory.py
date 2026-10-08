"""Seed data the Files fixtures and tests share.

Kept out of ``conftest`` on purpose: see the package docstring.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from alkera_core.files.ids import OrgScope
from alkera_core.files.path_labels import ino_label
from alkera_core.models._enums import TeamRole
from alkera_core.models.files.acl import FileShare
from alkera_core.models.files.stores import DedupDomain, FileDrive, FileStore
from alkera_core.models.files.tree import FileNode
from alkera_core.models.team import Team
from alkera_core.models.team_membership import TeamMembership
from alkera_core.models.user import User
from sqlalchemy.ext.asyncio import AsyncSession

#: Every Files test that needs a wall clock starts here, so a test that crosses
#: a day, TTL or week boundary states the crossing in its own body.
EPOCH = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FilesOrg:
    """One seeded tenant: its org root team and two real members."""

    org_team_id: uuid.UUID
    admin_id: uuid.UUID
    member_id: uuid.UUID

    @property
    def scope(self) -> OrgScope:
        return OrgScope(org_team_id=self.org_team_id)


async def _seed_org(session: AsyncSession) -> FilesOrg:
    """Seed a root team plus an admin and a member, committed.

    Written straight against the ORM rather than through the backend's
    ``membership_service``: api-core cannot import ``backend``, and these rows
    only have to be *valid* — the Files tests never exercise the membership
    invariants, they exercise what a valid principal id makes possible.
    """
    org = Team(id=uuid.uuid4(), parent_team_id=None, name=f"files-org-{uuid.uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    made: list[uuid.UUID] = []
    for role in (TeamRole.ADMIN, TeamRole.MEMBER):
        user = User(
            id=uuid.uuid4(),
            home_org_team_id=org.id,
            email=f"{role.value}-{uuid.uuid4().hex[:12]}@files.test",
            email_domain="files.test",
            first_name=role.value.title(),
            last_name="Tester",
        )
        session.add(user)
        await session.flush()
        session.add(TeamMembership(user_id=user.id, team_id=org.id, role=role))
        made.append(user.id)
    await session.commit()
    return FilesOrg(org_team_id=org.id, admin_id=made[0], member_id=made[1])


class FilesFactory:
    """Valid Files rows, built the way the product builds them.

    Every row satisfies the table's constraints — a drive has a store, a dedup
    domain and a traversal-only root whose ``path_ids`` is its own label; a tree
    node's ``path_ids`` is its parent's chain plus its own label and its depth
    matches — so a test that seeds through the factory is testing the repo, not
    its own hand-rolled setup.
    """

    def __init__(self, session: AsyncSession, default_org: FilesOrg) -> None:
        self._session = session
        self._default_org = default_org

    async def drive(self, *, org: FilesOrg | None = None) -> FileDrive:
        owner = org or self._default_org
        store = FileStore(
            id=uuid.uuid4(),
            driver="filesystem",
            bucket="",
            endpoint=f"/tmp/files-test/{uuid.uuid4().hex}",
            region="",
            capabilities={},
            transfer_modes=["single", "proxied"],
        )
        self._session.add(store)
        await self._session.flush()
        domain = DedupDomain(
            id=uuid.uuid4(),
            org_team_id=owner.org_team_id,
            region="",
            store_id=store.id,
        )
        self._session.add(domain)
        await self._session.flush()
        drive = FileDrive(
            id=uuid.uuid4(),
            org_team_id=owner.org_team_id,
            kind="org",
            store_id=store.id,
            dedup_domain_id=domain.id,
            quota_bytes=1 << 40,
            quota_nodes=1_000_000,
            next_ino=2,
        )
        self._session.add(drive)
        await self._session.flush()
        root_id = uuid.uuid4()
        root = FileNode(
            id=root_id,
            ino=1,
            drive_id=drive.id,
            org_team_id=owner.org_team_id,
            parent_id=None,
            kind="folder",
            name=b"",
            name_display="",
            name_key="",
            path_ids=ino_label(1),
            depth=0,
            # A plain folder, unlike the product's own root. This rig's root
            # stands for "the top folder of a drive" in tests about the
            # namespace, the trash, leases and operations — none of which are
            # about the skeleton — and a traversal-only container takes no
            # direct write at all, so nothing could be built under it. The real
            # root's rule is pinned where the real skeleton is built, in
            # `test_files_drives.py` and the backend's route tests.
            traversal_only=False,
        )
        self._session.add(root)
        await self._session.flush()
        drive.root_node_id = root_id
        await self._session.commit()
        return drive

    async def tree(self, spec: str, *, drive: FileDrive) -> dict[str, FileNode]:
        """Seed a tree from a whitespace-separated path spec.

        ``"a/ b/ b/c.txt"`` makes folder ``a``, folder ``b`` and file ``b/c.txt``;
        a trailing ``/`` is what marks a folder. Parents must be listed before
        their children — the spec is read in order, so a missing parent is a
        ``KeyError`` naming it rather than a silently orphaned row.
        """
        assert drive.root_node_id is not None
        root = await self._session.get(FileNode, drive.root_node_id)
        assert root is not None
        made: dict[str, FileNode] = {}
        for raw in spec.split():
            is_folder = raw.endswith("/")
            path = raw.rstrip("/")
            parent_path, _, leaf = path.rpartition("/")
            parent = made[parent_path] if parent_path else root
            node_id = uuid.uuid4()
            ino = drive.next_ino
            node = FileNode(
                id=node_id,
                ino=ino,
                drive_id=drive.id,
                org_team_id=drive.org_team_id,
                parent_id=parent.id,
                kind="folder" if is_folder else "file",
                name=leaf.encode(),
                name_display=leaf,
                name_key=leaf.casefold(),
                path_ids=f"{parent.path_ids}.{ino_label(ino)}",
                depth=parent.depth + 1,
            )
            drive.next_ino += 1
            self._session.add(node)
            made[path] = node
        await self._session.commit()
        return made

    async def grant(
        self,
        node: FileNode,
        principal_kind: str,
        principal_id: uuid.UUID,
        role: str,
    ) -> FileShare:
        share = FileShare(
            id=uuid.uuid4(),
            org_team_id=node.org_team_id,
            node_id=node.id,
            principal_kind=principal_kind,
            principal_id=principal_id,
            role=role,
            granted_by=principal_id,
        )
        self._session.add(share)
        await self._session.commit()
        return share


__all__ = ["EPOCH", "FilesFactory", "FilesOrg", "_seed_org"]
