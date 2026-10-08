"""The box fetches a question's attachments before it hands the question over.

These drive the mirror's own call site — a ``prompt`` relay carrying file parts
— through the real relay validation, the real materializer and the real REST
fetcher against a fake Files API: what lands on disk, what the harness is told,
and what a reader is told about a file that never arrived.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.attachments import (
    FAILED_HEADER,
    PROMPT_HEADER,
    MaterializedAttachments,
    RestContentFetcher,
    attachments_in,
    materialize_attachments,
)
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import ChatManifest, Event

CHAT_ID = "chat-attach"
DRIVE = "drive-1"


class _FakeSession:
    """The harness end of the mirror: records the prompts and the mirror's own
    events, so a test can read exactly what the agent was handed."""

    def __init__(self) -> None:
        # A real session carries the chat's manifest; the ask reads the pinned
        # effort off it for every turn.
        self.manifest = ChatManifest(session_id=CHAT_ID)
        self.prompts: list[str] = []
        self.events: list[Event] = []

    async def send_prompt(self, text: str) -> None:
        self.prompts.append(text)

    async def publish_event(self, event: Event) -> None:
        self.events.append(event)


class _FilesApi:
    """A fake Files API + content origin: the mint answers a redirect for a
    node it holds and a 404 for one it does not."""

    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies
        self.minted: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        route = request.url.path
        if route.endswith("/content"):
            node_id = route.split("/")[-2]
            self.minted.append(node_id)
            if node_id not in self.bodies:
                return httpx.Response(404, json={"error": {"code": "not_found", "message": ""}})
            return httpx.Response(302, headers={"location": f"http://origin.test/signed/{node_id}"})
        if route.startswith("/signed/"):
            return httpx.Response(200, content=self.bodies[route.rsplit("/", 1)[-1]])
        return httpx.Response(404)


def _file_part(node_id: str, filename: str, size: int) -> dict[str, Any]:
    return {
        "type": "file",
        "part_id": f"p-{node_id}",
        "message_id": "m1",
        "source": "file",
        "node_id": node_id,
        "filename": filename,
        "size": size,
        "mime": "text/plain",
        "sha256": "",
    }


def _mirror(tmp_path: Path, api: _FilesApi) -> tuple[ChatMirror, _FakeSession]:
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"), adapter_factory=FakeAdapterFactory(FakeAdapter)
    )
    transport = httpx.MockTransport(api)
    rest = CloudRestClient(
        api_url="http://files.test",
        token="device-jwt",
        agent_id=CHAT_ID,
        transport=transport,
    )
    fetcher = RestContentFetcher(
        api_url="http://files.test",
        headers={"Authorization": "Bearer device-jwt"},
        drive_id=DRIVE,
        transport=transport,
    )

    async def prepare(chat_id: str, message: Any) -> MaterializedAttachments:
        return await materialize_attachments(
            attachments_in(message),
            project_path=runtime.project.path,
            chat_id=chat_id,
            fetcher=fetcher,
        )

    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=rest,
        prepare_attachments=prepare,
    )
    session = _FakeSession()
    mirror._session = cast(Any, session)
    mirror._ready.set()
    return mirror, session


async def _relay_one(mirror: ChatMirror, relay: dict[str, Any]) -> None:
    """Put ``relay`` through the mirror's real validation and routing, then run
    the one question it queued."""
    await mirror._handle_relay(relay)
    await mirror._ask(mirror._prompts.get_nowait())


def _notes(session: _FakeSession) -> list[str]:
    return [
        str(getattr(event.part, "text", ""))
        for event in session.events
        if getattr(event, "part", None) is not None
    ]


@pytest.mark.asyncio
async def test_two_attachments_land_on_disk_and_the_prompt_names_their_paths(
    tmp_path: Path,
) -> None:
    bodies = {"n1": b"col_a,col_b\n1,2\n", "n2": b'{"k": 1}'}
    api = _FilesApi(bodies)
    mirror, session = _mirror(tmp_path, api)

    await _relay_one(
        mirror,
        {
            "kind": "prompt",
            "message_id": "m1",
            "seq": 1,
            "text": "what is in these?",
            "client_id": "c1",
            "user_id": "u1",
            "attachments": [
                _file_part("n1", "rows.csv", len(bodies["n1"])),
                _file_part("n2", "conf.json", len(bodies["n2"])),
            ],
        },
    )

    chat_dir = tmp_path / ".alkera" / "attachments" / CHAT_ID
    assert (chat_dir / "rows.csv").read_bytes() == bodies["n1"]
    assert (chat_dir / "conf.json").read_bytes() == bodies["n2"]
    assert api.minted == ["n1", "n2"]

    (prompt,) = session.prompts
    assert prompt.startswith(PROMPT_HEADER + "\n")
    assert str(chat_dir / "rows.csv") in prompt
    assert str(chat_dir / "conf.json") in prompt
    assert prompt.endswith("\n\nwhat is in these?")
    assert _notes(session) == []


@pytest.mark.asyncio
async def test_a_file_the_box_cannot_read_becomes_a_note_and_the_turn_still_runs(
    tmp_path: Path,
) -> None:
    bodies = {"n1": b"col_a\n1\n"}
    api = _FilesApi(bodies)
    mirror, session = _mirror(tmp_path, api)

    await _relay_one(
        mirror,
        {
            "kind": "prompt",
            "message_id": "m1",
            "seq": 1,
            "text": "compare them",
            "client_id": "c1",
            "user_id": "u1",
            "attachments": [
                _file_part("n1", "rows.csv", len(bodies["n1"])),
                _file_part("gone", "missing.csv", 4),
            ],
        },
    )

    chat_dir = tmp_path / ".alkera" / "attachments" / CHAT_ID
    assert (chat_dir / "rows.csv").read_bytes() == bodies["n1"]
    assert not (chat_dir / "missing.csv").exists()

    notes = _notes(session)
    assert any("missing.csv" in note and "404" in note for note in notes), notes

    # The turn still runs, with the file that did arrive named in the prompt —
    # and the one that did not named too, so the agent answers on what is there
    # instead of searching the disk for bytes the reader's words still link.
    (prompt,) = session.prompts
    assert str(chat_dir / "rows.csv") in prompt
    assert FAILED_HEADER in prompt
    assert "missing.csv" in prompt.split(FAILED_HEADER, 1)[1]
    assert str(chat_dir / "missing.csv") not in prompt
    assert prompt.endswith("\n\ncompare them")


@pytest.mark.asyncio
async def test_a_question_with_no_attachments_reaches_the_harness_unchanged(
    tmp_path: Path,
) -> None:
    api = _FilesApi({})
    mirror, session = _mirror(tmp_path, api)

    await _relay_one(
        mirror,
        {
            "kind": "prompt",
            "message_id": "m1",
            "seq": 1,
            "text": "just a question",
            "client_id": "c1",
            "user_id": "u1",
        },
    )

    assert session.prompts == ["just a question"]
    assert api.minted == []
    assert not (tmp_path / ".alkera" / "attachments").exists()
