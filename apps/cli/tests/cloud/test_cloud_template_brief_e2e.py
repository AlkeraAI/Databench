"""End to end: a chat template's brief reaches the model without reaching the user.

A chat started from a template is handed its author's words before the first
turn. Two things have to be true of that at once, and only a real subprocess can
show both: the model must READ the brief, and the person's bubble must stay
exactly what they typed — the brief is somebody else's writing quoted into the
session, not something the user said.

The channel that carries it is the harness's hidden per-turn steering block: the
adapter wraps the composed guidance in a ``<system-reminder>`` and leads the
user message with it as a SYNTHETIC part (``opencode_http.send_prompt``, which
deliberately leaves opencode's own trailing ``system`` field unset). So on the
wire the brief and the person's words travel in one message and are still two
separate things — the brief inside the reminder, the person's sentence outside
it — and that separation is what these tests pin.

A REAL bun-driven opencode subprocess (the ``opencode_e2e`` marker) is driven
against a scripted mock provider, so this costs no API spend. The unit tier
pins what the block SAYS (``test_source_context.py``); this pins where it lands.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_openai_server import text_chunks
from alkera_cli.cloud.source_context import source_brief, source_document

pytestmark = pytest.mark.opencode_e2e

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}

TEMPLATE_RECORD: dict[str, Any] = {
    "id": "44444444-4444-4444-4444-444444444444",
    "type": "chat_template",
    "title": "Weekly mentions",
    "version": 2,
}
#: A phrase that could only have come from the template's brief.
BRIEF_MARKER = "TEMPLATE_BRIEF_MARKER_7x9 chart last quarter's mentions for one region"

#: The harness's hidden steering channel, as it appears on the wire.
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


def _messages(request: dict[str, Any]) -> list[tuple[str, str]]:
    """``(role, text)`` for every message of a recorded model request."""
    out: list[tuple[str, str]] = []
    for message in request.get("messages", []):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", ""))
        content = message.get("content")
        if isinstance(content, str):
            out.append((role, content))
        elif isinstance(content, list):
            out.append(
                (
                    role,
                    "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict)),
                )
            )
    return out


def _steering(text: str) -> str:
    """Only what rode the hidden channel — every ``<system-reminder>`` block."""
    return "\n".join(_REMINDER.findall(text))


def _spoken(text: str) -> str:
    """Only what the person is quoted as saying — the message with every
    steering block removed."""
    return _REMINDER.sub("", text).strip()


async def _wait_for_steering(
    server: Any, needle: str, *, budget_seconds: float = 60.0
) -> tuple[dict[str, Any], str]:
    """The recorded request, and the message text, whose STEERING block carries
    ``needle``.

    Every request is scanned because opencode fires auxiliary calls (title
    generation among them) that replay the same message.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    seen = 0
    while loop.time() < deadline:
        for request in list(server.requests):
            for _role, text in _messages(request):
                if needle in _steering(text):
                    return request, text
        seen = len(server.requests)
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"no model request carried {needle!r} on the hidden steering channel within "
        f"{budget_seconds}s ({seen} requests seen)"
    )


async def _drain_to_idle(sub: Any, *, budget_seconds: float = 60.0) -> None:
    """Wait out one whole turn — a ``running`` status, then the next terminal.

    Waiting for ``running`` first skips an idle left buffered by a prior turn.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    running = False
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        try:
            async with asyncio.timeout(remaining):
                event = await anext(sub)
        except (TimeoutError, StopAsyncIteration):
            return
        status = getattr(event, "status", None)
        if status == "running":
            running = True
        elif running and status in ("idle", "error"):
            return


@pytest.mark.asyncio
async def test_the_templates_brief_reaches_the_model_and_not_the_persons_bubble(
    tmp_path: Path,
) -> None:
    """The brief rides the hidden per-turn channel: the model reads it in the
    steering block, and the words the person is quoted as saying are their
    question alone. A brief that arrived as the user's own text would be a
    template putting words in the user's mouth."""
    block = source_brief(source_document(TEMPLATE_RECORD), brief=BRIEF_MARKER)
    assert block is not None

    async with opencode_e2e_runtime(tmp_path, mock_script={"*": text_chunks("ok")}) as (
        runtime,
        sid,
        server,
    ):
        session = await runtime.open_chat(sid)
        await session.send_prompt("re-run it for EMEA", context=block, model=_MODEL)
        request, text = await _wait_for_steering(server, BRIEF_MARKER)
        await runtime.close_chat(sid)

    steering = _steering(text)
    # Framed as the author's text, with the instruction to ask first.
    assert "the chat template Weekly mentions" in steering
    assert "written by the template's author, not by the user" in steering
    # The person's own words are the prompt, and the brief is not among them.
    assert _spoken(text) == "re-run it for EMEA"
    for role, other in _messages(request):
        assert BRIEF_MARKER not in _spoken(other), f"the brief was quoted as the {role}'s own words"


@pytest.mark.asyncio
async def test_the_brief_does_not_ride_the_second_turn(tmp_path: Path) -> None:
    """It is what the chat opened from, not a standing instruction. Carrying it
    on every turn would spend the context window re-stating text the agent
    already has and keep telling it to ask questions it already asked.

    The first turn stays in the conversation the model is sent, as any message
    does; what must not happen is the brief being steered AGAIN — so the claim
    is about the second turn's own message, not about the history behind it."""
    block = source_brief(source_document(TEMPLATE_RECORD), brief=BRIEF_MARKER)
    assert block is not None

    async with opencode_e2e_runtime(tmp_path, mock_script={"*": text_chunks("ok")}) as (
        runtime,
        sid,
        server,
    ):
        session = await runtime.open_chat(sid)
        sub = session.subscribe()
        await session.send_prompt("re-run it for EMEA", context=block, model=_MODEL)
        await _wait_for_steering(server, BRIEF_MARKER)
        # A person's second turn comes after the first settles; sending over a
        # live turn supersedes it, and the superseded one never reaches the model.
        await _drain_to_idle(sub)
        await session.send_prompt("and for AMER", model=_MODEL)
        later: list[str] = []
        deadline = asyncio.get_running_loop().time() + 60.0
        while asyncio.get_running_loop().time() < deadline:
            later = [
                text
                for request in list(server.requests)
                for _role, text in _messages(request)
                if _spoken(text) == "and for AMER"
            ]
            if later:
                break
            await asyncio.sleep(0.1)
        await runtime.close_chat(sid)

    assert later, "the second turn never reached the model"
    for text in later:
        assert BRIEF_MARKER not in _steering(text), "the brief was steered a second time"
