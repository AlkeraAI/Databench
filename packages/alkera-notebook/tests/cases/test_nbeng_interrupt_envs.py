"""Interrupt escalation reported step by step as ``kernel.interrupt``
events, and the environment listings a notebook's readers are served."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest
from alkera_notebook.engine import CellsTarget, KernelInterrupt, NotebookClient, NotFoundError
from alkera_notebook.envs.models import EnvDescriptor
from alkera_notebook.envs.static import StaticEnvRegistry
from nbeng_harness import VIEWER, engine_for, notebook, run_cells, statuses, until


def interrupts(client: NotebookClient) -> list[KernelInterrupt]:
    return [e for e in list(client.queue._items) if isinstance(e, KernelInterrupt)]


async def running(client: NotebookClient, cid: str) -> None:
    async def is_running() -> bool:
        return (await statuses(client))[cid] == "running"

    await until(is_running, timeout_s=15)


async def test_an_interrupt_that_does_not_take_reports_each_escalation_step(
    tmp_path: Path,
) -> None:
    code = "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\ntime.sleep(60)"
    async with engine_for(tmp_path, escalation=(0.3, 1.0)) as engine:
        _session, ann, (a,) = await notebook(engine, [code])
        handle = await ann.run(CellsTarget(ids=[a]))
        await running(ann, a)
        kernel_id = (await ann.kernel("status")).kernel_id
        await ann.kernel("interrupt")
        record = await handle.wait(15)
        assert record.status == "kernel_restarted"
        steps = interrupts(ann)
        assert [e.step for e in steps] == ["signalled", "second_signal", "restarting", "done"]
        assert {e.run_id for e in steps} == {handle.run_id}
        assert {e.kernel_id for e in steps} == {kernel_id}
        ats = [e.at for e in steps]
        assert ats == sorted(ats) and ats[0].tzinfo is not None
        # Restarting is announced before the kernel's exit, the interrupt's
        # end before the run's.
        events = list(ann.queue._items)
        kinds = [getattr(e, "step", e.type) for e in events]
        assert kinds.index("restarting") < kinds.index("kernel.exited")
        assert kinds.index("done") < kinds.index("run.finished")


async def test_an_interrupt_that_takes_reports_only_the_signal(tmp_path: Path) -> None:
    async with engine_for(tmp_path, escalation=(1.0, 2.0)) as engine:
        _session, ann, (a,) = await notebook(engine, ["import time\ntime.sleep(30)"])
        handle = await ann.run(CellsTarget(ids=[a]))
        await running(ann, a)
        await ann.kernel("interrupt")
        assert (await handle.wait(10)).status == "interrupted"
        await asyncio.sleep(2.3)  # past both escalation deadlines
        assert [e.step for e in interrupts(ann)] == ["signalled", "done"]


async def test_an_interrupt_with_nothing_running_reports_nothing(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _session, ann, (a,) = await notebook(engine, ["x = 1"])
        await run_cells(ann, a)
        await ann.kernel("interrupt")
        await ann.kernel("interrupt_all")
        assert interrupts(ann) == []


# ------------------------------------------------------------------ environments


def env(env_id: str, kind: str = "uv_project", state: str = "ready") -> EnvDescriptor:
    return EnvDescriptor(
        env_id=env_id,
        kind=kind,  # type: ignore[arg-type]
        spec_root=f"/ws/{env_id}",
        prefix=f"/ws/{env_id}/.venv",
        interpreter=StaticEnvRegistry().interpreter,
        python_version="3.13.1",
        spec_hash="h",
        built_spec_hash="h",
        state=state,  # type: ignore[arg-type]
    )


class ListedEnvs(StaticEnvRegistry):
    """Detection finds ``detected``; the notebook resolves to ``current``;
    each environment has its own packages."""

    def __init__(self, current: EnvDescriptor, detected: Sequence[EnvDescriptor]) -> None:
        super().__init__()
        self.current = current
        self.detected = list(detected)
        self.asked: list[str] = []

    async def detect(self, notebook_path: str) -> list[EnvDescriptor]:
        return list(self.detected)

    async def resolve(self, notebook_path: str, recorded: str | None) -> EnvDescriptor:
        return self.current

    async def packages(self, env_id: str) -> list[tuple[str, str]]:
        self.asked.append(env_id)
        return [(f"{env_id}-pkg", "1.0")]


DEFAULT = env("default", "default")
PROJECT = env("proj", "uv_project", "stale")
SCRIPT = env("script", "script", "missing")


@pytest.mark.parametrize(
    ("current", "detected", "listed"),
    [
        pytest.param(DEFAULT, [DEFAULT, PROJECT], ["default", "proj"], id="current-detected"),
        pytest.param(SCRIPT, [DEFAULT, PROJECT], ["script", "default", "proj"], id="current-added"),
    ],
)
async def test_the_listing_names_the_current_env_among_all_found(
    tmp_path: Path, current: EnvDescriptor, detected: list[EnvDescriptor], listed: list[str]
) -> None:
    async with engine_for(tmp_path, envs=ListedEnvs(current, detected)) as engine:
        session, _ann, _ = await notebook(engine, ["x = 1"])
        viewer = session.attach(VIEWER)  # a reader may list them
        listing = await viewer.envs()
        assert listing.current.env_id == current.env_id
        assert [e.env_id for e in listing.envs] == listed
        assert {e.env_id: e.state for e in listing.envs}[current.env_id] == current.state
        assert listing.current.kind == current.kind


@pytest.mark.parametrize("shared", [True, False], ids=["shared", "per-member"])
async def test_the_listing_says_whether_members_share_the_environments(
    tmp_path: Path, shared: bool
) -> None:
    """The machine decides whether a workspace's members share its
    environments; the listing passes that on, so a person is told when what
    they install is theirs alone."""
    registry = ListedEnvs(DEFAULT, [DEFAULT])
    async with engine_for(tmp_path, envs=registry, shared_envs=shared) as engine:
        session, _ann, _ = await notebook(engine, ["x = 1"])
        listing = await session.attach(VIEWER).envs()
        assert listing.shared is shared
        assert listing.model_dump()["shared"] is shared


async def test_packages_are_served_only_for_a_listed_env(tmp_path: Path) -> None:
    registry = ListedEnvs(SCRIPT, [DEFAULT, PROJECT])
    async with engine_for(tmp_path, envs=registry) as engine:
        session, _ann, _ = await notebook(engine, ["x = 1"])
        viewer = session.attach(VIEWER)
        for env_id in ("proj", "script"):
            got = await viewer.env_packages(env_id)
            assert got.env_id == env_id
            assert [(p.name, p.version) for p in got.packages] == [(f"{env_id}-pkg", "1.0")]
        with pytest.raises(NotFoundError):
            await viewer.env_packages("someone-elses-env")
        assert registry.asked == ["proj", "script"]
