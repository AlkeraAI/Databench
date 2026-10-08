"""Kernel lifecycle through real processes: start, failures, crashes, caps,
restart, a dropped connection and orphan-free shutdown."""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.engine import InspectQuery, ReadQuery
from alkera_notebook.envs.static import StaticEnvRegistry
from alkera_notebook.events.models import AnyEvent, KernelExited, KernelStateEvent
from alkera_notebook.kernels.launch_local import group_pids
from nbeng_harness import engine_for, notebook, run_cells, statuses, text_of, until


def exits(events: list[AnyEvent]) -> list[KernelExited]:
    return [e for e in events if isinstance(e, KernelExited)]


class CountingLauncher:
    """The real launcher, counting launches."""

    def __init__(self) -> None:
        from alkera_notebook.kernels.launch_local import LocalSubprocessLauncher

        self.inner = LocalSubprocessLauncher()
        self.launched: list[Any] = []

    def launch(self, spec: Any) -> Any:
        k = self.inner.launch(spec)
        self.launched.append(k)
        return k


async def test_kern_start_only_on_first_run(tmp_path: Path) -> None:
    launcher = CountingLauncher()
    async with engine_for(tmp_path, launcher=launcher) as engine:
        _, client, (a,) = await notebook(engine, ["x = 1\nx"])
        reopened = await engine.open("nb.alknb.py")
        view = await reopened.attach(client.actor).read(ReadQuery())
        assert view.kernel.state == "absent"
        assert (await client.kernel("status")).state == "absent"
        assert launcher.launched == []
        assert (await run_cells(client, a)).status == "ok"
        assert len(launcher.launched) == 1
        assert (await client.kernel("status")).state == "idle"


async def test_kern_interpreter_missing(tmp_path: Path) -> None:
    envs = StaticEnvRegistry(interpreter=str(tmp_path / "nope" / "python"), python_version="3.13")
    async with engine_for(tmp_path, envs=envs) as engine:
        _, client, (a,) = await notebook(engine, ["x = 1"])
        record = await run_cells(client, a)
        assert (record.status, record.reason) == ("refused", "interpreter_missing")
        # The run bar shows the reason as it is: a sentence.
        assert record.message is not None and record.message.startswith("Interpreter not found: ")
        view = await client.read()
        assert any(
            n.kind == "kernel_unavailable" and n.data.get("reason") == "interpreter_missing"
            for n in view.notices
        )
        assert view.kernel.state == "absent"


async def test_kern_site_packages_crash_at_import(tmp_path: Path) -> None:
    # An interpreter whose startup fails before the kernel can connect, the
    # way a broken .pth or sitecustomize does.
    wrapper = tmp_path / "python-broken"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'exec {sys.executable} -c "import sys; '
        "sys.stderr.write('ImportError: broken site-packages .pth\\n'); sys.exit(1)\"\n"
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    envs = StaticEnvRegistry(interpreter=str(wrapper), python_version="3.13")
    async with engine_for(tmp_path, envs=envs) as engine:
        _, client, (a,) = await notebook(engine, ["x = 1"])
        record = await run_cells(client, a)
        assert (record.status, record.reason) == ("refused", "start_failed")
        notice = next(n for n in (await client.read()).notices if n.kind == "kernel_unavailable")
        assert "broken site-packages .pth" in notice.message


async def test_kern_segfault_mid_run(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, client, (a, b) = await notebook(
            engine, ["x = 1", "import ctypes\nctypes.string_at(0)"]
        )
        events: list[AnyEvent] = []
        session.runtime.hub.listen(events.append)
        assert (await run_cells(client, a)).status == "ok"
        record = await run_cells(client, b)
        assert record.status == "kernel_restarted"
        await until(lambda: exits(events))
        assert exits(events)[0].reason == "crashed"
        assert exits(events)[0].exit_code is not None and exits(events)[0].exit_code < 0
        assert set((await statuses(client)).values()) == {"not_run"}
        # A later run starts a new kernel and works.
        assert (await run_cells(client, a)).status == "ok"
        assert (await client.kernel("status")).state == "idle"


async def test_kern_kernel_cap(tmp_path: Path) -> None:
    async with engine_for(tmp_path, max_kernels=1) as engine:
        _, first, (a,) = await notebook(engine, ["x = 1"], path="one.alknb.py")
        _, second, (b,) = await notebook(engine, ["y = 2"], path="two.alknb.py")
        assert (await run_cells(first, a)).status == "ok"
        record = await run_cells(second, b)
        assert (record.status, record.reason) == ("refused", "kernel_cap")
        assert record.message == "At most 1 kernels may run at once"
        # Shutting the first kernel down frees the slot.
        await first.kernel("shutdown")
        assert (await run_cells(second, b)).status == "ok"


async def test_kern_restart_keeps_the_environment(tmp_path: Path) -> None:
    launcher = CountingLauncher()
    async with engine_for(tmp_path, launcher=launcher) as engine:
        _, client, (a, b) = await notebook(engine, ["x = 41", "y = x + 1\ny"])
        assert (await run_cells(client, a, b)).status == "ok"
        before = await client.kernel("status")
        after = await client.kernel("restart")
        assert after.state == "idle"
        assert after.kernel_id != before.kernel_id
        assert after.env is not None and before.env is not None
        assert after.env.env_id == before.env.env_id
        assert len(launcher.launched) == 2
        assert launcher.launched[0].pid != launcher.launched[1].pid
        assert group_pids(launcher.launched[0].pgid) == []
        assert set((await statuses(client)).values()) == {"not_run"}
        assert (await client.inspect(InspectQuery(what="variables"))).variables == []
        # Values are gone: running the leaf re-runs its upstream.
        record = await run_cells(client, b)
        assert [p.reason for p in record.plan] == ["upstream", "target"]
        assert await text_of(client, b) == "42"


DROP_REAL = (
    "import gc, socket\n"
    "for _o in gc.get_objects():\n"
    "    if isinstance(_o, socket.socket) and _o.family == socket.AF_UNIX:\n"
    "        try:\n"
    "            _o.shutdown(socket.SHUT_RDWR)\n"
    "        except OSError:\n"
    "            pass\n"
    "import time\n"
    "time.sleep(60)"
)


@pytest.mark.parametrize(
    ("kernel", "code"),
    [
        # The real kernel exits when its connection drops.
        pytest.param("real", DROP_REAL, id="kern.dropped_connection_kernel_exits"),
        # The stand-in keeps running without it: the engine kills the group.
        pytest.param(
            "fake",
            "drop_connection()\nimport time\ntime.sleep(60)",
            id="kern.dropped_connection_engine_kills",
        ),
    ],
)
async def test_kern_dropped_connection_ends_the_kernel(
    tmp_path: Path, kernel: str, code: str
) -> None:
    async with engine_for(tmp_path, kernel=kernel) as engine:
        session, client, (a, b) = await notebook(engine, ["x = 1", code])
        events: list[AnyEvent] = []
        session.runtime.hub.listen(events.append)
        assert (await run_cells(client, a)).status == "ok"
        k = session.runtime.kernel
        assert k is not None
        pgid = k.launched.pgid
        record = await run_cells(client, b)
        assert record.status == "kernel_restarted"
        await until(lambda: exits(events))
        assert exits(events)[0].reason == "connection_lost"
        await until(lambda: group_pids(pgid) == [], timeout_s=5)


@pytest.mark.parametrize(
    "spawn",
    [
        pytest.param("subprocess.Popen(['sleep', '300'])", id="child"),
        pytest.param("subprocess.Popen(['sh', '-c', 'sleep 300 & sleep 300'])", id="grandchild"),
    ],
)
async def test_kern_orphan_free_shutdown(tmp_path: Path, spawn: str) -> None:
    async with engine_for(tmp_path) as engine:
        session, client, (a,) = await notebook(engine, [f"import subprocess\np = {spawn}"])
        assert (await run_cells(client, a)).status == "ok"
        kernel = session.runtime.kernel
        assert kernel is not None
        pgid = kernel.launched.pgid
        await until(lambda: len(group_pids(pgid)) >= 2)
    # engine.close() ran on leaving the block.
    await until(lambda: group_pids(pgid) == [], timeout_s=5)


def kernel_states(events: list[AnyEvent]) -> list[KernelStateEvent]:
    return [e for e in events if isinstance(e, KernelStateEvent)]


async def test_kern_states_name_the_kernel_and_its_environment(tmp_path: Path) -> None:
    """A kernel's states, from ``starting`` on, name the kernel and the
    environment it runs in, so whoever records kernels can say both."""
    async with engine_for(tmp_path) as engine:
        session, client, (a,) = await notebook(engine, ["x = 1"])
        events: list[AnyEvent] = []
        session.runtime.hub.listen(events.append)
        assert (await run_cells(client, a)).status == "ok"
        kernel_id = (await client.kernel("status")).kernel_id
        assert kernel_id is not None
        states = kernel_states(events)
        assert [s.state for s in states][:2] == ["starting", "idle"]
        for state in states:
            assert (state.kernel_id, state.env_id) == (kernel_id, "static")


async def test_kern_a_failed_start_names_the_kernel_it_said_was_starting(
    tmp_path: Path,
) -> None:
    envs = StaticEnvRegistry(interpreter=str(tmp_path / "no-python"), python_version="3.13")
    async with engine_for(tmp_path, envs=envs) as engine:
        session, client, (a,) = await notebook(engine, ["x = 1"])
        events: list[AnyEvent] = []
        session.runtime.hub.listen(events.append)
        assert (await run_cells(client, a)).status == "refused"
        starting = next(s for s in kernel_states(events) if s.state == "starting")
        assert starting.kernel_id is not None and starting.env_id == "static"
        (exited,) = exits(events)
        assert exited.kernel_id == starting.kernel_id
        assert exited.reason == "interpreter_missing"
        # Nothing names a kernel once none is starting or running.
        absent = [s for s in kernel_states(events) if s.state == "absent"]
        assert absent and all(s.env_id is None for s in absent)


async def test_kern_traceback_quotes_the_cell_as_written(tmp_path: Path) -> None:
    """A failing last expression is reported on the cell's own line and
    quoted as written: the engine hands the kernel the cell's source, so the
    private name the format renamed and the parentheses it added stay out."""
    async with engine_for(tmp_path) as engine:
        _, client, (a,) = await notebook(engine, ["_zero = 0\nvalue = 1\n1 / _zero"])
        record = await run_cells(client, a)
        assert record.status == "error"
        cell = next(c for c in (await client.read()).cells if c.id == a)
        assert cell.output is not None and cell.output.error is not None
        frames = [ln for ln in cell.output.error.traceback if f'"cell-{a}"' in ln]
        assert frames, cell.output.error.traceback
        head, quoted = frames[-1].splitlines()[:2]
        assert head.endswith(", line 3, in <module>")
        assert quoted.strip() == "1 / _zero"
