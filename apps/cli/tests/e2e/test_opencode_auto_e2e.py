"""Auto-mode grounded-judge gating through a REAL opencode subprocess.

The runtime-level matrix (test_auto_mode_grounding) feeds the permission loop
synthetic requests; this tier proves the SAME behaviour end-to-end across the
live OpenCode transport — a real bun-driven opencode actually calls the native
`bash` tool, emits a real `permission.asked`, and the runtime resolves it through
the single `DecisionEngine.resolve` chokepoint with an INJECTED judge whose
verdict the test controls. So every claim is checked against a real side effect
(a directory created or not, a denial reason that reached the model or not)
rather than a mocked reply.

What the four cases pin, end-to-end:
- auto + ordinary write + judge ALLOW → the tool runs with NO human prompt and the
  filesystem change actually lands;
- auto + write + judge BLOCK → the command never runs, the judge's reason rides
  back to the model (not "user rejected"), and the turn CONTINUES;
- auto + destroy floor (`rm -rf`) → the human is prompted and the judge is NEVER
  consulted (the floor is unwaivable, not judge-able);
- default mode + write → the human is prompted and the judge is never consulted
  (grounding is an auto-mode-only path).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_openai_server import (
    FOLLOWUP_KEY,
    MockOpenAIServer,
    text_chunks,
    tool_call_chunks,
)
from alkera_cli.contracts.tool_types import ActionDescriptor
from alkera_cli.harness.permission_broker import PermissionBroker
from alkera_cli.harness.safety_judge import JudgeVerdict
from alkera_core.schemas.chat import Event, PermissionRequest, SessionStatusChanged

pytestmark = pytest.mark.opencode_e2e

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}

#: The shell tool the agent actually uses. One name on every platform now: on POSIX
#: it is OUR parent-hosted tool advertised under the bare native name (the vendor
#: patch behind ``ALKERA_PARENT_SHELL``); on Windows it is opencode's native bash
#: (the parent-hosted tool is POSIX-only). The auto-mode assertions are behavioral
#: (judge consulted, destroy floor prompts, write runs) and hold for either gate path
#: (the in-tool gate vs the harness loop).
_SHELL_TOOL = "bash"


class _FakeJudge:
    """Injected safety judge — the test owns the verdict, so the e2e exercises
    the whole auto-mode path deterministically with no real gateway call. Records
    the raw command of every action it was asked to judge."""

    def __init__(self, verdict: JudgeVerdict) -> None:
        self._verdict = verdict
        self.calls: list[str] = []

    async def judge(
        self,
        descriptor: ActionDescriptor,
        task_goal: str,
        *,
        workspace_root: str | None = None,
    ) -> JudgeVerdict:
        self.calls.append(descriptor.raw or "")
        return self._verdict


def _broker(prompts: list[PermissionRequest]) -> PermissionBroker:
    """A human broker that records every prompt it's asked to resolve and always
    allows — so a wrongly-prompted action still lets the turn finish, and the test
    fails on the recorded prompt rather than hanging."""

    async def _resolver(request: PermissionRequest) -> str:
        prompts.append(request)
        return "allow_once"

    return PermissionBroker(_resolver, default_timeout_seconds=30.0)


def _bash_script(command: str, *, reply: str = "done") -> dict[str, Any]:
    """A two-turn script: the FIRST model turn calls our parent-hosted shell tool
    (routed via "*", so an injected mode/system prompt can't break key matching);
    once the tool resolves, the follow-up turn returns plain text and the turn ends.

    On POSIX the tool named ``bash`` IS ours: opencode's native ShellTool is dropped
    from the advertised set and our loopback-MCP tool takes the name, so the agent
    runs commands through ours — which is where the auto-mode gate (judge / destroy
    floor) lives."""
    return {
        "*": tool_call_chunks(
            _SHELL_TOOL, {"command": command, "description": "run a shell command"}
        ),
        FOLLOWUP_KEY: text_chunks(reply),
    }


async def _drain(sub: Any, *, budget_seconds: float = 90.0) -> list[Event]:
    """Drain exactly one turn: wait for `running`, then return on the next
    idle/error."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    out: list[Event] = []
    seen_running = False
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return out
        try:
            async with asyncio.timeout(remaining):
                ev: Event = await anext(sub)
        except (TimeoutError, StopAsyncIteration):
            return out
        out.append(ev)
        if isinstance(ev, SessionStatusChanged):
            if ev.status == "running":
                seen_running = True
            elif ev.status in ("idle", "error") and seen_running:
                return out


def _statuses(events: list[Event]) -> list[str]:
    return [e.status for e in events if isinstance(e, SessionStatusChanged)]


def _requests_blob(server: MockOpenAIServer) -> str:
    """Everything the model saw across the turn — used to prove a denial reason
    actually flowed back into the conversation (tool-result feedback)."""
    return json.dumps(server.requests)


async def test_auto_ordinary_write_runs_when_judge_allows(tmp_path: Path) -> None:
    judge = _FakeJudge(JudgeVerdict("allow", reason="ordinary scratch dir"))
    target = "made_by_agent"
    async with opencode_e2e_runtime(
        tmp_path, mock_script=_bash_script(f"mkdir {target}"), safety_judge=judge
    ) as (runtime, sid, _server):
        prompts: list[PermissionRequest] = []
        session = await runtime.open_chat(sid, permission_broker=_broker(prompts))
        session.set_permission_mode("auto")
        sub = session.subscribe()
        await session.send_prompt("make a scratch directory", model=_MODEL)
        events = await _drain(sub)
        await runtime.close_chat(sid)

    statuses = _statuses(events)
    assert "idle" in statuses, f"turn never completed (statuses={statuses})"
    assert "error" not in statuses, f"turn errored (statuses={statuses})"
    # The write was handed to the judge — and ONLY the human was spared.
    assert any("mkdir" in c for c in judge.calls), f"write not judged (calls={judge.calls})"
    assert prompts == [], "auto wrongly prompted the human on a judge-cleared write"
    # The judge's allow actually let the command run — a real directory exists.
    assert (tmp_path / target).is_dir(), "the allowed mkdir never ran"


async def test_auto_write_blocked_by_judge_reason_reaches_model(tmp_path: Path) -> None:
    sentinel = "OFFTASK-SENTINEL-9173"
    judge = _FakeJudge(JudgeVerdict("block", reason=f"refused: {sentinel}"))
    target = "should_not_exist.txt"
    async with opencode_e2e_runtime(
        tmp_path,
        mock_script=_bash_script(f"touch {target}", reply="ok, I'll try something else"),
        safety_judge=judge,
    ) as (runtime, sid, server):
        prompts: list[PermissionRequest] = []
        session = await runtime.open_chat(sid, permission_broker=_broker(prompts))
        session.set_permission_mode("auto")
        sub = session.subscribe()
        await session.send_prompt("create a file", model=_MODEL)
        events = await _drain(sub)
        blob = _requests_blob(server)
        await runtime.close_chat(sid)

    statuses = _statuses(events)
    # A BLOCK is not a turn-ending error — the model gets a follow-up turn.
    assert "idle" in statuses, f"the blocked turn did not continue (statuses={statuses})"
    assert "error" not in statuses, f"a block must not error the turn (statuses={statuses})"
    assert any("touch" in c for c in judge.calls), f"write not judged (calls={judge.calls})"
    assert prompts == [], "a judge BLOCK must not also prompt the human"
    # The block actually prevented the side effect…
    assert not (tmp_path / target).exists(), "a blocked touch still created the file"
    # …and the judge's reason rode back to the model (not a generic rejection).
    assert sentinel in blob, "the judge's block reason never reached the model"


async def test_auto_destroy_floor_prompts_human_and_skips_judge(tmp_path: Path) -> None:
    judge = _FakeJudge(JudgeVerdict("allow", reason="should never be consulted"))
    async with opencode_e2e_runtime(
        tmp_path, mock_script=_bash_script("rm -rf doomed_dir"), safety_judge=judge
    ) as (runtime, sid, _server):
        prompts: list[PermissionRequest] = []
        session = await runtime.open_chat(sid, permission_broker=_broker(prompts))
        session.set_permission_mode("auto")
        sub = session.subscribe()
        await session.send_prompt("clean up the build dir", model=_MODEL)
        events = await _drain(sub)
        await runtime.close_chat(sid)

    statuses = _statuses(events)
    assert "idle" in statuses, f"turn never completed (statuses={statuses})"
    # The destroy floor is unwaivable: it goes to the human, never to the judge.
    assert judge.calls == [], f"the destroy floor reached the judge (calls={judge.calls})"
    assert len(prompts) == 1, f"the destroy floor did not prompt the human (prompts={prompts})"
    assert prompts[0].canonical_kind == "shell"


async def test_default_mode_write_prompts_human_and_skips_judge(tmp_path: Path) -> None:
    judge = _FakeJudge(JudgeVerdict("allow", reason="should never be consulted"))
    target = "made_in_default.txt"
    async with opencode_e2e_runtime(
        tmp_path, mock_script=_bash_script(f"touch {target}"), safety_judge=judge
    ) as (runtime, sid, _server):
        prompts: list[PermissionRequest] = []
        session = await runtime.open_chat(sid, permission_broker=_broker(prompts))
        session.set_permission_mode("default")
        sub = session.subscribe()
        await session.send_prompt("create a file", model=_MODEL)
        events = await _drain(sub)
        await runtime.close_chat(sid)

    statuses = _statuses(events)
    assert "idle" in statuses, f"turn never completed (statuses={statuses})"
    # Grounding is auto-only: in default mode a write prompts the human directly.
    assert judge.calls == [], f"default mode must not consult the judge (calls={judge.calls})"
    assert len(prompts) == 1, f"default mode did not prompt on a write (prompts={prompts})"
    # The human allowed it → the file really was created.
    assert (tmp_path / target).is_file(), "the human-allowed touch never ran"


async def test_read_only_write_denied_continues_with_reason(tmp_path: Path) -> None:
    # The user's scenario, live: read_only auto-rejects the write (decided_by="mode").
    # A provenance reason ("read-only mode, set by the user") rides back as a recoverable
    # tool-error (opencode CorrectedError) and the turn CONTINUES — the model gets a
    # follow-up turn to adapt, instead of silently ending on the denial.
    target = "should_not_exist.txt"
    async with opencode_e2e_runtime(
        tmp_path,
        mock_script=_bash_script(f"touch {target}", reply="ok, switching to read-only work"),
    ) as (runtime, sid, server):
        prompts: list[PermissionRequest] = []
        session = await runtime.open_chat(sid, permission_broker=_broker(prompts))
        session.set_permission_mode("read_only")
        sub = session.subscribe()
        await session.send_prompt("create a file", model=_MODEL)
        events = await _drain(sub)
        blob = _requests_blob(server)
        await runtime.close_chat(sid)

    statuses = _statuses(events)
    assert "idle" in statuses, f"read-only denial did not continue the turn (statuses={statuses})"
    assert "error" not in statuses
    assert prompts == [], "read_only auto-rejects — it must NOT prompt the human"
    assert not (tmp_path / target).exists(), "a read-only-denied touch still created the file"
    assert "read-only mode" in blob, "the provenance reason never reached the model"


async def test_manual_reject_ends_the_turn(tmp_path: Path) -> None:
    # default mode: a write PROMPTS the human; on a manual reject the turn ENDS (the
    # reason-less reject is a turn-ending opencode RejectedError) — it does NOT continue
    # to a follow-up model turn, so the scripted follow-up reply never reaches the model.
    sentinel = "FOLLOWUP-SHOULD-NOT-RUN-7321"
    target = "made_after_reject.txt"

    async def _reject(request: PermissionRequest) -> str:
        return "reject_once"

    async with opencode_e2e_runtime(
        tmp_path, mock_script=_bash_script(f"touch {target}", reply=sentinel)
    ) as (runtime, sid, server):
        session = await runtime.open_chat(
            sid, permission_broker=PermissionBroker(_reject, default_timeout_seconds=30.0)
        )
        session.set_permission_mode("default")
        sub = session.subscribe()
        await session.send_prompt("create a file", model=_MODEL)
        events = await _drain(sub)
        blob = _requests_blob(server)
        await runtime.close_chat(sid)

    statuses = _statuses(events)
    assert "idle" in statuses, f"the rejected turn never settled (statuses={statuses})"
    assert sentinel not in blob, "the turn continued to a follow-up after a manual reject"
    assert not (tmp_path / target).exists(), "the rejected touch still created the file"
