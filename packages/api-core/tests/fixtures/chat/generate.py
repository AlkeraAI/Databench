"""Regenerate the lineage fixture corpus at the CURRENT writer version.

Run from the repo root::

    uv run python packages/api-core/tests/fixtures/chat/generate.py

The script writes every event / part / manifest shape we currently
ship into `packages/api-core/tests/fixtures/chat/v<current_SCHEMA_VERSION>/`. Commit the
diff alongside the schema change that prompted the regeneration.

NEVER edit old fixture files by hand. Migrations go in the model's
`MIGRATIONS` dict; fixtures stay frozen as historical evidence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from alkera_core.credit_refusal import CreditRefusal, CreditRefusalCode
from alkera_core.schemas.chat import (
    AgentMessageChunk,
    AgentPart,
    AgentThoughtChunk,
    AvailableCommandsUpdate,
    ChatManifest,
    CommandExecuted,
    CommandResult,
    CompactionApplied,
    CompactionPart,
    ConversationCleared,
    FileEdited,
    FilePart,
    Heartbeat,
    LlmCallFinished,
    LlmCallStarted,
    MessageCompleted,
    MessageCreated,
    PartCreated,
    PartStarted,
    PartUpdated,
    PatchPart,
    PermissionOption,
    PermissionRequest,
    PermissionResolved,
    PlanUpdated,
    PromptCancelled,
    QuestionAnswered,
    QuestionOption,
    QuestionPrompt,
    QuestionRejected,
    QuestionRequest,
    RateLimited,
    RawPart,
    RawProviderPart,
    ReasoningPart,
    Retrying,
    RetryPart,
    RevertApplied,
    SessionCreated,
    SessionStatusChanged,
    SessionUpdated,
    SnapshotPart,
    StepFinishPart,
    StepStartPart,
    SubagentCompleted,
    SubagentStarted,
    SubtaskPart,
    Task,
    TaskList,
    TextPart,
    TokenTotals,
    TombstoneApplied,
    ToolCall,
    ToolCallPart,
    ToolCallUpdate,
    TurnFinished,
    TurnStarted,
)
from alkera_core.versioning import corpus_version, resolve_corpus_dir

_T = datetime(2026, 5, 26, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixture generators — one factory per known shape.
# ---------------------------------------------------------------------------


def _events() -> dict[str, object]:
    return {
        "session_created": SessionCreated(
            event_id="01H_session_created",
            time=_T,
            session_id="s1",
            title="Investigate slow query",
            cwd="/Users/x/proj",
            model={"provider_id": "anthropic", "model_id": "claude-opus-4-7"},
            agent="general",
            harness={"name": "alkera-cli", "version": "0.1.0"},
        ),
        "session_updated": SessionUpdated(
            event_id="01H_session_updated",
            time=_T,
            session_id="s1",
            title="Renamed",
        ),
        "message_created": MessageCreated(
            event_id="01H_message_created",
            time=_T,
            session_id="s1",
            message_id="m1",
            role="user",
        ),
        "message_completed": MessageCompleted(
            event_id="01H_message_completed",
            time=_T,
            session_id="s1",
            message_id="m1",
            finish_reason="stop",
            tokens={"input": 100, "output": 50, "cache_read": 10, "cache_write": 0},
            cost=0.01,
        ),
        "part_created": PartCreated(
            event_id="01H_part_created",
            time=_T,
            session_id="s1",
            part=TextPart(part_id="p1", message_id="m1", time=_T, text="hi"),
        ),
        "part_updated": PartUpdated(
            event_id="01H_part_updated",
            time=_T,
            session_id="s1",
            part_id="p1",
            patch={"state": "completed", "output": {"ok": True}},
        ),
        "revert_applied": RevertApplied(
            event_id="01H_revert_applied",
            time=_T,
            session_id="s1",
            to_message_id="m1",
        ),
        "compaction_applied": CompactionApplied(
            event_id="01H_compaction_applied",
            time=_T,
            session_id="s1",
            summarised_message_ids=["m1", "m2"],
            summary_text="user asked about query",
        ),
        "conversation_cleared": ConversationCleared(
            event_id="01H_conversation_cleared",
            time=_T,
            session_id="s1",
            cleared_message_ids=["m1", "m2"],
        ),
        "tombstone_applied": TombstoneApplied(
            event_id="01H_tombstone_applied",
            time=_T,
            session_id="s1",
            event_ids=["01H_message_created"],
            reason="pii redaction",
        ),
        # ----- New IR events (harness layer) -----
        "session_status_changed": SessionStatusChanged(
            event_id="01H_session_status_changed",
            time=_T,
            session_id="s1",
            status="running",
            phase="streaming_response",
            detail="calling claude-opus-4-7",
        ),
        "session_status_changed_refused": SessionStatusChanged(
            event_id="01H_session_status_changed_refused",
            time=_T,
            session_id="s1",
            status="error",
            phase="error",
            detail="Your budget on Data Science is used up.",
            turn_id="t1",
            refusal=CreditRefusal(
                code=CreditRefusalCode.MEMBER_BUDGET_EXHAUSTED,
                message="Your budget on Data Science is used up.",
                team_id="6b1f2c0e-3d4a-4b5c-8d6e-7f8091a2b3c4",
                team_name="Data Science",
                resets_at=_T,
                manage_url="https://app.alkera.dev/settings/billing",
            ),
        ),
        "part_started": PartStarted(
            event_id="01H_part_started",
            time=_T,
            session_id="s1",
            message_id="m1",
            part_id="p1",
            part_type="text",
        ),
        "agent_message_chunk": AgentMessageChunk(
            event_id="01H_agent_message_chunk",
            time=_T,
            session_id="s1",
            message_id="m1",
            part_id="p1",
            sequence=0,
            text="Hello",
            is_final=False,
        ),
        "agent_thought_chunk": AgentThoughtChunk(
            event_id="01H_agent_thought_chunk",
            time=_T,
            session_id="s1",
            message_id="m1",
            part_id="p_think",
            sequence=0,
            text="Let me think…",
        ),
        "turn_started": TurnStarted(
            event_id="01H_turn_started",
            time=_T,
            session_id="s1",
            turn_id="t1",
            user_message_id="m1",
            model={"provider_id": "anthropic", "model_id": "claude-opus-4-7"},
        ),
        "turn_finished": TurnFinished(
            event_id="01H_turn_finished",
            time=_T,
            session_id="s1",
            turn_id="t1",
            stop_reason="end_turn",
            cost_usd=0.012,
            tokens={"input": 100, "output": 50},
        ),
        "llm_call_started": LlmCallStarted(
            event_id="01H_llm_call_started",
            time=_T,
            session_id="s1",
            call_id="c1",
            turn_id="t1",
            model={"provider_id": "anthropic", "model_id": "claude-opus-4-7"},
        ),
        "llm_call_finished": LlmCallFinished(
            event_id="01H_llm_call_finished",
            time=_T,
            session_id="s1",
            call_id="c1",
            turn_id="t1",
            finish_reason="stop",
            tokens={"input": 100, "output": 50},
            cost_usd=0.012,
        ),
        "tool_call": ToolCall(
            event_id="01H_tool_call",
            time=_T,
            session_id="s1",
            tool_call_id="tc1",
            provider_call_id="call_tc1",
            message_id="m1",
            tool_name="read",
            tool_kind="read",
            input={"path": "/etc/hosts"},
            status="pending",
        ),
        "tool_call_update": ToolCallUpdate(
            event_id="01H_tool_call_update",
            time=_T,
            session_id="s1",
            tool_call_id="tc1",
            status="completed",
            output={"content": "127.0.0.1 localhost"},
        ),
        "permission_request": PermissionRequest(
            event_id="01H_permission_request",
            time=_T,
            session_id="s1",
            request_id="r1",
            tool_call_id="tc1",
            provider_call_id="call_tc1",
            permission_kind="edit",
            canonical_kind="edit",
            patterns=["read /etc/hosts"],
            insertions=1,
            deletions=1,
            preview={
                "kind": "diff",
                "content": "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new",
                "title": "x.py",
            },
            options=[
                PermissionOption(option_id="allow_once", name="Allow once"),
                PermissionOption(option_id="allow_always", name="Always allow"),
                PermissionOption(option_id="reject_once", name="Reject once"),
                PermissionOption(option_id="reject_always", name="Always reject"),
            ],
        ),
        "permission_resolved": PermissionResolved(
            event_id="01H_permission_resolved",
            time=_T,
            session_id="s1",
            request_id="r1",
            option_id="allow_once",
            decided_by="user",
            decided_by_user_id="11111111-1111-4111-8111-111111111111",
            decided_by_name="Dana Okafor",
            decided_via="slack",
        ),
        # B1: typed plan-approval kind discriminator.
        "question_request": QuestionRequest(
            event_id="01H_question_request",
            time=_T,
            session_id="s1",
            request_id="qr1",
            kind="question",
            questions=[
                QuestionPrompt(
                    question="Pick a color",
                    options=[
                        QuestionOption(label="Red"),
                        QuestionOption(label="Blue"),
                    ],
                )
            ],
        ),
        "question_request_plan_approval": QuestionRequest(
            event_id="01H_question_request_plan",
            time=_T,
            session_id="s1",
            request_id="qr2",
            kind="plan_approval",
            plan_markdown="## Plan\n\n1. Read the config\n2. Update the handler\n3. Add a test",
            questions=[
                QuestionPrompt(
                    question="Approve this plan?",
                    header="plan-approval",
                    options=[
                        QuestionOption(label="Accept — run normally"),
                        QuestionOption(label="Accept — bypass"),
                    ],
                    custom=True,
                )
            ],
        ),
        "question_answered": QuestionAnswered(
            event_id="01H_question_answered",
            time=_T,
            session_id="s1",
            request_id="qr1",
            answers=[["Red"]],
            decided_by="user",
            decided_by_user_id="11111111-1111-4111-8111-111111111111",
            decided_by_name="Dana Okafor",
            note="Skip the migration for now.",
        ),
        "question_rejected": QuestionRejected(
            event_id="01H_question_rejected",
            time=_T,
            session_id="s1",
            request_id="qr1",
        ),
        # File / command surface events.
        "file_edited": FileEdited(
            event_id="01H_file_edited",
            time=_T,
            session_id="s1",
            path="src/foo.py",
            insertions=2,
            deletions=0,
            preview={
                "kind": "diff",
                "content": ("--- src/foo.py\n+++ src/foo.py\n@@ -0,0 +1,2 @@\n+line a\n+line b\n"),
                "title": "foo.py",
            },
        ),
        "command_executed": CommandExecuted(
            event_id="01H_command_executed",
            time=_T,
            session_id="s1",
            name="/init",
            arguments="--force",
            message_id="m1",
        ),
        "command_result": CommandResult(
            event_id="01H_command_result",
            time=_T,
            session_id="s1",
            command="usage",
            outcome_kind="ok",
            payload={"window": "30d", "credits": {"plan": "pro"}},
            message=None,
        ),
        # B2-related: a tool update that carries `input` (the bug we
        # locked down in this sweep — early sightings of pending tool
        # calls now forward the input dict instead of dropping it).
        "tool_call_update_with_input": ToolCallUpdate(
            event_id="01H_tool_call_update_input",
            time=_T,
            session_id="s1",
            tool_call_id="tc1",
            status="running",
            input={"path": "/tmp/example.txt"},
        ),
        "subagent_started": SubagentStarted(
            event_id="01H_subagent_started",
            time=_T,
            session_id="s1",
            child_session_id="child_s2",
            parent_message_id="m1",
            agent_name="explore",
            description="explore the codebase",
            prompt="find every reference to FooBar and report file:line",
        ),
        "subagent_completed": SubagentCompleted(
            event_id="01H_subagent_completed",
            time=_T,
            session_id="s1",
            child_session_id="child_s2",
            summary="Found 3 references to FooBar",
        ),
        "available_commands_update": AvailableCommandsUpdate(
            event_id="01H_available_commands_update",
            time=_T,
            session_id="s1",
            commands=[{"name": "/clear", "description": "Clear chat history"}],
        ),
        "plan_updated": PlanUpdated(
            event_id="01H_plan_updated",
            time=_T,
            session_id="s1",
            entries=[
                {"id": "1", "text": "Read file", "status": "completed"},
                {"id": "2", "text": "Edit file", "status": "in_progress"},
            ],
        ),
        "heartbeat": Heartbeat(
            event_id="01H_heartbeat",
            time=_T,
            session_id="s1",
            last_activity_ms=12500,
        ),
        "rate_limited": RateLimited(
            event_id="01H_rate_limited",
            time=_T,
            session_id="s1",
            retry_in_ms=30_000,
            provider="anthropic",
            message="rate limit exceeded",
        ),
        "retrying": Retrying(
            event_id="01H_retrying",
            time=_T,
            session_id="s1",
            attempt=2,
            reason="network error",
            next_attempt_in_ms=2000,
        ),
        "prompt_cancelled": PromptCancelled(
            event_id="01H_prompt_cancelled",
            time=_T,
            session_id="s1",
            message_id="usr:c1",
            client_id="c1",
        ),
    }


def _parts() -> dict[str, object]:
    return {
        "text": TextPart(part_id="p1", message_id="m1", time=_T, text="hello"),
        # Synthetic text — the steering surface (plan-mode reminders,
        # opencode's internal nudges) that the renderer hides but
        # persists for replay.
        "text_synthetic": TextPart(
            part_id="p_syn",
            message_id="m1",
            time=_T,
            text="<system-reminder>\nplan mode active\n</system-reminder>",
            synthetic=True,
        ),
        "reasoning": ReasoningPart(part_id="p1", message_id="m1", time=_T, text="thinking…"),
        "file": FilePart(
            part_id="p1",
            message_id="m1",
            time=_T,
            sha256="a" * 64,
            filename="x.png",
            mime="image/png",
            size=42,
        ),
        # A file that came from the Files tree rather than the blob store: the
        # part names the NODE, and the digest may not be known yet.
        "file_node": FilePart(
            part_id="p1",
            message_id="m1",
            time=_T,
            node_id="6b16bf6f-3c1a-4b64-9a1f-0f5f9a52c0d1",
            filename="report.json",
            mime="application/json",
            size=20_000_000,
            source="file",
        ),
        "tool_call": ToolCallPart(
            part_id="p1",
            message_id="m1",
            time=_T,
            call_id="c1",
            name="read_file",
            input={"path": "/tmp/x"},
            state="completed",
            output={"content": "ok"},
        ),
        "step_start": StepStartPart(part_id="p1", message_id="m1", time=_T),
        "step_finish": StepFinishPart(
            part_id="p1",
            message_id="m1",
            time=_T,
            reason="stop",
            tokens={"input": 100, "output": 50},
            cost=0.01,
        ),
        "snapshot": SnapshotPart(part_id="p1", message_id="m1", time=_T, snapshot_hash="abc"),
        "patch": PatchPart(
            part_id="p1", message_id="m1", time=_T, hash="def", files=["a.py", "b.py"]
        ),
        "agent": AgentPart(part_id="p1", message_id="m1", time=_T, name="general"),
        "subtask": SubtaskPart(
            part_id="p1",
            message_id="m1",
            time=_T,
            prompt="do the thing",
            agent="explorer",
        ),
        "compaction": CompactionPart(part_id="p1", message_id="m1", time=_T, auto=True),
        "retry": RetryPart(part_id="p1", message_id="m1", time=_T, attempt=2, error="rate-limit"),
        "raw_provider": RawProviderPart(
            part_id="p1",
            message_id="m1",
            time=_T,
            provider_id="anthropic",
            payload={"vendor_field": 1},
        ),
        # RawPart isn't a "shape we'd write" by intent — it's the
        # unknown-tag fallback. We still capture a fixture so the
        # lineage test exercises the fallback path on disk.
        "raw_fallback": RawPart(part_id="p1", message_id="m1", time=_T, type="future_unknown_kind"),
    }


def _tasks() -> dict[str, object]:
    return {
        "minimal": TaskList(),
        "full": TaskList(
            tasks=[
                Task(
                    id="explore",
                    title="Survey the repo",
                    description="Find the seams the change touches.",
                    status="completed",
                    created_at=_T,
                    updated_at=_T,
                ),
                Task(
                    id="create-schema",
                    title="Create staging schema",
                    status="in_progress",
                    depends_on=["explore"],
                    created_at=_T,
                    updated_at=_T,
                ),
                Task(
                    id="load-data",
                    title="Load raw data",
                    status="pending",
                    depends_on=["create-schema"],
                    created_at=_T,
                    updated_at=_T,
                ),
                Task(
                    id="review",
                    title="Review the change",
                    description="Spawn the Review agent before delivering.",
                    status="pending",
                    depends_on=["load-data"],
                    created_at=_T,
                    updated_at=_T,
                ),
            ]
        ),
    }


def _manifests() -> dict[str, object]:
    return {
        "minimal": ChatManifest(session_id="s1"),
        "full": ChatManifest(
            session_id="s1",
            title="Sample",
            created_at=_T,
            updated_at=_T,
            cwd="/Users/x/proj",
            model={"provider_id": "anthropic", "model_id": "claude-opus-4-7"},
            agent="general",
            permission_mode="plan",
            tokens_total=TokenTotals(input=100, output=50, cache_read=10),
            cost_total=0.01,
            harness={"name": "alkera-cli", "version": "0.1.0"},
        ),
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def corpus() -> dict[str, dict[str, object]]:
    """Every model this corpus embeds, grouped by the directory it lands in."""
    return {
        "events": _events(),
        "parts": _parts(),
        "manifest": _manifests(),
        "tasks": _tasks(),
    }


def _corpus_version() -> str:
    """The corpus is stamped with the MAX SCHEMA_VERSION across every
    model included in this generator — the floor the corpus can carry.

    NEVER delete old fixture directories. They're the regression net
    for backward-compatibility: the lineage test loads every historical
    corpus with the current reader and asserts it still validates. A
    regenerate whose output differs from what is committed at that stamp
    lands in a NEW directory (`resolve_corpus_dir`) rather than rewriting
    a past writer's evidence."""
    return corpus_version(
        [model for group in corpus().values() for model in group.values()]  # type: ignore[misc]
    )


def regenerate(root: Path | None = None) -> Path:
    """Regenerate fixtures under ``<root>/v<X_Y_Z>/``; returns the target
    directory, which is a fresh one whenever the committed corpus at the
    stamped version says a different writer produced it."""
    root = root or Path(__file__).parent
    payload = {
        f"{group}/{name}.json": _encode(model)
        for group, models in corpus().items()
        for name, model in models.items()
    }
    version_dir = resolve_corpus_dir(root, _corpus_version(), payload)
    for relative, blob in payload.items():
        target = version_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    return version_dir


def _encode(model: object) -> bytes:
    payload = model.model_dump(mode="json")  # type: ignore[attr-defined]
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


if __name__ == "__main__":
    target = regenerate()
    print(f"wrote fixtures under {target}")
