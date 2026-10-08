"""How a cloud chat's mirror follows the model its row is pinned to.

The pin moves while the agent runs: a reader picks another model in an empty
chat the box already opened, switches mid-chat, or changes the effort. It reaches
the box as a relay, or, when the relay reached nobody, on the next read of the
chat row. Either way the session is repinned (every turn carries the pin, see
``alkera_cli.harness.turn_model``), and before the next turn the agent is opened
again when it was spawned without the pinned model: the box spawns opencode
knowing only the chat's model, and opencode reads its model list once. Opening
it again on the same token resumes the conversation the way a sleep and a wake
do. A turn still running keeps its agent.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from functools import cached_property
from typing import TYPE_CHECKING, Any

from alkera_core.chat_models.harnesses import capabilities_of
from alkera_core.chat_models.switching import ModelFacts, evaluate_switch, message_for

from alkera_cli.harness.turn_model import with_reasoning_history

if TYPE_CHECKING:
    import asyncio

    from alkera_cli.harness import ChatSession, PermissionBroker

logger = logging.getLogger(__name__)


def pin_reasoning(pin: Mapping[str, Any]) -> dict[str, Any]:
    """The reasoning a server pin says its model writes and reads, as
    ``GatewayModel`` fields (absent from a pin an older server wrote)."""
    fmt = pin.get("reasoning_format")
    reads = pin.get("reads_reasoning_formats") or ()
    return {
        "reasoning_format": fmt if isinstance(fmt, str) and fmt else None,
        "reads_reasoning_formats": tuple(f for f in reads if isinstance(f, str) and f),
    }


def same_pick(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Whether two manifest model dicts name the same model at the same effort."""
    return all(a.get(k) == b.get(k) for k in ("provider_id", "model_id", "effort"))


class PinnedModelFollowing:
    """The mirror's side of a model switch. A mixin over ``ChatMirror``."""

    if TYPE_CHECKING:
        _chat_id: str
        _model: dict[str, Any] | None
        _session: ChatSession | None
        _broker: PermissionBroker | None
        _idle: asyncio.Event
        _gateway_token: str | None
        _respawned_for: dict[str, Any] | None
        _harness_type: str
        _pin_lock: asyncio.Lock

        async def _reopen_on(self, gateway_token: str) -> None: ...

    def _seeded_pin(self, held: Mapping[str, Any]) -> dict[str, Any]:
        """The pin to lay on a chat's manifest before its agent opens: the row's
        pick, keeping the formats of the models the chat moved off (they are on
        the manifest, never on the row), so the agent still replays reasoning
        its model reads as written. ``held`` itself when the row names none."""
        if self._model is None:
            return dict(held)
        return with_reasoning_history(held, self._model)

    def _refusal_for(self, relay: Mapping[str, Any]) -> str | None:
        """Why this box will not apply a model relay, or ``None``. Defence in
        depth behind the server, which refuses first: a target that cannot read
        the reasoning the relay says the chat carries, or that this chat's agent
        cannot drive. A turn running now has not published its reasoning, so
        the format of the model it runs on counts too. A relay with no ledger
        (an older server) is not checked, and an effort change on the model the
        session is on never is."""
        ledger, pin = relay.get("ledger"), relay.get("pin")
        if not isinstance(ledger, list) or not isinstance(pin, Mapping):
            return None
        current = self._session.manifest.model if self._session is not None else self._model
        if current and current.get("model_id") == pin.get("id"):
            return None
        formats = [f for f in ledger if isinstance(f, str)]
        running = current.get("reasoning_format") if current else None
        if not self._idle.is_set() and isinstance(running, str) and running:
            formats.append(running)
        target = ModelFacts.from_mapping(pin)
        verdict = evaluate_switch(
            None,
            target,
            ledger=formats,
            harness_wires=capabilities_of(self._harness_type).wires,
            offered=True,
        )
        if verdict.allowed:
            return None
        return message_for(verdict, target) or str(verdict.reason_code)

    @cached_property
    def _refused_pins(self) -> dict[str, list[str]]:
        """The models a relay was refused for, each with the ledger it was
        refused over, so the row read that follows does not apply them."""
        return {}

    def _relay_refusal(self, relay: Mapping[str, Any]) -> str | None:
        """:meth:`_refusal_for`, remembered: a refused pin is kept with its
        ledger (the chat row holds that pin too), and one a later relay allows
        is forgotten."""
        pin, ledger = relay.get("pin"), relay.get("ledger")
        model_id = pin.get("id") if isinstance(pin, Mapping) else None
        refusal = self._refusal_for(relay)
        if isinstance(model_id, str) and model_id:
            if refusal is None:
                self._refused_pins.pop(model_id, None)
            else:
                formats = ledger if isinstance(ledger, list) else []
                self._refused_pins[model_id] = [f for f in formats if isinstance(f, str)]
        return refusal

    def _follows_row(self, pin: Any) -> bool:
        """Whether to follow the chat row's pin: not while it names a model a
        relay was refused for, judged again over that relay's ledger (the
        session may have moved since, and a pin it can now take is taken)."""
        model_id = pin.get("id") if isinstance(pin, Mapping) else None
        if not isinstance(model_id, str) or model_id not in self._refused_pins:
            return True
        refusal = self._refusal_for({"pin": pin, "ledger": self._refused_pins[model_id]})
        if refusal is None:
            self._refused_pins.pop(model_id, None)
            return True
        logger.debug("mirror %s: not following the row's model: %s", self._chat_id, refusal)
        return False

    async def _adopt_selection(self, selection: dict[str, Any]) -> None:
        """Pin the session to ``selection`` from its next turn on. The mirror
        keeps the pin too, so an agent it opens again opens on it. A pick the
        session already has moves nothing (the row is read on every poll)."""
        # Never between a turn's check that its agent carries the pin and its
        # send: a pick that arrives then is the next turn's.
        async with self._pin_lock:
            await self._repin(selection)

    async def _repin(self, selection: dict[str, Any]) -> None:
        current = self._session.manifest.model if self._session is not None else self._model
        self._model = with_reasoning_history(current, selection)
        if self._session is None or (current is not None and same_pick(current, selection)):
            return
        await self._session.set_model(selection)
        logger.info(
            "mirror %s: the model is now %s (%s)",
            self._chat_id,
            selection.get("model_id"),
            selection.get("effort") or "no effort",
        )

    async def _respawn_for_pinned_model(self) -> None:
        """Open the agent again, between turns, when it does not carry the pin.
        Once per pin: an agent opened again on a pin it still cannot carry (a
        config the box could not build for it) is not restarted before every
        turn; the turn's own refusal names why."""
        session = self._session
        if session is None or self._broker is None or not self._idle.is_set():
            return
        if session.serves_pinned_model() or not self._gateway_token:
            return
        pin = dict(session.manifest.model)
        if pin == self._respawned_for:
            return
        self._respawned_for = pin
        logger.info("mirror %s: opening the agent again on the chat's model", self._chat_id)
        await self._reopen_on(self._gateway_token)


__all__ = ["PinnedModelFollowing", "pin_reasoning", "same_pick"]
