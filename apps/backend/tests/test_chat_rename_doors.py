"""Renaming a chat, and reading one, through the object router.

``PUT /api/v1/objects/{id}`` is the only rename a chat has, and ``GET
/api/v1/objects/{id}`` is a second door onto the same row that ``GET
/api/v1/chats/{id}`` already answers for. Both used to decide through the
workspace-object policy, which asks about team scope and org admins and knows
nothing about the share on the chat's node — so the ladder came out inverted:
the colleague holding "Can edit", and even "Full access", was told the chat did
not exist, while an org admin nobody had shared it with read its title, its
pinned model and its whole spec, and renamed it.

Both doors now decide through ``chat.access``, so the answer cannot depend on
which one the caller knocked on. The tests below assert the status contract AND
the ``authz.decision`` row — the allow in the request's own transaction, the
deny in a committed session of its own — for every rung of the ladder.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from alkera_core.authz.policies import chat as chat_policy
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.authz.ladder import (
    ROLE_COMMENTER,
    ROLE_MANAGER,
    ROLE_READER,
    ROLE_WRITER,
)
from alkera_core.models import EventOutbox, User, WorkspaceObject
from httpx import AsyncClient
from sqlalchemy import select
from tests.chat_shares import files_on, share_chat  # noqa: F401
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


async def _decisions(chat_id: UUID, action: str) -> list[tuple[str, str]]:
    """Every decision filed about this chat for ``action``, oldest first."""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(EventOutbox)
                .where(
                    EventOutbox.type == "authz.decision",
                    EventOutbox.entity_id == str(chat_id),
                )
                .order_by(EventOutbox.id)
            )
        ).scalars()
    return [
        (row.payload["effect"], row.payload["reason"])
        for row in rows
        if row.payload["action"] == action
    ]


async def _owned_chat(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> tuple[dict[str, Any], User, str]:
    """A chat owned by an ordinary member, never shared with anybody."""
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    await login(client, member.email, password)
    created = await client.post("/api/v1/chats", json={"title": "Quarterly review"})
    assert created.status_code == 201, created.text
    body: dict[str, Any] = created.json()
    return body, member, password


async def _version(real_session: Any, chat_id: UUID) -> int:
    """The version the rename has to name. A chat's own read model does not
    publish it — the object router is where a chat is versioned — so the test
    reads the row rather than inventing a number the route would conflict on."""
    row = await real_session.get(WorkspaceObject, chat_id)
    await real_session.refresh(row)
    version: int = row.version
    return version


def _rename(client: AsyncClient, chat_id: str, version: int) -> Any:
    return client.put(
        f"/api/v1/objects/{chat_id}",
        json={"title": "Renamed", "expected_version": version},
    )


@pytest.mark.parametrize(
    ("role", "status", "effect", "reason"),
    [
        pytest.param(
            None,
            404,
            "deny",
            "not_in_audience",
            id="a-member-the-chat-is-not-shared-with",
        ),
        pytest.param(ROLE_READER, 403, "deny", "rename_rung_required", id="shared-can-view"),
        pytest.param(ROLE_COMMENTER, 403, "deny", "rename_rung_required", id="shared-can-comment"),
        pytest.param(ROLE_WRITER, 200, "allow", "writer_may_rename", id="shared-can-edit"),
        pytest.param(ROLE_MANAGER, 200, "allow", "writer_may_rename", id="shared-full-access"),
    ],
)
async def test_the_rung_decides_who_may_rename_and_the_decision_is_on_record(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: Any,
    role: str | None,
    status: int,
    effect: str,
    reason: str,
) -> None:
    chat, owner, _ = await _owned_chat(client, org_admin, real_session)
    chat_id = UUID(chat["id"])
    chat_row = await real_session.get(WorkspaceObject, chat_id)
    colleague, colleague_password = await make_member(
        real_session, org_id=org_admin.org_id, verified=True
    )
    assert colleague_password is not None
    if role is not None:
        await share_chat(real_session, chat=chat_row, owner=owner, user=colleague, role=role)
    await login(client, colleague.email, colleague_password)

    response = await _rename(client, chat["id"], await _version(real_session, UUID(chat["id"])))
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json()["title"] == "Renamed"
    if status == 403:
        assert response.json()["error"]["code"] == chat_policy.RENAME_DENIED_CODE
    assert await _decisions(chat_id, "rename") == [(effect, reason)]


async def test_a_can_edit_colleagues_rename_is_what_the_owner_then_reads(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The rename is a real edit, not a decision that stops at the policy: the
    owner's own door shows the new title."""
    chat, owner, owner_password = await _owned_chat(client, org_admin, real_session)
    chat_row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    colleague, colleague_password = await make_member(
        real_session, org_id=org_admin.org_id, verified=True
    )
    assert colleague_password is not None
    await share_chat(real_session, chat=chat_row, owner=owner, user=colleague, role=ROLE_WRITER)
    await login(client, colleague.email, colleague_password)
    version = await _version(real_session, UUID(chat["id"]))
    assert (await _rename(client, chat["id"], version)).status_code == 200

    await login(client, owner.email, owner_password)
    read = await client.get(f"/api/v1/chats/{chat['id']}")
    assert read.status_code == 200
    assert read.json()["title"] == "Renamed"


async def test_the_owner_renames_their_own_chat_with_no_share(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    chat, _owner, _password = await _owned_chat(client, org_admin, real_session)
    response = await _rename(client, chat["id"], await _version(real_session, UUID(chat["id"])))
    assert response.status_code == 200, response.text
    assert await _decisions(UUID(chat["id"]), "rename") == [("allow", "writer_may_rename")]


async def test_an_unshared_org_admin_gets_the_same_not_found_on_both_doors(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The two-doors bug, in one test. The admin is refused on the chat route
    and must be refused identically on the object route: same status, same
    body, and nothing of the chat — not its title, not its owner, not its spec
    — in either answer. Both refusals are on record as the chat policy's.
    """
    chat, _owner, _password = await _owned_chat(client, org_admin, real_session)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    through_chats = await client.get(f"/api/v1/chats/{chat['id']}")
    through_objects = await client.get(f"/api/v1/objects/{chat['id']}")
    assert through_chats.status_code == 404
    assert through_objects.status_code == 404
    assert through_objects.json()["error"]["code"] == through_chats.json()["error"]["code"]
    assert "Quarterly review" not in through_objects.text

    renamed = await _rename(client, chat["id"], await _version(real_session, UUID(chat["id"])))
    assert renamed.status_code == 404, renamed.text

    chat_id = UUID(chat["id"])
    assert await _decisions(chat_id, "read") == [("deny", "not_in_audience")] * 2
    assert await _decisions(chat_id, "rename") == [("deny", "not_in_audience")]


async def test_a_shared_reader_reads_the_chat_through_the_object_door_too(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The ladder decides the object door in both directions: the rung that
    opens ``/chats/{id}`` opens ``/objects/{id}``, and its decision is filed
    under the chat policy rather than the object one."""
    chat, owner, _ = await _owned_chat(client, org_admin, real_session)
    chat_row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    colleague, colleague_password = await make_member(
        real_session, org_id=org_admin.org_id, verified=True
    )
    assert colleague_password is not None
    await share_chat(real_session, chat=chat_row, owner=owner, user=colleague, role=ROLE_READER)
    await login(client, colleague.email, colleague_password)

    response = await client.get(f"/api/v1/objects/{chat['id']}")
    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Quarterly review"
    assert await _decisions(UUID(chat["id"]), "read") == [("allow", "shared_reads")]
