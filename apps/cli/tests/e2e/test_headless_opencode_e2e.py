"""Mock-e2e for the headless driver: `run_headless` against a REAL opencode
subprocess (bun-dev) and a scripted mock provider — the full pipeline the
headless experiment rig exercises, at zero API cost.

Proves (1) a plain text turn settles and surfaces its final text, (2) in
`default` mode a shell write flows through the headless permission auto-allow —
recorded as a would-have-prompted decision — and the command REALLY runs,
(3) a turn the provider cuts at the output cap is reported as truncated, and
(4) the per-model output limit is the cap opencode actually requests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_openai_server import (
    FOLLOWUP_KEY,
    MockOpenAIServer,
    text_chunks,
    tool_call_chunks,
)
from alkera_cli.chat.headless import run_headless

pytestmark = pytest.mark.opencode_e2e

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}

#: The shell tool the agent actually uses. One name on every platform now: on POSIX
#: the parent-hosted tool is advertised under the bare native name (the vendor patch
#: behind ``ALKERA_PARENT_SHELL`` drops opencode's ShellTool and re-advertises our
#: loopback-MCP ``alkera_bash`` as ``bash``); on Windows it IS opencode's native bash.
_SHELL_TOOL = "bash"


async def test_headless_text_turn_completes(tmp_path: Path) -> None:
    events_path = tmp_path / "artifacts" / "events.jsonl"
    async with opencode_e2e_runtime(
        tmp_path, mock_script={"*": text_chunks("Hi from the mock!")}
    ) as (runtime, sid, _server):
        result = await run_headless(
            tmp_path,
            ["say hi"],
            runtime=runtime,
            resume_session_id=sid,
            turn_model=_MODEL,
            timeout_seconds=120.0,
            events_path=events_path,
        )

    assert result.stop_reason == "completed"
    assert "Hi from the mock!" in result.final_text
    assert result.session_id == sid
    assert result.error_detail is None
    # The event stream was captured for offline inspection.
    parsed = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert any(p["event_type"] == "part.created" for p in parsed)
    assert any(p["event_type"] == "session.status_changed" for p in parsed)


async def test_headless_default_mode_write_prompt_auto_allowed_and_runs(tmp_path: Path) -> None:
    target = "made_by_headless"
    script = {
        "*": tool_call_chunks(
            _SHELL_TOOL, {"command": f"mkdir {target}", "description": "make a directory"}
        ),
        FOLLOWUP_KEY: text_chunks("created it"),
    }
    async with opencode_e2e_runtime(tmp_path, mock_script=script) as (runtime, sid, _server):
        result = await run_headless(
            tmp_path,
            ["make a scratch directory"],
            runtime=runtime,
            resume_session_id=sid,
            turn_model=_MODEL,
            permission_mode="default",  # a shell write PROMPTS in default mode
            timeout_seconds=120.0,
        )

    assert result.stop_reason == "completed"
    # The would-have-prompted request reached the headless resolver and was allowed.
    assert len(result.permission_prompts) == 1, result.permission_prompts
    assert result.permission_prompts[0]["decision"] == "allow_once"
    # The decision is durably audited in the per-chat log — the file
    # `harness.list_decisions` serves to the editor's Decisions tab.
    from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink

    audited = DecisionSink(tmp_path / ".alkera" / "chats" / sid).read()
    assert [(r.session_id, r.decision) for r in audited] == [(sid, "allow")], audited
    # The allow was real: the command executed against the workspace.
    assert (tmp_path / target).is_dir(), "the auto-allowed mkdir never ran"
    # The tool call surfaced in the structured result.
    assert any(_SHELL_TOOL in (tc["tool_name"] or "") for tc in result.tool_calls), (
        result.tool_calls
    )
    assert "created it" in result.final_text


async def test_headless_reports_a_provider_cut_turn_as_max_tokens(tmp_path: Path) -> None:
    """The provider stops at the output cap and reports `length`; opencode then
    goes idle with no turn result. Calling that "completed" hands a script a
    half-written answer as if it were the whole one."""
    cut_short = "Half an answer before the cap"
    async with opencode_e2e_runtime(
        tmp_path, mock_script={"*": text_chunks(cut_short, finish_reason="length")}
    ) as (runtime, sid, _server):
        result = await run_headless(
            tmp_path,
            ["write forever"],
            runtime=runtime,
            resume_session_id=sid,
            turn_model=_MODEL,
            timeout_seconds=120.0,
        )

    assert result.stop_reason == "max_tokens"
    assert cut_short in result.final_text


#: The openai-compatible wire carries the per-step cap under one of these.
_MAX_TOKENS_FIELDS = ("max_tokens", "max_completion_tokens")


def _requested_caps(server: MockOpenAIServer) -> list[int]:
    """Every per-step output cap the mock provider was asked for."""
    caps = [
        body[field]
        for body in server.requests
        for field in _MAX_TOKENS_FIELDS
        if isinstance(body.get(field), int)
    ]
    assert caps, f"no output cap on any request: {server.requests}"
    return caps


@pytest.mark.parametrize(
    ("model_limit", "expected_cap"),
    [
        pytest.param(
            {"context": 200_000, "input": 180_000, "output": 100_000}, 100_000, id="raised"
        ),
        pytest.param(None, 32_000, id="opencodes-own-ceiling"),
    ],
)
async def test_the_declared_output_limit_is_the_cap_opencode_requests(
    tmp_path: Path, model_limit: dict[str, int] | None, expected_cap: int
) -> None:
    """opencode clamps every step to its own 32k unless the env raises the
    ceiling, so a 100k model would silently write 32k answers. The no-limit
    case is the negative: it proves the declared limit is what moved the cap."""
    async with opencode_e2e_runtime(
        tmp_path, mock_script={"*": text_chunks("done")}, model_limit=model_limit
    ) as (runtime, sid, server):
        result = await run_headless(
            tmp_path,
            ["say done"],
            runtime=runtime,
            resume_session_id=sid,
            turn_model=_MODEL,
            timeout_seconds=120.0,
        )

    assert result.stop_reason == "completed"
    assert max(_requested_caps(server)) == expected_cap
