"""Workspace-object and Files seeds for the cross-tenant matrix.

Per org: a promoted result still waiting for its payload (the state the upload
routes accept), and a Files tree the product's own drive skeleton holds: a
folder under ``/Shared``, a file with a committed version, a share on the
folder, an open upload session, an operation and a settled conflict. Every row
names its org explicitly; the person is only the author.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from alkera_core.models import WorkspaceObject
from alkera_core.models.files.acl import FileShare
from alkera_core.models.files.history import FileConflict
from alkera_core.models.files.ops import FileOp
from alkera_core.models.files.uploads import FileUploadSession
from tests.files._files_kit import FilesFixtures

if TYPE_CHECKING:
    from route_matrix import Side, TwoOrgWorld
    from sqlalchemy.ext.asyncio import AsyncSession


async def seed(session: AsyncSession, world: TwoOrgWorld, side: Side) -> dict[str, str]:
    obj = WorkspaceObject(
        id=uuid.uuid4(),
        org_team_id=side.org_id,
        logical_id=f"xt-{uuid.uuid4().hex[:12]}",
        type="result",
        title="quarterly numbers",
        status="pending_upload",
        spec={},
        owner_user_id=side.member.id,
        visibility_scope="org",
        content_updated_at=time.time(),
    )
    session.add(obj)
    await session.commit()

    fx = FilesFixtures(session, side.org_id, side.member.id)
    drive = await fx.drive()
    folder = await fx.node(b"budgets", kind="folder", parent=await fx.shared())
    doc = await fx.node(b"memo.txt", kind="file", parent=folder)
    head = await fx.version(doc)
    copy = await fx.node(b"memo (conflicted copy).txt", kind="file", parent=folder)
    share = FileShare(
        id=uuid.uuid4(),
        org_team_id=side.org_id,
        node_id=folder.id,
        principal_kind="user",
        principal_id=side.member.id,
        role="reader",
        granted_by=side.admin.id,
    )
    upload = FileUploadSession(
        id=uuid.uuid4(),
        org_team_id=side.org_id,
        drive_id=drive.id,
        parent_id=folder.id,
        name=b"incoming.csv",
        dedup_domain_id=drive.dedup_domain_id,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    operation = FileOp(
        id=uuid.uuid4(),
        org_team_id=side.org_id,
        drive_id=drive.id,
        kind="move",
        actor=str(side.member.id),
    )
    conflict = FileConflict(
        id=uuid.uuid4(),
        org_team_id=side.org_id,
        node_id=doc.id,
        base_version_id=head.id,
        theirs_version_id=head.id,
        mine_version_id=head.id,
        actor=side.member.id,
        state="auto",
        copy_node_id=copy.id,
        arrived_from="holder",
        who="a box",
        displaced_by="a writer",
    )
    session.add_all([share, upload, operation, conflict])
    await session.commit()
    return {
        "object": str(obj.id),
        "drive": str(drive.id),
        "folder": str(folder.id),
        "file": str(doc.id),
        "version": str(head.id),
        "share": str(share.id),
        "upload_session": str(upload.id),
        "operation": str(operation.id),
        "conflict": str(conflict.id),
    }
