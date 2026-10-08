"""ONE identity names a tool result across the browser/machine seam.

A reader presses "Save as result" on a card in the browser; the cloud records
the intent and asks THIS machine for the rows behind it. The two sides have to
be naming the same thing, and for a long time they were not: the browser sends
the tool CALL id it reads off the card, and the mirror looked only for a
finalized transcript event with that id — which an agent-run query never
produces (it reaches the transcript as ``tool.call`` + ``tool.call_update``).
Every promote of a real query answered "no tool result with event id prt_…",
the object sat in ``pending_upload`` forever, and the CSV was unreachable.

The identity is the TOOL CALL ID: it is the only id both sides hold. The
transcript event id is still accepted as a fallback, so an object an older
client created under that name is never stranded.

Driven from a RECORDED stream — the same JSON the browser's own fold test
reads — through a real ``ChatSession``, so what is proved is that the machine
resolves what the browser actually sends.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.plugins.plugin_base.result_blob import write_rows_blob
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import Event, PartCreated, ToolCallPart
from pydantic import TypeAdapter

FIXTURES = Path(__file__).parent / "fixtures"
#: The demo's own turn: a Tinybird query the agent ran through `alkera_call_tool`.
RECORDED_SQL_QUERY = FIXTURES / "recorded_sql_query_0f0bec47.json"
#: The tool call id in that recording — the id the browser sends, verbatim.
RECORDED_CALL_ID = "prt_07ad57d1c0016yL2P6MozuMrVf"

CHAT_ID = "0f0bec47-69ea-417c-8850-fe1a22836979"
OWNER = "00000000-0000-4000-8000-000000000001"

_events = TypeAdapter(list[Event])


def recorded(path: Path = RECORDED_SQL_QUERY) -> list[Event]:
    return _events.validate_python(json.loads(path.read_text(encoding="utf-8")))


class _Objects:
    """The cloud's objects API, as far as a promote reaches it."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.uploads: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        object_id = parts[4] if len(parts) > 4 else ""
        tail = "/".join(parts[5:])
        if request.method == "GET" and object_id in self.objects:
            return httpx.Response(200, json=self.objects[object_id])
        if request.method == "POST" and object_id in self.objects:
            body = json.loads(request.content or b"{}")
            if tail == "payload/failed":
                self.failures.append({"object_id": object_id, "body": body})
                self.objects[object_id]["status"] = "failed"
                return httpx.Response(200, json=self.objects[object_id])
            self.uploads.append({"object_id": object_id, "body": body})
            self.objects[object_id]["status"] = "ready"
            return httpx.Response(200, json=self.objects[object_id])
        return httpx.Response(404, json={"error": {"code": "not_found", "message": ""}})

    def seed(self, *, event_id: str) -> str:
        object_id = str(uuid4())
        self.objects[object_id] = {
            "id": object_id,
            "type": "result",
            "title": "Daily citations last week",
            "status": "pending_upload",
            "spec": {"source_chat_id": CHAT_ID, "source_event_id": event_id},
            "owner_user_id": OWNER,
        }
        return object_id


def _runtime(tmp_path: Path, stream: list[Event]) -> HarnessRuntime:
    """A runtime whose project already holds THIS chat with `stream` recorded in
    it — the transcript a mirror reads back after a reconnect."""
    project = ProjectDirectory(tmp_path / ".alkera")
    chat = project.chats().create(session_id=CHAT_ID, title="demo smoke")
    for event in stream:
        chat.append_event(event)
    chat.close()
    return HarnessRuntime(project, adapter_factory=FakeAdapterFactory(FakeAdapter))


async def _settled_notes(mirror: ChatMirror) -> list[str]:
    """The mirror's own notes, once the persist pump has written them."""
    for _ in range(100):
        notes = _notes(mirror)
        if notes:
            return notes
        await asyncio.sleep(0.02)
    return _notes(mirror)


async def _mirror_over(
    tmp_path: Path, objects: _Objects, stream: list[Event]
) -> tuple[ChatMirror, HarnessRuntime]:
    """A mirror on a real chat session that has already recorded `stream`."""
    runtime = _runtime(tmp_path, stream)
    session = await runtime.open_chat(CHAT_ID)
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=CloudRestClient(
            api_url="http://objects.test",
            token="device-jwt",
            agent_id=CHAT_ID,
            transport=httpx.MockTransport(objects),
        ),
        user_id=OWNER,
        owner_user_id=OWNER,
    )
    mirror._session = session
    mirror._ready.set()
    return mirror, runtime


def _notes(mirror: ChatMirror) -> list[str]:
    session = mirror._session
    assert session is not None
    return [
        event.part.text
        for event in session.events()
        if isinstance(event, PartCreated) and getattr(event.part, "type", "") == "text"
    ]


# --------------------------------------------------------------------------- #
# the recording: what the browser would send, and what the machine resolves
# --------------------------------------------------------------------------- #


def test_the_recording_carries_no_event_whose_id_is_the_tool_call_id() -> None:
    """The premise. If a transcript event happened to be named by the call id,
    the old lookup would have worked and there would be nothing to fix."""
    ids = {str(event.event_id) for event in recorded()}
    assert RECORDED_CALL_ID not in ids
    assert any(getattr(event, "tool_call_id", None) == RECORDED_CALL_ID for event in recorded())


async def test_a_promote_by_tool_call_id_uploads_the_rows(tmp_path: Path) -> None:
    objects = _Objects()
    mirror, runtime = await _mirror_over(tmp_path, objects, recorded())
    try:
        object_id = objects.seed(event_id=RECORDED_CALL_ID)
        await mirror._on_promote(
            {"kind": "promote", "object_id": object_id, "event_id": RECORDED_CALL_ID}
        )

        assert objects.failures == []
        [upload] = objects.uploads
        assert upload["object_id"] == object_id
        envelope = upload["body"]["envelope"]
        assert envelope["columns"] == ["day", "shipments"]
        assert envelope["total"] == 7
        assert envelope["rows"][0] == ["2026-08-31", 1180]
        # The receipt is lifted from the tool's own provenance block, which only
        # exists once the `call_tool` wrapper has been unwrapped the way the
        # reader's card unwrapped it.
        receipt = upload["body"]["receipt"]
        assert receipt["engine"] == "tinybird"
        assert receipt["connection_name"] == "tinybird"
        assert receipt["row_count"] == 7
        assert receipt["duration_ms"] == 156
        assert receipt["sql"].startswith("SELECT run_day AS day")
        assert receipt["event_id"] == RECORDED_CALL_ID
        assert any("Saved 7 rows" in note for note in await _settled_notes(mirror))
    finally:
        await runtime.close_all()


async def test_a_promote_by_the_transcript_event_id_still_resolves(tmp_path: Path) -> None:
    """A result promoted by an older client named the EVENT. The object exists
    under that name, so the machine must still honour it."""
    rows = [[i, f"row-{i}"] for i in range(3)]
    objects = _Objects()
    project = ProjectDirectory(tmp_path / ".alkera")
    chat = project.chats().create(session_id=CHAT_ID, title="demo smoke")
    runtime = HarnessRuntime(project, adapter_factory=FakeAdapterFactory(FakeAdapter))
    handle = write_rows_blob(runtime.project.blobs(), columns=["id", "name"], rows=rows)
    chat.append_event(
        PartCreated(
            event_id="res-1",
            time=datetime(2026, 9, 7, tzinfo=UTC),
            session_id=CHAT_ID,
            part=ToolCallPart(
                part_id="res-1-part",
                message_id="m-1",
                call_id="call-res-1",
                name="sql.query",
                input={"connection": "pg-main", "sql": "select id, name from orders"},
                state="completed",
                output={
                    "columns": ["id", "name"],
                    "preview_rows": rows,
                    "row_count": 3,
                    "blob": handle.model_dump(mode="json"),
                },
            ),
        )
    )
    chat.close()
    session = await runtime.open_chat(CHAT_ID)
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=CloudRestClient(
            api_url="http://objects.test",
            token="device-jwt",
            agent_id=CHAT_ID,
            transport=httpx.MockTransport(objects),
        ),
        user_id=OWNER,
        owner_user_id=OWNER,
    )
    mirror._session = session
    mirror._ready.set()
    try:
        for event_id in ("call-res-1", "res-1"):
            objects.uploads.clear()
            object_id = objects.seed(event_id=event_id)
            await mirror._on_promote(
                {"kind": "promote", "object_id": object_id, "event_id": event_id}
            )
            assert len(objects.uploads) == 1, f"{event_id} did not resolve"
            assert objects.uploads[0]["body"]["envelope"]["total"] == 3
    finally:
        await runtime.close_all()


# --------------------------------------------------------------------------- #
# a promote the machine cannot honour
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("event_id", "expected"),
    [
        pytest.param("prt_nothinglikethis", "no tool result", id="no-such-result"),
        pytest.param(RECORDED_CALL_ID, "payload is unavailable", id="payload-unavailable"),
    ],
)
async def test_a_promote_it_cannot_honour_fails_the_object_with_the_reason(
    tmp_path: Path, event_id: str, expected: str
) -> None:
    """The reader has navigated to the object page by now, so the reason has to
    reach the OBJECT. It used to live only in the chat, and the object sat in
    `pending_upload` for ever — reading as "Saving…", and as "0 rows" in the
    list."""
    stream = recorded()
    if expected == "payload is unavailable":
        # A result whose rows never came back: the promote resolves the call and
        # then has nothing to upload.
        stream = [
            event.model_copy(update={"output": json.dumps({"result": {"note": "no rows"}})})
            if getattr(event, "tool_call_id", None) == RECORDED_CALL_ID
            and getattr(event, "status", None) == "completed"
            else event
            for event in stream
        ]
    objects = _Objects()
    mirror, runtime = await _mirror_over(tmp_path, objects, stream)
    try:
        object_id = objects.seed(event_id=event_id)
        await mirror._on_promote({"kind": "promote", "object_id": object_id, "event_id": event_id})

        assert objects.uploads == []
        [failure] = objects.failures
        assert failure["object_id"] == object_id
        assert expected in failure["body"]["reason"]
        assert objects.objects[object_id]["status"] == "failed"
        # And still said in the chat, where the reader may not have left yet.
        assert any(expected in note for note in await _settled_notes(mirror))
    finally:
        await runtime.close_all()
