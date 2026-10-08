"""A published reply that names a file the chat does not have is caught by the box.

On staging an agent wrote a query result out with ``blob.materialize``, charted
it, removed the file as clean-up (``rm -rf matplotlib-… result-9a21e98b.csv``)
and then replied with ``[Active prompts by category/locale](blob:<handle>)``.
The web opens a ``blob:`` link through the file the result was written to, the
drive had that file only in its trash, and the reply said the file "has not
arrived" for good. Nothing on the box noticed, and the agent never learned its
link pointed at nothing.

These pin the box's half of the fix through the mirror's own publish path: it
learns which file each result was written to from the entries it publishes,
checks a finished reply against the chat's folder once the reply's landing is
done, and hands what it found to the agent with its next turn — on the hidden
context channel, once, and only for files still missing then.

The entries are the staging transcript's shapes, word for word where it
matters (the ``call_tool`` wrapper, the JSON-string output).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.cloud.reply_files import (
    NOTE_LIMIT,
    ReplyFileCheck,
    materialized,
    missing_files_note,
)
from alkera_cli.harness import ChatSession, HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import ChatManifest

CHAT_ID = "5e76966f-9423-484b-8b7f-0ec781609d7e"
MEMBER = "00000000-0000-4000-8000-000000000001"
HANDLE = "9a21e98bc2c79b9d281d573492f1cb28875e2cfefebdf89fdd187ee4372f8bf8"
CALL = "prt_0f60d914a001VSd7opf7pQ9NJv"
RESULT = "result-9a21e98b.csv"
CHART = "active_prompts_by_category_locale.png"
REPLY = (
    "Read from pg (postgres): `target_prompts` (status, category, locale) lives there.\n\n"
    f"![Active target prompts by category and locale]({CHART})\n\n"
    "1,071 active prompts total. Full breakdown:\n\n"
    f"[Active prompts by category/locale](blob:{HANDLE})"
)
HANG = 10.0


def _materialize_entries(written: str) -> list[dict[str, Any]]:
    """``blob.materialize`` as the opencode harness put it on the transcript:
    announced as ``call_tool``, finished with the wrapper's input and the
    result as a JSON string."""
    wrapped: dict[str, Any] = {
        "args": {"path": "active_prompts.csv", "format": "csv", "handle": HANDLE},
        "name": "blob.materialize",
    }
    output = json.dumps(
        {"result": {"path": written, "format": "csv", "row_count": 80, "bytes": 1892}}
    )
    return [
        {
            "event_id": "call-1",
            "role": "tool",
            "kind": "tool.call",
            "payload": {"tool_call_id": CALL, "tool_name": "alkera_call_tool", "input": {}},
        },
        {
            "event_id": "call-1-running",
            "role": "tool",
            "kind": "tool.call_update",
            "payload": {"tool_call_id": CALL, "status": "running", "input": wrapped},
        },
        {
            "event_id": "call-1-done",
            "role": "tool",
            "kind": "tool.call_update",
            "payload": {
                "tool_call_id": CALL,
                "status": "completed",
                "input": wrapped,
                "output": output,
            },
        },
    ]


def _reply_entry(text: str = REPLY, *, synthetic: bool = False) -> dict[str, Any]:
    return {
        "event_id": "reply-1",
        "role": "assistant",
        "kind": "part.created",
        "payload": {
            "part": {
                "type": "text",
                "text": text,
                "part_id": "prt_reply",
                "message_id": "msg_reply",
                "synthetic": synthetic,
            }
        },
    }


class _RecordingSession:
    def __init__(self) -> None:
        self.manifest = ChatManifest(session_id=CHAT_ID)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send_prompt(self, text: str, **kwargs: Any) -> str:
        self.calls.append((text, kwargs))
        return "attempt-1"


class _Doc:
    def __init__(self) -> None:
        self.appended: list[dict[str, Any]] = []
        self.changed = asyncio.Event()

    async def send_op(
        self, intent: str, *, events: Any = (), meta: Any = None, op_id: Any = None
    ) -> None:
        del meta, op_id
        if intent == "append":
            self.appended.extend(events)
            self.changed.set()


def _mirror(tmp_path: Path) -> tuple[ChatMirror, _RecordingSession, _Doc]:
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"), adapter_factory=FakeAdapterFactory(FakeAdapter)
    )
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=CloudRestClient(
            api_url="http://missing.test",
            token="device-jwt",
            agent_id=CHAT_ID,
            transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
        ),
        user_id=MEMBER,
        owner_user_id=MEMBER,
    )
    session = _RecordingSession()
    doc = _Doc()
    mirror._ready.set()
    mirror._session = cast(ChatSession, session)
    mirror._doc = cast(Any, doc)
    mirror.working_dir.mkdir(parents=True, exist_ok=True)
    return mirror, session, doc


def _box_path(mirror: ChatMirror, name: str) -> str:
    """The absolute path the tool reports, as it would on the box."""
    return f"/opt/alkera-work/.alkera/chats/{CHAT_ID}/{mirror.working_dir.name}/{name}"


async def _publish(mirror: ChatMirror, doc: _Doc, entries: list[dict[str, Any]]) -> None:
    """Run the real publish pump over these entries until all are sent."""
    for entry in entries:
        mirror._outbound.put_nowait(entry)
    pump = asyncio.create_task(mirror._publisher())
    try:
        async with asyncio.timeout(HANG):
            while len(doc.appended) < len(entries):
                doc.changed.clear()
                await doc.changed.wait()
    finally:
        pump.cancel()


def _relay(text: str) -> dict[str, Any]:
    return {
        "kind": "prompt",
        "message_id": "8c4e3d6f-5031-4d5e-a08f-a4d6f2c3e444",
        "seq": 1,
        "text": text,
        "client_id": "c1",
        "user_id": MEMBER,
    }


# -- the mirror ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_next_turn_is_told_the_result_file_its_last_reply_linked_was_deleted(
    tmp_path: Path,
) -> None:
    mirror, session, doc = _mirror(tmp_path)
    (mirror.working_dir / CHART).write_bytes(b"\x89PNG")
    # The result was written out, and then removed before the reply: the file
    # the link opens is not in the chat.
    await _publish(mirror, doc, [*_materialize_entries(_box_path(mirror, RESULT)), _reply_entry()])

    # The reply itself still went out, unchanged.
    assert doc.appended[-1]["payload"]["part"]["text"] == REPLY

    await mirror._ask(_relay("can I get the csv?"))

    text, kwargs = session.calls[0]
    assert text == "can I get the csv?"
    context = kwargs.get("context") or ""
    assert f"`{RESULT}`" in context
    assert f"[Active prompts by category/locale](blob:{HANDLE})" in context
    # The chart is in the chat, so it is not named as missing.
    assert CHART not in context


@pytest.mark.asyncio
async def test_the_agent_is_told_once(tmp_path: Path) -> None:
    mirror, session, doc = _mirror(tmp_path)
    await _publish(mirror, doc, [*_materialize_entries(_box_path(mirror, RESULT)), _reply_entry()])

    await mirror._ask(_relay("first"))
    await mirror._ask(_relay("second"))

    assert RESULT in (session.calls[0][1].get("context") or "")
    assert RESULT not in (session.calls[1][1].get("context") or "")


@pytest.mark.asyncio
async def test_a_reply_whose_files_are_all_there_tells_the_agent_nothing(tmp_path: Path) -> None:
    mirror, session, doc = _mirror(tmp_path)
    (mirror.working_dir / CHART).write_bytes(b"\x89PNG")
    (mirror.working_dir / RESULT).write_text("category,locale,n\n")
    await _publish(mirror, doc, [*_materialize_entries(_box_path(mirror, RESULT)), _reply_entry()])

    await mirror._ask(_relay("thanks"))

    assert not session.calls[0][1].get("context")


@pytest.mark.asyncio
async def test_a_file_written_back_before_the_next_turn_is_not_reported(tmp_path: Path) -> None:
    mirror, session, doc = _mirror(tmp_path)
    (mirror.working_dir / CHART).write_bytes(b"\x89PNG")
    await _publish(mirror, doc, [*_materialize_entries(_box_path(mirror, RESULT)), _reply_entry()])
    (mirror.working_dir / RESULT).write_text("category,locale,n\n")

    await mirror._ask(_relay("thanks"))

    assert not session.calls[0][1].get("context")


@pytest.mark.asyncio
async def test_a_missing_chart_is_reported_as_well(tmp_path: Path) -> None:
    mirror, session, doc = _mirror(tmp_path)
    await _publish(mirror, doc, [_reply_entry()])

    await mirror._ask(_relay("where is the chart?"))

    context = session.calls[0][1].get("context") or ""
    assert f"`{CHART}`" in context
    # A result never written out is a label on the web, not a file to check.
    assert RESULT not in context


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param({**_reply_entry(), "role": "user"}, id="a-persons-words"),
        pytest.param(_reply_entry(synthetic=True), id="a-hidden-steering-part"),
    ],
)
@pytest.mark.asyncio
async def test_only_the_agents_visible_text_is_checked(
    tmp_path: Path, entry: dict[str, Any]
) -> None:
    mirror, session, doc = _mirror(tmp_path)
    await _publish(mirror, doc, [entry])

    await mirror._ask(_relay("next"))

    assert not session.calls[0][1].get("context")


# -- the bookkeeping -------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "tool_input", "output", "expected"),
    [
        pytest.param(
            "alkera_call_tool",
            {"name": "blob.materialize", "args": {"handle": "h"}},
            json.dumps({"result": {"path": "/x/a.csv"}}),
            ("h", "/x/a.csv"),
            id="call-tool-wrapper",
        ),
        pytest.param(
            "",
            {"name": "blob_materialize", "args": {"handle": "h"}},
            {"result": {"path": "/x/a.csv"}},
            ("h", "/x/a.csv"),
            id="wrapper-seen-without-its-announcement",
        ),
        pytest.param(
            "mcp__alkera__blob_materialize",
            {"handle": "h"},
            {"path": "/x/a.csv"},
            ("h", "/x/a.csv"),
            id="the-tool-itself",
        ),
        pytest.param(
            "alkera_call_tool",
            {"name": "blob.query", "args": {"handle": "h"}},
            {"result": {"path": "/x/a.csv"}},
            None,
            id="another-tool",
        ),
        pytest.param(
            "bash",
            {"name": "blob.materialize", "args": {"handle": "h"}},
            {"result": {"path": "/x/a.csv"}},
            None,
            id="a-named-tool-is-not-a-wrapper",
        ),
        pytest.param(
            "alkera_call_tool",
            {"name": "blob.materialize", "args": {"handle": "h"}},
            "not json",
            None,
            id="unreadable-output",
        ),
        pytest.param(
            "alkera_call_tool",
            {"name": "blob.materialize", "args": {}},
            {"result": {"path": "/x/a.csv"}},
            None,
            id="no-handle",
        ),
    ],
)
def test_materialized(
    tool_name: str, tool_input: Any, output: Any, expected: tuple[str, str] | None
) -> None:
    assert materialized(tool_name, tool_input, output) == expected


def test_a_running_or_failed_materialize_is_not_a_written_file(tmp_path: Path) -> None:
    check = ReplyFileCheck(chat_id=CHAT_ID, chat_folder=tmp_path, working_dir=tmp_path / "scratch")
    path = f"/opt/alkera-work/.alkera/chats/{CHAT_ID}/scratch/{RESULT}"
    entries = _materialize_entries(path)
    for status in ("running", "error"):
        check.observe({**entries[2], "payload": {**entries[2]["payload"], "status": status}})
    assert check.result_files == {}
    check.observe(entries[0])
    check.observe(entries[2])
    assert check.result_files == {HANDLE: path}


def test_memory_is_bounded_and_forgets_the_oldest(tmp_path: Path) -> None:
    check = ReplyFileCheck(
        chat_id=CHAT_ID, chat_folder=tmp_path, working_dir=tmp_path / "scratch", memory=2
    )
    for index in range(3):
        check.observe(
            {
                "kind": "tool.call_update",
                "payload": {
                    "tool_call_id": f"c{index}",
                    "status": "completed",
                    "input": {"name": "blob.materialize", "args": {"handle": f"h{index}"}},
                    "output": {"result": {"path": f"/p/{index}.csv"}},
                },
            }
        )
    assert check.result_files == {"h1": "/p/1.csv", "h2": "/p/2.csv"}


def test_the_note_names_at_most_its_limit(tmp_path: Path) -> None:
    check = ReplyFileCheck(chat_id=CHAT_ID, chat_folder=tmp_path, working_dir=tmp_path / "scratch")
    links = "\n".join(f"[f{i}](f{i}.csv)" for i in range(NOTE_LIMIT + 3))

    note = missing_files_note(check.missing(links))

    assert "`f0.csv`" in note
    assert f"`f{NOTE_LIMIT - 1}.csv`" in note
    assert f"`f{NOTE_LIMIT}.csv`" not in note
    assert "and 3 more" in note
