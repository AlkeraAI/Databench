"""Round-3 seams: every hop real, nothing hand-built on either side.

Round 2 found five producer/consumer mismatches that each side's own tests hid
behind a fixture typed by hand to look like the other side. These cases refuse
that shortcut: the browser's real REST calls, the real backend (uvicorn or
ASGI), the real docsync persist, the real ``ChatMirror`` on a real
``HarnessRuntime``, and the real tool registry — and, where the consumer is
TypeScript, the served bytes are recorded to a fixture the browser's own suite
reads back (``packages/api-core/tests/fixtures/objects/seam/``).

Set ``SEAM_FIXTURE_WRITE=1`` to re-record the fixtures from a real run.

The saved-query re-run case that lived here is retired: a query is no longer
an object this workspace saves and ``POST /objects/{id}/rerun`` went with it;
a chat template holding the SQL is what replaced it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import ChatMirror, CloudRestClient, CloudSocket
from alkera_cli.cloud.refusal import REFUSAL_COPY, quote_statement
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    QuestionOption,
    QuestionPrompt,
    QuestionRequest,
    ToolCall,
    ToolCallUpdate,
)
from alkera_core.schemas.objects import ChatPromptRecord
from backend.app_factory import process_app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, app_client, login, mint_cli_token, served_client

fastapi_app = process_app()

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parents[4]
SEAM_FIXTURES = REPO_ROOT / "packages/api-core/tests/fixtures/objects/seam"
#: The saved query the browser's real builder emits (pinned on the browser side
#: by ``querySpecSeam.test.ts``), the one the re-run route and the machine read.
QUERY_SPEC_FROM_BROWSER = SEAM_FIXTURES / "query_spec_from_browser.json"
#: The transcript page the server serves for a chat the machine answered — the
#: bytes ``CloudDataSource.getChatTurns`` folds on a cold open.
CHAT_PAGE_FROM_SERVER = SEAM_FIXTURES / "chat_messages_page_from_server.json"
#: The three entries a refusal note is — the words are Python's, the notice is
#: the browser's (``refusalNotice.test.ts`` reads these back).
REFUSAL_NOTE_ENTRIES = SEAM_FIXTURES / "refusal_note_entries.json"

#: A statement the classifier can read AND that mutates, so the refusal is the
#: read-only one rather than the "couldn't prove it" one.
REFUSED_STATEMENT = "delete from prompts where customer = 'acme'"

_T = datetime(2026, 9, 6, tzinfo=UTC)
WAIT = 10.0


# --------------------------------------------------------------------------- #
# helpers (kept local so this module stands on its own)
# --------------------------------------------------------------------------- #


async def _no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


async def _wait_for(predicate: Callable[[], bool], *, seconds: float = WAIT) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def _device_token(org_admin: OrgWithAdmin) -> str:
    return await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )


def _fake_runtime(
    tmp_path: Path, make: Callable[[], FakeAdapter] = FakeAdapter
) -> tuple[HarnessRuntime, FakeAdapterFactory]:
    factory = FakeAdapterFactory(make, available=True)
    return HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory), factory


@contextlib.asynccontextmanager
async def _publishing_mirror(
    *,
    chat_id: str,
    runtime: HarnessRuntime,
    rest: CloudRestClient,
    org_admin: OrgWithAdmin,
) -> AsyncIterator[ChatMirror]:
    """The box's mirror for ``chat_id``, publishing through the real gateway."""
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


async def _create_chat(browser: AsyncClient, title: str) -> str:
    created = await browser.post("/api/v1/chats", json={"title": title})
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _page(browser: AsyncClient, chat_id: str) -> dict[str, Any]:
    page = await browser.get(f"/api/v1/chats/{chat_id}/messages")
    assert page.status_code == 200, page.text
    body: dict[str, Any] = page.json()
    return body


async def _ask_is_on_the_page(browser: AsyncClient, chat_id: str, request_id: str) -> bool:
    """Whether the server's transcript already holds the ask ``request_id``."""
    for item in (await _page(browser, chat_id))["items"]:
        entry = item["payload"].get("payload")
        if isinstance(entry, dict) and entry.get("request_id") == request_id:
            return True
    return False


async def _wait_until_answerable(browser: AsyncClient, chat_id: str, request_id: str) -> None:
    """Wait until answering ``request_id`` is something the route can take.

    An ask is parked on the machine the moment the harness raises it, and it
    reaches the server only when the box publishes it. ``POST /chats/{id}/answer``
    reads the transcript, so the machine's own pending list says nothing about
    whether an answer will be accepted — a browser learns of an ask FROM the
    server in the first place, which makes the published row the readiness
    signal a reader actually waits on.
    """
    deadline = asyncio.get_running_loop().time() + WAIT
    while not await _ask_is_on_the_page(browser, chat_id, request_id):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"the ask {request_id!r} never reached the transcript")
        await asyncio.sleep(0.05)


# --------------------------------------------------------------------------- #
# S3-1 · the transcript page the browser folds on a cold open
# --------------------------------------------------------------------------- #


async def test_the_page_a_reopened_chat_reads_carries_the_events_the_browser_unwraps(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin, tmp_path: Path
) -> None:
    """Every hop of the demo's first question, then the page a RELOAD reads.

    The browser posts the question over REST; the server relays it; the real
    mirror hands it to the harness; the machine's answer is published through
    the real gateway; docsync persists it; ``GET /chats/{id}/messages`` pages it
    back. That page is what ``CloudDataSource.getChatTurns`` folds on a cold
    open.

    The row a machine publishes carries the whole transcript ENTRY —
    ``{event_id, role, kind, payload}`` — because that entry is the record the
    daemon's catch-up and the cost roll-ups read; the harness event is one
    level in. So the contract pinned here is the shape the browser has to
    unwrap: every machine row names its harness event at
    ``payload["payload"]["event_type"]``. The served bytes are also recorded
    for the browser's own suite (``CloudDataSource.serverPage.test.ts``), which
    folds them through the real source, so both sides read one artifact.
    """
    async with served_client(uvicorn_server) as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        chat_id = await _create_chat(browser, "Why are prompts down?")

        runtime, factory = _fake_runtime(tmp_path, lambda: FakeAdapter(reply_text="Answer: 42"))
        rest = CloudRestClient(
            api_url=f"http://{uvicorn_server}",
            token=await _device_token(org_admin),
            agent_id=chat_id,
        )
        async with _publishing_mirror(
            chat_id=chat_id, runtime=runtime, rest=rest, org_admin=org_admin
        ):
            posted = await browser.post(
                f"/api/v1/chats/{chat_id}/messages",
                json={"text": "what is 6*7?", "client_id": "web-1"},
            )
            assert posted.status_code == 201, posted.text

            # Before the box has taken anything: the page a reload reads
            # already carries the question. The prompt is the server's own
            # record of what was sent — written when it accepted the message,
            # not when a machine picked the turn up — and it is stored FLAT,
            # so the browser reads the words at `payload["text"]` rather than
            # unwrapping an entry that is not there.
            sent = (await _page(browser, chat_id))["items"][0]
            assert (sent["role"], sent["kind"]) == ("user", "prompt")
            assert sent["event_id"] == "usr:web-1"
            assert sent["payload"]["text"] == "what is 6*7?"
            assert sent["payload"]["client_id"] == "web-1"
            assert "payload" not in sent["payload"], "a person's message is not a machine's entry"

            adapter = factory.adapters[0]
            await _wait_for(lambda: bool(adapter.sent_prompts))

            # The machine goes back to idle AFTER it completes the message, and
            # that idle row is on the page too — so waiting only for
            # ``message.completed`` can read a page one row short of the turn.
            async def _answered() -> bool:
                kinds = [item["kind"] for item in (await _page(browser, chat_id))["items"]]
                return "message.completed" in kinds and kinds[-1] == "session.status_changed"

            deadline = asyncio.get_running_loop().time() + WAIT
            while not await _answered():
                assert asyncio.get_running_loop().time() < deadline, "the answer never landed"
                await asyncio.sleep(0.1)

        page = await _page(browser, chat_id)

    if os.environ.get("SEAM_FIXTURE_WRITE") == "1":
        CHAT_PAGE_FROM_SERVER.write_text(json.dumps(page, indent=2, sort_keys=True) + "\n")

    items = page["items"]
    assert items[0]["role"] == "user"
    machine = [item for item in items if item["role"] != "user"]
    assert machine, "the machine's answer is on the page"
    assert any(
        item["kind"] == "part.created" and json.dumps(item["payload"]).find("Answer: 42") != -1
        for item in machine
    ), "the answer's text is on the page"

    # The browser's contract, spelled where it is read: CloudDataSource's
    # `applyMessages` unwraps the entry (`harnessEventOf`) before folding, and
    # `foldHarnessEvent` returns without folding when `event_type` is missing.
    # So every machine row has to name its event one level in — a row that
    # named it nowhere would render a reopened chat empty.
    no_event_to_fold = [
        (item["seq"], item["kind"], sorted(item["payload"]))
        for item in machine
        if not isinstance(
            (item["payload"].get("payload") or {}).get("event_type")
            if isinstance(item["payload"].get("payload"), dict)
            else item["payload"].get("event_type"),
            str,
        )
    ]
    assert not no_event_to_fold, (
        "a reloaded chat folds nothing from these rows — neither the row's "
        f"payload nor the entry inside it names an event: {no_event_to_fold}"
    )
    # And it is the ENTRY that is stored, not the bare event: the browser's
    # unwrap is load-bearing, so the day the server stores the event flat this
    # test says so rather than the reader finding out.
    assert all(isinstance(item["payload"].get("payload"), dict) for item in machine), (
        "a machine row's payload is the published entry, with the harness event inside it"
    )

    # And the committed artifact the browser suite reads is the shape served now.
    recorded = json.loads(CHAT_PAGE_FROM_SERVER.read_text(encoding="utf-8"))
    served_shape = [(item["role"], item["kind"], sorted(item["payload"])) for item in items]
    recorded_shape = [
        (item["role"], item["kind"], sorted(item["payload"])) for item in recorded["items"]
    ]
    assert served_shape == recorded_shape, "re-record with SEAM_FIXTURE_WRITE=1"


async def test_the_page_a_reopened_chat_reads_carries_the_person_s_own_words() -> None:
    """The reader's question is on the page as a row of its own.

    A person's message is not something a machine published: the server wrote
    it when it accepted the send, and it stands on the page whether or not a
    box ever takes the turn. It is stored FLAT — no entry to unwrap, no
    ``event_type`` — which is exactly why the browser folds it through its own
    path (``foldRelayedPrompt``) rather than through the harness-event fold
    that would drop it. The day this row stops being served flat, a reader who
    reloads before the answer arrives sees an empty chat.
    """
    page = json.loads(CHAT_PAGE_FROM_SERVER.read_text(encoding="utf-8"))
    prompts = [item for item in page["items"] if item["kind"] == "prompt"]

    assert len(prompts) == 1, "the question the reader asked, once"
    prompt = prompts[0]
    assert prompt["role"] == "user"
    assert prompt["event_id"] == f"usr:{prompt['payload']['client_id']}"
    assert prompt["payload"]["text"].strip(), "the words the reader typed"
    assert "event_type" not in prompt["payload"], "a person's message is not a harness event"
    assert "payload" not in prompt["payload"], "and it is not a published entry either"
    # The hidden channel is beside the words, never part of them.
    assert prompt["payload"]["context"] == ""


async def test_a_page_written_before_attachments_existed_still_unwraps() -> None:
    """The served page names ``attachments``, and a page without it still reads.

    ``ChatPromptRecord`` grew ``attachments`` additively at 1.1.0, so the
    recorded page — the bytes the browser suite folds — carries it. That is the
    first half. The asymmetric half is the one a golden re-bless is prone to
    lose: a row a 1.0.0 writer left behind (no ``attachments`` key at all, and
    the old stamp) is still a prompt the reader unwraps — empty attachments,
    every pre-existing field untouched. Chats that predate the field are full
    of those rows; the day the reader stops reading one, a reopened chat loses
    its first question.
    """
    page = json.loads(CHAT_PAGE_FROM_SERVER.read_text(encoding="utf-8"))
    prompt = next(item for item in page["items"] if item["kind"] == "prompt")

    # The additive field is on the page the server serves today.
    assert prompt["payload"]["attachments"] == []
    assert prompt["payload"]["schema_version"] == ChatPromptRecord.SCHEMA_VERSION

    # The same row as a 1.0.0 writer left it: no key, the old stamp.
    old_payload = {k: v for k, v in prompt["payload"].items() if k != "attachments"}
    old_payload["schema_version"] = "1.0.0"
    read = ChatPromptRecord.model_validate(old_payload)

    assert read.attachments == []
    # Every pre-existing field survives the read unchanged.
    for field in ("kind", "text", "client_id", "user_id"):
        assert getattr(read, field) == prompt["payload"][field], field
    # And it is written back at today's version, with the field defaulted in.
    written = read.model_dump(mode="json")
    assert written["schema_version"] == ChatPromptRecord.SCHEMA_VERSION
    assert written["attachments"] == []

    # The other direction: a writer that DOES name attachments keeps them — the
    # default is a floor for old bytes, never a clamp on new ones.
    kept = ChatPromptRecord.model_validate(
        {
            **old_payload,
            "schema_version": ChatPromptRecord.SCHEMA_VERSION,
            "attachments": [
                {
                    "type": "file",
                    "part_id": "p-1",
                    "message_id": "m-1",
                    "filename": "rows.csv",
                    "mime": "text/csv",
                    "node_id": "n1",
                }
            ],
        }
    )
    assert [part.node_id for part in kept.attachments] == ["n1"]


# --------------------------------------------------------------------------- #
# S3-3 · the browser answers an ask through the route, the machine settles it
# --------------------------------------------------------------------------- #


async def test_an_answer_posted_by_the_browser_settles_the_ask_on_the_machine(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin, tmp_path: Path
) -> None:
    """``POST /chats/{id}/answer`` is the browser's only way to answer an ask;
    the mirror's ``_answer_interrupt`` is the only consumer. Round 2 found no
    producer at all; the route exists now, so the two are driven together:
    a question answered, and a permission ask declined, each through the real
    route into the real mirror and out to the harness adapter."""
    async with served_client(uvicorn_server) as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        chat_id = await _create_chat(browser, "asks")
        runtime, factory = _fake_runtime(tmp_path)
        rest = CloudRestClient(
            api_url=f"http://{uvicorn_server}",
            token=await _device_token(org_admin),
            agent_id=chat_id,
        )
        async with _publishing_mirror(
            chat_id=chat_id, runtime=runtime, rest=rest, org_admin=org_admin
        ) as mirror:
            adapter = factory.adapters[0]
            await adapter.feed(
                QuestionRequest(
                    event_id="q-1",
                    time=_T,
                    session_id=chat_id,
                    request_id="q-1",
                    questions=[
                        QuestionPrompt(
                            question="Which region?",
                            options=[QuestionOption(label="eu"), QuestionOption(label="us")],
                        )
                    ],
                )
            )
            await _wait_for(lambda: "q-1" in mirror.pending_interrupts)
            await _wait_until_answerable(browser, chat_id, "q-1")
            answered = await browser.post(
                f"/api/v1/chats/{chat_id}/answer",
                json={"interrupt_id": "q-1", "answers": [["eu"]]},
            )
            assert answered.status_code == 202, answered.text
            await _wait_for(lambda: adapter.question_replies == [("q-1", [["eu"]])])
            assert mirror.pending_interrupts == []

            # A read-only session settles a permission ask itself (a read is
            # auto-allowed, a write auto-refused), so the browser's other answer
            # shape is a declined question — the route's `reject` + `reason`.
            await adapter.feed(
                QuestionRequest(
                    event_id="q-2",
                    time=_T,
                    session_id=chat_id,
                    request_id="q-2",
                    questions=[
                        QuestionPrompt(
                            question="Include drafts?",
                            options=[QuestionOption(label="yes"), QuestionOption(label="no")],
                        )
                    ],
                )
            )
            await _wait_for(lambda: "q-2" in mirror.pending_interrupts)
            await _wait_until_answerable(browser, chat_id, "q-2")
            declined = await browser.post(
                f"/api/v1/chats/{chat_id}/answer",
                json={"interrupt_id": "q-2", "reject": True, "reason": "not now"},
            )
            assert declined.status_code == 202, declined.text
            await _wait_for(lambda: adapter.question_rejects == [("q-2", "not now")])


async def test_an_answer_waits_for_the_machine_s_ask_to_reach_the_transcript(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin, tmp_path: Path
) -> None:
    """``POST /chats/{id}/answer`` answers the TRANSCRIPT's ask, never the
    machine's pending list.

    The harness parks an ask on the box the instant it raises it; the row the
    route reads exists only once the box has published it. A slow uplink puts a
    long way between those two moments, so the publish is held here on purpose:
    the answer that arrives first is refused, the machine goes on holding the
    ask rather than settling it on a refusal, and the same answer sent once the
    row has landed is taken and reaches the harness. A reader never meets the
    refusal — a browser hears of an ask from the server — which is exactly why
    the published row, not the box's own state, is what a caller waits on.
    """
    async with served_client(uvicorn_server) as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        chat_id = await _create_chat(browser, "a held ask")
        runtime, factory = _fake_runtime(tmp_path)
        rest = CloudRestClient(
            api_url=f"http://{uvicorn_server}",
            token=await _device_token(org_admin),
            agent_id=chat_id,
        )
        async with _publishing_mirror(
            chat_id=chat_id, runtime=runtime, rest=rest, org_admin=org_admin
        ) as mirror:
            doc = mirror.doc
            assert doc is not None
            # The box's uplink, stalled on the ask's own row: everything else
            # about the box is real, only the moment the row lands is ours.
            published = asyncio.Event()
            send_op = doc.send_op

            async def hold_the_ask(intent: str, **payload: Any) -> dict[str, Any]:
                if intent == "append" and "q-held" in json.dumps(payload.get("events", [])):
                    await published.wait()
                return await send_op(intent, **payload)

            doc.send_op = hold_the_ask

            adapter = factory.adapters[0]
            await adapter.feed(
                QuestionRequest(
                    event_id="q-held",
                    time=_T,
                    session_id=chat_id,
                    request_id="q-held",
                    questions=[
                        QuestionPrompt(
                            question="Which region?",
                            options=[QuestionOption(label="eu"), QuestionOption(label="us")],
                        )
                    ],
                )
            )
            await _wait_for(lambda: "q-held" in mirror.pending_interrupts)
            assert not await _ask_is_on_the_page(browser, chat_id, "q-held")

            early = await browser.post(
                f"/api/v1/chats/{chat_id}/answer",
                json={"interrupt_id": "q-held", "answers": [["eu"]]},
            )
            assert early.status_code == 404, early.text
            assert early.json()["error"]["code"] == "ask_not_found"
            # Refused, and nothing downstream acted on it: the ask is still the
            # machine's to hold, so the reader can answer it once it is theirs.
            assert adapter.question_replies == []
            assert mirror.pending_interrupts == ["q-held"]

            published.set()
            await _wait_until_answerable(browser, chat_id, "q-held")
            answered = await browser.post(
                f"/api/v1/chats/{chat_id}/answer",
                json={"interrupt_id": "q-held", "answers": [["eu"]]},
            )
            assert answered.status_code == 202, answered.text
            await _wait_for(lambda: adapter.question_replies == [("q-held", [["eu"]])])
            assert mirror.pending_interrupts == []


# --------------------------------------------------------------------------- #
# S3-4 · the machine reports a publishing refusal through the real route
# --------------------------------------------------------------------------- #


async def test_the_box_reports_a_publishing_refusal_the_browser_then_reads(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """``CloudRestClient.report_publisher_state`` (the only caller) against the
    real ``PUT /chats/{id}/publisher-state`` route (the only consumer), speaking
    as the registered machine the chat is bound to; the chat row then reads
    ``refused`` with the reason on ``GET /chats/{id}`` — the row the browser's
    banner is typed against. The gateway suite drives this client at a stub."""
    from tests._compute_helpers import make_grant, make_machine_type

    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    booting = CloudRestClient(
        api_url="http://testserver",
        token=await _device_token(org_admin),
        agent_id="booting",
        transport=ASGITransport(app=fastapi_app),
    )
    registered = await booting.register_machine(
        name="demo-box",
        provider=machine_type.provider,
        provider_pod_id=f"pod-{uuid4().hex[:8]}",
        machine_type_code=machine_type.provider_type_id,
    )
    machine_id = str(registered["id"])
    box = booting.for_agent(machine_id)

    async with app_client(base_url="http://testserver", timeout=60.0) as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        chat_id = await _create_chat(browser, "refused")
        bound = await browser.get(f"/api/v1/chats/{chat_id}")
        assert bound.json()["machine_id"] == machine_id

        refused = await box.for_agent(machine_id).report_publisher_state(
            chat_id, state="refused", reason="forbidden: not this chat's publisher"
        )
        assert refused["machine_status"] == "refused"
        seen = (await browser.get(f"/api/v1/chats/{chat_id}")).json()
        assert seen["machine_status"] == "refused"
        assert seen["machine_refusal_reason"] == "forbidden: not this chat's publisher"

        cleared = await box.report_publisher_state(chat_id, state="publishing")
        assert cleared["machine_status"] != "refused"
        assert (await browser.get(f"/api/v1/chats/{chat_id}")).json()[
            "machine_refusal_reason"
        ] is None


# --------------------------------------------------------------------------- #
# S3-2 · a query the browser saved is re-run on the connection it named
# --------------------------------------------------------------------------- #

CONNECTION_ID = "8c4e3d6f-5031-4d5e-a08f-a4d6f2c3e444"
CONNECTION_HANDLE = "Tideline Postgres"


async def test_the_refusal_the_machine_writes_is_the_notice_the_browser_reads(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin, tmp_path: Path
) -> None:
    """A cloud chat refuses a write, in words, on the transcript.

    The wording is Python's (``refusal.py``), the rendering is TypeScript's
    (``harnessEventFold``'s system notice), and until now only the live suite
    crossed between them — a hard assertion that runs on a real stack against a
    real model, nowhere near a pull request. So the three entries the mirror
    publishes for a refusal are recorded here from a real run and read back by
    ``refusalNotice.test.ts``: rename the copy or drop the ``synthetic`` flag
    and one of the two sides goes red for free.
    """
    async with served_client(uvicorn_server) as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        chat_id = await _create_chat(browser, "a write the workspace will not run")
        runtime, factory = _fake_runtime(tmp_path)
        rest = CloudRestClient(
            api_url=f"http://{uvicorn_server}",
            token=await _device_token(org_admin),
            agent_id=chat_id,
        )
        async with _publishing_mirror(
            chat_id=chat_id, runtime=runtime, rest=rest, org_admin=org_admin
        ):
            adapter = factory.adapters[0]
            # The gate refuses the statement inside the tool; the arguments
            # arrived on the call before it, which is what the watch remembers.
            await adapter.feed(
                ToolCall(
                    event_id="call-1",
                    time=_T,
                    session_id=chat_id,
                    tool_call_id="t1",
                    message_id="m1",
                    tool_name="sql.query",
                    input={"connection": "warehouse", "sql": REFUSED_STATEMENT},
                    status="running",
                )
            )
            await adapter.feed(
                ToolCallUpdate(
                    event_id="call-1-done",
                    time=_T,
                    session_id=chat_id,
                    tool_call_id="t1",
                    status="error",
                    error_text="permission denied: this workspace is read-only",
                )
            )

            async def _noted() -> bool:
                items = (await _page(browser, chat_id))["items"]
                return any(
                    REFUSAL_COPY["read_only_workspace"] in json.dumps(item["payload"])
                    for item in items
                )

            deadline = asyncio.get_running_loop().time() + WAIT
            while not await _noted():
                assert asyncio.get_running_loop().time() < deadline, "the refusal was never said"
                await asyncio.sleep(0.1)

        items = (await _page(browser, chat_id))["items"]

    # The note is a system message, one synthetic text part, and its completion.
    note_id = next(
        item["payload"]["payload"]["message_id"]
        for item in items
        if item["payload"]["payload"].get("event_type") == "message.created"
        and item["payload"]["payload"].get("role") == "system"
    )
    entries = [
        item["payload"]
        for item in items
        if item["payload"]["payload"].get("message_id") == note_id
        or (item["payload"]["payload"].get("part") or {}).get("message_id") == note_id
    ]
    if os.environ.get("SEAM_FIXTURE_WRITE") == "1":
        REFUSAL_NOTE_ENTRIES.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n")

    kinds = [entry["kind"] for entry in entries]
    assert kinds == ["message.created", "part.created", "message.completed"], kinds
    part = entries[1]["payload"]["part"]
    assert part["type"] == "text"
    # The browser renders a system-authored text part as a NOTICE — plain text,
    # never markdown — and only because it is flagged synthetic.
    assert part["synthetic"] is True
    assert part["text"].startswith(REFUSAL_COPY["read_only_workspace"])
    assert quote_statement(REFUSED_STATEMENT) in part["text"]

    recorded = json.loads(REFUSAL_NOTE_ENTRIES.read_text(encoding="utf-8"))
    assert [entry["kind"] for entry in recorded] == kinds, "re-record with SEAM_FIXTURE_WRITE=1"
    assert recorded[1]["payload"]["part"]["text"] == part["text"], (
        "the wording the browser is tested against is not the wording the machine writes"
    )


@pytest.mark.parametrize(
    ("code", "message"),
    [
        pytest.param("not_publisher", "chat is served by machine m-2", id="known-code"),
        pytest.param("forbidden", "only the document's owner may write it", id="forbidden"),
        pytest.param("teapot", "", id="code-nobody-has-copy-for"),
    ],
)
@pytest.mark.anyio
async def test_a_mid_turn_refusal_reaches_the_banner_as_a_sentence(code: str, message: str) -> None:
    """The banner is read by whoever asked the question. A refusal code is the
    publishing service's word for a fault, so what lands on the chat is the
    sentence for that code -- and a code nobody wrote copy for still gets a
    sentence, never the raw pair."""
    from alkera_cli.cloud.publisher_identity import PublishingRefusal
    from alkera_cli.cloud.service import CloudMirrorService
    from alkera_cli.cloud.start_failures import REFUSAL_SENTENCES, UNKNOWN_REFUSAL_SENTENCE

    said: list[tuple[str, str, str]] = []
    kinds: list[str | None] = []
    service = object.__new__(CloudMirrorService)
    service._refused = {}  # type: ignore[attr-defined]

    async def report(
        chat_id: str, state: str, reason: str = "", *, kind: str | None = None
    ) -> None:
        said.append((chat_id, state, reason))
        kinds.append(kind)

    service.report_publisher_state = report  # type: ignore[method-assign]
    refusal = PublishingRefusal(code=code, message=message)
    await service._on_mirror_refused("c1", refusal)

    assert said == [("c1", "refused", REFUSAL_SENTENCES.get(code, UNKNOWN_REFUSAL_SENTENCE))]
    assert kinds[0] is not None, "the reader is shown a kind, never the sentence"
    on_the_banner = said[0][2]
    assert code not in on_the_banner
    assert ":" not in on_the_banner
    assert on_the_banner.endswith(".")
