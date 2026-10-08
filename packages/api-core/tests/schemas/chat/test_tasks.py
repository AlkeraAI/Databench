"""Pure-logic tests for the task DAG model (the unified TODO system).

Exercises the observable contract — blocked/ready derivation, DAG validation
(every rejection path), and the ``apply`` transform (upsert/delete/clear) — with
the negatives that catch over-eager code, not just the happy path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alkera_core.schemas.chat.tasks import Task, TaskList, TaskValidationError
from pydantic import ValidationError

NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 6, 21, 13, 0, 0, tzinfo=UTC)


def _list(*tasks: Task) -> TaskList:
    return TaskList(tasks=list(tasks))


def _t(id: str, *, status: str = "pending", depends_on: list[str] | None = None) -> Task:
    return Task(id=id, title=id.title(), status=status, depends_on=depends_on or [])


# --------------------------------------------------------------------------
# blocked_by / is_blocked / ready
# --------------------------------------------------------------------------


def test_blocked_by_when_prereq_incomplete() -> None:
    tl = _list(_t("a", status="in_progress"), _t("b", depends_on=["a"]))
    assert tl.blocked_by("b") == ["a"]
    assert tl.is_blocked("b") is True


def test_unblocked_when_prereq_completed() -> None:
    tl = _list(_t("a", status="completed"), _t("b", depends_on=["a"]))
    assert tl.blocked_by("b") == []
    assert tl.is_blocked("b") is False


def test_cancelled_prereq_still_blocks() -> None:
    # A cancelled prerequisite is NOT "cleared" — the dependent stays blocked so
    # the model must consciously drop the edge or cancel the dependent.
    tl = _list(_t("a", status="cancelled"), _t("b", depends_on=["a"]))
    assert tl.blocked_by("b") == ["a"]


def test_blocked_by_lists_only_uncleared_prereqs() -> None:
    tl = _list(
        _t("done", status="completed"),
        _t("wip", status="in_progress"),
        _t("b", depends_on=["done", "wip"]),
    )
    assert tl.blocked_by("b") == ["wip"]


def test_blocked_by_unknown_task_is_empty() -> None:
    assert _list(_t("a")).blocked_by("nope") == []


def test_ready_is_pending_and_unblocked_only() -> None:
    tl = _list(
        _t("a", status="completed"),
        _t("b", depends_on=["a"]),  # pending + prereq done → ready
        _t("c", status="in_progress"),  # not pending → not ready
        _t("d", status="pending"),  # pending, no deps → ready
        _t("e", depends_on=["d"]),  # pending but blocked (d not done) → not ready
    )
    assert sorted(t.id for t in tl.ready()) == ["b", "d"]


def test_status_counts() -> None:
    tl = _list(
        _t("a", status="completed"),
        _t("b", status="completed"),
        _t("c", status="in_progress"),
        _t("d"),
    )
    assert tl.status_counts() == {
        "pending": 1,
        "in_progress": 1,
        "completed": 2,
        "cancelled": 0,
    }


def test_render_lines_glyphs_and_blocked_suffix() -> None:
    # Authored explore→build→ship, but render order is active-first / done-last:
    # build (in_progress), ship (pending), then explore (completed) at the end.
    tl = _list(
        _t("explore", status="completed"),
        _t("build", status="in_progress", depends_on=["explore"]),
        _t("ship", depends_on=["build"]),
    )
    lines = tl.render_lines()
    assert lines[0].startswith("[~] build")
    assert "blocked by" not in lines[0]  # prereq completed → not blocked
    assert lines[1].startswith("[ ] ship")
    assert "(blocked by: build)" in lines[1]
    assert lines[2].startswith("[x] explore")  # finished task sinks to the end


def test_display_order_active_before_terminal_stable() -> None:
    # Active (pending/in_progress) first in insertion order, then terminal
    # (completed/cancelled) in insertion order — storage order is untouched.
    tl = _list(
        _t("a", status="completed"),
        _t("b", status="pending"),
        _t("c", status="cancelled"),
        _t("d", status="in_progress"),
        _t("e", status="pending"),
    )
    assert [t.id for t in tl.display_order()] == ["b", "d", "e", "a", "c"]
    assert [t.id for t in tl.tasks] == ["a", "b", "c", "d", "e"]  # storage unchanged


# --------------------------------------------------------------------------
# validate_dag — every rejection path
# --------------------------------------------------------------------------


def test_valid_dag_passes() -> None:
    _list(_t("a", status="completed"), _t("b", depends_on=["a"])).validate_dag()


def test_duplicate_id_rejected() -> None:
    with pytest.raises(TaskValidationError, match="duplicate"):
        _list(_t("a"), _t("a")).validate_dag()


@pytest.mark.parametrize(
    "bad_id",
    [
        pytest.param("", id="empty"),
        pytest.param("-leading-dash", id="leading-symbol"),
        pytest.param("has space", id="space"),
        pytest.param("a/b", id="slash"),
        pytest.param("a" * 65, id="too-long"),
    ],
)
def test_bad_slug_rejected(bad_id: str) -> None:
    with pytest.raises(TaskValidationError, match="invalid task id"):
        _list(Task(id=bad_id, title="x")).validate_dag()


def test_self_dependency_rejected() -> None:
    with pytest.raises(TaskValidationError, match="itself"):
        _list(_t("a", depends_on=["a"])).validate_dag()


def test_unknown_dependency_rejected() -> None:
    with pytest.raises(TaskValidationError, match="unknown task 'ghost'"):
        _list(_t("a", depends_on=["ghost"])).validate_dag()


def test_two_node_cycle_rejected() -> None:
    with pytest.raises(TaskValidationError, match="cycle"):
        _list(_t("a", depends_on=["b"]), _t("b", depends_on=["a"])).validate_dag()


def test_three_node_cycle_rejected() -> None:
    with pytest.raises(TaskValidationError, match="cycle"):
        _list(
            _t("a", depends_on=["c"]),
            _t("b", depends_on=["a"]),
            _t("c", depends_on=["b"]),
        ).validate_dag()


def test_diamond_dag_is_valid() -> None:
    # a → {b, c} → d is a DAG (shared ancestor/descendant, no cycle).
    _list(
        _t("a", status="completed"),
        _t("b", depends_on=["a"]),
        _t("c", depends_on=["a"]),
        _t("d", depends_on=["b", "c"]),
    ).validate_dag()


# --------------------------------------------------------------------------
# apply — upsert / delete / clear
# --------------------------------------------------------------------------


def test_apply_add_new_task_stamps_timestamps() -> None:
    out = TaskList().apply(upsert=[{"id": "a", "title": "Do A"}], now=NOW)
    (task,) = out.tasks
    assert task.id == "a" and task.title == "Do A" and task.status == "pending"
    assert task.created_at == NOW and task.updated_at == NOW


def test_apply_new_task_requires_title() -> None:
    with pytest.raises(TaskValidationError, match="needs a 'title'"):
        TaskList().apply(upsert=[{"id": "a"}], now=NOW)


def test_apply_new_task_bad_id_rejected() -> None:
    with pytest.raises(TaskValidationError, match="invalid task id"):
        TaskList().apply(upsert=[{"id": "bad id", "title": "x"}], now=NOW)


def test_apply_upsert_only_changes_given_fields() -> None:
    base = _list(Task(id="a", title="Orig", description="keep", created_at=NOW, updated_at=NOW))
    out = base.apply(upsert=[{"id": "a", "status": "completed"}], now=LATER)
    (task,) = out.tasks
    assert task.status == "completed"
    assert task.title == "Orig" and task.description == "keep"  # untouched
    assert task.created_at == NOW  # preserved
    assert task.updated_at == LATER  # bumped


def test_apply_change_dependencies() -> None:
    base = _list(_t("a", status="completed"), _t("b", status="completed"), _t("c"))
    out = base.apply(upsert=[{"id": "c", "depends_on": ["a", "b"]}], now=NOW)
    assert out.by_id()["c"].depends_on == ["a", "b"]


def test_apply_delete_removes_task() -> None:
    out = _list(_t("a"), _t("b")).apply(delete=["a"], now=NOW)
    assert [t.id for t in out.tasks] == ["b"]


def test_apply_delete_unknown_is_noop() -> None:
    out = _list(_t("a")).apply(delete=["ghost"], now=NOW)
    assert [t.id for t in out.tasks] == ["a"]


def test_apply_clear_then_add() -> None:
    base = _list(_t("a"), _t("b"))
    out = base.apply(clear=True, upsert=[{"id": "fresh", "title": "New"}], now=NOW)
    assert [t.id for t in out.tasks] == ["fresh"]


def test_apply_batch_dag_with_internal_deps() -> None:
    out = TaskList().apply(
        upsert=[
            {"id": "a", "title": "A"},
            {"id": "b", "title": "B", "depends_on": ["a"]},
            {"id": "c", "title": "C", "depends_on": ["b"]},
        ],
        now=NOW,
    )
    assert [t.id for t in out.tasks] == ["a", "b", "c"]
    assert out.blocked_by("c") == ["b"]


def test_apply_cycle_rejected_and_original_unchanged() -> None:
    base = _list(_t("a"), _t("b", depends_on=["a"]))
    before = base.model_dump()
    with pytest.raises(TaskValidationError, match="cycle"):
        base.apply(upsert=[{"id": "a", "depends_on": ["b"]}], now=NOW)
    assert base.model_dump() == before  # rejected mutation leaves the list intact


def test_apply_upsert_dep_on_deleted_task_rejected() -> None:
    base = _list(_t("a", status="completed"), _t("b"))
    with pytest.raises(TaskValidationError, match="unknown task 'a'"):
        base.apply(delete=["a"], upsert=[{"id": "b", "depends_on": ["a"]}], now=NOW)


def test_apply_preserves_order_new_appended() -> None:
    base = _list(_t("a"), _t("b"))
    out = base.apply(
        upsert=[{"id": "b", "status": "completed"}, {"id": "c", "title": "C"}], now=NOW
    )
    assert [t.id for t in out.tasks] == ["a", "b", "c"]


def test_apply_invalid_status_rejected() -> None:
    # status is a Literal — an out-of-enum value fails construction.
    with pytest.raises(ValidationError):
        TaskList().apply(upsert=[{"id": "a", "title": "A", "status": "done"}], now=NOW)


# --------------------------------------------------------------------------
# A terminal task is never "blocked" — every surface agrees (regression)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_terminal_task_is_never_blocked(status: str) -> None:
    # A done/abandoned task with an incomplete prerequisite must NOT report blocked
    # (the JSON view, the cards, and the reminder all read this) — they'd otherwise
    # disagree with render_lines, which already suppresses it.
    tl = _list(_t("a", status="in_progress"), _t("b", status=status, depends_on=["a"]))
    assert tl.blocked_by("b") == []
    assert tl.is_blocked("b") is False
    # render_lines shows no "(blocked by …)" suffix for the terminal task either.
    line = next(row for row in tl.render_lines() if row.split()[1] == "b")
    assert "blocked by" not in line


# --------------------------------------------------------------------------
# dependency hygiene: dedup, no aliasing, clear-to-empty
# --------------------------------------------------------------------------


def test_apply_dedups_dependencies() -> None:
    out = TaskList().apply(
        upsert=[
            {"id": "a", "title": "A"},
            {"id": "b", "title": "B", "depends_on": ["a", "a"]},
        ],
        now=NOW,
    )
    assert out.by_id()["b"].depends_on == ["a"]  # deduped
    assert out.blocked_by("b") == ["a"]  # no duplicate blocker leaks to the model


def test_apply_update_does_not_alias_caller_list() -> None:
    base = _list(_t("a", status="completed"), _t("b"))
    caller_deps = ["a"]
    out = base.apply(upsert=[{"id": "b", "depends_on": caller_deps}], now=NOW)
    caller_deps.append("a")  # mutate the caller's list AFTER apply
    assert out.by_id()["b"].depends_on == ["a"]  # the stored Task didn't alias it


def test_apply_clear_dependencies_via_empty_list() -> None:
    base = _list(
        _t("a", status="completed"), _t("b", status="completed"), _t("c", depends_on=["a", "b"])
    )
    out = base.apply(upsert=[{"id": "c", "depends_on": []}], now=NOW)
    assert out.by_id()["c"].depends_on == []  # [] clears, not "leave unchanged"
    assert out.blocked_by("c") == []
    assert [t.id for t in out.ready()] == ["c"]
