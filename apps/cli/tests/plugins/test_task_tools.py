"""``manage_tasks`` — the unified TODO tool: dispatch behavior + the root-only
subagent gate.

The tool is hot and ``app="tasks"`` (root-only): a subagent must neither SEE it in
the advertised descriptors nor be able to CALL it (defense in depth), while the
root agent can add/edit/complete/clear and always gets the full list back with
derived blocked state and a RELATIVE updated time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alkera_cli.plugins.plugin_base import ToolRegistry
from alkera_cli.plugins.plugin_base.mcp_entry import alkera_tool_descriptors
from alkera_cli.plugins.plugin_base.task_tools import register_task_tools
from alkera_core.project.chats.tasks import TaskStore
from alkera_core.project.directory import ProjectDirectory


def _registry(tmp_path: Path) -> ToolRegistry:
    registry = ToolRegistry(ProjectDirectory(tmp_path / ".alkera").blobs())
    register_task_tools(registry)
    return registry


def _store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "tasks.json")


# A non-None spawn marks a ROOT session (children have spawn=None). manage_tasks
# never calls it; it only needs to be non-None to pass the root-only dispatch gate.
async def _spawn(*_a: Any, **_k: Any) -> Any:  # pragma: no cover - never invoked
    raise AssertionError("spawn should not be called by manage_tasks")


async def _dispatch(
    registry: ToolRegistry, args: dict[str, Any], *, store: TaskStore, root: bool = True
) -> dict[str, Any]:
    return await registry.dispatch(
        "manage_tasks",
        args,
        task_store=store,
        spawn=_spawn if root else None,
    )


# --------------------------------------------------------------------------
# Root-only gating (the user's hard requirement: subagents can't use it)
# --------------------------------------------------------------------------


def test_manage_tasks_is_hot(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    assert "manage_tasks" in {s.name for s in registry.hot_prefix()}


def test_descriptors_expose_to_root_hide_from_subagent(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    root = {d.name for d in alkera_tool_descriptors(registry, allow_task_tools=True)}
    child = {d.name for d in alkera_tool_descriptors(registry, allow_task_tools=False)}
    assert "manage_tasks" in root
    assert "manage_tasks" not in child


async def test_subagent_dispatch_refused(tmp_path: Path) -> None:
    # spawn=None ⇒ a child session ⇒ the binding backstop refuses the tool.
    out = await _dispatch(_registry(tmp_path), {}, store=_store(tmp_path), root=False)
    assert "not available to subagents" in out["error"]


async def test_root_dispatch_allowed(tmp_path: Path) -> None:
    out = await _dispatch(_registry(tmp_path), {}, store=_store(tmp_path), root=True)
    assert "error" not in out
    assert out["tasks"] == [] and out["summary"]["total"] == 0


# --------------------------------------------------------------------------
# Behavior — add / list / complete / clear, with derived blocked + ready
# --------------------------------------------------------------------------


async def test_add_then_readback_with_blocked_and_ready(tmp_path: Path) -> None:
    registry, store = _registry(tmp_path), _store(tmp_path)
    out = await _dispatch(
        registry,
        {
            "upsert": [
                {"id": "a", "title": "Create schema"},
                {"id": "b", "title": "Load data", "depends_on": ["a"]},
            ]
        },
        store=store,
    )
    by_id = {t["id"]: t for t in out["tasks"]}
    assert by_id["b"]["blocked"] is True and by_id["b"]["blocked_by"] == ["a"]
    assert by_id["a"]["blocked"] is False
    assert out["summary"]["ready"] == ["a"]  # a is pending + unblocked
    # A no-op call reads the same persisted state back.
    again = await _dispatch(registry, {}, store=store)
    assert {t["id"] for t in again["tasks"]} == {"a", "b"}


async def test_mark_done_unblocks_dependent(tmp_path: Path) -> None:
    registry, store = _registry(tmp_path), _store(tmp_path)
    await _dispatch(
        registry,
        {"upsert": [{"id": "a", "title": "A"}, {"id": "b", "title": "B", "depends_on": ["a"]}]},
        store=store,
    )
    out = await _dispatch(registry, {"upsert": [{"id": "a", "status": "completed"}]}, store=store)
    by_id = {t["id"]: t for t in out["tasks"]}
    assert by_id["a"]["status"] == "completed"
    assert by_id["b"]["blocked"] is False
    assert out["summary"]["ready"] == ["b"]


async def test_clear_wipes_the_list(tmp_path: Path) -> None:
    registry, store = _registry(tmp_path), _store(tmp_path)
    await _dispatch(registry, {"upsert": [{"id": "a", "title": "A"}]}, store=store)
    out = await _dispatch(registry, {"clear": True}, store=store)
    assert out["tasks"] == []


async def test_delete_removes_task(tmp_path: Path) -> None:
    registry, store = _registry(tmp_path), _store(tmp_path)
    await _dispatch(
        registry,
        {"upsert": [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]},
        store=store,
    )
    out = await _dispatch(registry, {"delete": ["a"]}, store=store)
    assert [t["id"] for t in out["tasks"]] == ["b"]


async def test_updated_time_is_relative_not_raw(tmp_path: Path) -> None:
    registry, store = _registry(tmp_path), _store(tmp_path)
    out = await _dispatch(registry, {"upsert": [{"id": "a", "title": "A"}]}, store=store)
    ago = out["tasks"][0]["updated_ago"]
    assert ago == "just now"  # freshly stamped
    # Never a raw ISO timestamp leaking to the model.
    assert "T" not in ago and "Z" not in ago and "2026" not in ago


# --------------------------------------------------------------------------
# Errors surface as clean model-visible results (the model self-corrects)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        pytest.param(
            {"upsert": [{"id": "a", "title": "A", "depends_on": ["ghost"]}]},
            "unknown task 'ghost'",
            id="unknown-dep",
        ),
        pytest.param({"upsert": [{"id": "a"}]}, "needs a 'title'", id="new-without-title"),
        pytest.param(
            {"upsert": [{"id": "bad id", "title": "x"}]}, "invalid task id", id="bad-slug"
        ),
    ],
)
async def test_validation_errors_are_clean(
    tmp_path: Path, args: dict[str, Any], fragment: str
) -> None:
    out = await _dispatch(_registry(tmp_path), args, store=_store(tmp_path))
    assert fragment in out["error"]


async def test_cycle_rejected(tmp_path: Path) -> None:
    registry, store = _registry(tmp_path), _store(tmp_path)
    await _dispatch(
        registry,
        {"upsert": [{"id": "a", "title": "A"}, {"id": "b", "title": "B", "depends_on": ["a"]}]},
        store=store,
    )
    out = await _dispatch(registry, {"upsert": [{"id": "a", "depends_on": ["b"]}]}, store=store)
    assert "cycle" in out["error"]


async def test_no_store_is_a_clean_error_not_a_crash(tmp_path: Path) -> None:
    # A root session with no chat bound (task_store is None) → a clean model-visible
    # error, never an AttributeError crash on store.load().
    out = await _registry(tmp_path).dispatch("manage_tasks", {}, task_store=None, spawn=_spawn)
    assert "task system is unavailable" in out["error"]


def test_view_renders_relative_time_not_raw() -> None:
    # The model-facing view formats updated_at as a RELATIVE string ("5m ago"),
    # never a raw timestamp; an unstamped task yields "".
    from datetime import UTC, datetime, timedelta

    from alkera_cli.plugins.plugin_base.task_tools import _view
    from alkera_core.schemas.chat.tasks import Task, TaskList

    now = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
    tl = TaskList(
        tasks=[
            Task(id="aged", title="Aged", updated_at=now - timedelta(minutes=5)),
            Task(id="unstamped", title="Unstamped", updated_at=None),
        ]
    )
    out = _view(tl, now=now)
    by_id = {t.id: t for t in out.tasks}
    assert by_id["aged"].updated_ago == "5m ago"
    assert by_id["unstamped"].updated_ago == ""


def test_view_lists_active_tasks_before_finished_ones() -> None:
    # The model-facing result shows upcoming work first; finished tasks sink to the
    # end (the cards/TUI inherit this order from the JSON they render).
    from datetime import UTC, datetime

    from alkera_cli.plugins.plugin_base.task_tools import _view
    from alkera_core.schemas.chat.tasks import Task, TaskList

    tl = TaskList(
        tasks=[
            Task(id="done", title="Done", status="completed"),
            Task(id="now", title="Now", status="in_progress"),
            Task(id="next", title="Next", status="pending"),
        ]
    )
    out = _view(tl, now=datetime(2026, 6, 21, tzinfo=UTC))
    assert [t.id for t in out.tasks] == ["now", "next", "done"]


def test_the_task_list_never_tells_the_agent_to_delegate(tmp_path: Path) -> None:
    """The delegation guidance lives in the system prompt, where the subagent
    switch removes it; the task tool's own description must not smuggle it
    back in, or a box with subagents off is still told to spawn a reviewer."""
    registry = _registry(tmp_path)
    (manage,) = [
        d
        for d in alkera_tool_descriptors(registry, allow_task_tools=True)
        if d.name == "manage_tasks"
    ]
    text = manage.description.lower()
    assert "spawn" not in text and "subagent" not in text and "review agent" not in text
    assert "review the change" in text
