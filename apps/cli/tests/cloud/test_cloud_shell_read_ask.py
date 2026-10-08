"""A shell command that writes nothing is the reader's to answer, not the fence's.

The write fence guards where a chat may land bytes. A command that lands none
has nothing for it to guard, so putting one through it can only produce a wrong
answer — and the answer it produced was the worst kind: a refusal with no card,
carrying the sentence for a write ("I can only save files inside this chat's own
folder") over a command that saves nothing.

It happened whenever the destination reader could not parse the command, which
is its fail-closed default and correct on its own terms: a nested shell, a
command behind ``env``/``xargs``/``sudo``, an unbalanced quote. The classifier
reads INSIDE all of those and already calls ``bash -c "echo x > /tmp/y"`` a
write, so the two disagree only where the classifier positively says READ —
and there the fence was refusing a reader's ask before the reader saw it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket, fence
from alkera_cli.cloud.mirror import ChatMirror, _authorizes_a_write
from alkera_cli.contracts.tool_types import Effect
from alkera_cli.harness import HarnessRuntime, PermissionBroker
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.adapters.opencode_translate import (
    OpencodeEventTranslator,
    _TranslatorContext,
)
from alkera_cli.harness.permission_mode import PermissionMode
from alkera_cli.host import paths
from alkera_cli.plugins.plugin_base.permissions.bash import classify_command
from alkera_cli.plugins.plugin_base.permissions.config import (
    PermissionRule,
    PermissionsConfig,
    save_permissions,
)
from alkera_cli.plugins.plugin_base.permissions.shell import analyze_shell
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import PermissionOption, PermissionRequest

_T = datetime(2026, 9, 18, tzinfo=UTC)


def _shell_ask(command: str, *, effect: str) -> PermissionRequest:
    """A bash ask shaped the way the opencode translator shapes one: the
    canonical kind is ``shell`` for every command, and the classifier's verdict
    rides the subject's ``effect``."""
    return PermissionRequest(
        event_id="ev-1",
        time=_T,
        session_id="chat-1",
        request_id="req-1",
        tool_call_id="call-1",
        permission_kind="bash",
        canonical_kind="shell",
        patterns=[],
        subject={
            "capability": "shell",
            "effect": effect,
            "operation": "bash",
            "raw": command,
            "targets": [],
            "classifier": "opencode-tool",
        },
        options=[
            PermissionOption(option_id="allow_once", name="Allow"),
            PermissionOption(option_id="reject_once", name="Reject"),
        ],
    )


#: Commands the destination reader refuses to parse. The classifier's verdict is
#: read off the classifier itself so the two can never be recorded out of step.
_UNREADABLE_READS = [
    pytest.param('bash -c "ls -la"', id="a-nested-shell-that-only-lists"),
    pytest.param("env FOO=1 ls", id="a-listing-behind-env"),
    pytest.param("echo 'unterminated > out.txt", id="an-unbalanced-quote"),
    pytest.param("curl -o", id="curl-naming-no-destination"),
]

_UNREADABLE_WRITES = [
    pytest.param('python -c \'open("/tmp/x","w")\'', id="an-interpreter"),
]

#: Writes behind a wrapper or inside a nested shell. The shell model reads
#: through those, so it names the destination, and every one of these lands
#: outside the chat's folder: an escape the fence refuses in every stance,
#: where the old reader could only ask.
_WRITES_OUTSIDE_THE_MODEL_READS = [
    pytest.param('bash -c "echo hi > /tmp/y"', "/tmp/y", id="a-nested-shell-that-writes"),
    pytest.param("env FOO=1 tee /tmp/x", "/tmp/x", id="a-tee-behind-env"),
    pytest.param("xargs -I{} cp {} /tmp", "/tmp", id="a-copy-behind-xargs"),
    pytest.param("sudo tee /etc/hosts", "/etc/hosts", id="a-tee-behind-sudo"),
]
_WRITES_OUTSIDE = [pytest.param(p.values[0], id=p.id) for p in _WRITES_OUTSIDE_THE_MODEL_READS]


@pytest.mark.parametrize("command", _UNREADABLE_READS + _UNREADABLE_WRITES + _WRITES_OUTSIDE)
def test_the_destination_reader_cannot_vouch_for_any_of_these(command: str) -> None:
    """The premise the halves below stand on: none of these is a command the
    model can vouch for whole, so none is allowed on the model's word alone."""
    assert not analyze_shell(command).readable


@pytest.mark.parametrize(("command", "destination"), _WRITES_OUTSIDE_THE_MODEL_READS)
def test_the_model_still_names_the_destination_behind_the_wrapper(
    command: str, destination: str
) -> None:
    assert destination in [location.text for location in analyze_shell(command).writes]


@pytest.mark.parametrize("command", _UNREADABLE_READS)
def test_a_shell_ask_the_classifier_calls_a_read_is_not_a_write(command: str) -> None:
    """The bug, at the one line that caused it.

    ``canonical_kind`` is ``shell`` for every command opencode asks about, so a
    kind-first answer made every shell ask write-class — and the write fence
    then refused the ones it could not parse, with no card and the sentence for
    a write."""
    assert str(classify_command(command).effect) == "read"
    assert _authorizes_a_write(_shell_ask(command, effect="read")) is False


@pytest.mark.parametrize("command", _UNREADABLE_WRITES + _WRITES_OUTSIDE)
def test_a_shell_ask_the_classifier_calls_a_write_still_is_one(command: str) -> None:
    """The half that must not move: the classifier reads inside a nested shell,
    ``env``, ``xargs``, ``sudo`` and an interpreter, so every command that
    really lands bytes is still write-class and still meets the fence."""
    assert str(classify_command(command).effect) != "read"
    assert _authorizes_a_write(_shell_ask(command, effect="write")) is True


@pytest.mark.parametrize(
    "effect",
    [
        pytest.param("", id="an-empty-effect"),
        pytest.param("unknown", id="an-effect-no-classifier-produced"),
    ],
)
def test_a_shell_ask_with_no_readable_effect_stays_write_class(effect: str) -> None:
    """Fail closed where the verdict is missing: only a positive ``read``
    excuses an ask from the write fence, never the absence of a verdict."""
    assert _authorizes_a_write(_shell_ask("ls", effect=effect)) is True


def test_a_shell_ask_carrying_no_subject_at_all_stays_write_class() -> None:
    request = _shell_ask("ls", effect="read")
    assert _authorizes_a_write(request.model_copy(update={"subject": None})) is True


@pytest.mark.parametrize("command", _UNREADABLE_READS)
def test_the_write_fence_has_no_say_over_a_read(tmp_path: Path, command: str) -> None:
    """End to end over the fence's own entry point: the ask a reader must be
    shown is no longer answered by the fence on their behalf.

    ``ask_write_escape`` still names the whole command for a write it cannot
    parse — that is the fence working — so the difference is made by not
    reaching it for a read."""
    folder = tmp_path / ".alkera" / "chats" / "chat-1"
    (folder / "scratch").mkdir(parents=True)
    request = _shell_ask(command, effect="read")
    assert _authorizes_a_write(request) is False
    # And the fence, asked directly, would have refused it — which is what the
    # kind-first answer handed to the reader as a path-scope sentence.
    assert fence.ask_write_escape(request, folder=folder, base=folder / "scratch") == command


# ---------------------------------------------------------------------------
# The mirror: the ask reaches the reader
# ---------------------------------------------------------------------------


CHAT_ID = "chat-shell"
OWNER = "00000000-0000-4000-8000-000000000001"


class _Mirror:
    def __init__(self, mirror: ChatMirror, adapter: FakeAdapter) -> None:
        self.mirror = mirror
        self.adapter = adapter

    async def ask(self, request: PermissionRequest) -> str:
        """``"parked"`` when the ask is the reader's to answer, else the option
        the mirror answered it with on their behalf."""
        await self.adapter.feed(request)
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            for replied, option in self.adapter.permission_replies:
                if replied == request.request_id:
                    await asyncio.sleep(0.05)
                    return str(option)
            if request.request_id in self.mirror.pending_interrupts:
                return "parked"
            await asyncio.sleep(0.02)
        raise AssertionError(f"no answer for {request.request_id}")

    def wire(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while not self.mirror._outbound.empty():
            entry = self.mirror._outbound.get_nowait()
            if "__meta__" not in entry:
                out.append(entry)
        return out

    def prompting(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The asks tagged for a reader — the browser renders its decision
        controls only for these, so this IS "a card was offered"."""
        return [
            entry
            for entry in entries
            if entry.get("kind") == "permission.request"
            and entry.get("payload", {}).get("prompting") is True
        ]

    def notes(self, entries: list[dict[str, Any]]) -> list[str]:
        return [
            str(entry["payload"]["part"]["text"])
            for entry in entries
            if entry.get("kind") == "part.created"
            and entry.get("role") == "system"
            and entry.get("payload", {}).get("part", {}).get("type") == "text"
        ]


async def _mirror_in(
    mode: PermissionMode, tmp_path: Path, *, fenced: bool = False
) -> tuple[_Mirror, Any, HarnessRuntime]:
    workspace = tmp_path / "work"
    (workspace / "src").mkdir(parents=True)
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(ProjectDirectory(workspace / ".alkera"), adapter_factory=factory)
    save_permissions(
        workspace / ".alkera",
        # Every shell ask reaches the mirror's own resolver, reads included.
        # Without this the decision engine's read fast path answers first and
        # the mirror never sees a read at all — so a case built on it would
        # pass whatever the mirror does with one.
        PermissionsConfig(
            rules=[
                PermissionRule(capability="shell", effect=Effect.READ, decision="ask"),
                PermissionRule(capability="shell", effect=Effect.WRITE, decision="ask"),
            ]
        ),
    )
    runtime.project.chats().create(session_id=CHAT_ID, title="t", harness_type="agent").close()
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
        permission_mode=mode,
    )
    broker = PermissionBroker(handle._resolve_permission, default_timeout_seconds=None)
    handle._session = await handle.open_session(broker)
    # The session's own read fence answers an out-of-workspace location before
    # the broker sees it; what these cases are about is the decision the MIRROR
    # makes, and every command below stays inside the workspace anyway.
    if not fenced:
        handle._session._path_fence = None
    handle._session.set_permission_mode(mode)
    handle._mode = mode
    pump = asyncio.get_running_loop().create_task(handle._pump(handle._session.subscribe()))
    handle._outbound = asyncio.Queue()
    return _Mirror(handle, factory.adapters[0]), pump, runtime


@pytest.fixture
async def in_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Mirror]:
    home = tmp_path / "alkera-home"
    home.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    mirror, pump, runtime = await _mirror_in("default", tmp_path)
    try:
        yield mirror
    finally:
        pump.cancel()
        with contextlib.suppress(BaseException):
            await pump
        await runtime.close_chat(CHAT_ID)


@pytest.fixture
async def in_bypass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Mirror]:
    """A bypass chat with the box's REAL fence on the session, so the case is
    the whole path a browser chat takes: the fence's verdict, then the stance."""
    home = tmp_path / "alkera-home"
    home.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    mirror, pump, runtime = await _mirror_in("bypass", tmp_path, fenced=True)
    try:
        yield mirror
    finally:
        pump.cancel()
        with contextlib.suppress(BaseException):
            await pump
        await runtime.close_chat(CHAT_ID)


_UNREADABLE = [
    *[pytest.param(p.values[0], "read", id=p.id) for p in _UNREADABLE_READS],
    *[pytest.param(p.values[0], "write", id=p.id) for p in _UNREADABLE_WRITES],
]


@pytest.mark.parametrize(("command", "effect"), _UNREADABLE)
async def test_bypass_runs_a_command_the_fence_cannot_read(
    in_bypass: _Mirror, command: str, effect: str
) -> None:
    """The reported bug, end to end: a browser chat set to "Runs everything
    without asking" was refused ``python -c`` and ``uv run`` with no card and a
    sentence about a stance that asks nobody, and the model concluded the chat
    had no shell. The decision that reaches the box's harness is an allow: no
    card is offered, no note is said, and the adapter is replied ``allow_once``."""
    assert await in_bypass.ask(_shell_ask(command, effect=effect)) == "allow_once"
    entries = in_bypass.wire()
    assert in_bypass.prompting(entries) == []
    assert in_bypass.notes(entries) == []


@pytest.mark.parametrize("command", _WRITES_OUTSIDE)
async def test_bypass_refuses_a_write_the_model_reads_behind_a_wrapper(
    in_bypass: _Mirror, command: str
) -> None:
    """The hole the old reader left: ``sudo tee /etc/hosts`` ran in bypass
    because the fence could not read past ``sudo``. The model reads past it,
    the destination is off the chat's folder, and the fence refuses it in
    bypass as it would the plain write, with no card."""
    assert await in_bypass.ask(_shell_ask(command, effect="write")) == "reject_once"
    entries = in_bypass.wire()
    assert in_bypass.prompting(entries) == []
    log = in_bypass.mirror._session.decision_sink.directory / "decisions.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert [(row["decision"], row["decided_by"]) for row in rows] == [("reject", "fence")]


async def test_bypass_still_refuses_a_location_outside_the_workspace(in_bypass: _Mirror) -> None:
    """What bypass hands over is the asking, never the boundary: a read that
    escapes into a sibling chat is the fence's refusal in bypass too — decided
    at the harness's chokepoint, no card offered, the fence's sentence handed
    to the model as the tool's error rather than said as a note."""
    ask = _shell_ask("cat ../../chat-b/scratch/secret.csv", effect="read")
    assert await in_bypass.ask(ask) == "reject_once"
    entries = in_bypass.wire()
    assert in_bypass.prompting(entries) == []
    log = in_bypass.mirror._session.decision_sink.directory / "decisions.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert [(row["decision"], row["decided_by"]) for row in rows] == [("reject", "fence")]


@pytest.mark.parametrize("command", _UNREADABLE_READS)
async def test_default_mode_offers_a_card_for_a_shell_read(
    in_default: _Mirror, command: str
) -> None:
    """The reported bug, end to end: the ask reaches the reader.

    Default asks before every command, which is what its chip says. So a
    command the mirror is asked about is announced as the reader's to answer —
    where it came back refused, with no card, carrying the sentence for a write
    out of the chat's own folder."""
    assert await in_default.ask(_shell_ask(command, effect="read")) == "parked"
    entries = in_default.wire()
    assert len(in_default.prompting(entries)) == 1
    assert in_default.notes(entries) == []


async def test_default_mode_parks_a_write_inside_the_folder_on_the_reader(
    in_default: _Mirror,
) -> None:
    """The other half of the chip's promise: a command that changes something
    is the reader's to answer, so it is announced as theirs."""
    ask = _shell_ask("echo hi > scratch/a.txt", effect="write")
    assert await in_default.ask(ask) == "parked"
    entries = in_default.wire()
    assert len(in_default.prompting(entries)) == 1
    assert in_default.notes(entries) == []


@pytest.mark.parametrize("command", _UNREADABLE_WRITES)
async def test_default_mode_offers_a_card_for_an_unparseable_write_too(
    in_default: _Mirror, command: str
) -> None:
    """A command the fence cannot read — a nested shell, ``env``, ``xargs``,
    ``sudo``, an interpreter — is nobody's to allow automatically, and in a
    stance that asks it is the reader's: one card carrying the real command,
    no note. Refusing it here left ``default`` on a box with no ``git`` and no
    ``python``; the refusal belongs to the stances that ask nobody."""
    assert await in_default.ask(_shell_ask(command, effect="write")) == "parked"
    entries = in_default.wire()
    assert len(in_default.prompting(entries)) == 1
    assert in_default.notes(entries) == []


@pytest.mark.parametrize(("command", "destination"), _WRITES_OUTSIDE_THE_MODEL_READS)
async def test_default_mode_refuses_a_write_the_model_reads_landing_off_the_folder(
    in_default: _Mirror, command: str, destination: str
) -> None:
    """A write the model can place is judged where it lands. Off the chat's
    folder it is the fence's refusal, not the reader's question: no card, the
    folder sentence, the destination quoted."""
    assert await in_default.ask(_shell_ask(command, effect="write")) == "reject_once"
    entries = in_default.wire()
    assert in_default.prompting(entries) == []
    notes = in_default.notes(entries)
    assert len(notes) == 1 and destination in notes[0], notes


def _parent_shell_ask() -> PermissionRequest:
    """The bash ask exactly as opencode raises it for the parent-hosted shell:
    its generic MCP-tool ask, whose one pattern is the permission rule glob ``*``
    and whose metadata carries nothing. The command is not known at ask time —
    the tool gates it in-process with the real text."""
    ctx = _TranslatorContext(session_id="chat-1", workspace_root=Path("/w"), sandbox_dir=None)
    event = OpencodeEventTranslator(ctx).translate(
        {
            "type": "permission.asked",
            "properties": {
                "id": "req-star",
                "permission": "bash",
                "patterns": ["*"],
                "metadata": {},
            },
        }
    )
    assert isinstance(event, PermissionRequest)
    return event


async def test_default_mode_parks_the_parent_shell_ask_on_the_reader(in_default: _Mirror) -> None:
    """The ask names no command, so nothing about it can be vouched for and it
    is the reader's to answer — one card, no note. It was refused instead: the
    rule glob was classified as the command and the fence quoted it back as a
    write outside the chat's folder, which is the "no card, wrong sentence"
    pair a reader met on ``ls`` in a chat whose chip said Default."""
    assert await in_default.ask(_parent_shell_ask()) == "parked"
    entries = in_default.wire()
    assert len(in_default.prompting(entries)) == 1
    assert in_default.notes(entries) == []
