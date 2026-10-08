"""What an agent starting in a workspace is told about its environment, and
that a session's system prompt carries it, bounded."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.environment import render_environment_instructions, summarize_spec, write_spec
from alkera_cli.environment.instructions import (
    MAX_INSTRUCTIONS_CHARS,
    MAX_INSTRUCTIONS_FILE_BYTES,
    MAX_SUMMARY_CHARS,
    TOOL_SENTENCE,
)
from alkera_cli.environment.spec import (
    SPEC_FILENAME,
    EnvironmentSpec,
    NotPortable,
    PackageSpec,
    PythonInfo,
)
from alkera_cli.harness import HarnessRuntime
from alkera_cli.host import paths
from alkera_core.project.directory import ProjectDirectory

needs_posix = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symlinks")

SPEC = EnvironmentSpec(
    captured_at="2026-10-04T12:00:00+00:00",
    env_kind="venv",
    python=PythonInfo(version="3.12.4", implementation="cpython"),
    packages=[
        PackageSpec(name="pandas", version="2.2.3", requested=True),
        PackageSpec(name="numpy", version="2.1.0"),
        PackageSpec(
            name="mylib", version="0.1", source="editable", path="libs/mylib", requested=True
        ),
    ],
    path_entries=["tools"],
    not_portable=[NotPortable(kind="editable_outside_workspace", name="extlib")],
)


def test_nothing_to_say_says_nothing(tmp_path: Path) -> None:
    assert render_environment_instructions(tmp_path, tools=False) == ""


def test_with_the_tools_and_no_files_there_is_still_no_block(tmp_path: Path) -> None:
    assert render_environment_instructions(tmp_path, tools=True) == ""


def test_with_the_tools_and_a_file_the_tool_sentence_closes_the_block(tmp_path: Path) -> None:
    (tmp_path / "ENVIRONMENT.md").write_text("Run `make test`.\n", encoding="utf-8")
    text = render_environment_instructions(tmp_path, tools=True)
    assert text.endswith("\n\n" + TOOL_SENTENCE)


def test_environment_md_and_the_spec_summary_both_ride(tmp_path: Path) -> None:
    (tmp_path / "ENVIRONMENT.md").write_text("Run `make test`.\n", encoding="utf-8")
    write_spec(tmp_path, SPEC)

    text = render_environment_instructions(tmp_path, tools=False)

    assert "Run `make test`." in text
    assert "Python 3.12.4" in text
    assert "Installed directly: pandas 2.2.3." in text
    assert "numpy" not in text.split("Installed directly:")[1].split(".")[0]
    assert "Editable from this workspace: mylib (libs/mylib)." in text
    assert "Workspace folders on sys.path: tools." in text
    assert "extlib (editable install outside the workspace)" in text
    assert TOOL_SENTENCE not in text


def test_a_long_environment_md_is_cut_at_a_line_and_says_so(tmp_path: Path) -> None:
    lines = [f"line {i:05d} " + "x" * 40 for i in range(MAX_INSTRUCTIONS_CHARS // 20)]
    (tmp_path / "ENVIRONMENT.md").write_text("\n".join(lines), encoding="utf-8")

    text = render_environment_instructions(tmp_path, tools=False)

    body = text.split("workspace:\n\n", 1)[1]
    assert len(body) < MAX_INSTRUCTIONS_CHARS + 200
    assert "ENVIRONMENT.md continues" in body
    assert body.split("\n\n[")[0].splitlines()[-1] in lines


def test_an_oversized_environment_md_is_not_read(tmp_path: Path) -> None:
    (tmp_path / "ENVIRONMENT.md").write_text(
        "x" * (MAX_INSTRUCTIONS_FILE_BYTES + 1), encoding="utf-8"
    )
    assert render_environment_instructions(tmp_path, tools=False) == ""


def test_a_huge_spec_summary_is_bounded(tmp_path: Path) -> None:
    many = [PackageSpec(name="p" * 200 + str(i), version="1", requested=True) for i in range(500)]
    summary = summarize_spec(SPEC.model_copy(update={"packages": many}))
    assert len(summary) <= MAX_SUMMARY_CHARS
    assert "and 480 more" in summary or summary.endswith("…")


def test_an_unreadable_spec_is_left_out(tmp_path: Path) -> None:
    (tmp_path / SPEC_FILENAME).write_text("{broken", encoding="utf-8")
    assert render_environment_instructions(tmp_path, tools=False) == ""


@needs_posix
def test_a_symlinked_environment_md_is_never_followed(tmp_path: Path) -> None:
    secret = tmp_path / "host-secret"
    secret.write_text("the box's credential\n", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    os.symlink(secret, ws / "ENVIRONMENT.md")
    os.symlink(secret, ws / SPEC_FILENAME)
    assert render_environment_instructions(ws, tools=False) == ""


async def _session_instructions(
    project_root: Path, monkeypatch: pytest.MonkeyPatch, *, working_dir: Path | None = None
) -> str:
    """The instructions a real runtime hands the adapter when a chat opens."""
    monkeypatch.setattr(paths, "INSTRUCTIONS_FILE_PATH", project_root / "no-global-instructions.md")
    factory = FakeAdapterFactory()
    rt = HarnessRuntime(ProjectDirectory(project_root / ".alkera"), adapter_factory=factory)
    chat = rt._chats_store.create(title="t", harness_type="agent")
    sid = chat.session_id
    chat.close()
    await rt.open_chat(sid, working_dir=working_dir)
    try:
        return str(factory.configs[-1].harness_native.get("global_instructions", ""))
    finally:
        await rt.close_chat(sid)


async def test_a_session_reads_the_environment_of_the_folder_it_runs_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    folder = tmp_path / "chat-folder"
    project.mkdir()
    folder.mkdir()
    (project / "ENVIRONMENT.md").write_text("PROJECT ROOT NOTES\n", encoding="utf-8")
    (folder / "ENVIRONMENT.md").write_text("WORKSPACE NOTES\n", encoding="utf-8")

    text = await _session_instructions(project, monkeypatch, working_dir=folder)

    assert "WORKSPACE NOTES" in text
    assert "PROJECT ROOT NOTES" not in text
    # The workspace brief comes first; the environment follows it.
    assert text.index("## Your root folder") < text.index("## Workspace environment")


@needs_posix
async def test_a_local_session_reads_the_project_root_and_names_the_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ENVIRONMENT.md").write_text("PROJECT ROOT NOTES\n", encoding="utf-8")
    text = await _session_instructions(tmp_path, monkeypatch)
    assert text.startswith("## Workspace environment")
    assert "PROJECT ROOT NOTES" in text
    # POSIX sessions serve the environment tools beside bash.
    assert text.endswith(TOOL_SENTENCE)


async def test_a_session_without_environment_files_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await _session_instructions(tmp_path, monkeypatch) == ""
