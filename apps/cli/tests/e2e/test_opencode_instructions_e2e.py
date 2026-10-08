"""End-to-end proof that repo instruction files reach the model.

A REAL bun-driven opencode subprocess (the `opencode_e2e` marker) is pointed at a
project containing `ALKERA.md` + `AGENTS.md`, and we assert both files' content
lands in the system prompt the model actually receives — with `ALKERA.md` first
(precedence) and `AGENTS.md` still present (additive).

This is the end-to-end half of the instruction-file support; the exhaustive unit
coverage of discovery / additivity / precedence / subdir "nearby" attachment /
size caps lives in the vendored bun suite (`vendor/opencode/packages/opencode/
test/session/instruction.test.ts`). Together they keep the mocked + live tiers in
lockstep.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from _helpers.opencode_runner import opencode_e2e_adapter, opencode_e2e_runtime
from _mocks.mock_openai_server import text_chunks
from alkera_cli.harness.adapter import PromptInput
from alkera_cli.host import paths

pytestmark = pytest.mark.opencode_e2e

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}


def _system_text(request: dict[str, Any]) -> str:
    """Concatenate the SYSTEM/developer message content of a recorded request,
    preserving order — i.e. the system prompt the model received that turn."""
    out: list[str] = []
    for message in request.get("messages", []):
        if not isinstance(message, dict) or message.get("role") not in ("system", "developer"):
            continue
        content = message.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out.extend(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return "\n".join(out)


async def _wait_for_system_prompt(server: Any, needle: str, *, budget_seconds: float = 60.0) -> str:
    """Poll until some recorded request carries `needle` in its system prompt and
    return that system text. Scans ALL requests because opencode fires auxiliary
    calls (e.g. title generation) with their own, smaller system prompts."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    while loop.time() < deadline:
        for request in server.requests:
            system_text = _system_text(request)
            if needle in system_text:
                return system_text
        await asyncio.sleep(0.1)
    seen = sorted({_system_text(r)[:80] for r in server.requests if _system_text(r)})
    raise AssertionError(
        f"no model request carried {needle!r} in its system prompt within "
        f"{budget_seconds}s. Distinct system-prompt openings seen:\n"
        + "\n".join(repr(s) for s in seen)
    )


@pytest.mark.asyncio
async def test_repo_alkera_and_agents_md_reach_system_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project-root ALKERA.md and AGENTS.md are BOTH injected into the system
    prompt (additive), with ALKERA.md emitted first (precedence)."""
    (tmp_path / "ALKERA.md").write_text("ALKERA_MARKER_7x9 alkera-specific guidance\n")
    (tmp_path / "AGENTS.md").write_text("AGENTS_MARKER_7x9 generic agent guidance\n")

    async with opencode_e2e_adapter(
        tmp_path, monkeypatch, mock_script={"*": text_chunks("ok")}
    ) as (adapter, server):
        await adapter.send_prompt(PromptInput(text="ping instructions", model=_MODEL))
        system_text = await _wait_for_system_prompt(server, "ALKERA_MARKER_7x9")

    # Additive: the generic AGENTS.md is loaded alongside, not suppressed.
    assert "AGENTS_MARKER_7x9" in system_text
    # opencode prefixes each instruction block with its source path.
    assert "Instructions from:" in system_text
    # Precedence: ALKERA.md's block leads the AGENTS.md block.
    assert system_text.index("ALKERA_MARKER_7x9") < system_text.index("AGENTS_MARKER_7x9")


@pytest.mark.asyncio
async def test_global_instructions_reach_model_via_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user's `~/.alkera/instructions.md` flows through the WHOLE alkera path —
    `HarnessRuntime.open_chat` reads + caps it → the opencode adapter materializes it
    into the sandbox → opencode injects it via `config.instructions[]`."""
    instructions = tmp_path / "instructions.md"
    instructions.write_text("GLOBAL_MARKER_7x9 prefer DuckDB everywhere\n")
    monkeypatch.setattr(paths, "INSTRUCTIONS_FILE_PATH", instructions)

    async with opencode_e2e_runtime(tmp_path, mock_script={"*": text_chunks("ok")}) as (
        runtime,
        sid,
        server,
    ):
        session = await runtime.open_chat(sid)
        await session.send_prompt("ping global", model=_MODEL)
        system_text = await _wait_for_system_prompt(server, "GLOBAL_MARKER_7x9")
        await runtime.close_chat(sid)

    assert "Instructions from:" in system_text
