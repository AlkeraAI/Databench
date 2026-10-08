"""Re-running a file tool call from the input the transcript recorded.

The workspace performs an approved write or edit itself when the harness
that raised the ask is gone. What is pinned: both harnesses' input spellings
land the same bytes, a relative path lands in the chat's folder, an anchor
that is not in the file writes nothing, a destination outside the folder is
refused before anything is written, and a tool whose effect is not a function
of its input is not replayed at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alkera_cli.cloud.replay import (
    ANCHOR_MISSING,
    NO_CONTENT,
    OUTSIDE_FOLDER,
    ReplayResult,
    apply_edits,
    replay_file_tool,
    replayable,
)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    chat = tmp_path / "ws" / ".alkera" / "chats" / "chat-1"
    chat.mkdir(parents=True)
    return chat


@pytest.mark.parametrize(
    ("tool", "spelling"),
    [
        pytest.param("write", "filePath", id="opencode-write"),
        pytest.param("Write", "file_path", id="claude-Write"),
    ],
)
def test_a_write_lands_the_content_at_the_named_path(
    folder: Path, tool: str, spelling: str
) -> None:
    target = folder / "sleepy.txt"
    result = replay_file_tool(tool, {spelling: str(target), "content": "resumed\n"}, folder=folder)
    assert result == ReplayResult(tool="write", path=str(target), ok=True, bytes_written=8)
    assert target.read_text(encoding="utf-8") == "resumed\n"
    assert result.output["replayed"] is True and result.output["bytes"] == 8
    assert "written (8 bytes)" in result.summary


def test_a_relative_path_lands_inside_the_chat_folder(folder: Path) -> None:
    result = replay_file_tool("write", {"filePath": "notes/a.md", "content": "x"}, folder=folder)
    assert result is not None and result.ok
    assert (folder / "notes" / "a.md").read_text(encoding="utf-8") == "x"


@pytest.mark.parametrize(
    ("tool", "tool_input", "expected"),
    [
        pytest.param(
            "edit",
            {"oldString": "b", "newString": "B"},
            "a B b c",
            id="opencode-edit-first-only",
        ),
        pytest.param(
            "Edit",
            {"old_string": "b", "new_string": "B"},
            "a B b c",
            id="claude-Edit-first-only",
        ),
        pytest.param(
            "edit",
            {"oldString": "b", "newString": "B", "replaceAll": True},
            "a B B c",
            id="opencode-replaceAll",
        ),
        pytest.param(
            "Edit",
            {"old_string": "b", "new_string": "B", "replace_all": True},
            "a B B c",
            id="claude-replace_all",
        ),
        pytest.param(
            "Edit",
            {"old_string": "b", "new_string": "B", "replace_all": "yes"},
            "a B b c",
            id="replace_all-must-be-true-not-truthy",
        ),
        pytest.param(
            "multiedit",
            {"edits": [{"oldString": "a", "newString": "A"}, {"oldString": "c", "newString": "C"}]},
            "A b b C",
            id="multiedit-in-order",
        ),
        pytest.param(
            "MultiEdit",
            {
                "edits": [
                    {"old_string": "a b", "new_string": "ab"},
                    {"old_string": "ab", "new_string": "AB"},
                ]
            },
            "AB b c",
            id="claude-MultiEdit-second-edit-sees-the-first",
        ),
    ],
)
def test_an_edit_rewrites_the_file_in_either_spelling(
    folder: Path, tool: str, tool_input: dict[str, Any], expected: str
) -> None:
    target = folder / "f.txt"
    target.write_text("a b b c")
    result = replay_file_tool(tool, {"filePath": str(target), **tool_input}, folder=folder)
    assert result is not None and result.ok, result
    assert target.read_text(encoding="utf-8") == expected


def test_an_empty_anchor_creates_the_file_from_the_replacement(folder: Path) -> None:
    target = folder / "new.txt"
    result = replay_file_tool(
        "edit", {"filePath": str(target), "oldString": "", "newString": "made"}, folder=folder
    )
    assert result is not None and result.ok
    assert target.read_text(encoding="utf-8") == "made"


def test_an_anchor_not_in_the_file_writes_nothing(folder: Path) -> None:
    target = folder / "f.txt"
    target.write_bytes(b"a b c")
    result = replay_file_tool(
        "edit", {"filePath": str(target), "oldString": "zzz", "newString": "Z"}, folder=folder
    )
    assert result == ReplayResult(tool="edit", path=str(target), ok=False, error=ANCHOR_MISSING)
    assert target.read_bytes() == b"a b c", "a failed edit leaves the bytes untouched"
    assert result.output["error"] == ANCHOR_MISSING and "bytes" not in result.output


def test_a_multiedit_whose_later_anchor_is_missing_writes_nothing(folder: Path) -> None:
    target = folder / "f.txt"
    target.write_bytes(b"a b c")
    result = replay_file_tool(
        "multiedit",
        {
            "filePath": str(target),
            "edits": [{"oldString": "a", "newString": "A"}, {"oldString": "q", "newString": "Q"}],
        },
        folder=folder,
    )
    assert result is not None and not result.ok
    assert target.read_bytes() == b"a b c", "all or nothing"


def test_a_write_without_content_writes_nothing(folder: Path) -> None:
    target = folder / "f.txt"
    result = replay_file_tool("write", {"filePath": str(target), "content": 7}, folder=folder)
    assert result == ReplayResult(tool="write", path=str(target), ok=False, error=NO_CONTENT)
    assert not target.exists()


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("{elsewhere}/x.txt", id="absolute-outside"),
        pytest.param("../../escape.txt", id="relative-escape"),
        pytest.param("*.txt", id="wildcard"),
    ],
)
def test_a_destination_outside_the_chat_folder_is_refused_before_writing(
    folder: Path, tmp_path: Path, raw: str
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path = raw.format(elsewhere=elsewhere)
    before = sorted(p for p in tmp_path.rglob("*") if p.is_file())
    result = replay_file_tool("write", {"filePath": path, "content": "leak"}, folder=folder)
    assert result == ReplayResult(tool="write", path=path, ok=False, error=OUTSIDE_FOLDER)
    assert sorted(p for p in tmp_path.rglob("*") if p.is_file()) == before


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        pytest.param("bash", {"command": "echo hi > out.txt"}, id="shell"),
        pytest.param("read", {"filePath": "f.txt"}, id="read"),
        pytest.param("webfetch", {"url": "https://example.com"}, id="network"),
        pytest.param("task", {"prompt": "do it"}, id="subagent"),
        pytest.param("write", {"content": "no path"}, id="write-without-a-path"),
    ],
)
def test_a_call_the_input_does_not_determine_is_not_replayed(
    folder: Path, tool: str, tool_input: dict[str, Any]
) -> None:
    assert replay_file_tool(tool, tool_input, folder=folder) is None
    assert not list(folder.iterdir())


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        pytest.param("write", True, id="write"),
        pytest.param("Write", True, id="Write"),
        pytest.param(" edit ", True, id="edit-padded"),
        pytest.param("MultiEdit", True, id="MultiEdit"),
        pytest.param("bash", False, id="bash"),
        pytest.param("patch", False, id="patch"),
        pytest.param("", False, id="empty"),
    ],
)
def test_replayable_names_exactly_the_file_tools(tool: str, expected: bool) -> None:
    assert replayable(tool) is expected


def test_apply_edits_refuses_a_malformed_edit() -> None:
    assert apply_edits("abc", {"oldString": "a"}) is None
    assert apply_edits("abc", {"edits": [{"old_string": "a", "new_string": 3}]}) is None
    assert apply_edits("abc", {"edits": ["not a mapping"]}) == "abc"
