"""The TODO system's prompt surfaces: the always-on steering block + the dynamic
per-turn task reminder (the live checklist re-shown each root turn)."""

from __future__ import annotations

from datetime import UTC, datetime

from alkera_cli.harness.system_prompt import (
    TASKS_SYSTEM_BLOCK,
    compose_main_agent_guidance,
    render_task_reminder,
)
from alkera_core.schemas.chat.tasks import Task, TaskList

NOW = datetime(2026, 6, 21, tzinfo=UTC)


def test_tasks_block_is_in_main_agent_guidance() -> None:
    guidance = compose_main_agent_guidance()
    assert TASKS_SYSTEM_BLOCK.strip() in guidance
    assert "manage_tasks" in guidance  # the tool is named so the model knows to call it


def test_render_reminder_is_none_when_empty() -> None:
    assert render_task_reminder(TaskList()) is None


def test_render_reminder_active_list() -> None:
    tl = TaskList(
        tasks=[
            Task(id="explore", title="Survey", status="completed", updated_at=NOW),
            Task(id="build", title="Build it", status="in_progress", depends_on=["explore"]),
            Task(id="ship", title="Ship it", depends_on=["build"]),
        ]
    )
    reminder = render_task_reminder(tl)
    assert reminder is not None
    # Tells the model the tool + how to keep it current + how to clear it.
    assert "manage_tasks" in reminder
    assert "clear" in reminder
    # The live checklist with status glyphs + blocked-by.
    assert "[x] explore" in reminder
    assert "[~] build" in reminder
    assert "[ ] ship" in reminder
    assert "(blocked by: build)" in reminder
    # PLAIN text — the adapter adds the single <system-reminder> wrapper, so the
    # reminder itself must not carry the tags, and must never leak a raw timestamp.
    assert "<system-reminder>" not in reminder
    assert "2026" not in reminder and "T00:00" not in reminder
