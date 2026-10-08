"""The memory guard against real kernel processes and launcher-measured RSS."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alkera_notebook.engine import CellsTarget, MemoryPolicy
from alkera_notebook.events.models import AnyEvent, KernelExited
from alkera_notebook.kernels.launch_local import group_pids
from nbeng_harness import MiB, alloc_code, engine_for, notebook, run_cells, until

POLICY = MemoryPolicy(reserve_bytes=0, interval_s=0.1, kernel_start_bytes=16 * MiB)


def ooms(events: list[AnyEvent]) -> list[KernelExited]:
    return [e for e in events if isinstance(e, KernelExited) and e.reason == "out_of_memory"]


async def test_mem_cell_past_budget_is_killed_within_one_interval(tmp_path: Path) -> None:
    budget = 400 * MiB
    async with engine_for(tmp_path, budget=budget, memory=POLICY) as engine:
        session, client, (a,) = await notebook(engine, [alloc_code("done", 900, 25, 0.05)])
        events: list[AnyEvent] = []
        session.runtime.hub.listen(events.append)
        handle = await client.run(CellsTarget(ids=[a]))
        await until(lambda: session.runtime.kernel is not None)
        kernel = session.runtime.kernel
        assert kernel is not None
        pgid = kernel.launched.pgid
        record = await handle.wait(30)
        assert record.status == "kernel_restarted"
        event = ooms(events)[0]
        assert event.limit_bytes == budget
        assert event.peak_rss_bytes is not None and event.peak_rss_bytes > budget
        # 25 MiB every ~0.06 s for one 0.1 s interval plus a `ps`: far less
        # than the 500 MiB the cell would still have allocated.
        assert event.peak_rss_bytes < budget + 150 * MiB
        assert event.largest_process == kernel.pid
        assert event.message == "The kernel ran out of memory and was restarted"
        await until(lambda: group_pids(pgid) == [], timeout_s=5)
        assert set(c.status for c in (await client.read()).cells) == {"not_run"}
        assert engine.guard_kills and engine.guard_kills[0].stage == 1


async def test_mem_false_self_report_is_ignored(tmp_path: Path) -> None:
    # The fake kernel claims 1 byte of RSS in its hello and reports tiny
    # variables; the launcher's measurement is what decides.
    async with engine_for(tmp_path, budget=300 * MiB, memory=POLICY, kernel="fake") as engine:
        session, client, (a,) = await notebook(engine, [alloc_code("x", 600, 25, 0.05)])
        events: list[AnyEvent] = []
        session.runtime.hub.listen(events.append)
        handle = await client.run(CellsTarget(ids=[a]))
        await until(lambda: session.runtime.kernel is not None)
        kernel = session.runtime.kernel
        assert kernel is not None and kernel.hello.get("rss_bytes") == 1
        assert (await handle.wait(30)).status == "kernel_restarted"
        assert ooms(events) and ooms(events)[0].largest_process == kernel.pid


async def test_mem_two_kernels_the_larger_is_killed(tmp_path: Path) -> None:
    # The big kernel sits still holding 250 MiB; the small one keeps
    # allocating and pushes the total over. The larger one goes.
    async with engine_for(tmp_path, budget=420 * MiB, memory=POLICY) as engine:
        big_s, big, (b,) = await notebook(
            engine, [alloc_code("held", 250, 25, 0.0)], path="big.alknb.py"
        )
        small_s, small, (s,) = await notebook(
            engine, [alloc_code("more", 400, 10, 0.05)], path="small.alknb.py"
        )
        assert (await run_cells(big, b)).status == "ok"
        big_kernel = big_s.runtime.kernel
        assert big_kernel is not None
        big_events: list[AnyEvent] = []
        big_s.runtime.hub.listen(big_events.append)
        handle = await small.run(CellsTarget(ids=[s]))
        await until(lambda: ooms(big_events), timeout_s=30)
        assert not big_kernel.alive or await until(lambda: not big_kernel.alive)
        small_kernel = small_s.runtime.kernel
        assert small_kernel is not None and small_kernel.alive
        assert engine.guard_kills[0].kernel_ids == (big_kernel.kernel_id,)
        await small.kernel("interrupt")
        await handle.wait(30)


async def test_mem_admission_refused(tmp_path: Path) -> None:
    policy = MemoryPolicy(reserve_bytes=0, interval_s=0.1, kernel_start_bytes=64 * MiB)
    async with engine_for(tmp_path, budget=32 * MiB, memory=policy) as engine:
        _, client, (a,) = await notebook(engine, ["x = 1"])
        record = await run_cells(client, a)
        assert (record.status, record.reason) == ("refused", "memory")
        assert (await client.kernel("status")).state == "absent"


async def test_mem_queued_runs_on_a_killed_kernel_end_kernel_restarted(tmp_path: Path) -> None:
    async with engine_for(tmp_path, budget=300 * MiB, memory=POLICY) as engine:
        _, client, (a, b) = await notebook(engine, [alloc_code("x", 600, 25, 0.05), "y = 2"])
        first = await client.run(CellsTarget(ids=[a]))
        second = await client.run(CellsTarget(ids=[b]))
        assert second.info.status == "queued"
        records = await asyncio.gather(first.wait(30), second.wait(30))
        assert [r.status for r in records] == ["kernel_restarted", "kernel_restarted"]
        # The next run starts a fresh kernel.
        assert (await run_cells(client, b)).status == "ok"
