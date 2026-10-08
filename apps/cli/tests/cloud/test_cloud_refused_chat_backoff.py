"""A chat the gateway refuses is not retried every poll, costs no other chat
its session, and does not keep a folder it will not be served from.

The refused-start branch used to remember the refusal and nothing else: the
next poll (15 s later) made room for the chat first — on a box at its cap,
that put the idlest served chat to sleep — then took the refused chat's folder
again, opened it, and was refused again. An org out of credit with one chat
bound to a full box cost another reader their session every 15 s, and the
refused chat's lease stayed with a box that would never serve it.

Pinned here, through the fake mirrors, the fake folders and the service clock
(no sleeps):

* a refused chat waits, doubling to a ceiling, before it is opened again —
  and news on its row (a reader said something) ends the wait at once;
* a retry of a chat whose last start was refused never evicts a served chat;
  it is served only into a slot that is free;
* a refusal that is a verdict (no credit, not the publisher) hands the folder
  back; one that is transient (``not_found``, ``timeout``) keeps it;
* a condition that heals by itself (a 503 or 429 on the folder take, a
  ``not_found`` mid-turn, a start that met a connection reset) is waited out
  and never put on the chat as a refusal until it has outlasted its tries,
  and a message ends the take's wait as it ends a start's.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from _mirror_service import Clock, build_service
from alkera_cli.cloud.faults import TRANSIENT_LIMIT, refusal_kind
from alkera_cli.cloud.mirror import ChatMirrorRefusedError
from alkera_cli.cloud.publisher_identity import PublishingRefusal
from alkera_cli.cloud.rest import CloudApiError
from alkera_cli.cloud.start_failures import UNOPENED_SENTENCE
from alkera_cli.harness.adapter import HarnessUnavailableError
from alkera_sdk.client import AlkeraHTTPError

MACHINE = "machine:x"
IDLE = "chat-idle"
BROKE = "chat-broke"
POLL = 15.0


class Folders:
    """Custody that takes every folder at once and remembers which it holds."""

    def __init__(self) -> None:
        self.enabled = True
        self.held_now: dict[str, Any] = {}
        self.takes: list[str] = []
        #: What the next takes of a chat raise, in order; empty takes it.
        self.fail_take: dict[str, list[BaseException]] = {}

    def bind_machine(self, machine_id: str) -> None:
        return None

    def take(self, chat_id: str, chat: Any, *, instance: str) -> Any:
        self.takes.append(chat_id)
        if self.fail_take.get(chat_id):
            raise self.fail_take[chat_id].pop(0)
        held = SimpleNamespace(record=SimpleNamespace(node_id=f"node-{chat_id}"), live=None)
        self.held_now[chat_id] = held
        return held

    def held(self, chat_id: str) -> Any:
        return self.held_now.get(chat_id)

    def live(self, chat_id: str, working_dir: Path) -> Any:
        return None

    def owed(self) -> list[str]:
        return []

    def beat(self, chat_id: str) -> bool:
        return chat_id in self.held_now

    def push(self, chat_id: str) -> Any:
        return None

    def hand_back(
        self, chat_id: str, *, recover: bool = True, ending: str | None = None, gone: bool = False
    ) -> Any:
        self.held_now.pop(chat_id, None)
        return None

    def release(self, chat_id: str) -> bool:
        return self.held_now.pop(chat_id, None) is not None


class Rig:
    """A box capped at ``cap`` chats serving ``IDLE``; ``BROKE`` is bound to
    it too, and every start of ``BROKE`` is refused with ``code``."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        code: str = "insufficient_credit",
        cap: int = 1,
        failure: BaseException | None = None,
    ) -> None:
        self.clock = Clock()
        self.folders = Folders()
        self.idle = {"id": IDLE, "machine_id": MACHINE, "last_seq": 1}
        self.broke = {"id": BROKE, "machine_id": MACHINE, "last_seq": 1, "pending_turn": True}
        self.chats: list[dict[str, Any]] = [self.idle]
        self.reported: list[tuple[str, str]] = []
        #: Every refusal put on a chat, with its sentence.
        self.refusals: list[tuple[str, str]] = []
        self.kinds: list[str | None] = []
        self.starts: dict[str, int] = {}
        self.service, self.built = build_service(
            tmp_path,
            clock=self.clock,
            folders=self.folders,
            max_mirrors=cap,
            poll_interval=POLL,
        )
        self.service._machine_id = MACHINE
        self.service._rest.list_chats = self.list_chats  # type: ignore[method-assign]
        self.service._rest.get_chat = self.get_chat  # type: ignore[method-assign]
        self.service._rest.report_publisher_state = self.report  # type: ignore[method-assign]
        plain = self.service._mirror_factory

        def factory(chat_id: str, chat: dict[str, Any]) -> Any:
            mirror = plain(chat_id, chat)
            self.starts[chat_id] = self.starts.get(chat_id, 0) + 1
            if chat_id == BROKE:
                mirror.fail_start = failure or ChatMirrorRefusedError(BROKE, code)
            return mirror

        self.service._mirror_factory = factory

    async def list_chats(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": list(self.chats), "next_cursor": None}

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        return next(chat for chat in self.chats if chat["id"] == chat_id)

    async def report(
        self,
        chat_id: str,
        *,
        state: str,
        reason: str = "",
        refusal_kind: str | None = None,
        ending: str | None = None,
    ) -> None:
        self.reported.append((chat_id, state))
        if state == "refused":
            self.refusals.append((chat_id, reason))
            self.kinds.append(refusal_kind)

    async def poll(self, *, after: float = POLL) -> None:
        """One poll tick: the clock moves on by the interval, then the pass."""
        self.clock.advance(after)
        await self.service.sync_once()
        await self.service.settle_background()


async def test_a_refused_chat_waits_doubling_before_it_is_opened_again(tmp_path: Path) -> None:
    rig = Rig(tmp_path, code="insufficient_credit", cap=2)
    rig.chats.append(rig.broke)
    await rig.poll()
    assert rig.starts[BROKE] == 1

    # The first wait is twice the poll interval: the next tick does not open it.
    await rig.poll()
    assert rig.starts[BROKE] == 1, "a refused chat was opened again on the very next poll"
    await rig.poll()
    assert rig.starts[BROKE] == 2

    # Refused twice in a row: the wait doubled, so two more ticks are not enough.
    await rig.poll()
    await rig.poll()
    await rig.poll()
    assert rig.starts[BROKE] == 2
    await rig.poll()
    assert rig.starts[BROKE] == 3


async def test_news_on_the_row_ends_the_wait_at_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path, code="insufficient_credit", cap=2)
    rig.chats.append(rig.broke)
    await rig.poll()
    assert rig.starts[BROKE] == 1

    rig.broke["last_seq"] = 2  # the reader said something (credit may be back)
    await rig.poll()
    assert rig.starts[BROKE] == 2


async def test_a_retried_refusal_never_puts_a_served_chat_to_sleep(tmp_path: Path) -> None:
    rig = Rig(tmp_path, code="insufficient_credit", cap=2)
    await rig.poll()
    assert set(rig.service.mirrors) == {IDLE}
    rig.chats.append(rig.broke)
    await rig.poll()
    assert rig.starts[BROKE] == 1

    # The box is now full with a second served chat, and the first is idle.
    other = {"id": "chat-other", "machine_id": MACHINE, "last_seq": 1}
    rig.chats.append(other)
    await rig.poll()
    assert set(rig.service.mirrors) == {IDLE, "chat-other"}

    for _ in range(12):
        await rig.poll(after=60.0)
    assert set(rig.service.mirrors) == {IDLE, "chat-other"}, (
        "a chat that will be refused again took a served chat's slot"
    )
    assert not rig.built[IDLE].stopped and not rig.built["chat-other"].stopped
    assert rig.starts[BROKE] == 1, "with no free slot, the refused chat is not opened again"


def _wont_start() -> HarnessUnavailableError:
    return HarnessUnavailableError("agent session 'ses_x' pinned in this chat's manifest is gone")


async def test_a_chat_whose_start_keeps_failing_never_puts_a_served_chat_to_sleep(
    tmp_path: Path,
) -> None:
    """Seen on a restarted box: a chat that failed to start was tried again
    fifteen times in 37 minutes, and every try put another reader's chat to
    sleep to make room for it. After two failed starts in a row it takes only
    a slot that is already free."""
    rig = Rig(tmp_path, cap=2, failure=_wont_start())
    rig.chats = [rig.broke]
    await rig.poll()
    await rig.poll(after=60.0)
    assert rig.starts[BROKE] == 2, "two failed starts, both in a free slot"

    other = {"id": "chat-other", "machine_id": MACHINE, "last_seq": 1}
    rig.chats += [rig.idle, other]
    await rig.poll(after=1.0)
    assert set(rig.service.mirrors) == {IDLE, "chat-other"}

    for _ in range(12):
        await rig.poll(after=400.0)
    assert set(rig.service.mirrors) == {IDLE, "chat-other"}
    assert not rig.built[IDLE].stopped and not rig.built["chat-other"].stopped
    assert rig.starts[BROKE] == 2, "with no free slot, it is not opened again"
    # The reader is told it waits for a slot, once for the whole wait.
    assert rig.reported.count((BROKE, "waiting")) == 1


async def test_a_chat_the_full_box_cannot_open_reads_waiting_until_it_is_served(
    tmp_path: Path,
) -> None:
    """Seen under load: chats with a message waiting read ``ready`` for four
    minutes while the box served others. The box says ``waiting`` once when it
    has no slot to give, and ``publishing`` when it opens the chat."""
    rig = Rig(tmp_path, cap=1)
    await rig.poll()
    rig.built[IDLE].busy = True  # something runs there: it is not put to sleep
    waiting = {"id": "chat-waiting", "machine_id": MACHINE, "last_seq": 1, "pending_turn": True}
    rig.chats.append(waiting)
    for _ in range(3):
        await rig.poll()
    assert rig.reported.count(("chat-waiting", "waiting")) == 1
    assert "chat-waiting" not in rig.service.mirrors

    # The served chat goes idle: the next pass makes room and opens the chat.
    rig.built[IDLE].busy = False
    await rig.poll()
    assert "chat-waiting" in rig.service.mirrors


async def test_one_failed_start_may_still_make_room(tmp_path: Path) -> None:
    """A single failure can be a passing fault (a gateway hiccup): the next
    try may still put the idlest chat to sleep, or a chat on a full box could
    wait a day for a slot."""
    rig = Rig(tmp_path, cap=2, failure=_wont_start())
    rig.chats = [rig.broke]
    await rig.poll()
    assert rig.starts[BROKE] == 1

    other = {"id": "chat-other", "machine_id": MACHINE, "last_seq": 1}
    rig.chats += [rig.idle, other]
    await rig.poll(after=1.0)
    assert set(rig.service.mirrors) == {IDLE, "chat-other"}

    await rig.poll(after=60.0)
    assert rig.starts[BROKE] == 2, "the retry made room"
    assert rig.built[IDLE].stopped or rig.built["chat-other"].stopped


@pytest.mark.parametrize(
    ("code", "released"),
    [
        pytest.param("insufficient_credit", True, id="no-credit-hands-the-folder-back"),
        pytest.param("not_publisher", True, id="not-the-publisher-hands-the-folder-back"),
        pytest.param("not_found", False, id="transient-keeps-the-folder"),
        pytest.param("timeout", False, id="timeout-keeps-the-folder"),
    ],
)
async def test_a_verdict_hands_the_folder_back(tmp_path: Path, code: str, released: bool) -> None:
    rig = Rig(tmp_path, code=code, cap=2)
    rig.chats.append(rig.broke)
    await rig.poll()

    assert rig.folders.takes.count(BROKE) == 1
    assert (rig.folders.held(BROKE) is None) is released
    assert BROKE not in rig.service.mirrors
    assert rig.service.refused == {BROKE: code}
    if released:
        assert (BROKE, "refused") in rig.reported


# --- a condition that heals by itself is not a refusal ---------------------------


def _unreachable() -> httpx.ConnectError:
    return httpx.ConnectError("refused", request=httpx.Request("GET", "https://drive.invalid/x"))


def _drive(status: int) -> AlkeraHTTPError:
    return AlkeraHTTPError(label="files.lease", status=status, code=None, message="", trace_id=None)


@pytest.mark.parametrize(
    ("cause", "kind"),
    [
        pytest.param("not_found", "transient", id="code-not-found"),
        pytest.param("timeout", "transient", id="code-timeout"),
        pytest.param("forbidden", "verdict", id="code-forbidden"),
        pytest.param("insufficient_credit", "verdict", id="code-no-credit"),
        pytest.param(_drive(503), "transient", id="drive-503"),
        pytest.param(_drive(429), "transient", id="drive-429"),
        pytest.param(_drive(403), "verdict", id="drive-403"),
        pytest.param(_drive(404), "verdict", id="drive-404"),
        pytest.param(
            CloudApiError(502, {}, method="GET", path="/x"), "transient", id="backend-502"
        ),
        pytest.param(CloudApiError(409, {}, method="GET", path="/x"), "verdict", id="backend-409"),
        pytest.param(_unreachable(), "transient", id="connect-error"),
        pytest.param(ConnectionResetError("reset"), "transient", id="connection-reset"),
        pytest.param(_wont_start(), "verdict", id="agent-session-gone"),
        pytest.param(ValueError("bad pin"), "verdict", id="a-defect"),
    ],
)
def test_one_classifier_decides_transient_or_verdict(cause: Any, kind: str) -> None:
    assert refusal_kind(cause) == kind


@pytest.mark.parametrize(
    "fault",
    [
        pytest.param(_drive(503), id="drive-503"),
        pytest.param(_drive(429), id="drive-429"),
        pytest.param(_unreachable(), id="drive-unreachable"),
    ],
)
async def test_a_passing_fault_on_the_folder_take_is_not_put_on_the_chat(
    tmp_path: Path, fault: BaseException
) -> None:
    """A new chat's first take meets the burst its own creation caused. It is
    tried again on a doubling wait and the chat never reads refused for it;
    only a fault that outlasts every try is said, and said once."""
    rig = Rig(tmp_path, cap=2)
    rig.chats = [rig.broke]
    rig.folders.fail_take[BROKE] = [fault] * (TRANSIENT_LIMIT + 2)
    for _ in range(TRANSIENT_LIMIT - 1):
        await rig.poll(after=400.0)
    assert rig.folders.takes.count(BROKE) == TRANSIENT_LIMIT - 1, "each wait ends in a retry"
    assert rig.refusals == [], "a passing fault on the take was put on the chat"

    await rig.poll(after=400.0)
    await rig.poll(after=400.0)
    assert [chat for chat, _ in rig.refusals] == [BROKE], "said once, after the last try"
    assert "folder could not be taken" in rig.refusals[0][1]


async def test_a_take_the_drive_refuses_outright_is_said_at_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path, cap=2)
    rig.chats = [rig.broke]
    rig.folders.fail_take[BROKE] = [_drive(403)]
    await rig.poll()
    assert [chat for chat, _ in rig.refusals] == [BROKE]


async def test_a_message_ends_the_wait_after_a_failed_take(tmp_path: Path) -> None:
    rig = Rig(tmp_path, cap=2)
    rig.chats = [rig.broke]
    rig.folders.fail_take[BROKE] = [_drive(503)]
    await rig.poll()
    assert rig.folders.takes.count(BROKE) == 1

    await rig.poll(after=1.0)
    assert rig.folders.takes.count(BROKE) == 1, "the wait holds while nothing is new"
    rig.broke["last_seq"] = 2  # the reader sends a message
    await rig.poll(after=1.0)
    assert rig.folders.takes.count(BROKE) == 2, "the message did not end the take's wait"


@pytest.mark.parametrize(
    ("code", "said"),
    [
        pytest.param("not_found", False, id="document-not-there-yet"),
        pytest.param("timeout", False, id="hello-timed-out"),
        pytest.param("forbidden", True, id="forbidden-is-a-verdict"),
    ],
)
async def test_a_refusal_mid_turn_is_said_only_when_it_is_a_verdict(
    tmp_path: Path, code: str, said: bool
) -> None:
    rig = Rig(tmp_path, cap=2)
    await rig.poll()
    await rig.service._on_mirror_refused(IDLE, PublishingRefusal(code, "mid-turn"))
    await rig.service.settle_background()
    assert ([chat for chat, _ in rig.refusals] == [IDLE]) is said
    assert (IDLE in rig.service.refused) is said


async def test_a_start_that_met_a_passing_fault_is_not_put_on_the_chat(tmp_path: Path) -> None:
    rig = Rig(tmp_path, cap=2, failure=ConnectionResetError("the gateway reset the connection"))
    rig.chats = [rig.broke]
    for _ in range(TRANSIENT_LIMIT - 1):
        await rig.poll(after=400.0)
    assert rig.starts[BROKE] == TRANSIENT_LIMIT - 1
    assert rig.refusals == []

    for _ in range(3):
        await rig.poll(after=400.0)
    assert [chat for chat, _ in rig.refusals] == [BROKE], "said once, after the last try"


async def test_a_transient_start_refusal_is_said_once_it_outlasts_its_tries(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, code="not_found", cap=2)
    rig.chats = [rig.broke]
    for _ in range(TRANSIENT_LIMIT - 1):
        await rig.poll(after=400.0)
    assert rig.refusals == []
    for _ in range(3):
        await rig.poll(after=400.0)
    assert rig.refusals == [(BROKE, UNOPENED_SENTENCE)]
    assert rig.kinds == ["transcript_unopened"]


@pytest.mark.parametrize(
    ("session_state", "words", "kind"),
    [
        pytest.param("starting", "could not start the chat", "start_failed", id="never-ran"),
        pytest.param("waking", "could not resume the chat", "resume_failed", id="resumed"),
    ],
)
async def test_a_failed_start_tells_a_first_start_from_a_resume(
    tmp_path: Path, session_state: str, words: str, kind: str
) -> None:
    rig = Rig(tmp_path, cap=2, failure=_wont_start())
    rig.broke["session_state"] = session_state
    rig.chats = [rig.broke]
    await rig.poll()
    assert len(rig.refusals) == 1
    assert words in rig.refusals[0][1]
    assert rig.kinds == [kind], "the reader is shown the kind, not the sentence"
