"""The runtime chokepoint on an ask the fence cannot vouch for.

A ``PathFence`` answers two questions ahead of every allow: the location that
escapes, and — new — whether the ask's reach could not be proved at all. The
second has one contract: the mode's own refusal still comes first, then a
person is asked where the stance asks one. Bypass asks nobody and runs the
command through the ordinary ladder, exactly as it runs a classified write —
what bypass hands over is the asking, never the boundary, so a location the
fence DID read as an escape is still refused there. One decision row each way.

A fenced session is a cloud box's, and a box keeps one local policy file for
every chat placed on it, of every org. Nothing there could record an answer as
one person's, so no ask on such a session offers a standing grant, an answer
that names one anyway binds the call it was given on, and a rule already in
that file decides nothing there. A local session keeps its standing answers.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.contracts.tool_types import ActionDescriptor
from alkera_cli.harness import HarnessRuntime, PathFence, PermissionBroker
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.runtime import ChatSession
from alkera_cli.plugins.plugin_base.permissions.config import add_local_rule
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import PermissionOption, PermissionRequest

_T = datetime(2026, 9, 20, tzinfo=UTC)

#: What the opencode translator offers on a shell ask it CAN vouch for — the
#: real four, standing grants included. Withholding happens downstream.
_ALL_DECISIONS = (
    ("allow_once", "Allow once"),
    ("allow_always", "Always allow"),
    ("reject_once", "Reject once"),
    ("reject_always", "Always reject"),
)


def _shell_ask(command: str, *, effect: str = "read") -> PermissionRequest:
    return PermissionRequest(
        event_id=f"ev-{abs(hash(command)) % 10_000}",
        time=_T,
        session_id="chat-1",
        request_id=f"req-{abs(hash(command)) % 10_000}",
        tool_call_id="call-1",
        permission_kind="bash",
        canonical_kind="shell",
        patterns=[command],
        subject={
            "capability": "shell",
            "effect": effect,
            "operation": "bash",
            "raw": command,
            "targets": [],
            "classifier": "opencode-tool",
        },
        options=[
            PermissionOption(option_id=option_id, name=name) for option_id, name in _ALL_DECISIONS
        ],
    )


class _Person:
    def __init__(self, option: str) -> None:
        self.option = option
        self.asked: list[str] = []

    async def __call__(self, request: PermissionRequest) -> Any:
        self.asked.append(request.request_id)
        return self.option


@pytest.fixture
async def open_session(tmp_path: Path) -> AsyncIterator[Any]:
    runtimes: list[HarnessRuntime] = []

    async def _open(
        person: _Person,
        *,
        unknown: bool | Callable[[PermissionRequest], bool],
        escapes: str | None = None,
        raises: bool = False,
        fenced: bool = True,
    ) -> ChatSession:
        """``unknown`` is the fence's verdict on reach: a bool for every ask, or
        the predicate itself when a test needs it to differ per ask. ``raises``
        makes BOTH of the fence's questions throw — the real ones are one call
        into the same judge, so whatever breaks one breaks the other.
        ``fenced=False`` opens the local session a laptop runs, with no fence."""
        workspace = tmp_path / "work"
        workspace.mkdir(exist_ok=True)
        runtime = HarnessRuntime(
            ProjectDirectory(workspace / ".alkera"),
            adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
        )
        runtimes.append(runtime)

        def _broken(request: PermissionRequest) -> Any:
            raise RuntimeError("the fence could not run")

        must_ask: Callable[[PermissionRequest], bool] | None
        if raises:
            must_ask = _broken
        elif callable(unknown):
            must_ask = unknown
        else:
            must_ask = (lambda request: True) if unknown else None
        return await runtime.open_chat(
            create=True,
            harness_type="agent",
            permission_broker=PermissionBroker(person, default_timeout_seconds=None),
            path_fence=PathFence(
                escape=_broken if raises else (lambda request: escapes),
                reason="outside",
                must_ask=must_ask,
            )
            if fenced
            else None,
        )

    try:
        yield _open
    finally:
        for runtime in runtimes:
            for session_id in list(runtime.open_session_ids):
                await runtime.close_chat(session_id)


def _rows(session: ChatSession) -> list[tuple[str, str]]:
    path = session.decision_sink.directory / "decisions.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [(row["decision"], row["decided_by"]) for row in rows]


@pytest.mark.parametrize("mode", ["default", "auto"])
@pytest.mark.parametrize("answer", ["allow_once", "reject_once"])
async def test_an_unprovable_ask_is_the_persons_where_the_stance_asks(
    open_session: Any, mode: str, answer: str
) -> None:
    person = _Person(answer)
    session = await open_session(person, unknown=True)
    session.set_permission_mode(mode)
    ask = _shell_ask("git status")
    option, _reason, decided_by = await session._decide_permission(ask)
    assert person.asked == [ask.request_id]
    assert option == answer
    assert decided_by == "human"
    assert _rows(session) == [("allow" if answer == "allow_once" else "reject", "human")]


@pytest.mark.parametrize("effect", ["read", "write"])
async def test_an_unprovable_ask_runs_where_the_stance_runs_everything(
    open_session: Any, effect: str
) -> None:
    """Bypass's chip says it runs everything without asking. A command the fence
    cannot read is not a boundary it caught, so the stance decides it the way it
    decides a classified write: an allow, nobody asked, the policy on record."""
    person = _Person("reject_once")
    session = await open_session(person, unknown=True)
    session.set_permission_mode("bypass")
    option, reason, decided_by = await session._decide_permission(
        _shell_ask("python3 -c 'print(1)'", effect=effect)
    )
    assert person.asked == []
    assert option == "allow_once"
    assert reason is None
    assert decided_by not in ("human", "fence", "mode")
    rows = _rows(session)
    assert rows and all(decision == "allow" for decision, _ in rows)
    assert all(by not in ("human", "fence") for _, by in rows)


async def test_an_escape_the_fence_read_is_still_refused_in_bypass(open_session: Any) -> None:
    """The floor bypass does not waive: a location outside the session's bounds
    is the fence's refusal, with the fence's sentence, before any allow — and
    before the unprovable question is even asked."""
    person = _Person("allow_once")
    session = await open_session(person, unknown=True, escapes="/home/box/.alkera/auth.yml")
    session.set_permission_mode("bypass")
    option, reason, decided_by = await session._decide_permission(
        _shell_ask("cat /home/box/.alkera/auth.yml", effect="read")
    )
    assert person.asked == []
    assert option == "reject_once"
    assert decided_by == "fence"
    assert reason == "outside"
    assert _rows(session) == [("reject", "fence")]


@pytest.mark.parametrize("mode", ["read_only", "plan"])
async def test_the_modes_refusal_comes_before_the_fences_question(
    open_session: Any, mode: str
) -> None:
    """An analyst's stance runs no shell: the ask is the MODE's refusal, with the
    mode's sentence, and no person is asked about it."""
    person = _Person("allow_once")
    session = await open_session(person, unknown=True)
    session.set_permission_mode(mode)
    option, reason, decided_by = await session._decide_permission(
        _shell_ask("git status", effect="write")
    )
    assert person.asked == []
    assert option == "reject_once"
    assert decided_by == "mode"
    assert reason and "mode" in reason
    assert _rows(session) == [("reject", "mode")]


async def test_a_fence_that_never_says_unknown_leaves_the_ladder_alone(open_session: Any) -> None:
    """The control: with no ``must_ask``, a read-class shell ask in ``auto`` is
    decided by the ordinary ladder, and a read auto-allows with no person."""
    person = _Person("reject_once")
    session = await open_session(person, unknown=False)
    session.set_permission_mode("auto")
    option, _reason, decided_by = await session._decide_permission(_shell_ask("ls"))
    assert person.asked == []
    assert option == "allow_once"
    assert decided_by != "human"


# -- what such an ask may offer, and what an answer to it binds ---------------


async def _offered(session: ChatSession, ask: PermissionRequest) -> list[str]:
    """The decisions a reader is offered on ``ask`` — read off the stream every
    card is built from (the transcript, a cloud mirror, the TUI), not off the
    event the adapter handed in."""
    stream = session.subscribe()
    await session.publish_event(ask)
    async for event in stream:
        if isinstance(event, PermissionRequest) and event.request_id == ask.request_id:
            return [option.option_id for option in event.options]
    raise AssertionError("the ask never reached the stream")


def _never(request: PermissionRequest) -> bool:
    return False


def _raises_reach(request: PermissionRequest) -> bool:
    raise RuntimeError("the fence could not run")


@pytest.mark.parametrize(
    "reach",
    [
        pytest.param(True, id="unprovable"),
        pytest.param(_never, id="read-by-the-fence"),
        pytest.param(_raises_reach, id="judge-raised"),
    ],
)
async def test_a_fenced_ask_offers_no_standing_grant(open_session: Any, reach: Any) -> None:
    """A card is built from what the ask offers. On a box nothing records a
    standing answer as one person's, so a grant there would promise the reader
    a rule nothing writes, whether or not the fence could read the command."""
    session = await open_session(_Person("allow_once"), unknown=reach)
    offered = await _offered(session, _shell_ask("echo hi", effect="write"))
    assert offered == ["allow_once", "reject_once"]


async def test_a_local_ask_keeps_its_standing_grant(open_session: Any) -> None:
    """The complement: a laptop's session has no fence and one owner, and keeps
    every decision the translator offered."""
    session = await open_session(_Person("allow_once"), unknown=False, fenced=False)
    offered = await _offered(session, _shell_ask("echo hi", effect="write"))
    assert offered == ["allow_once", "allow_always", "reject_once", "reject_always"]


@pytest.mark.parametrize(
    ("answer", "bound"),
    [
        pytest.param("allow_always", "allow_once", id="allow"),
        pytest.param("reject_always", "reject_once", id="reject"),
    ],
)
@pytest.mark.parametrize(
    "unknown", [pytest.param(True, id="unprovable"), pytest.param(False, id="read")]
)
async def test_a_standing_answer_on_a_fenced_session_binds_one_call(
    open_session: Any, answer: str, bound: str, unknown: bool
) -> None:
    """A client that answers with a standing grant anyway — a card drawn before
    the offer was withdrawn, a scripted client — must not mint one on this path.
    opencode turns an ``always`` into a coarse prefix rule and then stops raising
    the ask for everything it matches, which would retire the fence for every
    later command under that prefix, and Alkera's own rule would land in the
    box's one policy file. The person's answer still decides the call they were
    asked about, and nothing is written."""
    person = _Person(answer)
    session = await open_session(person, unknown=unknown)
    session.set_permission_mode("default")
    ask = _shell_ask("uv run python -c 'print(7)'", effect="write")
    option, _reason, decided_by = await session._decide_permission(ask)
    assert person.asked == [ask.request_id]
    assert option == bound
    assert decided_by == "human"
    assert _rows(session) == [("allow" if bound == "allow_once" else "reject", "human")]
    assert not (tmp_path_of(session) / "permissions.local.yml").exists()


def tmp_path_of(session: ChatSession) -> Path:
    """The ``.alkera`` directory the session's runtime keeps its policy in."""
    return Path(session._runtime.project.path)


async def test_an_unprovable_command_is_asked_again_after_a_standing_answer(
    open_session: Any,
) -> None:
    """Why the offer is withheld rather than honoured: the fence's contract is
    that a command whose reach it cannot read runs on a person's approval EVERY
    time. A second, byte-identical one is a second ask."""
    person = _Person("allow_always")
    session = await open_session(person, unknown=True)
    session.set_permission_mode("default")
    command = "uv run python -c 'print(7)'"
    first = _shell_ask(command, effect="write")
    await session._decide_permission(first)
    again = _shell_ask(command, effect="write").model_copy(update={"request_id": "req-again"})
    option, _reason, decided_by = await session._decide_permission(again)
    assert person.asked == [first.request_id, "req-again"]
    assert (option, decided_by) == ("allow_once", "human")


@pytest.mark.parametrize(
    "unknown", [pytest.param(True, id="unprovable"), pytest.param(False, id="read")]
)
async def test_a_standing_answer_on_a_box_does_not_reach_the_next_chat(
    open_session: Any, unknown: bool
) -> None:
    """The leak this closes: one person's "Always allow" on a box used to land
    in the box's one ``permissions.local.yml`` and decide the next chat placed
    there, another org's included. The next chat's person is asked, and would
    refuse."""
    granter = _Person("allow_always")
    granted_in = await open_session(granter, unknown=unknown)
    granted_in.set_permission_mode("default")
    await granted_in._decide_permission(_shell_ask("echo hi", effect="write"))
    assert granter.asked

    other = _Person("reject_once")
    other_chat = await open_session(other, unknown=unknown)
    other_chat.set_permission_mode("default")
    ask = _shell_ask("echo hi", effect="write").model_copy(update={"request_id": "req-other"})
    option, _reason, decided_by = await other_chat._decide_permission(ask)
    assert other.asked == ["req-other"]
    assert (option, decided_by) == ("reject_once", "human")


@pytest.mark.parametrize(
    ("decision", "answer", "by_rule"),
    [
        pytest.param("allow", "reject_once", "allow_once", id="recorded-allow"),
        pytest.param("deny", "allow_once", "reject_once", id="recorded-deny"),
    ],
)
@pytest.mark.parametrize("fenced", [pytest.param(True, id="box"), pytest.param(False, id="laptop")])
async def test_a_rule_already_in_the_box_local_file_decides_nothing_there(
    open_session: Any, decision: str, answer: str, by_rule: str, fenced: bool
) -> None:
    """A box that ran an older build holds answers in its local file already.
    They are nobody's in particular, so on a fenced session they decide nothing
    either way: the person is asked, and their answer stands. The same file on
    a laptop is its one owner's, and decides."""
    person = _Person(answer)
    session = await open_session(person, unknown=False, fenced=fenced)
    session.set_permission_mode("default")
    ask = _shell_ask("echo hi", effect="write")
    recorded = ActionDescriptor.model_validate(ask.subject)
    assert add_local_rule(tmp_path_of(session), recorded, decision=decision, mode="default")
    option, _reason, decided_by = await session._decide_permission(ask)
    if fenced:
        assert person.asked == [ask.request_id]
        assert (option, decided_by) == (answer, "human")
    else:
        assert person.asked == []
        assert (option, decided_by) == (by_rule, "rule")


async def test_a_local_standing_grant_is_recorded_and_outlives_its_session(
    open_session: Any,
) -> None:
    """The clamp is the box's alone: on a laptop the answer is the one owner's,
    so the engine writes Alkera's own rule and a new session over the same
    project decides the covered command by it, with nobody asked."""
    granter = _Person("allow_always")
    granted_in = await open_session(granter, unknown=False, fenced=False)
    granted_in.set_permission_mode("default")
    option, _reason, decided_by = await granted_in._decide_permission(
        _shell_ask("echo hi", effect="write")
    )
    # A shell "always" never travels to the vendor as one; Alkera's rule carries it.
    assert (option, decided_by) == ("allow_once", "human")

    refuser = _Person("reject_once")
    restarted = await open_session(refuser, unknown=False, fenced=False)
    restarted.set_permission_mode("default")
    option, _reason, decided_by = await restarted._decide_permission(
        _shell_ask("echo hi again", effect="write")
    )
    assert refuser.asked == []
    assert (option, decided_by) == ("allow_once", "rule")


# -- a fence question that raises --------------------------------------------


@pytest.mark.parametrize(
    ("answer", "option"),
    [
        pytest.param("allow_once", "allow_once", id="allow"),
        pytest.param("allow_always", "allow_once", id="allow-always-binds-one-call"),
        pytest.param("reject_once", "reject_once", id="reject"),
    ],
)
async def test_a_fence_question_that_raises_is_asked_not_dropped(
    open_session: Any, answer: str, option: str
) -> None:
    """A fence that throws is a reach that was not proved — not the end of the
    chat. The exception used to leave ``_decide_permission`` and kill the
    permission loop, the one task that answers asks, so every later ask in that
    chat hung on a card nobody could settle. It is now the person's question,
    decided like any other the fence cannot vouch for."""
    person = _Person(answer)
    session = await open_session(person, unknown=False, raises=True)
    session.set_permission_mode("default")
    ask = _shell_ask("uv run python -c 'print(7)'", effect="write")
    decided = await session._decide_permission(ask)
    assert person.asked == [ask.request_id]
    assert (decided[0], decided[2]) == (option, "human")
    assert _rows(session) == [("allow" if option == "allow_once" else "reject", "human")]


async def test_a_second_ask_survives_a_fence_that_raised_on_the_first(open_session: Any) -> None:
    """What the wedge cost: the ask AFTER the one that threw. The loop that
    carries every ask must still be there, so the same session answers again."""
    person = _Person("allow_once")
    session = await open_session(person, unknown=False, raises=True)
    session.set_permission_mode("default")
    await session._decide_permission(_shell_ask("mkdir sub", effect="write"))
    second = _shell_ask("uv run pytest", effect="write")
    option, _reason, decided_by = await session._decide_permission(second)
    assert person.asked[-1] == second.request_id
    assert (option, decided_by) == ("allow_once", "human")


@pytest.mark.parametrize("mode", ["read_only", "plan"])
async def test_a_raising_fence_does_not_reopen_a_stance_that_refuses(
    open_session: Any, mode: str
) -> None:
    """Failing closed to a question must not become a way past the stance: an
    analyst's chat runs no shell, and a broken fence does not change that."""
    person = _Person("allow_once")
    session = await open_session(person, unknown=False, raises=True)
    session.set_permission_mode(mode)
    option, _reason, decided_by = await session._decide_permission(
        _shell_ask("uv run pytest", effect="write")
    )
    assert person.asked == []
    assert (option, decided_by) == ("reject_once", "mode")


# -- a fence question that raises, where the stance asks nobody --------------


def _raising_reach(request: PermissionRequest) -> bool:
    raise RuntimeError("the reach judge could not run")


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param("both", id="the-whole-judge"),
        pytest.param("reach", id="only-the-reach-question"),
    ],
)
async def test_a_judge_that_could_not_run_still_bounds_the_stance_that_asks_nobody(
    open_session: Any, broken: str
) -> None:
    """A boundary that could not be checked is not a boundary that was cleared.

    Bypass hands over the ASKING, never the bound — a location the fence read as
    an escape is refused there already. A judge that did not run read nothing,
    so it cleared nothing: without this, a fence that threw let
    ``cat …/auth.yml`` through in the one stance that asks nobody, which is the
    box's own credential file read by a command no one saw."""
    person = _Person("allow_once")
    session = await open_session(
        person,
        unknown=_raising_reach if broken == "reach" else False,
        raises=broken == "both",
    )
    session.set_permission_mode("bypass")
    option, reason, decided_by = await session._decide_permission(
        _shell_ask("cat /home/box/.alkera/auth.yml", effect="read")
    )
    assert person.asked == []
    assert (option, decided_by) == ("reject_once", "fence_error")
    assert reason and "could not" in reason
    assert _rows(session) == [("reject", "fence_error")]


async def test_a_broken_judge_refuses_in_bypass_without_ending_the_chat(
    open_session: Any,
) -> None:
    """The refusal is the action's, not the chat's: it carries a reason, so the
    model is told and the turn goes on, and the loop that serves every later ask
    is still there to serve the next one."""
    person = _Person("allow_once")
    session = await open_session(person, unknown=False, raises=True)
    session.set_permission_mode("bypass")
    first = await session._decide_permission(_shell_ask("uv run pytest", effect="write"))
    second = await session._decide_permission(_shell_ask("mkdir sub", effect="write"))
    assert first[0] == second[0] == "reject_once"
    assert first[1] and second[1], "a reason-less reject would end the turn"
    assert _rows(session) == [("reject", "fence_error"), ("reject", "fence_error")]


async def test_a_reach_the_judge_read_as_unprovable_still_runs_in_bypass(
    open_session: Any,
) -> None:
    """The line this draws: a fence that RAN and said "I cannot read where this
    command goes" is a verdict, and bypass runs on it exactly as before — what
    that stance waives is the question, not the boundary. Only a judge that
    could not run at all is refused."""
    person = _Person("reject_once")
    session = await open_session(person, unknown=True)
    session.set_permission_mode("bypass")
    option, _reason, decided_by = await session._decide_permission(
        _shell_ask("uv run python -c 'print(7)'", effect="write")
    )
    assert person.asked == []
    assert option == "allow_once"
    assert decided_by not in ("fence", "fence_error", "human")


# -- a destructive line on a command the fence cannot read --------------------
#
# opencode's own bash tool on a fenced session: ``rm`` is a reach the fence can
# never prove, so every such ask takes the path above. On a laptop "Always allow
# this exact command" records the line; on a box nothing records it, so it is
# not offered and the identical line is asked again.


def _classified_ask(command: str, request_id: str) -> PermissionRequest:
    """opencode's own bash ask for ``command``, as the translator hands it to
    the session: the real classifier's subject and the translator's options."""
    from alkera_cli.harness.adapters.opencode_translate import (
        OpencodeEventTranslator,
        _TranslatorContext,
    )

    event = OpencodeEventTranslator(_TranslatorContext(session_id="chat-1")).translate(
        {
            "type": "permission.asked",
            "properties": {
                "id": request_id,
                "permission": "bash",
                "patterns": [command],
                "always": [command.split()[0] + " *"],
                "metadata": {},
            },
        }
    )
    assert isinstance(event, PermissionRequest)
    return event


async def _offered_named(session: ChatSession, ask: PermissionRequest) -> list[tuple[str, str]]:
    stream = session.subscribe()
    await session.publish_event(ask)
    async for event in stream:
        if isinstance(event, PermissionRequest) and event.request_id == ask.request_id:
            return [(option.option_id, option.name) for option in event.options]
    raise AssertionError("the ask never reached the stream")


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("rm -rf a/", id="rm"),
        pytest.param("git push --force origin main", id="force-push"),
        pytest.param("chmod -R 777 build", id="chmod-recursive"),
        pytest.param("rm -rf $TARGET", id="expanding-variable"),
        pytest.param("rm -rf build/*", id="expanding-glob"),
    ],
)
async def test_a_destructive_line_on_a_box_offers_no_standing_grant(
    open_session: Any, command: str
) -> None:
    session = await open_session(_Person("allow_once"), unknown=True)
    offered = await _offered_named(session, _classified_ask(command, "req-offer"))
    assert offered == [("allow_once", "Allow once"), ("reject_once", "Reject once")]


async def test_a_destructive_line_on_a_laptop_offers_its_exact_consent(open_session: Any) -> None:
    """The complement: with no fence, the exact line is what "Always allow"
    records, and the card says so."""
    session = await open_session(_Person("allow_once"), unknown=False, fenced=False)
    offered = await _offered_named(session, _classified_ask("rm -rf a/", "req-offer"))
    assert ("allow_always", "Always allow this exact command") in offered


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("rm -rf a/", id="exact-destructive"),
        pytest.param("rm -rf $TARGET", id="expanding-destructive"),
        pytest.param("touch notes.txt", id="family-write"),
    ],
)
@pytest.mark.parametrize("mode", ["default", "auto"])
async def test_an_always_on_a_box_is_asked_again(
    open_session: Any, command: str, mode: str
) -> None:
    """A standing answer to a line the fence cannot read binds the one call,
    and the identical line is asked again."""
    person = _Person("allow_always")
    session = await open_session(person, unknown=True)
    session.set_permission_mode(mode)
    first = await session._decide_permission(_classified_ask(command, "req-1"))
    assert (first[0], first[2]) == ("allow_once", "human")
    person.option = "reject_once"
    option, _reason, decided_by = await session._decide_permission(
        _classified_ask(command, "req-2")
    )
    assert person.asked == ["req-1", "req-2"]
    assert (option, decided_by) == ("reject_once", "human")


async def test_the_box_owners_exact_allow_still_decides_an_unprovable_line(
    open_session: Any,
) -> None:
    """The committed ``permissions.yml`` is the box owner's policy, not a chat's
    answer: an exact-text allow there still decides the line it names, and
    only that line."""
    person = _Person("reject_once")
    session = await open_session(person, unknown=True)
    session.set_permission_mode("default")
    (tmp_path_of(session) / "permissions.yml").write_text(
        "rules:\n  - capability: shell\n    decision: allow\n    match: 'rm -rf a/'\n"
    )
    same = await session._decide_permission(_classified_ask("rm -rf a/", "req-1"))
    other = await session._decide_permission(_classified_ask("rm -rf b/", "req-2"))
    assert (same[0], same[2]) == ("allow_once", "rule")
    assert (other[0], other[2]) == ("reject_once", "human")
    assert person.asked == ["req-2"]
