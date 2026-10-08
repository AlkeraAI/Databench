"""Round-3c seams: the promote half of the Tier-2 chain, both sides real.

The chain used to open on "the browser saves a query and re-runs it", but a
saved query is no longer a kind this workspace mints and the re-run route was
deleted with it — a chat template is what a reader saves a chat as now — so the
case that began at ``POST /objects`` with ``type: "query"`` is retired rather
than re-pointed; the surviving hops it shared with the case below (the mirror's
promote, the payload upload, and what the browser reads back) are that case's.

The served bytes at the browser-facing end are recorded to
``packages/api-core/tests/fixtures/objects/seam/`` for the browser's own suite.
Set ``SEAM_FIXTURE_WRITE=1`` to re-record them from a real run.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import ChatMirror, CloudRestClient, CloudSocket
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.plugins.plugin_base.delivery import PREVIEW_ROW_CAP
from alkera_core.project.directory import ProjectDirectory
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, login, mint_cli_token, served_client

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parents[4]
SEAM_FIXTURES = REPO_ROOT / "packages/api-core/tests/fixtures/objects/seam"
#: The saved query the browser's real builder emits (pinned by
#: ``querySpecSeam.test.ts``; read by the server-side seam test).
QUERY_SPEC_FROM_BROWSER = SEAM_FIXTURES / "query_spec_from_browser.json"
#: The chart the browser's real builder emits (pinned by ``chartSpecSeam.test.ts``).
CHART_SPEC_FROM_BROWSER = SEAM_FIXTURES / "chart_spec_from_browser.json"
CHART_SPEC_PERSISTED = SEAM_FIXTURES / "chart_spec_persisted.json"
#: What this run leaves at the browser-facing ends, for ``tier2ChainFromRoutes.test.tsx``.
TIER2_CHAIN = SEAM_FIXTURES / "tier2_chain_from_routes.json"

#: The cloud id the saved query names — the leased connection's record id.
CONNECTION_ID = "8c4e3d6f-5031-4d5e-a08f-a4d6f2c3e444"
CONNECTION_HANDLE = "Tideline Postgres"
#: More rows than the preview cap, so the result spills to a blob on the
#: machine and the promote has to read the FULL rows back from that blob —
#: the C6 shape (preview + handle on the transcript, rows in the object).
ROW_COUNT = PREVIEW_ROW_CAP + 10

WAIT = 15.0


async def _no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


async def _wait_for(predicate: Callable[[], bool], *, seconds: float = WAIT) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def _poll(
    fetch: Callable[[], Any], done: Callable[[Any], bool], *, seconds: float = WAIT, what: str
) -> Any:
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        value = await fetch()
        if done(value):
            return value
        assert asyncio.get_running_loop().time() < deadline, f"{what} never happened"
        await asyncio.sleep(0.1)


async def _device_token(org_admin: OrgWithAdmin) -> str:
    return await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )


@contextlib.asynccontextmanager
async def _publishing_mirror(
    *, chat_id: str, runtime: HarnessRuntime, rest: CloudRestClient, org_admin: OrgWithAdmin
) -> AsyncIterator[ChatMirror]:
    """The box's mirror for ``chat_id``, publishing through the real gateway
    and reading objects through the real routes."""
    socket = CloudSocket(rest, sleep=_no_sleep)
    await socket.start()
    mirror = ChatMirror(
        chat_id=chat_id,
        runtime=runtime,
        socket=socket,
        rest=rest,
        user_id=str(org_admin.admin_id),
        owner_user_id=str(org_admin.admin_id),
        chunk_interval=0.05,
    )
    await mirror.start()
    try:
        assert mirror.doc is not None and mirror.doc.live.is_set()
        yield mirror
    finally:
        await mirror.stop()
        await socket.stop()
        await runtime.close_all()


async def _page(browser: AsyncClient, chat_id: str) -> dict[str, Any]:
    page = await browser.get(f"/api/v1/chats/{chat_id}/messages")
    assert page.status_code == 200, page.text
    body: dict[str, Any] = page.json()
    return body


def _result_of(output: Any) -> dict[str, Any]:
    """The tool's result, wherever the transport put it (the browser's
    ``readResult`` does the same)."""
    if not isinstance(output, dict):
        return {}
    inner = output.get("result")
    return inner if isinstance(inner, dict) else output


def _tool_result_entry(page: dict[str, Any], call_id: str) -> dict[str, Any] | None:
    for item in page["items"]:
        event = (item.get("payload") or {}).get("payload") or {}
        part = event.get("part") if isinstance(event, dict) else None
        if isinstance(part, dict) and part.get("call_id") == call_id:
            return item
    return None


def _shape(value: Any) -> Any:
    """The key structure of a JSON document, values erased — what a
    re-recording has to agree on before the browser suite trusts it."""
    if isinstance(value, dict):
        return {key: _shape(child) for key, child in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return type(value).__name__


async def test_a_promote_the_machine_cannot_honour_fails_the_object_the_browser_reads(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin, tmp_path: Path
) -> None:
    """The other exit of the promote seam: ``CloudRestClient.fail_payload`` →
    the real ``POST /objects/{id}/payload/failed`` → ``status: failed`` with
    the reason on ``GET /objects/{id}`` — the row the object page's "could not
    be saved" line reads. The only prior crossing was a fake objects app."""
    async with served_client(uvicorn_server) as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        created = await browser.post("/api/v1/chats", json={"title": "nothing to save"})
        assert created.status_code == 201, created.text
        chat_id = str(created.json()["id"])
        runtime = HarnessRuntime(
            ProjectDirectory(tmp_path / ".alkera"),
            adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
        )
        rest = CloudRestClient(
            api_url=f"http://{uvicorn_server}",
            token=await _device_token(org_admin),
            agent_id=chat_id,
        )
        async with _publishing_mirror(
            chat_id=chat_id, runtime=runtime, rest=rest, org_admin=org_admin
        ):
            promoted = await browser.post(
                f"/api/v1/chats/{chat_id}/promote",
                json={
                    "event_id": "prt_never_ran",
                    "title": "Ghost",
                    "columns": [],
                    "chart_spec": None,
                },
            )
            assert promoted.status_code == 201, promoted.text
            object_id = str(promoted.json()["id"])
            obj = await _poll(
                lambda: browser.get(f"/api/v1/objects/{object_id}"),
                lambda response: response.json()["status"] != "pending_upload",
                what="the machine refusing the promote",
            )
        record = obj.json()
        assert record["status"] == "failed"
        assert record["spec"]["failure_reason"] == "no tool result with event id prt_never_ran"
        # A failed result has no rows and no file: the routes say so, they do
        # not answer with an empty document.
        assert (await browser.get(f"/api/v1/objects/{object_id}/rows")).status_code == 409
        assert (await browser.get(f"/api/v1/objects/{object_id}/export.csv")).status_code == 409
