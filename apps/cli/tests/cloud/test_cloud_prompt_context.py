"""A prompt's ``context`` reaches the model and never the person's bubble.

A Slack-born chat's first turn carries a briefing and the channel's history
beside the words the person typed. The server records both on the prompt row
(``text`` and ``context``); the box has to hand the model both while the
transcript -- the harness's echo of what it accepted -- shows only the words.
The channel for that already exists: the per-turn hidden system block, which
both adapters deliver where the model reads and keep out of the user echo.

Three seams, each pinned at its boundary:

* the catch-up read (``_relay_from_transcript``) carries ``context`` off a
  recorded row, and reads a row written before the field existed as empty;
* the mirror's ask hands the session the words as the prompt text and the
  context on the hidden channel -- the saved-report brief rides the same way;
* the chat session puts that context into the adapter's ``system`` block and
  leaves the prompt text exactly the person's words.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import ChatMirror, _relay_from_transcript
from alkera_cli.harness import ChatSession, HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import ChatManifest
from alkera_core.schemas.objects import ChatPromptRecord

CHAT_ID = "chat-context"
MEMBER = "00000000-0000-4000-8000-000000000001"
BRIEFING = (
    "You are answering inside a Slack thread, not the Alkera web app.\n"
    "<slack-channel-history>\n<@U0AMY>: morning all\n</slack-channel-history>\n"
    "<@U0AMY> asked:"
)


def _row(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "8c4e3d6f-5031-4d5e-a08f-a4d6f2c3e444",
        "seq": 1,
        "role": "user",
        "kind": "prompt",
        "event_id": "e-1",
        "payload": payload,
    }


def test_a_recorded_prompt_carries_its_context_into_the_relay() -> None:
    record = ChatPromptRecord(text="hi", context=BRIEFING, client_id="c1", user_id=MEMBER)
    relay = _relay_from_transcript(_row(record.model_dump(mode="json")), chat_id=CHAT_ID)
    assert relay is not None
    assert relay["text"] == "hi"
    assert relay["context"] == BRIEFING


def test_a_prompt_recorded_before_the_field_existed_reads_as_having_no_context() -> None:
    """The row a 1.1.0 writer left behind: no ``context`` key at all."""
    old = {
        "schema_version": "1.1.0",
        "kind": "prompt",
        "text": "which prompts moved this week?",
        "client_id": "c1",
        "user_id": MEMBER,
        "attachments": [],
    }
    relay = _relay_from_transcript(_row(old), chat_id=CHAT_ID)
    assert relay is not None
    assert relay["text"] == "which prompts moved this week?"
    assert relay["context"] == ""


class _RecordingSession:
    """The one call the ask makes on a session, kept for the assertion."""

    def __init__(self) -> None:
        # A real session carries the chat's manifest; the ask reads the pinned
        # effort off it for every turn (none pinned here, so no variant rides).
        self.manifest = ChatManifest(session_id=CHAT_ID)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send_prompt(self, text: str, **kwargs: Any) -> str:
        self.calls.append((text, kwargs))
        return "attempt-1"


def _mirror(tmp_path: Path) -> tuple[ChatMirror, _RecordingSession]:
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"), adapter_factory=FakeAdapterFactory(FakeAdapter)
    )
    rest = CloudRestClient(
        api_url="http://context.test",
        token="device-jwt",
        agent_id=CHAT_ID,
        transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
    )
    mirror = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=rest,
        user_id=MEMBER,
        owner_user_id=MEMBER,
    )
    session = _RecordingSession()
    mirror._ready.set()
    mirror._session = cast(ChatSession, session)
    return mirror, session


def _relay(text: str, context: str | None = None) -> dict[str, Any]:
    relay: dict[str, Any] = {
        "kind": "prompt",
        "message_id": "8c4e3d6f-5031-4d5e-a08f-a4d6f2c3e444",
        "seq": 1,
        "text": text,
        "client_id": "c1",
        "user_id": MEMBER,
    }
    if context is not None:
        relay["context"] = context
    return relay


async def test_the_ask_hands_the_words_as_the_prompt_and_the_context_hidden(
    tmp_path: Path,
) -> None:
    mirror, session = _mirror(tmp_path)

    await mirror._ask(_relay("hi", BRIEFING))

    assert len(session.calls) == 1
    text, kwargs = session.calls[0]
    assert text == "hi"
    assert kwargs.get("context") == BRIEFING


async def test_a_prompt_with_no_context_hides_nothing(tmp_path: Path) -> None:
    mirror, session = _mirror(tmp_path)

    await mirror._ask(_relay("hi"))

    assert session.calls == [("hi", {})] or session.calls == [("hi", {"context": None})]


async def test_the_saved_report_brief_rides_the_same_hidden_channel(tmp_path: Path) -> None:
    """A chat started from a saved report is handed the folder's brief the same
    way: the model reads it first, the person's bubble is their question. The
    brief is consumed by the first ask and does not ride the second."""
    mirror, session = _mirror(tmp_path)
    mirror._source_brief = "This chat was started FROM a saved report."

    await mirror._ask(_relay("re-run it for EMEA", BRIEFING))
    await mirror._ask(_relay("and for AMER"))

    first_text, first = session.calls[0]
    assert first_text == "re-run it for EMEA"
    assert first["context"].startswith("This chat was started FROM a saved report.")
    assert first["context"].endswith(BRIEFING)
    second_text, second = session.calls[1]
    assert second_text == "and for AMER"
    assert not second.get("context")


async def test_a_chat_session_puts_the_context_where_the_person_never_sees_it(
    tmp_path: Path,
) -> None:
    """At the adapter: the prompt text is exactly the words, the context is in
    the hidden system block -- and only on the turn that carried it."""
    factory = FakeAdapterFactory(lambda: FakeAdapter(reply_text="ok"))
    runtime = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)
    chat = runtime._chats_store.create(title="c", harness_type="agent")
    sid = chat.session_id
    chat.close()
    session = await runtime.open_chat(sid)
    try:
        await session.send_prompt("hi", context=BRIEFING)
        await session.send_prompt("and postgres?")
        sent = factory.adapters[-1].sent_prompts
        assert [p.text for p in sent] == ["hi", "and postgres?"]
        assert sent[0].system is not None and BRIEFING in sent[0].system
        assert "Slack thread" not in (sent[1].system or "")
    finally:
        await runtime.close_chat(sid)
