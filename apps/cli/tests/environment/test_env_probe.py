"""The probe script, run by a real interpreter, and the parser of what it prints."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import pytest
from _helpers.pyenv import UV, make_venv, offline_env, run_py
from alkera_cli.environment import LocalRunner, ProbeError
from alkera_cli.environment.probe import (
    MARKER,
    PROBE_PATH,
    parse_probe_output,
    probe_in_process,
    run_probe,
)

needs_posix = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symlinks")


def test_the_marker_is_spelled_the_same_in_the_script() -> None:
    script = PROBE_PATH.read_text(encoding="utf-8")
    assert re.search(r'^MARKER = "([^"]+)"$', script, re.M).group(1) == MARKER  # type: ignore[union-attr]


def test_the_script_uses_nothing_outside_the_standard_library() -> None:
    script = PROBE_PATH.read_text(encoding="utf-8")
    imported = set(re.findall(r"^(?:from|import) ([a-z_]+)", script, re.M))
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}


@pytest.mark.parametrize(
    "output,message",
    [
        pytest.param("", "printed no result", id="empty"),
        pytest.param("Traceback: boom\n", "boom", id="crash-tail-shown"),
        pytest.param(MARKER + "{not json\n", "unreadable", id="bad-json"),
        pytest.param(MARKER + '{"files": 3}\n', "unreadable", id="bad-shape"),
    ],
)
def test_unreadable_output_is_a_probe_error(output: str, message: str) -> None:
    with pytest.raises(ProbeError, match=message):
        parse_probe_output(output)


def test_the_last_marker_line_wins_over_noise_around_it() -> None:
    out = f'warning: x\n{MARKER}{{"root": "a"}}\nstderr noise\n{MARKER}{{"root": "b"}}\n'
    assert parse_probe_output(out).root == "b"


def test_project_files_are_read_whole_digested_and_never_through_a_link(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("a==1\n", encoding="utf-8")
    (tmp_path / "requirements").mkdir()
    (tmp_path / "requirements" / "dev.txt").write_text("b==2\n", encoding="utf-8")
    (tmp_path / "pixi.lock").write_text("big\n", encoding="utf-8")
    (tmp_path / "not-requirements.txt").write_text("x\n", encoding="utf-8")
    (tmp_path / "libs").mkdir()

    probe = probe_in_process(str(tmp_path), paths=("libs", "missing"))

    assert set(probe.files) == {"requirements.txt", "requirements/dev.txt", "pixi.lock"}
    assert (probe.files["requirements.txt"].text or "").splitlines() == ["a==1"]
    assert probe.files["pixi.lock"].text is None and probe.files["pixi.lock"].sha256
    assert probe.paths == {"libs": True, "missing": False}
    assert probe.env is None


@needs_posix
def test_a_symlinked_project_file_is_not_read(tmp_path: Path) -> None:
    secret = tmp_path / "outside.txt"
    secret.write_text("top secret\n", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    os.symlink(secret, ws / "requirements.txt")
    assert probe_in_process(str(ws)).files == {}


@pytest.mark.skipif(UV is None, reason="needs uv on PATH")
def test_a_real_interpreter_reports_itself_its_packages_and_its_pth_entries(tmp_path: Path) -> None:
    env = offline_env(tmp_path)
    python = make_venv(tmp_path / "venv", env)
    site = Path(run_py(python, "import sysconfig; print(sysconfig.get_paths()['purelib'])", env))
    (tmp_path / "ws" / "tools").mkdir(parents=True)
    (site / "extra.pth").write_text(
        f"# comment\nimport os\n{tmp_path / 'ws' / 'tools'}\n/no/such/dir\n", encoding="utf-8"
    )
    (site / "legacy.egg-link").write_text(f"{tmp_path / 'ws'}\n.\n", encoding="utf-8")

    probe = asyncio.run(
        run_probe(LocalRunner(base_env=env), python=str(python), root=str(tmp_path / "ws"))
    )

    assert probe.env is not None
    assert probe.env.python.version == run_py(
        python, "import platform; print(platform.python_version())", env
    )
    assert probe.env.pyvenv_cfg is not None
    assert {(e.kind, Path(e.path).name) for e in probe.env.path_entries} == {
        ("pth", "tools"),
        ("egg-link", "ws"),
    }


def test_an_interpreter_that_does_not_exist_is_a_probe_error(tmp_path: Path) -> None:
    with pytest.raises(ProbeError, match="failed"):
        asyncio.run(run_probe(LocalRunner(), python=str(tmp_path / "nope"), root=str(tmp_path)))
