"""The model a cloud chat runs on, from the chat row to the harness manifest.

A browser reader picks a model when they start a chat; the server records what
the gateway catalog said about it; the box has to turn that into the pinned
manifest shape the harness opens the session with. That last hop is the one
this file covers, and it matters because every way of getting it wrong is
SILENT: a pin with no provider id is dropped by the runtime and the chat quietly
answers on the box's default, which looks exactly like a chat nobody picked a
model for.

The translation is not re-implemented here — ``build_manifest_model`` is the one
place the pinning shape is written, shared with the CLI picker and the editor's
``new_chat`` — so what is pinned below is what a chat started from any surface
gets.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from _mirror_service import Clock, build_service
from alkera_cli.cloud.mirror import RelayRefusedError, pinned_model
from alkera_cli.cloud.model_follow import same_pick
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.harness.adapters.opencode_alkera import build_manifest_model

#: A pin exactly as ``ChatSessionRead.model`` serialises one.
PIN: dict[str, Any] = {
    "schema_version": "1.0.0",
    "id": "claude-opus-4.5",
    "display_name": "Claude Opus 4.5",
    "wire": "anthropic",
    "efforts": ["low", "medium", "high"],
    "effort": "high",
    "context_window": 200000,
    "max_output_tokens": 64000,
}


def test_a_pin_becomes_the_manifest_the_harness_opens_a_session_with() -> None:
    """The provider id is the whole point: the runtime reads
    ``manifest.model.provider_id`` and pins NOTHING when it is missing, so a
    translation that dropped it would leave every picked model unused with
    nothing on screen to say so."""
    manifest = pinned_model(PIN)

    assert manifest is not None
    assert manifest["provider_id"] == "alkera-anthropic"
    assert manifest["model_id"] == "claude-opus-4.5"
    assert manifest["effort"] == "high"
    # The limits ride along so the box can arm opencode's own overflow
    # detection; zero there disables it silently.
    assert manifest["context_window"] == 200000
    assert manifest["max_output_tokens"] == 64000


def test_an_openai_pin_is_filed_under_the_openai_provider() -> None:
    """The wire is the only thing that decides which provider a model is filed
    under, and it is a fact only the catalog knows — which is why the pin
    carries it rather than the box guessing from the model id."""
    manifest = pinned_model(
        {**PIN, "id": "gpt-5.2", "wire": "openai", "efforts": [], "effort": None}
    )

    assert manifest is not None
    assert manifest["provider_id"] == "alkera-openai"
    assert manifest["model_id"] == "gpt-5.2"


def test_the_translation_is_the_one_the_other_surfaces_use() -> None:
    """Not a re-implementation: a chat started from the browser must pin the
    same shape as one started from the CLI picker or the editor, or a resumed
    chat would come back on a different model depending on who opened it."""
    assert pinned_model(PIN) == build_manifest_model(
        GatewayModel(
            id="claude-opus-4.5",
            display_name="Claude Opus 4.5",
            wire="anthropic",
            efforts=("low", "medium", "high"),
            context_window=200000,
            max_output_tokens=64000,
        ),
        "high",
    )


@pytest.mark.parametrize(
    "pin",
    [
        pytest.param(None, id="a chat nobody picked a model for"),
        pytest.param({}, id="a row from before the picker existed"),
        pytest.param({"id": "", "wire": "anthropic"}, id="an empty model id"),
        pytest.param({"id": "x", "wire": "bedrock"}, id="a wire this box does not know"),
        pytest.param("claude-opus-4.5", id="a bare string where an object belongs"),
    ],
)
def test_an_unusable_pin_reads_as_no_model(pin: Any) -> None:
    """A pin the box cannot read is not guessed at: it reads as no model, the
    session opens with none, and the harness refuses every turn on it with the
    reason rendered in the chat (the adapter's model gate) — never a default
    the reader did not choose."""
    assert pinned_model(pin) is None


def test_the_service_hands_the_chats_pin_to_the_mirror_it_builds(tmp_path: Path) -> None:
    """The seam that was cut and never connected: ``ChatMirror`` has accepted a
    ``model`` since it was written and the only production constructor never
    passed one, so every cloud chat ran the harness default however the reader
    chose. This is the wire, end to end from the chat row."""
    service, _built = build_service(tmp_path, clock=Clock())
    chat = {"title": "Ops", "owner_user_id": "u-1", "model": PIN}

    mirror = service._default_mirror("chat-1", chat)

    assert mirror._model == pinned_model(PIN)


def test_a_chat_row_with_no_model_builds_a_mirror_that_pins_nothing(tmp_path: Path) -> None:
    """A row from before every chat carried a pin opens with no model. Nothing
    is guessed for it: the harness has no default model any more, so its turns
    are refused by the adapter with the reason in the chat until a reader pins
    one (a model relay, below, repins the running session)."""
    service, _built = build_service(tmp_path, clock=Clock())

    mirror = service._default_mirror("chat-1", {"title": "Ops", "owner_user_id": "u-1"})

    assert mirror._model is None


# ---------------------------------------------------------------------------
# The switch a reader makes on an OPEN chat, relayed to a running box
# ---------------------------------------------------------------------------


class _Manifest:
    def __init__(self, model: dict[str, Any]) -> None:
        self.model = model


class _RecordingSession:
    """A session that records what it was repinned to, keeps the pin on its
    manifest the way the real one does, and answers whether its running agent
    carries the pinned model."""

    def __init__(self, model: dict[str, Any] | None = None, *, serves: bool = True) -> None:
        self.selections: list[dict[str, Any]] = []
        self.manifest = _Manifest(dict(model or {}))
        self.serves = serves

    async def set_model(self, selection: dict[str, Any]) -> None:
        self.selections.append(selection)
        self.manifest.model = dict(selection)

    def serves_pinned_model(self) -> bool:
        return self.serves


async def test_a_model_relay_repins_the_running_session(tmp_path: Path) -> None:
    """The half of the fix the box owns. The route has already resolved the pick
    and written it on the chat row; this is how a box that is ALREADY running
    the chat answers the next turn on the new model instead of waiting for a
    resume — and it goes through the same translation the row does, so a chat
    started on a model and one switched onto it run identically."""
    service, _built = build_service(tmp_path, clock=Clock())
    mirror = service._default_mirror("chat-1", {"title": "Ops", "owner_user_id": "u-1"})
    session = _RecordingSession()
    mirror._session = session  # type: ignore[assignment]

    await mirror._on_model({"kind": "model", "pin": PIN, "user_id": "u-1"})

    assert session.selections == [pinned_model(PIN)]


@pytest.mark.parametrize(
    "pin",
    [
        pytest.param({}, id="an empty pin"),
        pytest.param({"id": "claude-opus-4.5"}, id="a pin with no wire"),
        pytest.param({"id": "", "wire": "anthropic"}, id="a pin with no id"),
        pytest.param({"id": "m", "wire": "gemini"}, id="a wire this box has no provider for"),
        pytest.param("claude-opus-4.5", id="a bare id where a pin belongs"),
    ],
)
async def test_a_pin_the_box_cannot_translate_leaves_the_session_where_it_was(
    tmp_path: Path, pin: Any
) -> None:
    """Refused, never applied half-way. A reader who believes they moved a chat
    onto a cheaper model and a box that quietly stayed on the expensive one is
    the failure that costs money, so an untranslatable pin raises rather than
    reaching ``set_model`` with a shape the harness would spawn wrong."""
    service, _built = build_service(tmp_path, clock=Clock())
    mirror = service._default_mirror("chat-1", {"title": "Ops", "owner_user_id": "u-1"})
    session = _RecordingSession()
    mirror._session = session  # type: ignore[assignment]

    with pytest.raises(RelayRefusedError):
        await mirror._on_model({"kind": "model", "pin": pin})

    assert session.selections == []


async def test_a_model_relay_reaches_the_handler_through_the_dispatch(tmp_path: Path) -> None:
    """Dispatched by kind ahead of the shared union, the way the mode switch is:
    a box built before the variant existed would otherwise log the reader's
    switch as an unknown kind and leave the chat on the model they just moved
    off — the exact silent no-op this whole change removes."""
    service, _built = build_service(tmp_path, clock=Clock())
    mirror = service._default_mirror("chat-1", {"title": "Ops", "owner_user_id": "u-1"})
    session = _RecordingSession()
    mirror._session = session  # type: ignore[assignment]
    mirror._ready.set()

    await mirror._handle_relay({"kind": "model", "pin": PIN, "user_id": "u-1"})

    assert session.selections == [pinned_model(PIN)]


# ---------------------------------------------------------------------------
# A switch whose relay reached nobody: the row is followed on every read
# ---------------------------------------------------------------------------

OTHER = {**PIN, "id": "claude-sonnet-4.6", "display_name": "Claude Sonnet 4.6", "effort": "low"}


async def test_a_row_naming_another_model_repins_the_running_session(tmp_path: Path) -> None:
    from alkera_cli.cloud.service import CloudMirrorService

    service, _built = build_service(tmp_path, clock=Clock())
    mirror = service._default_mirror("chat-1", {"title": "Ops", "owner_user_id": "u-1"})
    session = _RecordingSession(pinned_model(PIN))
    mirror._session = session  # type: ignore[assignment]

    await CloudMirrorService._follow_record(mirror, {"model": OTHER})

    assert session.selections == [pinned_model(OTHER)]
    # The mirror keeps the pick too: an agent it opens again opens on it.
    assert mirror._model == pinned_model(OTHER)


@pytest.mark.parametrize(
    "row",
    [
        pytest.param({"model": PIN}, id="the-pin-the-session-already-has"),
        pytest.param({}, id="a-row-that-names-no-model"),
        pytest.param({"model": None}, id="a-row-whose-model-is-null"),
        pytest.param({"model": {"id": "m", "wire": "gemini"}}, id="a-pin-the-box-cannot-run"),
    ],
)
async def test_a_row_that_changes_nothing_moves_nothing(
    tmp_path: Path, row: dict[str, Any]
) -> None:
    """Read on every poll tick: a row that agrees with the session (or says
    nothing usable) must not repin it, or every poll would announce a switch."""
    from alkera_cli.cloud.service import CloudMirrorService

    service, _built = build_service(tmp_path, clock=Clock())
    mirror = service._default_mirror("chat-1", {"title": "Ops", "owner_user_id": "u-1"})
    session = _RecordingSession(pinned_model(PIN))
    mirror._session = session  # type: ignore[assignment]

    await CloudMirrorService._follow_record(mirror, row)

    assert session.selections == []


async def test_an_effort_change_on_the_row_is_a_change(tmp_path: Path) -> None:
    from alkera_cli.cloud.service import CloudMirrorService

    service, _built = build_service(tmp_path, clock=Clock())
    mirror = service._default_mirror("chat-1", {"title": "Ops", "owner_user_id": "u-1"})
    session = _RecordingSession(pinned_model(PIN))
    mirror._session = session  # type: ignore[assignment]

    await CloudMirrorService._follow_record(mirror, {"model": {**PIN, "effort": "low"}})

    assert [s["effort"] for s in session.selections] == ["low"]


# ---------------------------------------------------------------------------
# An agent spawned without the pinned model is opened again before the turn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("serves", "idle", "reopened"),
    [
        pytest.param(False, True, True, id="pinned-model-not-carried-between-turns"),
        pytest.param(True, True, False, id="pinned-model-carried"),
        pytest.param(False, False, False, id="a-turn-still-running-keeps-its-agent"),
    ],
)
async def test_the_agent_is_opened_again_only_for_a_model_it_lacks(
    tmp_path: Path, serves: bool, idle: bool, reopened: bool
) -> None:
    from alkera_cli.harness import PermissionBroker

    async def _never(_request: object) -> object:
        raise AssertionError

    service, _built = build_service(tmp_path, clock=Clock())
    mirror = service._default_mirror("chat-1", {"title": "Ops", "owner_user_id": "u-1"})
    mirror._session = _RecordingSession(pinned_model(PIN), serves=serves)  # type: ignore[assignment]
    mirror._broker = PermissionBroker(_never, default_timeout_seconds=None)
    mirror._gateway_token = "gw-1"
    if not idle:
        mirror._idle.clear()
    tokens: list[str] = []

    async def _reopen(token: str) -> None:
        tokens.append(token)

    mirror._reopen_on = _reopen  # type: ignore[method-assign]

    await mirror._respawn_for_pinned_model()

    assert tokens == (["gw-1"] if reopened else [])


async def test_a_pin_the_reopened_agent_still_lacks_is_not_reopened_again(tmp_path: Path) -> None:
    """A config the box cannot build for the pin leaves the agent without it
    after the reopen too; that costs one restart, not one before every turn."""
    from alkera_cli.harness import PermissionBroker

    async def _never(_request: object) -> object:
        raise AssertionError

    service, _built = build_service(tmp_path, clock=Clock())
    mirror = service._default_mirror("chat-1", {"title": "Ops", "owner_user_id": "u-1"})
    session = _RecordingSession(pinned_model(PIN), serves=False)
    mirror._session = session  # type: ignore[assignment]
    mirror._broker = PermissionBroker(_never, default_timeout_seconds=None)
    mirror._gateway_token = "gw-1"
    tokens: list[str] = []

    async def _reopen(token: str) -> None:
        tokens.append(token)

    mirror._reopen_on = _reopen  # type: ignore[method-assign]

    await mirror._respawn_for_pinned_model()
    await mirror._respawn_for_pinned_model()
    assert tokens == ["gw-1"]
    # A new pick is a new reason to reopen.
    other = pinned_model(OTHER)
    assert other is not None
    await session.set_model(other)
    await mirror._respawn_for_pinned_model()
    assert tokens == ["gw-1", "gw-1"]


# ---------------------------------------------------------------------------
# The box re-checks the server's verdict on the relay it is handed
# ---------------------------------------------------------------------------

READER = {
    **PIN,
    "id": "claude-fable-5.1",
    "reasoning_format": "anthropic:claude-fable-5-1",
    "reads_reasoning_formats": ["anthropic:claude-opus-4-5"],
}
NOT_A_READER = {**PIN, "id": "gpt-5.5", "wire": "openai", "efforts": [], "effort": None}


@pytest.mark.parametrize(
    ("pin", "ledger", "applied"),
    [
        pytest.param(
            READER, ["anthropic:claude-opus-4-5"], True, id="a-target-that-reads-the-history"
        ),
        pytest.param(NOT_A_READER, ["anthropic:claude-opus-4-5"], False, id="a-target-that-cannot"),
        pytest.param(NOT_A_READER, None, True, id="an-older-server-sends-no-ledger"),
        pytest.param(NOT_A_READER, [], True, id="no-history-moves-anywhere"),
        pytest.param(
            {**PIN, "effort": "low"},
            ["anything:else"],
            True,
            id="an-effort-change-is-never-refused",
        ),
    ],
)
async def test_the_box_re_checks_a_switch_against_the_relays_ledger(
    tmp_path: Path, pin: dict[str, Any], ledger: list[str] | None, applied: bool
) -> None:
    service, _built = build_service(tmp_path, clock=Clock())
    mirror = service._default_mirror("chat-1", {"title": "Ops", "owner_user_id": "u-1"})
    session = _RecordingSession(pinned_model(PIN))
    mirror._session = session  # type: ignore[assignment]
    relay: dict[str, Any] = {"kind": "model", "pin": pin, "user_id": "u-1"}
    if ledger is not None:
        relay["ledger"] = ledger

    if applied:
        await mirror._on_model(relay)
        assert session.selections == [pinned_model(pin)]
    else:
        with pytest.raises(RelayRefusedError, match="can't read"):
            await mirror._on_model(relay)
        assert session.selections == []


async def test_a_relay_the_box_refused_is_not_applied_by_the_next_row_read(
    tmp_path: Path,
) -> None:
    """The row holds the pin the relay carried, and it is read on every poll:
    a refusal the next row read undid would be no refusal at all. A row naming
    a model nobody refused is still followed."""
    from alkera_cli.cloud.service import CloudMirrorService

    service, _built = build_service(tmp_path, clock=Clock())
    mirror = service._default_mirror("chat-1", {"title": "Ops", "owner_user_id": "u-1"})
    session = _RecordingSession(pinned_model(PIN))
    mirror._session = session  # type: ignore[assignment]

    with pytest.raises(RelayRefusedError, match="can't read"):
        await mirror._on_model(
            {"kind": "model", "pin": NOT_A_READER, "ledger": ["anthropic:claude-opus-4-5"]}
        )
    await CloudMirrorService._follow_record(mirror, {"model": NOT_A_READER})

    assert session.selections == []
    assert session.manifest.model == pinned_model(PIN)

    await CloudMirrorService._follow_record(mirror, {"model": READER})
    assert session.selections == [pinned_model(READER)]


async def test_a_pin_refused_once_is_followed_when_a_later_relay_allows_it(
    tmp_path: Path,
) -> None:
    from alkera_cli.cloud.service import CloudMirrorService

    service, _built = build_service(tmp_path, clock=Clock())
    mirror = service._default_mirror("chat-1", {"title": "Ops", "owner_user_id": "u-1"})
    session = _RecordingSession(pinned_model(PIN))
    mirror._session = session  # type: ignore[assignment]
    with pytest.raises(RelayRefusedError):
        await mirror._on_model({"kind": "model", "pin": NOT_A_READER, "ledger": ["x:y"]})

    await mirror._on_model({"kind": "model", "pin": NOT_A_READER, "ledger": []})
    await CloudMirrorService._follow_record(mirror, {"model": NOT_A_READER})

    assert session.selections == [pinned_model(NOT_A_READER)]


@pytest.mark.parametrize(
    ("turn_running", "applied"),
    [
        pytest.param(True, False, id="a-turn-running-on-a-reasoning-model-holds-it"),
        pytest.param(False, True, id="between-turns-the-relays-ledger-decides"),
    ],
)
async def test_the_box_counts_the_reasoning_of_the_turn_it_is_running(
    tmp_path: Path, turn_running: bool, applied: bool
) -> None:
    """The relay's ledger is the transcript's, which does not hold the running
    turn's reasoning yet: while a turn runs on a model that writes reasoning,
    the box counts that model's format too."""
    service, _built = build_service(tmp_path, clock=Clock())
    mirror = service._default_mirror("chat-1", {"title": "Ops", "owner_user_id": "u-1"})
    running = pinned_model({**PIN, "reasoning_format": "anthropic:claude-opus-4-5"})
    session = _RecordingSession(running)
    mirror._session = session  # type: ignore[assignment]
    if turn_running:
        mirror._idle.clear()
    relay = {"kind": "model", "pin": NOT_A_READER, "ledger": []}

    if applied:
        await mirror._on_model(relay)
        assert session.selections == [pinned_model(NOT_A_READER)]
    else:
        with pytest.raises(RelayRefusedError, match="can't read"):
            await mirror._on_model(relay)
        assert session.selections == []


async def test_a_claude_agent_box_refuses_an_openai_target(tmp_path: Path) -> None:
    from alkera_cli.harness.registry import CLAUDE_HARNESS

    service, _built = build_service(tmp_path, clock=Clock())
    mirror = service._default_mirror("chat-1", {"title": "Ops", "owner_user_id": "u-1"})
    mirror._harness_type = CLAUDE_HARNESS
    mirror._session = _RecordingSession(pinned_model(PIN))  # type: ignore[assignment]

    with pytest.raises(RelayRefusedError, match="Anthropic models only"):
        await mirror._on_model({"kind": "model", "pin": NOT_A_READER, "ledger": []})


# ---------------------------------------------------------------------------
# A switch that lands while a turn is being prepared never splits the check
# from the send
# ---------------------------------------------------------------------------


class _AgentSession:
    """A session whose agent carries only the model it was spawned with, as a
    box's agent does: opencode reads its model list once, at spawn. It records
    each send as ``(model the turn names, model the agent was spawned on)``."""

    def __init__(self, model: dict[str, Any]) -> None:
        self.manifest = _Manifest(dict(model))
        self.agent_model = dict(model)
        self.sent: list[tuple[str, str]] = []
        self.events: list[Any] = []
        self.on_send: Any = None

    async def set_model(self, selection: dict[str, Any]) -> None:
        self.manifest.model = dict(selection)

    def serves_pinned_model(self) -> bool:
        return same_pick(self.manifest.model, self.agent_model)

    async def send_prompt(self, text: str, **_turn: Any) -> None:
        # The real session composes the turn (waits) before it reads the pin.
        if self.on_send is not None:
            await self.on_send()
        named = str(self.manifest.model.get("model_id"))
        self.sent.append((named, str(self.agent_model.get("model_id"))))

    async def publish_event(self, event: Any) -> None:
        self.events.append(event)


def _turn_mirror(tmp_path: Path, session: _AgentSession) -> Any:
    from alkera_cli.harness import PermissionBroker

    async def _never(_request: object) -> object:
        raise AssertionError

    service, _built = build_service(tmp_path, clock=Clock())
    mirror = service._default_mirror("chat-1", {"title": "Ops", "owner_user_id": "u-1"})
    mirror._session = session
    mirror._broker = PermissionBroker(_never, default_timeout_seconds=None)
    mirror._gateway_token = "gw-1"
    mirror._ready.set()
    reopened: list[str] = []

    async def _reopen(token: str) -> None:
        reopened.append(token)
        session.agent_model = dict(session.manifest.model)

    mirror._reopen_on = _reopen
    mirror.reopened = reopened
    return mirror


HAIKU = {
    **PIN,
    "id": "claude-haiku-4.5",
    "display_name": "Claude Haiku 4.5",
    "efforts": ["none", "low", "medium", "high"],
    "effort": "none",
}


async def test_a_switch_that_lands_while_the_first_message_is_prepared_runs_it_on_the_new_model(
    tmp_path: Path,
) -> None:
    """A fresh chat switched to Haiku before its first message: the box opened
    the chat (and spawned its agent) on the row it read a moment before the
    switch, and the row carrying Haiku reached it while the message was being
    prepared. The check that the agent carries the pin used to run before
    that wait, so the turn named ``claude-haiku-4.5::none`` to an agent spawned
    without it and failed. The check now runs after it: the agent is opened
    again on Haiku and the turn runs there."""
    session = _AgentSession(pinned_model(PIN) or {})
    mirror = _turn_mirror(tmp_path, session)

    async def _prepare(_chat_id: str, _relay: Any) -> Any:
        from alkera_cli.cloud.attachments import MaterializedAttachments

        await mirror.adopt_model(HAIKU)
        return MaterializedAttachments()

    mirror._prepare_attachments = _prepare

    await mirror._ask({"kind": "prompt", "text": "echo haiku-ok", "client_id": "c-1"})

    assert mirror.reopened == ["gw-1"]
    assert session.sent == [("claude-haiku-4.5", "claude-haiku-4.5")]


async def test_a_switch_that_lands_while_the_turn_is_being_sent_waits_for_the_next_turn(
    tmp_path: Path,
) -> None:
    """A pick that arrives once the turn has been checked and is being handed
    over is applied after the hand-over, not in the middle of it: the turn
    runs on the model its agent carries, and the next turn reopens the agent."""
    session = _AgentSession(pinned_model(PIN) or {})
    mirror = _turn_mirror(tmp_path, session)
    switched: list[asyncio.Task[None]] = []

    async def _during_send() -> None:
        switched.append(asyncio.create_task(mirror.adopt_model(HAIKU)))
        # Every other ready task runs before the send completes.
        for _ in range(5):
            await asyncio.sleep(0)

    session.on_send = _during_send
    await mirror._ask({"kind": "prompt", "text": "one", "client_id": "c-1"})
    await asyncio.gather(*switched)

    assert session.sent == [("claude-opus-4.5", "claude-opus-4.5")]
    assert session.manifest.model["model_id"] == "claude-haiku-4.5"
    session.on_send = None
    mirror._idle.set()  # the first turn ended
    await mirror._ask({"kind": "prompt", "text": "two", "client_id": "c-2"})
    assert mirror.reopened == ["gw-1"]
    assert session.sent[-1] == ("claude-haiku-4.5", "claude-haiku-4.5")
