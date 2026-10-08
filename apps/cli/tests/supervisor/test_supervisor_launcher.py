"""Every worker the supervisor starts names this build by its absolute path.

An install that leaves the build at ``/opt/alkera/alkera.dist/alkera`` with
nothing on ``PATH`` started the supervisor by its full path, so it registered,
then started each org's worker as a bare ``alkera`` that ``unshare`` could not
find (exit 127). These cases build the worker's command line the way the
supervisor does, from that host's facts, and require an absolute path to an
existing executable.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from alkera_cli.host import launcher as launcher_module
from alkera_cli.host.launcher import LauncherNotFoundError, resolve_launcher, worker_argv
from alkera_cli.supervisor import service as service_module

needs_posix = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX exec bits")


def _binary(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _never_on_path(name: str) -> str | None:
    return None


@needs_posix
def test_the_worker_command_of_an_ssh_install_names_the_build_absolutely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SSH host: a compiled build under the install root, started as a bare
    ``alkera``, with no ``alkera`` on ``PATH``."""
    installed = _binary(tmp_path / "opt" / "alkera" / "alkera.dist" / "alkera")
    monkeypatch.setattr(sys, "argv", ["alkera", "box", "supervise"])
    monkeypatch.setattr(sys, "executable", str(installed))
    monkeypatch.setattr(launcher_module, "_COMPILED", True)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    argv = worker_argv(service_module._alkera_argv(), tmp_path / "orgs" / "1")
    program = Path(argv[0])
    assert program.is_absolute()
    assert program == installed.resolve()
    assert os.access(program, os.X_OK)
    assert argv[1:] == (
        "cloud-mirror",
        "worker",
        "--org-root",
        str(tmp_path / "orgs" / "1"),
        "--control-fd",
        "0",
    )


@needs_posix
@pytest.mark.parametrize(
    ("argv0", "on_path", "expected"),
    [
        pytest.param("RELATIVE", False, "script", id="a-relative-path-is-made-absolute"),
        pytest.param("alkera", True, "on-path", id="a-bare-name-is-looked-up"),
        pytest.param("ABSOLUTE", False, "script", id="an-absolute-path-is-kept"),
    ],
)
def test_from_source_the_console_script_that_started_it_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv0: str, on_path: bool, expected: str
) -> None:
    script = _binary(tmp_path / "venv" / "bin" / "alkera")
    elsewhere = _binary(tmp_path / "elsewhere" / "alkera")
    monkeypatch.chdir(tmp_path)
    given = {
        "RELATIVE": os.path.join("venv", "bin", "alkera"),
        "ABSOLUTE": str(script),
    }.get(argv0, argv0)

    def which(name: str) -> str | None:
        return str(elsewhere) if on_path and name == "alkera" else None

    found = resolve_launcher(
        argv0=given, executable="/usr/bin/python3", compiled=False, which=which
    )
    assert found == (script if expected == "script" else elsewhere).resolve()


@needs_posix
@pytest.mark.parametrize(
    "executable",
    [
        pytest.param("relative/alkera", id="not-absolute"),
        pytest.param("/no/such/alkera", id="missing"),
    ],
)
def test_nothing_runnable_is_refused_never_a_bare_name(executable: str) -> None:
    with pytest.raises(LauncherNotFoundError):
        resolve_launcher(argv0="alkera", executable=executable, compiled=True, which=_never_on_path)


@needs_posix
def test_a_file_that_cannot_be_executed_is_not_a_launcher(tmp_path: Path) -> None:
    plain = tmp_path / "alkera"
    plain.write_text("not a program", encoding="utf-8")
    plain.chmod(0o644)
    with pytest.raises(LauncherNotFoundError):
        resolve_launcher(
            argv0=str(plain), executable=str(plain), compiled=True, which=_never_on_path
        )
