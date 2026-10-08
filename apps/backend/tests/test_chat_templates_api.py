"""Chat templates through the real routes: saving one out of a chat, reading
it, editing it under a version guard, and deleting it.

Three of these deserve a reviewer's attention. The save pins that the copy is
taken from the chat's WORKING DIRECTORY and not from the chat folder above it,
so a template cannot carry the chat's own records — the manifest, the trace
digest and the runtime directory — into somebody else's drive. The edit pins
that reading a template is not permission to rewrite the brief every future
chat is handed. And the object surface pins that a template's title is renamed
where the folder is, not through the generic object route, because a rename
that moved the row and left the folder behind is a rename the next read
disagrees with.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_core.authz import ActingContext
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import acl as files_acl
from alkera_core.files.authz.grants import Principal
from alkera_core.files.clock import SystemClock
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.namespace import Namespace, NodeAttrs
from alkera_core.files.objects_bridge import CHAT_SANDBOX_FOLDER
from alkera_core.models import EventOutbox, User, WorkspaceObject
from alkera_core.models.files.acl import FileShare
from alkera_core.models.files.tree import FileNode
from alkera_core.models.org_audit_event import OrgAuditEvent
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, app_client, login, make_member

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

TEMPLATES = "/api/v1/chat-templates"
#: The chat records a template must never carry: they describe the run, not the
#: work, and the digest in particular is the half most likely to hold something
#: the chat's owner never meant to hand round.
CHAT_RECORDS = (b"manifest.json", b"trace.digest.json", b".runtime")


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": secrets.token_hex(8)}


def _without_trace(body: Any) -> Any:
    """The refusal body with the per-request trace id dropped, so two refusals
    can be compared for the only thing that matters — that they are the same
    answer."""
    error = dict(body["error"])
    error.pop("trace_id", None)
    return {"error": error}


def _code(response: Any) -> str:
    body = response.json()
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("code") or "")
    return str(body.get("code") or "")


async def _create_chat(client: AsyncClient, *, title: str = "Quarterly numbers") -> dict[str, Any]:
    made = await client.post("/api/v1/chats", json={"title": title})
    assert made.status_code == 201, made.text
    return dict(made.json())


async def _node(db: AsyncSession, object_id: uuid.UUID) -> FileNode:
    """The object's own folder. Narrowed to a folder because the derived
    members inside it (a template's README) point back at the same object."""
    return (
        await db.execute(
            select(FileNode).where(
                FileNode.target_object_id == object_id,
                FileNode.kind == "folder",
                FileNode.trashed_at.is_(None),
            )
        )
    ).scalar_one()


async def _children(db: AsyncSession, parent_id: uuid.UUID) -> list[FileNode]:
    return list(
        (
            await db.execute(
                select(FileNode).where(
                    FileNode.parent_id == parent_id, FileNode.trashed_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )


async def _scratch(db: AsyncSession, folder_id: uuid.UUID) -> FileNode:
    assert CHAT_SANDBOX_FOLDER is not None
    found = [row for row in await _children(db, folder_id) if row.name == CHAT_SANDBOX_FOLDER]
    assert found, "the folder has no working directory"
    return found[0]


async def _seed_working_files(db: AsyncSession, owner: User, chat_node: FileNode) -> list[bytes]:
    """Put a file and a nested folder in the chat's working directory, and a
    record beside it — so the copy has something to carry AND something it must
    leave behind."""
    ctx = ActingContext.for_user(user_id=owner.id, org_id=owner.home_org_team_id, email=owner.email)
    async with team_service.files_transaction(db, ctx) as repo:
        scratch = await _scratch(repo.session, uuid.UUID(str(chat_node.id)))
        namespace = Namespace(repo, ctx, SystemClock())
        await namespace.create(
            DriveId(scratch.drive_id),
            NodeId(scratch.id),
            "file",
            b"findings.md",
            attrs=NodeAttrs(mode=0o644),
        )
        nested = await namespace.create(
            DriveId(scratch.drive_id),
            NodeId(scratch.id),
            "folder",
            b"data",
            attrs=NodeAttrs(mode=0o755),
        )
        await namespace.create(
            DriveId(nested.drive_id),
            NodeId(nested.id),
            "file",
            b"rows.csv",
            attrs=NodeAttrs(mode=0o644),
        )
        for record in CHAT_RECORDS:
            await namespace.create(
                DriveId(chat_node.drive_id),
                NodeId(chat_node.id),
                "folder" if record == b".runtime" else "file",
                record,
                attrs=NodeAttrs(mode=0o644),
            )
    await db.commit()
    return [b"findings.md", b"data"]


async def _share(db: AsyncSession, *, node: FileNode, owner: User, user: User, role: str) -> None:
    """Grant ``user`` a rung on ``node``, the way the share dialog does.

    Written here rather than reused from the chat helper because a template's
    object id is carried by its README as well as by its folder, so a grant
    resolved from the object id alone would not know which node it meant.
    """
    ctx = ActingContext.for_user(user_id=owner.id, org_id=owner.home_org_team_id, email=owner.email)
    async with team_service.files_transaction(db, ctx) as repo:
        live = await repo.session.get(FileNode, node.id)
        assert live is not None
        await files_acl.grant(repo, ctx, live, Principal(kind="user", id=user.id), role)
    await db.commit()


async def _descendant_names(db: AsyncSession, root: FileNode) -> set[bytes]:
    """Every name under ``root``, walked child by child."""
    names: set[bytes] = set()
    frontier = [uuid.UUID(str(root.id))]
    while frontier:
        parent = frontier.pop()
        for row in await _children(db, parent):
            names.add(row.name)
            frontier.append(uuid.UUID(str(row.id)))
    return names


async def _save(client: AsyncClient, chat_id: str, **body: Any) -> Any:
    return await client.post(TEMPLATES, json={"source_chat_id": chat_id, **body}, headers=_idem())


async def _decisions(org_id: uuid.UUID, entity: str) -> list[EventOutbox]:
    """The ``authz.decision`` rows this surface filed, in the order they landed."""
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == entity,
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


async def test_saving_a_chat_copies_its_working_files_and_writes_the_brief(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The whole point of the route: the template's folder holds copies of what
    the chat was working on — new ids, the same names, the nesting kept — and
    the row carries the digest, the chat's pin and how far the transcript had
    got. The README the folder renders quotes the brief verbatim, because that
    is what a reader who opens the folder in Files is handed."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_node = await _node(real_session, uuid.UUID(chat["id"]))
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    await _seed_working_files(real_session, owner, chat_node)

    saved = await _save(client, chat["id"], brief="Run it for the next quarter")

    assert saved.status_code == 201, saved.text
    body = saved.json()
    assert body["brief"] == "Run it for the next quarter"
    assert body["title"] == "Quarterly numbers"
    assert body["source_chat_id"] == chat["id"]
    assert body["files_node_id"] is not None
    template_node = await _node(real_session, uuid.UUID(body["id"]))
    scratch = await _scratch(real_session, uuid.UUID(str(template_node.id)))
    copied = {row.name: row for row in await _children(real_session, uuid.UUID(str(scratch.id)))}
    assert set(copied) == {b"findings.md", b"data"}
    source_scratch = await _scratch(real_session, uuid.UUID(str(chat_node.id)))
    source = {
        row.name: row for row in await _children(real_session, uuid.UUID(str(source_scratch.id)))
    }
    assert copied[b"findings.md"].id != source[b"findings.md"].id
    nested = await _children(real_session, uuid.UUID(str(copied[b"data"].id)))
    assert [row.name for row in nested] == [b"rows.csv"]


async def test_a_template_never_carries_the_chats_own_records(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The copy is taken from the working directory, so the manifest, the trace
    digest and the runtime directory beside it are not reachable from the plan
    at all. A template that carried them would hand a colleague the chat's
    transcript metadata along with its files."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_node = await _node(real_session, uuid.UUID(chat["id"]))
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    await _seed_working_files(real_session, owner, chat_node)

    saved = await _save(client, chat["id"])
    assert saved.status_code == 201, saved.text

    template_node = await _node(real_session, uuid.UUID(saved.json()["id"]))
    names = await _descendant_names(real_session, template_node)
    assert names, "the template folder is empty"
    assert not any(record in names for record in CHAT_RECORDS)
    # ...and the copy really did carry the working files, so the assertion above
    # is not passing because nothing was copied at all.
    assert b"findings.md" in names


async def test_the_brief_defaults_to_the_digest_of_the_conversation(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """Nobody wants to answer "what was this chat for?" twice. With no brief the
    server writes one from the transcript, headed with the chat it came from."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client, title="Revenue by region")
    chat_node = await _node(real_session, uuid.UUID(chat["id"]))
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    await _seed_working_files(real_session, owner, chat_node)

    saved = await _save(client, chat["id"])

    assert saved.status_code == 201, saved.text
    assert "Revenue by region" in saved.json()["brief"]


async def test_a_chat_with_no_working_directory_is_refused_by_name(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """There is nothing to copy, and an empty template would misdescribe the
    conversation it claims to come from — so the caller is told which fact is
    missing rather than handed a template of nothing."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_node = await _node(real_session, uuid.UUID(chat["id"]))
    scratch = await _scratch(real_session, uuid.UUID(str(chat_node.id)))
    scratch.trashed_at = datetime.now(UTC)
    await real_session.commit()

    refused = await _save(client, chat["id"])

    assert refused.status_code == 422, refused.text
    assert _code(refused) == "chat.no_working_directory"


async def test_a_chat_the_caller_cannot_read_is_an_opaque_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """A chat is private until it is shared, and naming one you cannot read
    must tell you nothing about whether it is there: the refusal is byte-equal
    to the one an id nobody holds gets."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)

    async with app_client() as other:
        await login(other, member.email, password)
        refused = await _save(other, chat["id"])
        absent = await _save(other, str(uuid.uuid4()))

    assert refused.status_code == 404, refused.text
    assert absent.status_code == 404, absent.text
    assert _without_trace(refused.json()) == _without_trace(absent.json())


async def test_a_reader_may_save_a_shared_chat_into_their_own_drive(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """Reading the chat and copying its files is all a save takes: the template
    is the reader's, in the reader's own folder, and nothing of the source's
    sharing comes with it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_node = await _node(real_session, uuid.UUID(chat["id"]))
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    await _seed_working_files(real_session, owner, chat_node)
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await _share(real_session, node=chat_node, owner=owner, user=member, role="reader")

    async with app_client() as other:
        await login(other, member.email, password)
        saved = await _save(other, chat["id"])

    assert saved.status_code == 201, saved.text
    assert saved.json()["owner_user_id"] == str(member.id)


async def test_the_read_of_a_template_is_on_record(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """Every decision this surface makes is filed, under the template's own
    resource type rather than the generic object one — so an auditor can tell a
    template read from a result read without re-deriving it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_node = await _node(real_session, uuid.UUID(chat["id"]))
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    await _seed_working_files(real_session, owner, chat_node)
    saved = await _save(client, chat["id"])
    assert saved.status_code == 201, saved.text

    read = await client.get(f"{TEMPLATES}/{saved.json()['id']}")

    assert read.status_code == 200, read.text
    rows = await _decisions(org_admin.org_id, "chat_template")
    assert [row.payload["action"] for row in rows].count("read") >= 1
    assert {row.payload["effect"] for row in rows} == {"allow"}
    assert all(row.payload["policy"] == "chat_template.access" for row in rows)


async def test_editing_the_brief_takes_the_edit_rung_not_merely_a_read(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The brief speaks in the author's name to every chat started from the
    template, so a reader rewriting it would be putting words in their mouth.
    The refusal is a plain 403: they can already see it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_node = await _node(real_session, uuid.UUID(chat["id"]))
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    await _seed_working_files(real_session, owner, chat_node)
    saved = await _save(client, chat["id"])
    assert saved.status_code == 201, saved.text
    template_id = saved.json()["id"]
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    template_node = await _node(real_session, uuid.UUID(template_id))
    await _share(real_session, node=template_node, owner=owner, user=member, role="reader")

    async with app_client() as other:
        await login(other, member.email, password)
        refused = await other.put(
            f"{TEMPLATES}/{template_id}", json={"brief": "mine now", "expected_version": 1}
        )

    assert refused.status_code == 403, refused.text
    assert _code(refused) == "chat_template.write_rung_required"


async def test_an_edit_that_did_not_read_the_row_is_a_conflict(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """``expected_version`` is compared under the row's lock, so a stale number
    is refused rather than silently overwriting somebody else's edit."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_node = await _node(real_session, uuid.UUID(chat["id"]))
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    await _seed_working_files(real_session, owner, chat_node)
    saved = await _save(client, chat["id"])
    assert saved.status_code == 201, saved.text
    template_id = saved.json()["id"]

    first = await client.put(
        f"{TEMPLATES}/{template_id}", json={"brief": "first", "expected_version": 1}
    )
    stale = await client.put(
        f"{TEMPLATES}/{template_id}", json={"brief": "second", "expected_version": 1}
    )

    assert first.status_code == 200, first.text
    assert first.json()["brief"] == "first"
    assert stale.status_code == 409, stale.text
    assert _code(stale) == "version_conflict"


async def test_a_reader_cannot_delete_the_template_everyone_starts_from(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """Deleting takes it away from everyone it was shared with, so it stays the
    owner's and the org admins'. The owner's own delete tombstones the row and
    the template stops being readable."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_node = await _node(real_session, uuid.UUID(chat["id"]))
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    await _seed_working_files(real_session, owner, chat_node)
    saved = await _save(client, chat["id"])
    assert saved.status_code == 201, saved.text
    template_id = saved.json()["id"]
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    template_node = await _node(real_session, uuid.UUID(template_id))
    await _share(real_session, node=template_node, owner=owner, user=member, role="manager")

    async with app_client() as other:
        await login(other, member.email, password)
        refused = await other.delete(f"{TEMPLATES}/{template_id}")
    removed = await client.delete(f"{TEMPLATES}/{template_id}")
    gone = await client.get(f"{TEMPLATES}/{template_id}")

    assert refused.status_code == 403, refused.text
    assert removed.status_code == 204, removed.text
    assert gone.status_code == 404, gone.text


async def test_the_listing_is_cut_to_what_the_caller_may_read(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """A page of templates is cut by the same predicate the policy decides
    through — and a saved template is on its owner's page."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_node = await _node(real_session, uuid.UUID(chat["id"]))
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    await _seed_working_files(real_session, owner, chat_node)
    saved = await _save(client, chat["id"])
    assert saved.status_code == 201, saved.text

    listed = await client.get(TEMPLATES)

    assert listed.status_code == 200, listed.text
    ids = [row["id"] for row in listed.json()["items"]]
    assert saved.json()["id"] in ids
    assert all(row["files_node_id"] is not None for row in listed.json()["items"])


async def _direct_grants(db: AsyncSession, node: FileNode) -> list[FileShare]:
    """The live ``file_shares`` rows on ``node`` itself — what the share dialog
    lists without "via a parent folder"."""
    rows = await db.execute(
        select(FileShare).where(FileShare.node_id == node.id, FileShare.revoked_at.is_(None))
    )
    return list(rows.scalars().all())


async def _folder_the_org_reads(db: AsyncSession, owner: User, beside: FileNode) -> FileNode:
    """A folder next to ``beside`` whose org-wide "Can view" is a deliberate share."""
    ctx = ActingContext.for_user(user_id=owner.id, org_id=owner.home_org_team_id, email=owner.email)
    async with team_service.files_transaction(db, ctx) as repo:
        assert beside.parent_id is not None
        folder = await Namespace(repo, ctx, SystemClock()).create(
            DriveId(beside.drive_id),
            NodeId(beside.parent_id),
            "folder",
            b"Team shelf",
            attrs=NodeAttrs(mode=0o755),
        )
        live = await repo.session.get(FileNode, folder.id)
        assert live is not None
        await files_acl.grant(
            repo, ctx, live, Principal(kind="org", id=owner.home_org_team_id), "reader"
        )
    await db.commit()
    return folder


async def test_a_saved_template_is_the_owners_alone(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """Saving writes no grant the saver did not ask for: the template's folder
    carries no direct org or team rung, a colleague is told it does not exist,
    and the refusal is on record as a private object outside their audience."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_node = await _node(real_session, uuid.UUID(chat["id"]))
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    await _seed_working_files(real_session, owner, chat_node)
    saved = await _save(client, chat["id"])
    assert saved.status_code == 201, saved.text
    template_id = saved.json()["id"]
    template_node = await _node(real_session, uuid.UUID(template_id))

    assert await _direct_grants(real_session, template_node) == []
    template = await real_session.get(WorkspaceObject, uuid.UUID(template_id))
    assert template is not None
    assert template.visibility_scope == "private"

    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    async with app_client() as other:
        await login(other, member.email, password)
        read = await other.get(f"{TEMPLATES}/{template_id}")
        listed = await other.get(TEMPLATES)
    assert read.status_code == 404, read.text
    assert listed.status_code == 200, listed.text
    assert template_id not in [row["id"] for row in listed.json()["items"]]
    denied = [
        row
        for row in await _decisions(org_admin.org_id, "chat_template")
        if row.payload["effect"] == "deny" and row.payload["action"] == "read"
    ]
    assert denied, "the colleague's refused read was not filed"
    assert {row.payload["reason"] for row in denied} == {"not_in_audience"}
    assert all(row.payload["attrs"]["visibility_scope"] == "private" for row in denied)


async def test_a_template_saved_into_a_shared_folder_inherits_and_adds_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """A template saved into a folder the org already reads is read through
    that folder — the share dialog's "via a parent folder" — and the save
    writes no direct grant of its own on top of it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_node = await _node(real_session, uuid.UUID(chat["id"]))
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    await _seed_working_files(real_session, owner, chat_node)
    shelf = await _folder_the_org_reads(real_session, owner, chat_node)

    saved = await _save(client, chat["id"], destination_id=str(shelf.id))
    assert saved.status_code == 201, saved.text
    template_id = saved.json()["id"]
    template_node = await _node(real_session, uuid.UUID(template_id))
    assert template_node.parent_id == shelf.id

    assert await _direct_grants(real_session, template_node) == []
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    async with app_client() as other:
        await login(other, member.email, password)
        read = await other.get(f"{TEMPLATES}/{template_id}")
    assert read.status_code == 200, read.text
    allowed = [
        row
        for row in await _decisions(org_admin.org_id, "chat_template")
        if row.payload["action"] == "read" and row.payload["resource"]["id"] == template_id
    ]
    assert allowed, "the colleague's read was not filed"
    assert allowed[-1].payload["effect"] == "allow"
    assert allowed[-1].payload["reason"] == "shared_reads"


async def test_a_replayed_save_makes_one_template(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """A client retrying after a commit-ambiguous timeout must not end up with
    two copies of the same folder charged to its drive."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_node = await _node(real_session, uuid.UUID(chat["id"]))
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    await _seed_working_files(real_session, owner, chat_node)
    key = _idem()

    first = await client.post(
        TEMPLATES, json={"source_chat_id": chat["id"], "brief": "once"}, headers=key
    )
    replay = await client.post(
        TEMPLATES, json={"source_chat_id": chat["id"], "brief": "once"}, headers=key
    )

    assert first.status_code == 201, first.text
    assert replay.status_code == 201, replay.text
    assert first.json()["id"] == replay.json()["id"]


async def test_a_templates_title_and_spec_are_not_edited_through_the_object_route(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """A template's title names its folder in the drive, so renaming it here
    would move the row and leave the folder behind; its spec is not a document a
    client writes at all. Both refusals name the field, because the caller is
    allowed to know which surface owns it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_node = await _node(real_session, uuid.UUID(chat["id"]))
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    await _seed_working_files(real_session, owner, chat_node)
    saved = await _save(client, chat["id"])
    assert saved.status_code == 201, saved.text
    template_id = saved.json()["id"]

    renamed = await client.put(
        f"/api/v1/objects/{template_id}", json={"title": "Renamed", "expected_version": 1}
    )
    respecced = await client.put(
        f"/api/v1/objects/{template_id}", json={"spec": {"brief": "x"}, "expected_version": 1}
    )

    assert renamed.status_code == 422, renamed.text
    assert _code(renamed) == "title_not_editable_here"
    assert respecced.status_code == 422, respecced.text
    assert _code(respecced) == "spec_not_editable_here"


async def test_the_save_records_the_org_audit_row_from_inside_the_files_role(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The route's foreign statements run as the platform, not as the tenant.

    ``@idempotent_route`` runs the whole handler inside the Files claim's
    transaction, which is stamped ``alkera_files_app`` — a role that by design
    can read neither ``teams`` (the role resolver's ancestor walk) nor
    ``workspace_objects`` (the chat row), and cannot write ``org_audit_events``
    at all. Without the windows that step out of the role for exactly those
    statements the save is a 500 before it ever reaches the tree, so this pins
    the three of them at once: the caller's roles resolved, the chat read, and
    the org's audit row written for the template that was made.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_node = await _node(real_session, uuid.UUID(chat["id"]))
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    await _seed_working_files(real_session, owner, chat_node)

    # The premise: the role the handler's transaction runs as cannot touch any
    # of the three tables the save needs. If a GRANT ever widened it, this test
    # would stop proving that the windows are what makes the save work.
    async with AsyncSessionLocal() as probe:
        await probe.execute(text("SET LOCAL ROLE alkera_files_app"))
        for table in ("teams", "workspace_objects", "org_audit_events"):
            with pytest.raises(ProgrammingError):
                await probe.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
            await probe.rollback()
            await probe.execute(text("SET LOCAL ROLE alkera_files_app"))
        await probe.rollback()

    saved = await _save(client, chat["id"], brief="Run it for the next quarter")

    assert saved.status_code == 201, saved.text
    template_id = saved.json()["id"]
    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(OrgAuditEvent).where(
                        OrgAuditEvent.org_team_id == org_admin.org_id,
                        OrgAuditEvent.action == "chat.template_saved",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [row.target for row in rows] == [template_id], (
        "the save's audit row is written as the platform; under the Files role the "
        "INSERT into org_audit_events is refused and the whole request 500s"
    )
    assert rows[0].detail["source_chat_id"] == chat["id"]
