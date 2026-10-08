"""What a browser chat's permission card may promise, on the box that keeps it.

A card offers "Always allow" where something narrower than everything would be
recorded by it — Alkera's own ``(capability, operation)`` rule, written when the
ask goes through the decision engine. A cloud session puts its workspace fence
ahead of that engine, and a command whose reach the fence cannot read never
reaches it: by the fence's own contract such a command runs on a person's
approval EVERY time. ``uv run python -c '…'`` is one of those — the destination
reader will not parse an interpreter — so the card that offered a standing grant
over it promised a rule nothing wrote, and the next identical command asked
again under the settled words "Always allow by …".

The box is where that is settled, because the box is what enforces it. The ask
it publishes carries only the decisions it can honour, and the relay guard then
refuses an answer naming one it did not — so a card drawn before the grant was
withheld cannot mint one either.

A command the fence reads end to end does reach the engine, and there the
answer is recorded as the chat owner's own: the box keeps one policy file for
every chat placed on it, of every org, so a rule names the person it answers
for and decides nobody else's chat. A chat the box was not told the owner of
has nobody to record an answer for, and offers none.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import yaml
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.harness import HarnessRuntime, PermissionBroker
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.adapters.opencode_translate import ask_options
from alkera_cli.host import paths
from alkera_cli.plugins.plugin_base.permissions.bash import classify_command
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import PermissionRequest

_T = datetime(2026, 9, 21, tzinfo=UTC)

CHAT_ID = "chat-grant"
OWNER = "00000000-0000-4000-8000-000000000001"
COLLEAGUE = "00000000-0000-4000-8000-000000000002"

#: A command the destination reader will not parse (an interpreter), and one it
#: reads end to end into this chat's own folder. Both are write-class, so the
#: stance prompts on both and the fence's verdict is the only difference.
UNREADABLE = "uv run python -c 'print(7)'"
READABLE = "echo hi > out.txt"

_ALL_DECISIONS = ["allow_once", "allow_always", "reject_once", "reject_always"]
_NO_STANDING_GRANT = ["allow_once", "reject_once"]


def _shell_ask(command: str, *, request_id: str) -> PermissionRequest:
    """A bash ask shaped as the opencode translator shapes one, carrying the
    decisions IT offers — the standing grants included, since Alkera's own rule
    is what it counts on to record them."""
    descriptor = classify_command(command)
    options = ask_options(always=["**"], descriptor=descriptor)
    assert [option.option_id for option in options] == _ALL_DECISIONS, (
        "the translator no longer offers a standing grant here; nothing left to withhold"
    )
    return PermissionRequest(
        event_id=f"ev-{request_id}",
        time=_T,
        session_id=CHAT_ID,
        request_id=request_id,
        tool_call_id=f"call-{request_id}",
        permission_kind="bash",
        canonical_kind="shell",
        patterns=[command],
        subject=descriptor.model_dump(mode="json"),
        options=options,
    )


class _Mirror:
    def __init__(self, mirror: ChatMirror, adapter: FakeAdapter) -> None:
        self.mirror = mirror
        self.adapter = adapter
        self._seen: list[dict[str, Any]] = []

    async def offered(self, request: PermissionRequest) -> list[str]:
        """Put ``request`` on the agent's stream and return the decisions the
        browser is given for it — read off this chat's outbound entries, which
        are what a card is built from."""
        await self.adapter.feed(request)
        return await self._until(
            lambda: next(
                (
                    [str(option["option_id"]) for option in entry["payload"].get("options", [])]
                    for entry in self._drain()
                    if entry.get("kind") == "permission.request"
                    and entry.get("payload", {}).get("request_id") == request.request_id
                ),
                None,
            ),
            f"no card for {request.request_id}",
        )

    async def parked(self, request_id: str) -> None:
        await self._until(
            lambda: True if request_id in self.mirror.pending_interrupts else None,
            f"{request_id} never reached a reader",
        )

    async def replied(self, request_id: str) -> str:
        """What the box answered the agent's adapter with."""
        return await self._until(
            lambda: next(
                (
                    str(option)
                    for replied, option in self.adapter.permission_replies
                    if replied == request_id
                ),
                None,
            ),
            f"no reply for {request_id}",
        )

    def answer(self, request_id: str, option: str, *, by: str = OWNER) -> None:
        """A reader's answer, as the relay delivers it: the server names who
        gave it."""
        self.mirror._answer_interrupt(request_id, {"option_id": option, "user_id": by})

    def standing_rules(self) -> list[dict[str, Any]]:
        """The rules this box recorded — the store a standing grant means. Read
        off disk rather than from a loaded config, so a rule written by ANY door
        shows up here whether or not the policy would honour it."""
        local = self.mirror._runtime.project.path / "permissions.local.yml"
        if not local.exists():
            return []
        return list((yaml.safe_load(local.read_text()) or {}).get("rules") or [])

    def _drain(self) -> list[dict[str, Any]]:
        while not self.mirror._outbound.empty():
            entry = self.mirror._outbound.get_nowait()
            if "__meta__" not in entry:
                self._seen.append(entry)
        return list(self._seen)

    async def _until(self, read: Callable[[], Any], complaint: str) -> Any:
        """Poll ``read`` until it returns something, or fail saying what never
        arrived — a hang here is a contract that changed, not a slow box."""
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            value = read()
            if value is not None:
                return value
            await asyncio.sleep(0.02)
        raise AssertionError(complaint)


@contextlib.asynccontextmanager
async def _open_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, owner: str
) -> AsyncIterator[_Mirror]:
    home = tmp_path / "alkera-home"
    home.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    workspace = tmp_path / "work"
    (workspace / "src").mkdir(parents=True)
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(ProjectDirectory(workspace / ".alkera"), adapter_factory=factory)
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
        user_id=owner,
        owner_user_id=owner or None,
        permission_mode="default",
    )
    handle._session = await handle.open_session(
        PermissionBroker(handle._resolve_permission, default_timeout_seconds=None)
    )
    handle._session.set_permission_mode("default")
    pump = asyncio.get_running_loop().create_task(handle._pump(handle._session.subscribe()))
    handle._outbound = asyncio.Queue()
    try:
        yield _Mirror(handle, factory.adapters[0])
    finally:
        pump.cancel()
        with contextlib.suppress(BaseException):
            await pump
        await runtime.close_chat(CHAT_ID)


@pytest.fixture
async def chat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Mirror]:
    """A browser chat in the stance that asks, with the box's REAL fence on the
    session: the whole path an ask takes from the agent to a reader."""
    async with _open_chat(tmp_path, monkeypatch, owner=OWNER) as opened:
        yield opened


@pytest.fixture
async def ownerless(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Mirror]:
    """The same chat on a box that was not told whose chat it is."""
    async with _open_chat(tmp_path, monkeypatch, owner="") as opened:
        yield opened


async def test_a_command_the_fence_cannot_read_offers_no_standing_grant(chat: _Mirror) -> None:
    """The reported card. Its "Always allow" named a rule this path never
    writes, and the very next identical command was asked again under it."""
    assert await chat.offered(_shell_ask(UNREADABLE, request_id="req-1")) == _NO_STANDING_GRANT


async def test_a_command_the_fence_reads_offers_the_owners_standing_grant(chat: _Mirror) -> None:
    """A write the fence reads end to end goes through the engine, which records
    the answer as this chat owner's: the card offers what the box will honour."""
    assert await chat.offered(_shell_ask(READABLE, request_id="req-2")) == _ALL_DECISIONS


async def test_a_chat_with_no_owner_offers_no_standing_grant(ownerless: _Mirror) -> None:
    """With nobody to record an answer for, the box offers none, readable or not."""
    assert await ownerless.offered(_shell_ask(READABLE, request_id="req-o1")) == (
        _NO_STANDING_GRANT
    )


async def test_a_standing_answer_in_a_chat_with_no_owner_is_refused(ownerless: _Mirror) -> None:
    """An older client's "Always allow" on such an ask is refused by the relay
    guard and records nothing; deciding it once still runs the command."""
    assert await ownerless.offered(_shell_ask(READABLE, request_id="req-o2")) == (
        _NO_STANDING_GRANT
    )
    await ownerless.parked("req-o2")
    ownerless.answer("req-o2", "allow_always")
    assert ownerless.mirror._ignored_relays == 1
    ownerless.answer("req-o2", "allow_once")
    assert await ownerless.replied("req-o2") == "allow_once"
    assert ownerless.standing_rules() == [], "the box recorded an answer for nobody"


async def test_a_standing_answer_to_such_an_ask_is_refused_and_the_ask_stays(
    chat: _Mirror,
) -> None:
    """A card drawn before the grant was withheld cannot mint one after it: the
    relay guard refuses an option the ask does not offer, and the ask is still
    the reader's to answer. Deciding it once then runs the command."""
    ask = _shell_ask(UNREADABLE, request_id="req-3")
    assert await chat.offered(ask) == _NO_STANDING_GRANT
    await chat.parked("req-3")
    chat.answer("req-3", "allow_always")
    assert chat.mirror._ignored_relays == 1
    assert chat.mirror.pending_interrupts == ["req-3"]
    chat.answer("req-3", "allow_once")
    assert await chat.replied("req-3") == "allow_once"


async def test_the_next_identical_command_is_a_second_card(chat: _Mirror) -> None:
    """Why the grant is withheld and not honoured: the fence's contract is that
    a command whose reach it cannot read runs on a person's approval every time.
    The second card offers no standing grant either — nothing about answering
    the first changed what this path can record."""
    assert await chat.offered(_shell_ask(UNREADABLE, request_id="req-4")) == _NO_STANDING_GRANT
    await chat.parked("req-4")
    chat.answer("req-4", "allow_once")
    assert await chat.replied("req-4") == "allow_once"
    assert await chat.offered(_shell_ask(UNREADABLE, request_id="req-5")) == _NO_STANDING_GRANT


@pytest.mark.parametrize(
    ("answer", "decision"),
    [
        pytest.param("allow_always", "allow", id="allow"),
        pytest.param("reject_always", "deny", id="reject"),
    ],
)
async def test_a_standing_answer_is_recorded_as_the_owners_and_decides_the_next_ask(
    chat: _Mirror, answer: str, decision: str
) -> None:
    """The reader's "always" is recorded in the box's policy file naming the
    chat owner, and decides the next identical ask with no card. The agent is
    told only the decision on this call: a vendor told ``always`` learns a
    coarse rule of its own and stops raising the ask the fence judges."""
    assert await chat.offered(_shell_ask(READABLE, request_id="req-6")) == _ALL_DECISIONS
    await chat.parked("req-6")
    chat.answer("req-6", answer)
    once = "allow_once" if decision == "allow" else "reject_once"
    assert await chat.replied("req-6") == once
    assert [(r.get("decision"), r.get("owner")) for r in chat.standing_rules()] == [
        (decision, OWNER)
    ]
    await chat.adapter.feed(_shell_ask(READABLE, request_id="req-6b"))
    assert await chat.replied("req-6b") == once
    assert "req-6b" not in chat.mirror.pending_interrupts


async def test_a_standing_answer_to_an_unreadable_ask_records_nothing(chat: _Mirror) -> None:
    """The other half, and the one the relay could have walked around: an ask
    whose reach the fence cannot prove never reaches the engine, so a relayed
    "Always allow" on it must leave the store exactly as it found it. Answering
    it once still runs the command."""
    assert await chat.offered(_shell_ask(UNREADABLE, request_id="req-7")) == _NO_STANDING_GRANT
    await chat.parked("req-7")
    chat.answer("req-7", "allow_always")
    chat.answer("req-7", "allow_once")
    assert await chat.replied("req-7") == "allow_once"
    assert chat.standing_rules() == [], "the relay minted a rule the fence never backed"


@pytest.mark.parametrize("answer", ["allow_always", "reject_always"])
async def test_a_colleagues_standing_answer_decides_only_this_ask(
    chat: _Mirror, answer: str
) -> None:
    """A colleague who may answer in the owner's chat answers this one ask. An
    "Always" from them would become the OWNER's rule on this box, applied in
    the owner's other chats without asking, so the box records nothing and the
    next identical ask is a card again. The server refuses such an answer
    first; this is the box's own line for one that arrives anyway."""
    assert await chat.offered(_shell_ask(READABLE, request_id="req-8")) == _ALL_DECISIONS
    await chat.parked("req-8")
    chat.answer("req-8", answer, by=COLLEAGUE)
    once = "allow_once" if answer == "allow_always" else "reject_once"
    assert await chat.replied("req-8") == once
    assert chat.standing_rules() == [], "a colleague's answer became the owner's rule"
    assert await chat.offered(_shell_ask(READABLE, request_id="req-8b")) == _ALL_DECISIONS
    await chat.parked("req-8b")


async def test_a_standing_answer_naming_nobody_decides_only_this_ask(chat: _Mirror) -> None:
    """A relay or a recorded answer that names nobody (an older server) is
    not the owner's: it decides this ask and records no rule."""
    assert await chat.offered(_shell_ask(READABLE, request_id="req-9")) == _ALL_DECISIONS
    await chat.parked("req-9")
    chat.mirror._answer_interrupt("req-9", {"option_id": "allow_always"})
    assert await chat.replied("req-9") == "allow_once"
    assert chat.standing_rules() == []
