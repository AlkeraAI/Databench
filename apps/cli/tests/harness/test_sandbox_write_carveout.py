"""The chat-sandbox permission carve-out — what makes file-based plan mode work.

In plan / read_only mode the broker refuses project writes, but a mutation confined to
THIS chat's sandbox dir (``<chat>/sandbox/``) is auto-allowed in EVERY mode, so the model
can write its ``plan.md`` + scratch files there. ``_is_sandbox_write`` is the predicate
that decides this at the harness permission chokepoint (``ChatSession._decide_permission``);
the Claude adapter has the equivalent carve-out in ``can_use_tool``.

Two families qualify. A path-bearing ``fs`` edit/write qualifies when its path lands inside
the sandbox. A ``shell`` command qualifies only when EVERY destination the fence can read
out of it is inside the sandbox — a redirect is a write that carries no path argument, so
``echo plan >> sandbox/plan.md`` is the same scratch write as the edit-tool spelling and is
covered, while a command that writes elsewhere, writes nothing readable, or hides its
destinations behind a wrapper or a glob is left to the gate, which is the side that asks.

These tests pin the predicate's boundaries so the carve-out can't widen into the project
tree, into another chat's sandbox, or out of the workspace entirely.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alkera_cli.contracts.tool_types import ActionDescriptor, Effect, ResourceRef
from alkera_cli.harness.runtime import ChatSession


def _session(sandbox_dir: Path | None, alkera_dir: Path) -> Any:
    """A real ``ChatSession`` carrying only what ``_is_sandbox_write`` reads — the bound
    tool binding's sandbox dir + the project's ``.alkera`` path (its parent is the worktree
    root a relative path resolves against).

    Built with ``__new__`` rather than a duck-typed double so the predicate runs its own
    helpers (``_shell_writes_only_into`` / ``_within_sandbox``) instead of whatever a
    stand-in happens to expose.
    """
    session = ChatSession.__new__(ChatSession)
    session._tool_binding = SimpleNamespace(sandbox_dir=sandbox_dir)
    session._runtime = SimpleNamespace(project=SimpleNamespace(path=alkera_dir))
    session._path_fence = None  # a local session: no alias of the sandbox to map
    # Default admits every sandbox write; which folder holds the sandbox matters only
    # to the modes that refuse a shared one (see test_stance_contract).
    session._permission_mode = "default"
    session._chat = SimpleNamespace(path=alkera_dir / "chats" / "c")
    return session


def _fs(
    path: str | None, effect: Effect = Effect.WRITE, capability: str = "fs"
) -> ActionDescriptor:
    return ActionDescriptor(
        capability=capability,
        effect=effect,
        operation="edit",
        targets=[ResourceRef(kind="file", name=path)] if path else [],
        raw=path,
        classifier="test",
    )


def _shell(command: str, effect: Effect = Effect.WRITE) -> ActionDescriptor:
    return ActionDescriptor(
        capability="shell",
        effect=effect,
        operation="run",
        targets=[],
        raw=command,
        classifier="test",
    )


def test_absolute_write_under_sandbox_is_allowed(tmp_path: Path) -> None:
    sandbox = tmp_path / ".alkera" / "chats" / "c1" / "sandbox"
    sess = _session(sandbox, tmp_path / ".alkera")
    d = _fs(str(sandbox / "plan.md"))
    assert sess._is_sandbox_write(d) is True


def test_worktree_relative_write_under_sandbox_is_allowed(tmp_path: Path) -> None:
    # opencode reports the path RELATIVE to the worktree (project root). It must resolve
    # against the project root (.alkera's parent) and land inside the sandbox.
    sandbox = tmp_path / ".alkera" / "chats" / "c1" / "sandbox"
    sess = _session(sandbox, tmp_path / ".alkera")
    d = _fs(".alkera/chats/c1/sandbox/plan.md")
    assert sess._is_sandbox_write(d) is True


def test_path_falls_back_to_targets_when_raw_missing(tmp_path: Path) -> None:
    sandbox = tmp_path / ".alkera" / "chats" / "c1" / "sandbox"
    sess = _session(sandbox, tmp_path / ".alkera")
    d = ActionDescriptor(
        capability="fs",
        effect=Effect.WRITE,
        operation="edit",
        targets=[ResourceRef(kind="file", name=str(sandbox / "scratch.parquet"))],
        raw=None,
        classifier="test",
    )
    assert sess._is_sandbox_write(d) is True


def test_destroy_in_sandbox_is_allowed_scratch_cleanup(tmp_path: Path) -> None:
    sandbox = tmp_path / ".alkera" / "chats" / "c1" / "sandbox"
    sess = _session(sandbox, tmp_path / ".alkera")
    d = _fs(str(sandbox / "old.csv"), effect=Effect.DESTROY)
    assert sess._is_sandbox_write(d) is True


@pytest.mark.parametrize(
    ("path", "effect", "capability", "reason"),
    [
        pytest.param("plan.md", Effect.WRITE, "fs", "project-root file", id="project_root"),
        pytest.param(
            ".alkera/chats/c1/notes.md",
            Effect.WRITE,
            "fs",
            "under .alkera but not sandbox",
            id="alkera_not_sandbox",
        ),
        pytest.param(
            ".alkera/chats/c2/sandbox/p.md",
            Effect.WRITE,
            "fs",
            "ANOTHER chat's sandbox",
            id="other_chat_sandbox",
        ),
        pytest.param(
            "../escape.md",
            Effect.WRITE,
            "fs",
            "traversal out of the project",
            id="traversal",
        ),
        pytest.param(
            ".alkera/chats/c1/sandbox/../../c2/sandbox/p.md",
            Effect.WRITE,
            "fs",
            "traversal that starts inside the sandbox and leaves it",
            id="traversal_out_of_sandbox",
        ),
        pytest.param(
            ".alkera/chats/c1/sandbox/*.md",
            Effect.WRITE,
            "fs",
            "what a glob expands to is decided when the command runs, not now",
            id="wildcard_in_sandbox",
        ),
    ],
)
def test_writes_outside_this_sandbox_are_not_allowed(
    tmp_path: Path, path: str, effect: Effect, capability: str, reason: str
) -> None:
    sandbox = tmp_path / ".alkera" / "chats" / "c1" / "sandbox"
    sess = _session(sandbox, tmp_path / ".alkera")
    assert sess._is_sandbox_write(_fs(path, effect, capability)) is False, reason


def test_read_in_sandbox_is_not_a_write(tmp_path: Path) -> None:
    # Reads never need the carve-out (they auto-allow anyway); the predicate is for writes.
    sandbox = tmp_path / ".alkera" / "chats" / "c1" / "sandbox"
    sess = _session(sandbox, tmp_path / ".alkera")
    assert sess._is_sandbox_write(_fs(str(sandbox / "data.csv"), Effect.READ)) is False


def test_shell_redirect_into_sandbox_is_allowed(tmp_path: Path) -> None:
    # A redirect is the write that carries no path argument. Reading the destination is
    # what lets `echo plan >> sandbox/plan.md` be the same scratch write the edit tool
    # makes, instead of being refused while the edit-tool spelling is allowed.
    sandbox = tmp_path / ".alkera" / "chats" / "c1" / "sandbox"
    sess = _session(sandbox, tmp_path / ".alkera")
    assert sess._is_sandbox_write(_shell(f"echo plan >> {sandbox}/plan.md")) is True


def test_shell_redirect_to_worktree_relative_sandbox_path_is_allowed(tmp_path: Path) -> None:
    # The command spells its destination worktree-relative, the way the edit tool does.
    sandbox = tmp_path / ".alkera" / "chats" / "c1" / "sandbox"
    sess = _session(sandbox, tmp_path / ".alkera")
    assert sess._is_sandbox_write(_shell("echo plan > .alkera/chats/c1/sandbox/plan.md")) is True


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        pytest.param(
            "echo x > /etc/hosts",
            "a destination nowhere near this chat",
            id="absolute_outside",
        ),
        pytest.param(
            "echo x > .alkera/chats/c2/sandbox/p.md",
            "ANOTHER chat's sandbox",
            id="other_chat_sandbox",
        ),
        pytest.param(
            "echo x > ../escape.md",
            "traversal out of the project",
            id="traversal",
        ),
        pytest.param(
            "echo ok > {sandbox}/ok.md && echo bad > /tmp/bad.md",
            "EVERY destination must be inside — one outside refuses the whole command",
            id="one_segment_escapes",
        ),
        pytest.param(
            "cp /etc/hosts {sandbox}/../stolen",
            "a copy whose destination leaves the sandbox",
            id="copy_out_of_sandbox",
        ),
        pytest.param(
            "tee {sandbox}/a.md /tmp/b.md",
            "tee writes every operand, and one of them is outside",
            id="tee_second_operand_outside",
        ),
        pytest.param(
            "echo x >| /tmp/clobbered",
            "the noclobber override is the same write as >",
            id="noclobber_override",
        ),
        pytest.param(
            'bash -c "echo x > {sandbox}/plan.md"',
            "a wrapper carries a command this fence does not run; invisible is not inside",
            id="nested_shell_wrapper",
        ),
        pytest.param(
            "echo x > {sandbox}/*.md",
            "what a glob expands to is decided when the command runs",
            id="wildcard_destination",
        ),
        pytest.param(
            "ls {sandbox}",
            "a command whose writes this reader cannot see is left to the gate",
            id="writes_nothing_readable",
        ),
    ],
)
def test_shell_commands_that_do_not_write_only_into_the_sandbox(
    tmp_path: Path, command: str, reason: str
) -> None:
    sandbox = tmp_path / ".alkera" / "chats" / "c1" / "sandbox"
    sess = _session(sandbox, tmp_path / ".alkera")
    assert sess._is_sandbox_write(_shell(command.format(sandbox=sandbox))) is False, reason


def test_no_sandbox_dir_or_descriptor_is_not_a_write(tmp_path: Path) -> None:
    # A read-only session never makes a sandbox dir → nothing to carve out.
    sess_none = _session(None, tmp_path / ".alkera")
    assert sess_none._is_sandbox_write(_fs("anything")) is False
    sandbox = tmp_path / ".alkera" / "chats" / "c1" / "sandbox"
    sess = _session(sandbox, tmp_path / ".alkera")
    assert sess._is_sandbox_write(None) is False


def test_plan_mode_description_names_the_sandbox_exception() -> None:
    # The switch-into-plan notice is built from this; it must not read as "all writes
    # refused" or the model won't try to write its plan.md.
    from alkera_cli.harness.permission_mode import _MODE_DESCRIPTION

    desc = _MODE_DESCRIPTION["plan"].lower()
    assert "sandbox" in desc and "plan.md" in desc
