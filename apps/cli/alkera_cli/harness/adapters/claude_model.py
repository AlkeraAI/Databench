"""Which model string the claude agent's client runs, and when it must change.

The model string (``<model>::<effort>::<display>``) is claude's only per-request
channel: the gateway reads the effort and the thinking display out of it. A turn
names its model and effort (see :mod:`alkera_cli.harness.turn_model`), and the
client has to be running that string when the turn is written.

The SDK's ``set_model`` cannot change it through the gateway: claude validates a
new model with a non-streaming probe, the gateway answers every request as a
stream, and the probe fails ("Unable to validate model") after the gateway has
already billed it. So a change is applied the way a resume applies one: the
client is spawned again on the new string, resuming the conversation, and
swapped in. Never while a turn is still open, which the swap would cut: the new
turn then runs on the string already in use, and its stamp says so.

This module only decides; the adapter spawns.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from alkera_core.gateway import (
    join_model_variant,
    split_model_effort,
    split_model_variant,
    thinking_display_for_effort,
)
from alkera_core.schemas.chat import SessionStatusChanged

from alkera_cli.harness.adapter import HarnessModelError, PromptInput
from alkera_cli.harness.adapters.opencode_alkera import ANTHROPIC_PROVIDER_ID

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeSDKClient

    from alkera_cli.harness.event_bus import EventBus

logger = logging.getLogger(__name__)


@dataclass
class ClaudeModelState:
    """The spawn-time model, the string the client runs now, and whether the
    conversation has been written to (a respawn resumes only one that has)."""

    spawn_provider: str = ""
    spawn_model_id: str | None = None
    #: The string the running client runs: the spawn-time model as its first
    #: turn names it (:meth:`spawn_string`), or the one a switch respawned it
    #: on. ``None`` while the session has no model of its own.
    applied: str | None = None
    #: The base model and effort the last turn ran on: a turn naming neither
    #: stays there rather than snapping back to the spawn-time model.
    ran: tuple[str, str | None] | None = None
    queried: bool = False

    @classmethod
    def from_config(cls, model: Mapping[str, str] | None) -> ClaudeModelState:
        model = model or {}
        model_id = str(model.get("model_id") or "") or None
        state = cls(spawn_provider=str(model.get("provider_id") or ""), spawn_model_id=model_id)
        state.applied = state.spawn_string()
        return state

    def spawn_string(self) -> str | None:
        """The string the first client is spawned on: the spawn-time model and
        effort with the thinking display that effort carries, exactly what a
        turn on them asks for, so the session's first turn finds its client
        already running it rather than spawning a second one."""
        if not self.spawn_model_id:
            return None
        base, effort = split_model_effort(self.spawn_model_id)
        if not base:
            return self.spawn_model_id
        return join_model_variant(base, effort, thinking_display_for_effort(effort))

    def _choice(
        self, prompt_model: Mapping[str, str] | None, variant: str | None
    ) -> tuple[str | None, str | None]:
        spawn_base, spawn_effort = split_model_effort(self.spawn_model_id or "")
        if prompt_model:
            provider_id = str(prompt_model.get("provider_id") or "")
            if provider_id not in (ANTHROPIC_PROVIDER_ID, self.spawn_provider):
                named = f"{provider_id}/{prompt_model.get('model_id') or '?'}"
                raise HarnessModelError(
                    f"The claude agent runs Anthropic models only, and {named} is not one."
                )
            base, effort = split_model_effort(str(prompt_model.get("model_id") or ""))
            return (base or spawn_base or None), (variant or effort)
        if self.ran is not None:
            return self.ran[0], (variant or self.ran[1])
        return (spawn_base or None), (variant or spawn_effort)

    def plan(
        self, prompt_model: Mapping[str, str] | None, variant: str | None, *, turn_open: bool
    ) -> tuple[str | None, dict[str, str] | None]:
        """``(string to respawn on, or None to keep the client; the stamp of what
        the turn runs on, or None when the session has no model of its own)``.
        Raises ``HarnessModelError`` for a model off the Anthropic wire."""
        base, effort = self._choice(prompt_model, variant)
        if not base:
            return None, None
        desired: str | None = join_model_variant(base, effort, thinking_display_for_effort(effort))
        if desired == self.applied:
            desired = None
        elif turn_open:
            base, effort, _ = split_model_variant(self.applied or self.spawn_model_id or base)
            desired = None
        self.ran = (base, effort)
        stamp = {"provider_id": ANTHROPIC_PROVIDER_ID, "model_id": base}
        if effort:
            stamp["effort"] = effort
        return desired, stamp


class ClaudeModelSwitching:
    """The claude adapter's side of a switch: plan it, and swap in a client on
    the new model string before the turn is written. A mixin over
    ``ClaudeAgentAdapter``, whose client lifecycle it drives."""

    if TYPE_CHECKING:
        _models: ClaudeModelState
        _model_id: str | None
        _resume: bool
        _bus: EventBus
        _state: Any
        _translator_ctx: Any

        async def _spawn_client(self, *, resume: bool) -> ClaudeSDKClient: ...
        async def _teardown(self) -> None: ...
        async def _pump(self) -> None: ...
        def _our_sid(self) -> str: ...

    async def _apply_turn_model(self, prompt: PromptInput) -> dict[str, str] | None:
        """Respawn on the turn's model string when it changed; the stamp of what
        the turn runs on. A refusal or a failed spawn is the turn's error."""
        try:
            open_turn = bool(self._translator_ctx.open_queries)
            desired, stamp = self._models.plan(prompt.model, prompt.variant, turn_open=open_turn)
            if desired is not None:
                await self._respawn_on_model(desired)
        except HarnessModelError as exc:
            await self._bus.publish(
                SessionStatusChanged(
                    event_id=secrets.token_hex(10),
                    time=datetime.now(UTC),
                    session_id=self._our_sid(),
                    status="error",
                    phase="error",
                    detail=str(exc),
                    turn_id=prompt.turn_id,
                )
            )
            raise
        return stamp

    async def _respawn_on_model(self, desired: str) -> None:
        """Swap in a client spawned on ``desired``, resuming the conversation
        when it has one. The new client connects FIRST, as ``clear`` does: a
        spawn that fails leaves the running client and its model untouched.
        ``retiring`` covers the whole swap so the old pump's end is no crash."""
        previous, self._model_id = self._model_id, desired
        self._state.retiring = True
        try:
            client = await self._spawn_client(resume=self._resume or self._models.queried)
        except BaseException as exc:
            self._model_id, self._state.retiring = previous, False
            raise HarnessModelError(f"The agent could not move onto {desired}: {exc}") from exc
        try:
            await self._teardown()
            self._state.client = client
            self._state.pump_task = asyncio.create_task(self._pump(), name="claude-pump")
        finally:
            self._state.retiring = False
        self._models.applied = desired
        logger.info("chat %s: the agent now runs %s", self._our_sid(), desired)


__all__ = ["ClaudeModelState", "ClaudeModelSwitching"]
