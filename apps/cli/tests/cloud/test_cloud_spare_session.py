"""A chat made ahead of its owner's first message starts no agent until that
message, and the message then runs on the one session the box opens for it:
every hop real.

A spare is a chat the backend creates ahead of its owner's first send (its
row, its folder and its placement are ready, so the send skips that work). It
is nobody's chat until the send claims it, its box's included: the box does
not list it, so no agent server starts for a chat nobody has written in. The
owner's first send claims the row and posts the message; the box then lists
the chat, opens one session and runs the message on it. The browser's warm
and claim go through the real routes, the box reads the rows the way its
discovery does, and the fake harness records every session opened and every
prompt sent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_core.db.session import AsyncSessionLocal
from test_cloud_round3d_picks_seams import (
    PROVIDER,
    _box_rest,
    _box_runtime,
    _mirror_from_row,
    _post,
    _wait_for,
)
from tests._compute_helpers import make_grant, make_machine_type
from tests.conftest import OrgWithAdmin, login, served_client
from tests.test_chat_machine_binding_seam import _heartbeat, _register

pytestmark = pytest.mark.asyncio


async def _box_up(org: OrgWithAdmin) -> str:
    """An org box registered and beating, the way the daemon does it, so the
    backend has a live machine to place the spare on."""
    async with AsyncSessionLocal() as session:
        machine_type = await make_machine_type(session)
        await make_grant(session, org_team_id=org.org_id, machine_type_id=machine_type.id)
        code = machine_type.provider_type_id
    machine_id = await _register(org, code, "pod-spare-session")
    await _heartbeat(org, machine_id)
    return machine_id


async def test_a_spare_starts_no_agent_until_its_first_message_which_runs_on_one_session(
    uvicorn_server: str, org_admin: OrgWithAdmin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _box_up(org_admin)
    async with served_client(uvicorn_server) as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        warmed = await browser.post("/api/v1/chats/spare")
        assert warmed.status_code == 200, warmed.text
        assert warmed.json()["state"] == "warm"
        listed = await browser.get("/api/v1/chats")
        assert listed.json()["items"] == [], "a person never lists the spare"

        # The box's discovery does not see it either: nothing to start.
        runtime, factory = _box_runtime(tmp_path, monkeypatch)
        rest = await _box_rest(uvicorn_server, org_admin, "warming-box")
        assert (await rest.list_chats())["items"] == [], "no agent for an unclaimed spare"

        claimed = await browser.post("/api/v1/chats", json={"claim_spare": True})
        assert claimed.status_code == 201, claimed.text
        chat_id = claimed.json()["id"]
        await _post(browser, chat_id, "what is 6*7?", "web-1")

        rows = (await rest.list_chats())["items"]
        assert [str(row["id"]) for row in rows] == [chat_id], "the claimed chat is the box's"
        async with _mirror_from_row(
            row=rows[0],
            runtime=runtime,
            rest=rest,
            org_admin=org_admin,
            directory_already_there=True,
        ) as mirror:
            assert mirror.state == "running"
            adapter = factory.adapters[0]
            await _wait_for(lambda: bool(adapter.sent_prompts), seconds=30.0)
            assert len(factory.adapters) == 1, "one session, opened for the message"
            assert factory.configs[0].model is not None
            assert factory.configs[0].model["provider_id"] == PROVIDER
            assert "6*7" in str(adapter.sent_prompts[0].text)
