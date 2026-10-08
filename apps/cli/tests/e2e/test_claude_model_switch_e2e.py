"""The claude agent runs the model a turn names, read off what the provider received.

A real ``claude`` subprocess (driven by the SDK) is pointed at the scripted mock
Anthropic server, which records every request body. The agent is spawned on one
model; a later turn names another. The claude agent takes a model per turn
through the SDK's ``set_model``, so the switch needs no respawn, but the adapter
used to fix the model at construction and push only the effort.

Every assertion is on the ``model`` field the mock received.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from _helpers.claude_runner import claude_e2e_adapter
from _mocks.mock_anthropic_server import _last_user_message, text_events
from alkera_cli.harness.adapter import HarnessModelError, PromptInput
from alkera_cli.harness.adapters.opencode_alkera import ANTHROPIC_PROVIDER_ID
from alkera_core.schemas.chat import Event, SessionStatusChanged, TurnFinished, TurnStarted

pytestmark = [pytest.mark.claude_e2e, pytest.mark.asyncio]

MODEL_A = "claude-opus-4-5"
MODEL_B = "claude-sonnet-4-5"


async def _drain_turn(sub: Any, *, budget: float = 60.0) -> list[Event]:
    out: list[Event] = []
    running = False
    async with asyncio.timeout(budget):
        while True:
            ev: Event = await anext(sub)
            out.append(ev)
            if isinstance(ev, TurnFinished):
                return out
            if isinstance(ev, SessionStatusChanged):
                if ev.status == "running":
                    running = True
                elif running and ev.status in ("idle", "error", "aborted"):
                    return out


def _models_for(server: Any, text: str) -> list[str]:
    """The model every request carrying ``text`` as its newest user message
    was sent with."""
    return [str(r.get("model", "")) for r in server.requests if text in _last_user_message(r)]


def _model(model_id: str) -> dict[str, str]:
    return {"provider_id": ANTHROPIC_PROVIDER_ID, "model_id": model_id}


async def test_a_turn_naming_another_model_reaches_the_provider_on_it(tmp_path: Path) -> None:
    async with claude_e2e_adapter(
        tmp_path,
        mock_script={"*": text_events("ok")},
        model=_model(MODEL_A),
    ) as (adapter, server):
        sub = adapter.subscribe()
        await adapter.send_prompt(PromptInput(text="first on a", variant="high"))
        first = await _drain_turn(sub)
        await adapter.send_prompt(
            PromptInput(text="then on b", model=_model(MODEL_B), variant="low")
        )
        second = await _drain_turn(sub)

    on_a = _models_for(server, "first on a")
    on_b = _models_for(server, "then on b")
    assert on_a and all(m.startswith(f"{MODEL_A}::high") for m in on_a), on_a
    assert on_b and all(m.startswith(f"{MODEL_B}::low") for m in on_b), on_b

    def stamp(events: list[Event]) -> dict[str, str] | None:
        return next(e.model for e in events if isinstance(e, TurnStarted))

    assert stamp(first) == {
        "provider_id": ANTHROPIC_PROVIDER_ID,
        "model_id": MODEL_A,
        "effort": "high",
    }
    assert stamp(second) == {
        "provider_id": ANTHROPIC_PROVIDER_ID,
        "model_id": MODEL_B,
        "effort": "low",
    }


async def test_a_later_turn_naming_no_model_stays_on_the_switched_one(tmp_path: Path) -> None:
    async with claude_e2e_adapter(
        tmp_path,
        mock_script={"*": text_events("ok")},
        model=_model(MODEL_A),
    ) as (adapter, server):
        sub = adapter.subscribe()
        await adapter.send_prompt(PromptInput(text="move", model=_model(MODEL_B), variant="low"))
        await _drain_turn(sub)
        await adapter.send_prompt(PromptInput(text="stay", variant="high"))
        await _drain_turn(sub)

    stay = _models_for(server, "stay")
    assert stay and all(m.startswith(f"{MODEL_B}::high") for m in stay), stay


async def test_a_model_off_the_anthropic_wire_is_refused_before_the_turn(
    tmp_path: Path,
) -> None:
    async with claude_e2e_adapter(
        tmp_path,
        mock_script={"*": text_events("must never be asked")},
        model=_model(MODEL_A),
    ) as (adapter, server):
        sub = adapter.subscribe()
        with pytest.raises(HarnessModelError):
            await adapter.send_prompt(
                PromptInput(
                    text="on openai",
                    model={"provider_id": "alkera-openai", "model_id": "gpt-5.5"},
                    turn_id="T",
                )
            )
        status = await asyncio.wait_for(anext(sub), timeout=5)

    assert isinstance(status, SessionStatusChanged)
    assert (status.status, status.turn_id) == ("error", "T")
    assert _models_for(server, "on openai") == []
