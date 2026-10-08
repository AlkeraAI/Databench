"""The thin callers of the environment library: ``alkera env`` and the daemon's
``environment.*`` methods. The library is exhausted elsewhere; these pin the
contract each surface adds (exit codes, where the spec lands, the response)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from _helpers.pyenv import UV, build_wheel, make_venv, offline_env, uv_install
from alkera_cli.daemon.methods.environment import (
    EnvironmentGetRequest,
    environment_get,
)
from alkera_cli.environment import SPEC_FILENAME, read_spec
from alkera_cli.main import app
from typer.testing import CliRunner

needs_uv = pytest.mark.skipif(UV is None, reason="needs uv on PATH")
cli = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    env = offline_env(tmp_path)
    for key in [k for k in env if k.startswith(("UV_", "PIP_"))]:
        monkeypatch.setenv(key, env[key])
    root = tmp_path / "ws"
    root.mkdir()
    wheels = tmp_path / "wheels"
    build_wheel(wheels, "alpha", "1.0")
    (root / "requirements.txt").write_text(
        f"--find-links {wheels.as_posix()}\nalpha==1.0\n", encoding="utf-8"
    )
    python = make_venv(root / ".venv", env)
    uv_install(python, env, "-r", str(root / "requirements.txt"))
    return root


@needs_uv
def test_env_capture_writes_the_spec_from_the_workspace_venv(workspace: Path) -> None:
    result = cli.invoke(app, ["env", "capture", "-p", str(workspace)])
    assert result.exit_code == 0, result.output
    spec = read_spec(workspace)
    assert spec is not None and spec.env_kind == "venv"
    assert spec.package("alpha").version == "1.0"  # type: ignore[union-attr]


def test_env_capture_without_a_venv_captures_the_project_files(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("alpha==1.0\n", encoding="utf-8")
    result = cli.invoke(app, ["env", "capture", "-p", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["env_kind"] == "none"


@needs_uv
def test_env_recreate_dry_run_prints_the_plan_and_changes_nothing(
    workspace: Path, tmp_path: Path
) -> None:
    cli.invoke(app, ["env", "capture", "-p", str(workspace)])
    target = tmp_path / "clone"
    result = cli.invoke(
        app,
        ["env", "recreate", "-p", str(workspace), "--target", str(target), "--dry-run", "--json"],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["dry_run"] is True
    assert [s["purpose"] for s in report["steps"]] == ["create_env", "install_packages"]
    assert not target.exists()


@needs_uv
def test_env_recreate_exits_non_zero_when_a_package_is_not_installed(
    workspace: Path, tmp_path: Path
) -> None:
    cli.invoke(app, ["env", "capture", "-p", str(workspace)])
    spec = json.loads((workspace / SPEC_FILENAME).read_text(encoding="utf-8"))
    spec["packages"][0]["version"] = "9.9"
    (workspace / SPEC_FILENAME).write_text(json.dumps(spec), encoding="utf-8")

    result = cli.invoke(
        app, ["env", "recreate", "-p", str(workspace), "--target", str(tmp_path / "c")]
    )

    assert result.exit_code == 1
    assert "Not recreated (not_installed): alpha 9.9" in result.output


def test_env_recreate_without_a_spec_exits_2(tmp_path: Path) -> None:
    result = cli.invoke(
        app, ["env", "recreate", "-p", str(tmp_path), "--target", str(tmp_path / "e")]
    )
    assert result.exit_code == 2


def _server() -> Any:
    return cast(Any, SimpleNamespace())


@needs_uv
def test_the_daemon_reads_back_the_spec_and_the_instructions(workspace: Path) -> None:
    result = cli.invoke(app, ["env", "capture", "-p", str(workspace)])
    assert result.exit_code == 0, result.output
    request = EnvironmentGetRequest(project_path=str(workspace))
    got = asyncio.run(environment_get(_server(), request))
    assert got.spec is not None and got.spec["env_kind"] == "venv"
    assert "alpha 1.0" in got.summary
    assert got.instructions.startswith("## Workspace environment")


async def test_the_daemon_reports_no_spec_as_none(tmp_path: Path) -> None:
    got = await environment_get(_server(), EnvironmentGetRequest(project_path=str(tmp_path)))
    assert (got.spec, got.summary, got.instructions) == (None, "", "")


def test_the_daemon_offers_no_method_that_runs_or_installs() -> None:
    import alkera_cli.daemon.methods  # noqa: F401  (registers every handler)
    from alkera_cli.daemon.protocol import METHODS

    names = set(METHODS)
    assert "environment.get" in names
    assert not {"environment.capture", "environment.recreate"} & names
