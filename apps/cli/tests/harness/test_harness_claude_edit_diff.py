"""Claude adapter: it computes a write/edit diff from the tool input.

The Claude Agent SDK hands the adapter only the tool input — no diff, no
post-write signal. So the adapter builds the diff itself in `_can_use_tool`
(reading the file's current bytes), ships it on the `PermissionRequest` so the
UI shows the change BEFORE the user approves, and drains it onto a synthesized
`FileEdited` once the write completes. These tests pin the counts, the create
marker, and the request/file-edited emission against hand-computed values —
opencode gets this from its harness for free, so the parity lives here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters._file_diff import (
    count_diff_lines,
    diff_preview,
    unified_diff_for,
)
from alkera_cli.harness.adapters.claude_agent import (
    ClaudeAgentAdapter,
    _apply_edit_to_text,
    _edit_diff_from_input,
)
from alkera_cli.harness.claude_binary import ResolvedClaudeBinary
from alkera_cli.harness.event_bus import EventBus
from alkera_core.schemas.chat import FileEdited, PermissionRequest, ToolCallUpdate


class _Ctx:
    def __init__(self, tool_use_id: str | None = None) -> None:
        self.tool_use_id = tool_use_id
        self.suggestions: list[Any] = []
        self.signal = None


def _adapter(tmp_path: Path) -> ClaudeAgentAdapter:
    config = SessionConfig(session_id="our-sid", project_dir=tmp_path, chat_dir=tmp_path / "chat")
    binary = ResolvedClaudeBinary(path=Path("/usr/bin/true"), source="path")
    return ClaudeAgentAdapter(config, binary=binary, event_bus=EventBus())


# ---------------------------------------------------------------------------
# Pure diff shaping (_file_diff)
# ---------------------------------------------------------------------------


def test_unified_diff_for_create_is_all_additions_from_zero() -> None:
    """A create (old empty) yields a hunk header anchored at original line zero
    (`@@ -0,0`) with every body line a `+` — the create signal a renderer reads
    to say "Created" not "Edited"."""
    diff = unified_diff_for("", "line a\nline b\n", "scratch/new.txt")
    assert "@@ -0,0 +1,2 @@" in diff
    assert count_diff_lines(diff) == (2, 0)


def test_unified_diff_for_edit_mixes_add_and_remove() -> None:
    diff = unified_diff_for("x\n", "y\nz\n", "scratch/f.txt")
    assert "@@ -0,0" not in diff  # NOT a create
    assert count_diff_lines(diff) == (2, 1)


def test_diff_preview_titles_with_basename() -> None:
    ins, dels, preview = diff_preview("models/marts/x.sql", unified_diff_for("", "a\n", "x"))
    assert (ins, dels) == (1, 0)
    assert preview["kind"] == "diff" and preview["title"] == "x.sql"


def test_count_diff_lines_excludes_file_headers() -> None:
    # The `+++`/`---` header lines are NOT counted as additions/deletions.
    diff = "--- a\n+++ a\n@@ -1 +1,2 @@\n-old\n+new1\n+new2\n"
    assert count_diff_lines(diff) == (2, 1)


# ---------------------------------------------------------------------------
# _apply_edit_to_text — proposed new text from the tool input
# ---------------------------------------------------------------------------


def test_apply_edit_write_replaces_whole_file() -> None:
    assert _apply_edit_to_text("old body\n", {"content": "new body\n"}) == "new body\n"


def test_apply_edit_single_replacement() -> None:
    out = _apply_edit_to_text("a x a\n", {"old_string": "x", "new_string": "Y"})
    assert out == "a Y a\n"  # only the first by default


def test_apply_edit_replace_all() -> None:
    out = _apply_edit_to_text(
        "a x a x\n", {"old_string": "x", "new_string": "Y", "replace_all": True}
    )
    assert out == "a Y a Y\n"


def test_apply_edit_missing_old_string_returns_none() -> None:
    # An edit whose target isn't in the file can't be applied — no preview.
    assert _apply_edit_to_text("nothing here\n", {"old_string": "zzz", "new_string": "y"}) is None


def test_apply_edit_multiedit_applies_in_order() -> None:
    out = _apply_edit_to_text(
        "1 2 3\n",
        {
            "edits": [
                {"old_string": "1", "new_string": "one"},
                {"old_string": "3", "new_string": "three"},
            ]
        },
    )
    assert out == "one 2 three\n"


def test_apply_edit_default_replaces_first_occurrence_only() -> None:
    # The default (no replace_all) swaps ONLY the first of several occurrences —
    # the SDK's documented Edit semantics. A naive global replace is the bug.
    out = _apply_edit_to_text("x x x\n", {"old_string": "x", "new_string": "YY"})
    assert out == "YY x x\n"


def test_apply_edit_multiedit_later_edit_sees_earlier_result() -> None:
    # MultiEdit applies edits sequentially against the ACCUMULATING text, so a
    # later edit may target a string only present AFTER an earlier edit ran.
    # `foo`→`bar`, then `bar`→`baz` must land `baz` (not stay `foo` / `bar`).
    out = _apply_edit_to_text(
        "foo\n",
        {
            "edits": [
                {"old_string": "foo", "new_string": "bar"},
                {"old_string": "bar", "new_string": "baz"},
            ]
        },
    )
    assert out == "baz\n"


def test_apply_edit_multiedit_later_failure_aborts_whole_preview() -> None:
    # If a LATER edit's old_string is absent (after earlier edits applied), the
    # whole edit can't be applied as a unit — return None so no half-applied
    # preview is shown. The first edit succeeding must not leak a partial result.
    out = _apply_edit_to_text(
        "alpha\n",
        {
            "edits": [
                {"old_string": "alpha", "new_string": "beta"},
                {"old_string": "ZZZ", "new_string": "x"},
            ]
        },
    )
    assert out is None


def test_apply_edit_non_string_replacement_returns_none() -> None:
    # A non-string new_string can't be applied — None, never a TypeError.
    assert _apply_edit_to_text("a\n", {"old_string": "a", "new_string": 5}) is None


# ---------------------------------------------------------------------------
# _edit_diff_from_input — reads the file, builds the diff
# ---------------------------------------------------------------------------


def test_edit_diff_from_input_write_to_missing_file_is_create(tmp_path: Path) -> None:
    path = tmp_path / "new.sql"  # does NOT exist
    captured = _edit_diff_from_input({"file_path": str(path), "content": "a\nb\n"})
    assert captured is not None
    insertions, deletions, preview = captured
    assert (insertions, deletions) == (2, 0)
    assert "@@ -0,0" in preview["content"]  # create marker


def test_edit_diff_from_input_edit_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "f.sql"
    path.write_text("keep\ndrop me\nkeep2\n", encoding="utf-8")
    captured = _edit_diff_from_input(
        {"file_path": str(path), "old_string": "drop me", "new_string": "added one\nadded two"}
    )
    assert captured is not None
    insertions, deletions, preview = captured
    assert (insertions, deletions) == (2, 1)
    assert "@@ -0,0" not in preview["content"]  # an edit, not a create


def test_edit_diff_from_input_no_change_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "f.sql"
    path.write_text("same\n", encoding="utf-8")
    # Writing identical content is a no-op — nothing to preview.
    assert _edit_diff_from_input({"file_path": str(path), "content": "same\n"}) is None


def test_edit_diff_from_input_no_path_returns_none() -> None:
    assert _edit_diff_from_input({"content": "x"}) is None


def test_edit_diff_counts_first_occurrence_only_by_default(tmp_path: Path) -> None:
    # Three identical lines; the default Edit changes only the FIRST — so the
    # diff is one line removed + one added, NOT three. The count must track the
    # real edit, not the number of occurrences.
    path = tmp_path / "f.txt"
    path.write_text("foo\nfoo\nfoo\n", encoding="utf-8")
    captured = _edit_diff_from_input(
        {"file_path": str(path), "old_string": "foo", "new_string": "bar"}
    )
    assert captured is not None
    assert (captured[0], captured[1]) == (1, 1)


def test_edit_diff_counts_all_occurrences_with_replace_all(tmp_path: Path) -> None:
    # replace_all swaps all three lines → three removed + three added.
    path = tmp_path / "f.txt"
    path.write_text("foo\nfoo\nfoo\n", encoding="utf-8")
    captured = _edit_diff_from_input(
        {"file_path": str(path), "old_string": "foo", "new_string": "bar", "replace_all": True}
    )
    assert captured is not None
    assert (captured[0], captured[1]) == (3, 3)


def test_edit_diff_multiedit_later_failure_yields_no_preview(tmp_path: Path) -> None:
    # The second edit's old_string is absent → the whole edit is unappliable →
    # None, so no diff is shipped and (downstream) no FileEdited can drain.
    path = tmp_path / "g.txt"
    path.write_text("alpha\n", encoding="utf-8")
    captured = _edit_diff_from_input(
        {
            "file_path": str(path),
            "edits": [
                {"old_string": "alpha", "new_string": "beta"},
                {"old_string": "ZZZ", "new_string": "x"},
            ],
        }
    )
    assert captured is None


def test_edit_diff_unreadable_file_is_treated_as_create(tmp_path: Path) -> None:
    # A file present on disk but not decodable as UTF-8 reads as "" — so a Write
    # over it builds an all-additions CREATE diff (old empty), never raises.
    path = tmp_path / "bin.dat"
    path.write_bytes(b"\xff\xfe\x00not utf8")
    captured = _edit_diff_from_input({"file_path": str(path), "content": "hello\n"})
    assert captured is not None
    insertions, deletions, preview = captured
    assert (insertions, deletions) == (1, 0)
    assert "@@ -0,0" in preview["content"]  # the create marker — old text was empty


def test_edit_diff_edit_to_identical_text_returns_none(tmp_path: Path) -> None:
    # An Edit that replaces a string with itself produces new == old → no diff.
    path = tmp_path / "f.txt"
    path.write_text("a b\n", encoding="utf-8")
    noop = _edit_diff_from_input({"file_path": str(path), "old_string": "a", "new_string": "a"})
    assert noop is None


# ---------------------------------------------------------------------------
# _can_use_tool ships the diff on the PermissionRequest
# ---------------------------------------------------------------------------


async def _gate(
    adapter: ClaudeAgentAdapter, tool: str, tool_input: dict[str, Any], tool_use_id: str
) -> PermissionRequest:
    sub = adapter._bus.subscribe()
    task = asyncio.create_task(adapter._can_use_tool(tool, tool_input, _Ctx(tool_use_id)))
    req: PermissionRequest | None = None
    async for ev in sub:
        if isinstance(ev, PermissionRequest):
            req = ev
            break
    assert req is not None
    await adapter.resolve_permission(req.request_id, "allow_once")
    await asyncio.wait_for(task, timeout=1.0)
    return req


async def test_can_use_tool_write_ships_create_diff_on_request(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    path = tmp_path / "new.sql"  # missing → create
    req = await _gate(adapter, "Write", {"file_path": str(path), "content": "x\ny\n"}, "tu1")
    assert req.insertions == 2 and req.deletions == 0
    assert req.preview is not None and "@@ -0,0" in req.preview["content"]


async def test_can_use_tool_edit_ships_diff_on_request(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    path = tmp_path / "f.sql"
    path.write_text("old\n", encoding="utf-8")
    req = await _gate(
        adapter, "Edit", {"file_path": str(path), "old_string": "old", "new_string": "new"}, "tu2"
    )
    assert req.insertions == 1 and req.deletions == 1
    assert req.preview is not None and "@@ -0,0" not in req.preview["content"]


async def test_can_use_tool_bash_has_no_diff(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    req = await _gate(adapter, "Bash", {"command": "ls"}, "tu3")
    assert req.insertions is None and req.deletions is None and req.preview is None


async def test_can_use_tool_write_without_tool_use_id_ships_no_diff(tmp_path: Path) -> None:
    # The diff + stash are keyed on the SDK's tool_use_id. When the SDK gives
    # none (a str is required), the request carries no diff and nothing is
    # stashed — so a later result has nothing to drain. Pins the isinstance guard.
    adapter = _adapter(tmp_path)
    path = tmp_path / "new.sql"  # missing → would otherwise be a create diff
    sub = adapter._bus.subscribe()
    task = asyncio.create_task(
        adapter._can_use_tool("Write", {"file_path": str(path), "content": "a\nb\n"}, _Ctx(None))
    )
    req: PermissionRequest | None = None
    async for ev in sub:
        if isinstance(ev, PermissionRequest):
            req = ev
            break
    assert req is not None
    assert req.insertions is None and req.deletions is None and req.preview is None
    assert req.tool_call_id is None
    assert adapter._pending_edit_diffs == {}  # nothing stashed without a key
    await adapter.resolve_permission(req.request_id, "allow_once")
    await asyncio.wait_for(task, timeout=1.0)


async def test_can_use_tool_multiedit_later_failure_stashes_nothing(tmp_path: Path) -> None:
    # When the edit is unappliable (a later MultiEdit step's old_string absent),
    # no diff is captured → no stash → a completed result emits NO FileEdited.
    adapter = _adapter(tmp_path)
    path = tmp_path / "f.txt"
    path.write_text("alpha\n", encoding="utf-8")
    req = await _gate(
        adapter,
        "MultiEdit",
        {
            "file_path": str(path),
            "edits": [
                {"old_string": "alpha", "new_string": "beta"},
                {"old_string": "ZZZ", "new_string": "x"},
            ],
        },
        "tu_me",
    )
    assert req.insertions is None and req.deletions is None and req.preview is None
    assert "tu_me" not in adapter._pending_edit_diffs
    # A completed result drains nothing — no FileEdited queued.
    sub = adapter._bus.subscribe()
    await adapter._maybe_emit_file_edited(
        ToolCallUpdate(
            event_id="u",
            time=adapter._now(),
            session_id="our-sid",
            tool_call_id="tu_me",
            status="completed",
            output="ok",
        )
    )
    with pytest.raises(asyncio.TimeoutError):

        async def _pull() -> FileEdited:
            async for ev in sub:
                if isinstance(ev, FileEdited):
                    return ev  # pragma: no cover
            raise AssertionError("stream closed")  # pragma: no cover

        await asyncio.wait_for(_pull(), timeout=0.2)


# ---------------------------------------------------------------------------
# FileEdited drains the stash on the completed result (translator stays pure)
# ---------------------------------------------------------------------------


async def test_completed_result_emits_file_edited_with_stashed_diff(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    path = tmp_path / "new.sql"
    await _gate(adapter, "Write", {"file_path": str(path), "content": "a\nb\n"}, "tu4")
    # The ask stashed the diff. The completed tool result drains it to a FileEdited.
    sub = adapter._bus.subscribe()
    update = ToolCallUpdate(
        event_id="u",
        time=adapter._now(),
        session_id="our-sid",
        tool_call_id="tu4",
        status="completed",
        output="File created successfully.",
    )
    await adapter._maybe_emit_file_edited(update)
    fe: FileEdited | None = None
    async for ev in sub:
        if isinstance(ev, FileEdited):
            fe = ev
            break
    assert fe is not None
    assert fe.path == str(path)
    assert fe.insertions == 2 and fe.deletions == 0
    assert fe.preview is not None and "@@ -0,0" in fe.preview["content"]
    # Idempotent: the stash was popped, so a second completed update emits nothing.
    assert "tu4" not in adapter._pending_edit_diffs


async def test_running_update_does_not_drain_stash(tmp_path: Path) -> None:
    # The interim `running` block-stop update carries the same tool_call_id and
    # must NOT consume the stash before the real completion lands.
    adapter = _adapter(tmp_path)
    path = tmp_path / "new.sql"
    await _gate(adapter, "Write", {"file_path": str(path), "content": "a\n"}, "tu5")
    running = ToolCallUpdate(
        event_id="r",
        time=adapter._now(),
        session_id="our-sid",
        tool_call_id="tu5",
        status="running",
        input={"file_path": str(path)},
    )
    await adapter._maybe_emit_file_edited(running)
    assert "tu5" in adapter._pending_edit_diffs  # still stashed


async def test_failed_result_drops_stash_without_file_edited(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    path = tmp_path / "new.sql"
    await _gate(adapter, "Write", {"file_path": str(path), "content": "a\n"}, "tu6")
    sub = adapter._bus.subscribe()
    failed = ToolCallUpdate(
        event_id="e",
        time=adapter._now(),
        session_id="our-sid",
        tool_call_id="tu6",
        status="error",
        error_text="boom",
    )
    await adapter._maybe_emit_file_edited(failed)
    assert "tu6" not in adapter._pending_edit_diffs  # dropped
    # No FileEdited should be queued — drain with a short timeout, expect nothing.
    with pytest.raises(asyncio.TimeoutError):

        async def _pull() -> FileEdited:
            async for ev in sub:
                if isinstance(ev, FileEdited):
                    return ev  # pragma: no cover
            raise AssertionError("stream closed")  # pragma: no cover

        await asyncio.wait_for(_pull(), timeout=0.2)
