"""A tab from before the draft was renamed keeps working beside a current one.

A web or VS Code build from before ``chat_workspace`` became ``chat_draft``
subscribes to ``doc:chat_workspace:<chat>`` and sends envelopes under that
type. The socket maps both to the current name on the way in and back on the
way out, so the old tab is served exactly as it always was, and it edits the
SAME stored draft a current tab does: two people, one on each build, type into
one draft and both end with the same text.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import CrdtDoc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import create_app
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, serve_controlled
from tests.crdt.crdt_client import LaneClient
from tests.crdt.crdt_world import make_world
from tests.test_ws_gateway import logged_in

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


async def _stored(doc_id: str) -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(CrdtDoc.doc_type, CrdtDoc.projection).where(CrdtDoc.doc_id == doc_id)
        )
        return [(str(doc_type), str(projection.get("text"))) for doc_type, projection in rows]


async def test_an_old_tab_and_a_new_tab_share_one_draft(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    current = world.ref.channel
    assert current.startswith("doc:chat_draft:")
    legacy = current.replace("doc:chat_draft:", "doc:chat_workspace:", 1)
    async with AsyncExitStack() as stack:
        server = await stack.enter_async_context(serve_controlled(create_app()))
        owner = await logged_in(world.owner.user.email, world.owner.password)
        writer = await logged_in(world.writer.user.email, world.writer.password)
        stack.push_async_callback(owner.aclose)
        stack.push_async_callback(writer.aclose)
        old = LaneClient(owner, legacy, "old")
        new = LaneClient(writer, current, "new")
        for tab in (old, new):
            await tab.connect(server.addr)
            stack.push_async_callback(tab.disconnect)
            # The tab drops every frame not addressed to the channel it named,
            # so an old tab answered under the new name never syncs at all.
            await tab.wait_synced()

        await old.type_token("[from-the-old-build]")
        await new.type_token("[from-the-new-build]")
        for tab in (old, new):
            await tab.settle()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20
        while not (
            "[from-the-new-build]" in old.text
            and "[from-the-old-build]" in new.text
            and old.text == new.text
        ):
            assert loop.time() < deadline, (old.text, new.text, old.trace[-6:])
            for tab in (old, new):
                await tab.hello()
            await asyncio.sleep(0.2)

        # One document, stored under the name drafts have always had: never a
        # second one for either spelling.
        assert await _stored(world.ref.doc_id) == [("chat_workspace", old.text)]
        # Everything the old tab heard named the channel the way it said it.
        named = {
            frame["channel"] for frame in old.received if isinstance(frame.get("channel"), str)
        } | {
            f"doc:{frame['envelope']['doc_type']}:{frame['envelope']['doc_id']}"
            for frame in old.received
            if frame.get("t") == "doc"
        }
        assert named == {legacy}, named
        assert any(frame.get("t") == "doc" for frame in old.received)
        assert not old.errors and not new.errors, (old.errors, new.errors)
