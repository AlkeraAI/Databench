"""A run whose environment cannot be had ends ``refused`` with a reason a
client can show: a build command that failed, or an environment that cannot
be found. A run never waits for an approval: an environment not built yet is
built by the run's own kernel start. The reason rides the run's
``run.finished`` (code and message) and a notice on the run's first cell.

Real registry (``LocalEnvRegistry``) over a real workspace; only the build
command runner is scripted, since a real failing ``uv sync`` needs a broken
sandbox to fail the way the box's did."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from alkera_notebook.envs import (
    CommandResult,
    CommandRunner,
    LocalCommandRunner,
    LocalEnvRegistry,
)
from alkera_notebook.events.models import AnyEvent, RunFinished
from nbeng_harness import engine_for, notebook, run_cells, until

PYPROJECT = '[project]\nname = "proj"\nversion = "0"\nrequires-python = ">=3.10"\n'
PERMISSION_DENIED = (
    'failed to find initial working directory "/home/alkera/.alkera/envs/default": '
    "permission denied"
)


class FailingRunner:
    """Every build command fails the way the box's sandbox refused it."""

    def __init__(self) -> None:
        self.ran: list[list[str]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> CommandResult:
        self.ran.append(list(argv))
        return CommandResult(returncode=1, stdout="", stderr=PERMISSION_DENIED)


def uv_project(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "pyproject.toml").write_text(PYPROJECT)
    (ws / "uv.lock").write_text("version = 1\n")
    return ws


def registry(tmp_path: Path, runner: CommandRunner | None = None) -> LocalEnvRegistry:
    return LocalEnvRegistry(
        tmp_path / "ws",
        tmp_path / "ws-envs",
        runner=runner or LocalCommandRunner(),
        uv="uv",
        cache_dir=tmp_path / "uv-cache",
    )


def finished(events: list[AnyEvent]) -> list[RunFinished]:
    return [e for e in events if isinstance(e, RunFinished)]


async def test_a_run_builds_an_environment_never_built_without_waiting_for_anyone(
    tmp_path: Path,
) -> None:
    """No approval stands between a run and its environment's first build:
    the run's kernel start builds it (here the scripted build fails, so the
    run ends refused with the build's own reason, not a wait)."""
    uv_project(tmp_path)
    runner = FailingRunner()
    async with engine_for(tmp_path, envs=registry(tmp_path, runner)) as engine:
        session, client, (a,) = await notebook(engine, ["x = 1"])
        events: list[AnyEvent] = []
        session.runtime.hub.listen(events.append)
        record = await run_cells(client, a)
        assert (record.status, record.reason) == ("refused", "env_build_failed")
        await until(lambda: finished(events))
        (done,) = finished(events)
        notice = next(n for n in (await client.read()).notices if n.cell_id == a)
        assert (done.reason, notice.data["reason"]) == ("env_build_failed", "env_build_failed")
        assert notice.data["run_id"] == record.run_id
        assert [argv[:2] for argv in runner.ran][:1] == [["uv", "sync"]]


async def test_a_failed_build_refuses_the_run_and_quotes_the_cause(tmp_path: Path) -> None:
    ws = uv_project(tmp_path)
    envs = registry(tmp_path, FailingRunner())
    env = await envs.resolve(str(ws / "nb.alknb.py"), None)
    assert env.kind == "uv_project" and env.state == "missing"
    async with engine_for(tmp_path, envs=envs) as engine:
        session, client, (a,) = await notebook(engine, ["x = 1"])
        events: list[AnyEvent] = []
        session.runtime.hub.listen(events.append)
        record = await run_cells(client, a)
        assert (record.status, record.reason) == ("refused", "env_build_failed")
        assert record.message is not None
        assert record.message.startswith("The environment could not be built: ")
        assert "permission denied" in record.message
        await until(lambda: finished(events))
        (done,) = finished(events)
        assert (done.reason, done.message) == ("env_build_failed", record.message)
        notice = next(n for n in (await client.read()).notices if n.cell_id == a)
        assert notice.kind == "kernel_unavailable"
        assert notice.data["reason"] == "env_build_failed"
        assert "permission denied" in notice.message
        # A second run retries the failed build rather than starting a kernel
        # on an interpreter that is not there.
        again = await run_cells(client, a)
        assert again.reason == "env_build_failed"


async def test_an_environment_that_cannot_be_found_refuses_the_run(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir(parents=True)
    async with engine_for(tmp_path, envs=registry(tmp_path)) as engine:
        _, client, (a,) = await notebook(engine, ["x = 1"], settings={"env": "./no-such-env"})
        record = await run_cells(client, a)
        assert (record.status, record.reason) == ("refused", "env_unavailable")
        assert record.message


@pytest.mark.parametrize(
    ("log", "expected"),
    [
        pytest.param("", "The environment could not be built: uv failed", id="no-log"),
        pytest.param(
            "$ uv sync\nboom: disk full",
            "The environment could not be built: uv failed: boom: disk full",
            id="last-line-quoted",
        ),
        pytest.param(
            "$ uv sync", "The environment could not be built: uv failed", id="only-the-command"
        ),
    ],
)
def test_env_failure_message(log: str, expected: str) -> None:
    from alkera_notebook.engine.session import env_failure_message
    from alkera_notebook.envs import EnvBuildError

    assert env_failure_message(EnvBuildError("uv failed", log=log)) == expected


def test_env_failure_message_bounds_a_long_line() -> None:
    from alkera_notebook.engine.session import env_failure_message
    from alkera_notebook.envs import EnvBuildError

    message = env_failure_message(EnvBuildError("uv failed", log="x" * 5000 + "END"))
    assert message.endswith("END") and len(message) < 400
