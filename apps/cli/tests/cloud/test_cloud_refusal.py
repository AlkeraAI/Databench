"""What a reader sees when a cloud chat will not run something.

A cloud session is read-only, so three things get refused: a statement that
modifies data, a statement the classifier cannot parse (it fails closed to a
write, so we cannot PROVE it is a read), and a shell command or file write.
Each has to reach the transcript as one plain sentence with the offending
statement quoted beside it — not as a raw permission failure, and not as a
turn that ends in an error.

The sentences themselves live in exactly one module; a grep here is what keeps
a second phrasing of the same refusal from appearing anywhere else.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.cloud.refusal import (
    MAX_STATEMENT_CHARS,
    REFUSAL_COPY,
    RefusalWatch,
    quote_statement,
)
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    Event,
    PermissionOption,
    PermissionRequest,
    PermissionResolved,
    ToolCall,
    ToolCallUpdate,
)

_T = datetime(2026, 9, 6, tzinfo=UTC)
CHAT_ID = "chat-refusal"
OWNER = "00000000-0000-4000-8000-000000000001"

UNPROVABLE = REFUSAL_COPY["unprovable_read"]
READ_ONLY = REFUSAL_COPY["read_only_workspace"]
SHELL = REFUSAL_COPY["read_only_shell"]


# --------------------------------------------------------------------------- #
# a mirror with a real session and no gateway
# --------------------------------------------------------------------------- #


class _Mirror:
    """A running mirror pump over a FakeAdapter: feed harness events in, read
    the durable entries the mirror would publish out."""

    def __init__(self, mirror: ChatMirror, adapter: FakeAdapter) -> None:
        self.mirror = mirror
        self.adapter = adapter

    async def feed(self, *events: Event) -> None:
        for event in events:
            await self.adapter.feed(event)
        await asyncio.sleep(0.05)

    def entries(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while not self.mirror._outbound.empty():
            entry = self.mirror._outbound.get_nowait()
            if "__meta__" not in entry:
                out.append(entry)
        return out

    def notes(self) -> list[str]:
        """The text of every assistant-visible note the mirror put on the tape."""
        return [
            str(entry["payload"]["part"]["text"])
            for entry in self.entries()
            if entry["kind"] == "part.created"
            and entry["payload"].get("part", {}).get("type") == "text"
            and entry["role"] == "system"
        ]


@pytest.fixture
async def mirror(tmp_path: Path) -> AsyncIterator[_Mirror]:
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory)
    store = runtime.project.chats()
    store.create(session_id=CHAT_ID, title="t", harness_type="agent").close()
    handle = ChatMirror(
        chat_id=CHAT_ID,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=CloudRestClient(
            api_url="http://objects.test",
            token="device-jwt",
            agent_id=CHAT_ID,
            transport=httpx.MockTransport(lambda _r: httpx.Response(404, json={})),
        ),
        user_id=OWNER,
        owner_user_id=OWNER,
    )
    handle._session = await runtime.open_chat(CHAT_ID)
    handle._session.set_permission_mode("read_only")
    # The subscription is registered here, as the mirror's own start() does,
    # so nothing published before the task's first step is missed.
    pump = asyncio.get_running_loop().create_task(handle._pump(handle._session.subscribe()))
    handle._outbound = asyncio.Queue()
    try:
        yield _Mirror(handle, factory.adapters[0])
    finally:
        pump.cancel()
        with __import__("contextlib").suppress(BaseException):
            await pump
        await runtime.close_chat(CHAT_ID)


def _tool_refusal(sql: str | None = None, **args: Any) -> list[Event]:
    """The event pair the in-tool gate produces: the call, then its refusal."""
    payload: dict[str, Any] = dict(args)
    if sql is not None:
        payload["sql"] = sql
    return [
        ToolCall(
            event_id="tc-1",
            time=_T,
            session_id=CHAT_ID,
            tool_call_id="call-1",
            message_id="m-1",
            tool_name="sql.query",
            input=payload,
            status="running",
        ),
        ToolCallUpdate(
            event_id="tc-2",
            time=_T,
            session_id=CHAT_ID,
            tool_call_id="call-1",
            status="error",
            error_text=(
                "permission denied: Refused: this chat is in read-only mode, which the "
                "user set — it auto-rejects write actions."
            ),
        ),
    ]


def _ask_refusal(subject: dict[str, Any] | None) -> list[Event]:
    """The event pair a vendor tool ask produces when the policy refuses it
    outright — no human is asked, so no card is ever shown."""
    return [
        PermissionRequest(
            event_id="pr-1",
            time=_T,
            session_id=CHAT_ID,
            request_id="req-1",
            tool_call_id="call-9",
            permission_kind="bash",
            canonical_kind="shell",
            subject=subject,
            options=[PermissionOption(option_id="allow_once", name="Allow")],
        ),
        PermissionResolved(
            event_id="pr-2",
            time=_T,
            session_id=CHAT_ID,
            request_id="req-1",
            option_id="reject_once",
            decided_by="policy",
        ),
    ]


# --------------------------------------------------------------------------- #
# the three refusal paths
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("sql", "copy", "quoted"),
    [
        pytest.param(
            "UPDATE orders SET status = 'paid' WHERE id = 7",
            READ_ONLY,
            "\"UPDATE orders SET status = 'paid' WHERE id = 7\"",
            id="update",
        ),
        pytest.param(
            "DELETE FROM orders WHERE id = 7",
            READ_ONLY,
            '"DELETE FROM orders WHERE id = 7"',
            id="delete",
        ),
        pytest.param(
            "TRUNCATE TABLE orders",
            READ_ONLY,
            '"TRUNCATE TABLE orders"',
            id="truncate-command-verb",
        ),
        pytest.param(
            "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
            READ_ONLY,
            None,
            id="data-modifying-cte",
        ),
        pytest.param(
            "please just delete the rows for me",
            UNPROVABLE,
            '"please just delete the rows for me"',
            id="unparseable-prose",
        ),
        pytest.param(
            "SELEKT * FRM orders WERE",
            UNPROVABLE,
            None,
            id="unparseable-typo",
        ),
    ],
)
async def test_a_refused_statement_reaches_the_transcript_in_plain_language(
    mirror: _Mirror, sql: str, copy: str, quoted: str | None
) -> None:
    await mirror.feed(*_tool_refusal(sql))
    notes = mirror.notes()
    assert len(notes) == 1, notes
    headline, _, detail = notes[0].partition("\n")
    assert headline == copy
    if quoted is not None:
        assert detail == quoted
    else:
        assert sql in detail


async def test_a_refused_shell_command_reaches_the_transcript_in_plain_language(
    mirror: _Mirror,
) -> None:
    await mirror.feed(
        *_ask_refusal({"capability": "shell", "effect": "write", "raw": "rm -rf /var/data"})
    )
    assert mirror.notes() == [f'{SHELL}\n"rm -rf /var/data"']


async def test_a_refused_file_write_is_not_called_a_read_only_connection(
    mirror: _Mirror,
) -> None:
    """A file is not a connection: the read-only sentence is about statements
    that modify data, and a refused file write is the card's own reason."""
    await mirror.feed(
        *_tool_refusal(None, file_path="/srv/etl/models/orders.sql", content="select 1")
    )
    assert mirror.notes() == []


def _fs_ask_refusal(kind: str, raw: str) -> list[Event]:
    ask, resolved = _ask_refusal({"capability": "fs", "effect": "read", "raw": raw})
    return [ask.model_copy(update={"permission_kind": kind, "canonical_kind": "fs"}), resolved]


@pytest.mark.parametrize("mode", ["read_only", "default", "auto", "plan"])
@pytest.mark.parametrize(
    "raw",
    [pytest.param("/etc/shadow", id="absolute"), pytest.param("etc/shadow", id="relative")],
)
async def test_a_refused_read_never_gets_the_read_only_sentence(
    mirror: _Mirror, mode: str, raw: str
) -> None:
    """The walkthrough's `cat /etc/shadow`: a policy-refused READ carried the
    read-only banner in every mode. A read is not a statement that modifies data."""
    mirror.mirror._mode = mode
    await mirror.feed(*_fs_ask_refusal("read", raw))
    assert mirror.notes() == []


@pytest.mark.parametrize("mode", ["default", "auto", "bypass"])
async def test_outside_an_analyst_mode_a_refused_write_statement_says_nothing(
    mirror: _Mirror, mode: str
) -> None:
    """In a writing mode a refused write was a rule's or a judge's decision — the
    connection is not read-only, and saying it is would be false."""
    mirror.mirror._mode = mode
    await mirror.feed(*_tool_refusal("DELETE FROM orders WHERE id = 7"))
    await mirror.feed(
        *_ask_refusal({"capability": "shell", "effect": "write", "raw": "rm -rf /var/data"})
    )
    assert mirror.notes() == []


async def test_a_read_only_connection_says_so_in_any_mode(mirror: _Mirror) -> None:
    """A connection with no write path refuses a write in every mode, and the
    sentence is true there too."""
    mirror.mirror._mode = "default"
    call, update = _tool_refusal("DELETE FROM orders WHERE id = 7")
    update = update.model_copy(
        update={
            "error_text": (
                "permission denied: connection 'pg' is read-only by its nature — a "
                "replica. No permission mode changes that; offer the closest read instead."
            )
        }
    )
    await mirror.feed(call, update)
    assert mirror.notes() == [f'{READ_ONLY}\n"DELETE FROM orders WHERE id = 7"']


def test_the_read_only_sentence_speaks_for_the_connection_not_for_a_person() -> None:
    assert READ_ONLY == "This connection is read-only, so statements that modify data are refused."


async def test_an_unparseable_statement_through_a_vendor_ask_says_it_cannot_be_proven(
    mirror: _Mirror,
) -> None:
    await mirror.feed(
        *_ask_refusal(
            {"capability": "sql", "effect": "write", "confidence": "unknown", "raw": "??? orders"}
        )
    )
    assert mirror.notes() == [f'{UNPROVABLE}\n"??? orders"']


# --------------------------------------------------------------------------- #
# what must NOT produce a refusal note
# --------------------------------------------------------------------------- #


async def test_a_successful_tool_call_says_nothing(mirror: _Mirror) -> None:
    call, _ = _tool_refusal("SELECT 1")
    await mirror.feed(
        call,
        ToolCallUpdate(
            event_id="tc-3",
            time=_T,
            session_id=CHAT_ID,
            tool_call_id="call-1",
            status="completed",
            output={"rows": []},
        ),
    )
    assert mirror.notes() == []


async def test_a_tool_error_that_is_not_a_refusal_says_nothing(mirror: _Mirror) -> None:
    call, _ = _tool_refusal("SELECT 1")
    await mirror.feed(
        call,
        ToolCallUpdate(
            event_id="tc-4",
            time=_T,
            session_id=CHAT_ID,
            tool_call_id="call-1",
            status="error",
            error_text="connection 'pg-main' timed out",
        ),
    )
    assert mirror.notes() == []


async def test_a_readers_own_reject_is_not_explained_back_to_them(mirror: _Mirror) -> None:
    ask, _ = _ask_refusal({"capability": "shell", "raw": "rm -rf /var/data"})
    await mirror.feed(
        ask,
        PermissionResolved(
            event_id="pr-3",
            time=_T,
            session_id=CHAT_ID,
            request_id="req-1",
            option_id="reject_once",
            decided_by="user",
        ),
    )
    assert mirror.notes() == []


async def test_an_allowed_ask_says_nothing(mirror: _Mirror) -> None:
    ask, _ = _ask_refusal({"capability": "shell", "raw": "ls"})
    await mirror.feed(
        ask,
        PermissionResolved(
            event_id="pr-4",
            time=_T,
            session_id=CHAT_ID,
            request_id="req-1",
            option_id="allow_once",
            decided_by="policy",
        ),
    )
    assert mirror.notes() == []


async def test_the_refusal_note_neither_ends_the_turn_nor_errors_it(mirror: _Mirror) -> None:
    """The note is the mirror's own message. It never counts against the turn's
    budget, and it never lands as the turn's error — the agent keeps going and
    offers a read-only alternative in its own words."""
    await mirror.feed(*_tool_refusal("DELETE FROM orders"))
    entries = mirror.entries()
    completions = [e for e in entries if e["kind"] == "message.completed"]
    assert completions, entries
    assert all(e["payload"].get("error") is None for e in completions)
    assert all(e["payload"]["finish_reason"] == "stop" for e in completions)
    assert mirror.mirror._meter.tool_calls == 0


# --------------------------------------------------------------------------- #
# the quoted statement is inert
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("UPDATE a\nSET b = 1", '"UPDATE a SET b = 1"', id="newline-collapses"),
        pytest.param("UPDATE\ta", '"UPDATE a"', id="tab-collapses"),
        pytest.param("UP\x00DATE a", '"UP DATE a"', id="null-byte-becomes-space"),
        pytest.param("a\u202eb", '"ab"', id="bidi-override-dropped"),
        pytest.param("a\u200bb", '"ab"', id="zero-width-dropped"),
        pytest.param("   ", "", id="blank-quotes-nothing"),
    ],
)
def test_the_quoted_statement_is_one_inert_line(raw: str, expected: str) -> None:
    assert quote_statement(raw) == expected


def test_a_long_statement_is_bounded() -> None:
    quoted = quote_statement("SELECT " + "x" * 5000)
    assert len(quoted) <= MAX_STATEMENT_CHARS + 2
    assert quoted.endswith('…"')


def test_a_markdown_link_in_a_statement_survives_as_text_for_the_renderer() -> None:
    """The daemon does not mangle the statement — it hands the renderer inert
    text and the renderer never treats it as markup. Proving the words are still
    there is what makes the browser-side test the real guarantee."""
    quoted = quote_statement("SELECT '[click me](https://evil.example/x)' FROM t")
    assert "[click me](https://evil.example/x)" in quoted


# --------------------------------------------------------------------------- #
# the copy lives in exactly one module
# --------------------------------------------------------------------------- #


def _sources() -> list[Path]:
    root = Path(__file__).resolve().parents[3]
    out: list[Path] = []
    for area in ("apps", "packages"):
        for path in (root / area).rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx"} or not path.is_file():
                continue
            if any(
                part in {"node_modules", "_generated", "generated", ".venv"} for part in path.parts
            ):
                continue
            out.append(path)
    return out


@pytest.mark.parametrize("copy", sorted(REFUSAL_COPY.values()))
def test_the_refusal_copy_is_spelled_in_exactly_one_module(copy: str) -> None:
    """A second spelling is how a product ends up refusing the same thing two
    different ways. Tests are allowed to name it (they import it); nothing else
    may hard-code it."""
    module = Path(__file__).resolve().parents[3] / "apps/cli/alkera_cli/cloud/refusal.py"
    hits = [
        path
        for path in _sources()
        if path != module
        and path != Path(__file__).resolve()
        and copy in path.read_text("utf-8", "ignore")
    ]
    assert hits == [], f"{copy!r} is also spelled in {[str(p) for p in hits]}"


@pytest.mark.parametrize("copy", sorted(REFUSAL_COPY.values()))
def test_the_refusal_copy_never_names_a_surface_the_reader_does_not_have(copy: str) -> None:
    """The reader is in a browser with no editor, no terminal and no settings
    page. Naming one is an instruction they cannot follow."""
    forbidden = ("vs code", "vscode", "terminal", "preference", "settings", "cli", "ide")
    lowered = copy.lower()
    assert not [word for word in forbidden if re.search(rf"\b{re.escape(word)}\b", lowered)]


def test_every_refusal_reason_has_copy() -> None:
    watch = RefusalWatch()
    assert (
        watch.note(
            ToolCall(
                event_id="e",
                time=_T,
                session_id=CHAT_ID,
                tool_call_id="c",
                message_id="m",
                tool_name="sql.query",
                input={"sql": "SELECT 1"},
            )
        )
        is None
    )
    for reason in REFUSAL_COPY:
        assert REFUSAL_COPY[reason].strip()


def test_the_watch_does_not_grow_without_bound() -> None:
    watch = RefusalWatch(remember=2)
    for index in range(10):
        watch.note(
            ToolCall(
                event_id=f"e{index}",
                time=_T,
                session_id=CHAT_ID,
                tool_call_id=f"c{index}",
                message_id="m",
                tool_name="sql.query",
                input={"sql": "SELECT 1"},
            )
        )
    assert len(watch._calls) == 2
