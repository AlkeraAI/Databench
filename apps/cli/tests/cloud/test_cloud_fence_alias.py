"""The fence accepts the sandbox's alias of the working directory — the
``/home/alkera`` a container mounts the folder at — and only that: a name under
the alias is judged as the host path it is bound to, a ``..`` that climbs out
of the alias climbs out of the host directory, a name that merely starts with
the alias's letters is a foreign path, and a fence with no alias (a box that
never mounted one) judges a real ``/home/alkera`` as the foreign path it is."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.cloud.fence import SessionFence
from alkera_core.schemas.chat import PermissionOption, PermissionRequest

HOME = "/home/alkera"
_T = datetime(2026, 9, 27, tzinfo=UTC)


class _Box:
    def __init__(self, root: Path, *, aliased: bool) -> None:
        self.root = root / "work"
        self.home = root / "alkera-home"
        self.home.mkdir(parents=True)
        (self.home / "auth.yml").write_text("token: secret\n")
        self.folder = self.root / ".alkera" / "chats" / "chat-a"
        self.working = self.folder / "scratch"
        self.working.mkdir(parents=True)
        (self.folder / "manifest.json").write_text("{}")
        (self.working / "notes.md").write_text("hello\n")
        (self.working / "sub").mkdir()
        other = self.root / ".alkera" / "chats" / "chat-b" / "scratch"
        other.mkdir(parents=True)
        (other / "secret.csv").write_text("a,b\n")
        self.fence = SessionFence(
            root=self.root,
            folder=self.folder,
            working_dir=self.working,
            home=self.home,
            system_roots=("/usr", "/bin"),
            aliases=((HOME, self.working),) if aliased else (),
        )
        self.env = {"HOME": HOME if aliased else str(self.working), "PATH": "/usr/bin"}


@pytest.fixture
def box(tmp_path: Path) -> _Box:
    return _Box(tmp_path, aliased=True)


@pytest.fixture
def plain(tmp_path: Path) -> _Box:
    return _Box(tmp_path, aliased=False)


def _ask(kind: str, raw: str, *, patterns: list[str] | None = None) -> PermissionRequest:
    writing = kind in ("edit", "write")
    return PermissionRequest(
        event_id="ev-1",
        time=_T,
        session_id="chat-a",
        request_id="req-1",
        tool_call_id="call-1",
        permission_kind=kind,
        canonical_kind="other",
        patterns=patterns or [],
        subject={
            "capability": "fs",
            "effect": "write" if writing else "read",
            "operation": kind,
            "raw": raw,
            "targets": [{"kind": "file", "name": raw}],
            "classifier": "opencode-tool",
        },
        options=[
            PermissionOption(option_id="allow_once", name="Allow"),
            PermissionOption(option_id="reject_once", name="Reject"),
        ],
    )


# --------------------------------------------------------------------------- #
# canonical(): the lexical mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(f"{HOME}/notes.md", "{wd}/notes.md", id="under-the-alias"),
        pytest.param(HOME, "{wd}", id="the-alias-itself"),
        pytest.param(f"{HOME}/sub/../notes.md", "{wd}/sub/../notes.md", id="dotdot-inside-kept"),
        pytest.param(f"{HOME}/../etc/passwd", f"{HOME}/../etc/passwd", id="dotdot-out-not-mapped"),
        pytest.param(f"{HOME}/a/../../x", f"{HOME}/a/../../x", id="dotdot-out-after-a-descent"),
        pytest.param(f"{HOME}/./sub", "{wd}/./sub", id="dot-is-not-a-step"),
        pytest.param(f"{HOME}X/notes.md", f"{HOME}X/notes.md", id="a-longer-name-is-not-it"),
        pytest.param("/home/alkeraX", "/home/alkeraX", id="a-longer-name-bare"),
        pytest.param("notes.md", "notes.md", id="relative-untouched"),
        pytest.param("/etc/passwd", "/etc/passwd", id="foreign-untouched"),
    ],
)
def test_canonical_maps_the_alias_and_only_the_alias(box: _Box, text: str, expected: str) -> None:
    assert box.fence.canonical(text) == expected.format(wd=box.working)


def test_a_fence_without_an_alias_maps_nothing(plain: _Box) -> None:
    assert plain.fence.canonical(f"{HOME}/notes.md") == f"{HOME}/notes.md"


# --------------------------------------------------------------------------- #
# File-tool asks
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("writing", [False, True], ids=["read", "write"])
def test_a_file_under_the_alias_is_inside(box: _Box, writing: bool) -> None:
    ask = _ask("edit" if writing else "read", f"{HOME}/notes.md")
    verdict = box.fence.judge_ask(ask, writing=writing)
    assert not verdict.escaped and not verdict.unknown


def test_a_new_file_under_the_alias_is_a_write_inside(box: _Box) -> None:
    verdict = box.fence.judge_ask(_ask("write", f"{HOME}/sub/report.html"), writing=True)
    assert not verdict.escaped


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(f"{HOME}/../etc/passwd", id="dotdot-out-of-the-alias"),
        pytest.param(f"{HOME}/../manifest.json", id="dotdot-onto-the-chats-records"),
        pytest.param(f"{HOME}X/notes.md", id="a-longer-name"),
        pytest.param(f"{HOME}/../../chat-b/scratch/secret.csv", id="a-sibling-chat"),
    ],
)
@pytest.mark.parametrize("writing", [False, True], ids=["read", "write"])
def test_what_climbs_out_of_the_alias_is_refused(box: _Box, raw: str, writing: bool) -> None:
    verdict = box.fence.judge_ask(_ask("edit" if writing else "read", raw), writing=writing)
    assert verdict.escaped, raw
    # The refusal quotes the path as the model spelled it, not the host path.
    assert verdict.target == raw


def test_without_the_alias_the_same_spelling_is_the_foreign_path_it_is(plain: _Box) -> None:
    """The alias is accepted only where the launch mounts it: a box that never
    did has a real ``/home/alkera`` (or none), and neither is the chat's."""
    verdict = plain.fence.judge_ask(_ask("read", f"{HOME}/notes.md"), writing=False)
    assert verdict.escaped


def test_a_glob_under_the_alias_is_judged_by_its_mapped_prefix(box: _Box) -> None:
    inside = _ask("glob", f"{HOME}/sub/*.py", patterns=[f"{HOME}/sub/*.py"])
    assert not box.fence.judge_ask(inside, writing=False).escaped
    outside = _ask("glob", f"{HOME}/../*.json", patterns=[f"{HOME}/../*.json"])
    assert box.fence.judge_ask(outside, writing=False).escaped


def test_the_asks_own_shape_is_left_alone(box: _Box) -> None:
    """Mapping happens on a copy: the ask the harness keeps still spells the
    alias, so the reply and the audit name what the model named."""
    ask = _ask("read", f"{HOME}/notes.md")
    box.fence.judge_ask(ask, writing=False)
    subject: dict[str, Any] = dict(ask.subject or {})
    assert subject["raw"] == f"{HOME}/notes.md"


# --------------------------------------------------------------------------- #
# Shell commands
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"cat {HOME}/notes.md", id="read-under-the-alias"),
        pytest.param("cat ~/notes.md", id="tilde-expands-to-the-alias"),
        pytest.param(f"cd {HOME}/sub && ls", id="cd-into-the-alias"),
        pytest.param(f"echo hi > {HOME}/out.txt", id="write-under-the-alias"),
        pytest.param(f"cp notes.md {HOME}/copy.md", id="copy-into-the-alias"),
    ],
)
def test_a_shell_command_under_the_alias_is_inside(box: _Box, command: str) -> None:
    verdict = box.fence.judge_shell(command, cwd=box.working, env=box.env)
    assert not verdict.escaped and not verdict.unknown, command


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"cat {HOME}/../manifest.json", id="read-the-chats-records"),
        pytest.param(f"echo x > {HOME}/../manifest.json", id="write-the-chats-records"),
        pytest.param(f"cat {HOME}/../../chat-b/scratch/secret.csv", id="read-a-sibling-chat"),
        pytest.param(f"cd {HOME}/.. && cat manifest.json", id="cd-out-of-the-alias"),
        pytest.param(f"cat {HOME}X/notes.md", id="a-longer-name"),
    ],
)
def test_a_shell_command_that_climbs_out_of_the_alias_is_refused(box: _Box, command: str) -> None:
    verdict = box.fence.judge_shell(command, cwd=box.working, env=box.env)
    assert verdict.escaped, command


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the alias is a Linux bind mount; on Windows Path('/home/alkera') is a drive-less path "
    "the fence rightly reads as outside the working directory",
)
def test_a_shell_cwd_spelled_at_the_alias_is_the_working_directory(box: _Box) -> None:
    verdict = box.fence.judge_shell("ls", cwd=Path(f"{HOME}/sub"), env=box.env)
    assert not verdict.escaped
    assert box.fence.judge_shell("ls", cwd=Path(f"{HOME}/.."), env=box.env).escaped


def test_without_the_alias_a_shell_read_of_it_is_refused(plain: _Box) -> None:
    verdict = plain.fence.judge_shell(f"cat {HOME}/notes.md", cwd=plain.working, env=plain.env)
    assert verdict.escaped
