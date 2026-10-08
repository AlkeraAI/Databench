"""Scheduler engine — beat + run_now dispatch, run lifecycle, heartbeat, events.

The store's claim/lease safety is covered in test_scheduler_store; here we drive
the orchestration on top: a due job (beat) and a run_now both execute their
runner OFF the request path, completion advances the schedule, failures are
recorded, and a long run is kept alive by the heartbeat so it isn't reclaimed.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alkera_cli.plugins.plugin_base.scheduler import (
    FORCE_RUN_KEY,
    EventTrigger,
    IntervalTrigger,
    JobEvent,
    OnceTrigger,
    ScheduledJob,
    Scheduler,
    SchedulerStore,
    new_job,
)

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _scheduler(tmp_path: Path, *, now: datetime = T0, hb: float = 20.0) -> Scheduler:
    store = SchedulerStore(tmp_path / ".alkera" / "scheduler", lease_ttl_seconds=30.0)
    return Scheduler(store, clock=lambda: now, heartbeat_interval_seconds=hb)


async def _await_job(sched: Scheduler, job_id: str) -> None:
    """Await the background run task for a job (spawned synchronously by
    run_now/tick, so it's present immediately after)."""
    task = sched._tasks.get(job_id)
    if task is not None:
        await task


async def test_run_now_runs_completes_and_reschedules(tmp_path: Path) -> None:
    sched = _scheduler(tmp_path, now=T0 + timedelta(seconds=100))
    calls: list[str] = []
    sched.register_runner("work", lambda job: _record(calls, job.job_id))
    sched.register(new_job("j", IntervalTrigger(seconds=60), now=T0, kind="work"))

    assert await sched.run_now("j") is True
    await _await_job(sched, "j")

    assert calls == ["j"]
    job = sched._store.get("j")
    assert job is not None
    assert job.state == "scheduled" and job.lease is None  # recurring → requeued
    assert job.next_run_at == (T0 + timedelta(seconds=100)) + timedelta(seconds=60)


async def test_run_now_marks_the_run_forced_without_persisting_it(tmp_path: Path) -> None:
    # run_now flags the run as an explicit "Run now" via a transient payload marker the
    # runner reads (to force through a skip-unchanged gate). It rides on an IN-MEMORY copy
    # only — the persisted job keeps its real payload, so a later cadence run isn't forced
    # and complete() re-reads a clean record.
    sched = _scheduler(tmp_path)
    seen: list[dict[str, object]] = []

    async def _capture(job: ScheduledJob) -> None:
        seen.append(dict(job.payload))

    sched.register_runner("work", _capture)
    sched.register(
        new_job("j", IntervalTrigger(seconds=60), now=T0, kind="work", payload={"connection": "db"})
    )

    assert await sched.run_now("j") is True
    await _await_job(sched, "j")

    assert seen == [{"connection": "db", FORCE_RUN_KEY: True}]  # runner saw the force marker
    persisted = sched._store.get("j")
    assert persisted is not None
    assert persisted.payload == {"connection": "db"}  # NOT persisted — disk stays clean


async def test_reschedule_soon_runs_the_job_gated_not_forced(tmp_path: Path) -> None:
    # The contrast to run_now: the file-watcher nudge (reschedule_soon) makes an already-run job
    # due, the beat claims it, and the runner sees a CLEAN payload — NO force marker — so the
    # skip-gate still decides whether the change is material (an unchanged source is a no-op).
    sched = _scheduler(tmp_path, now=T0 + timedelta(seconds=60))
    seen: list[dict[str, object]] = []

    async def _capture(job: ScheduledJob) -> None:
        seen.append(dict(job.payload))

    sched.register_runner("work", _capture)
    sched.register(
        new_job(
            "j", IntervalTrigger(seconds=900), now=T0, kind="work", payload={"connection": "db"}
        )
    )
    assert await sched.run_now("j") is True  # one run so it's already-run + scheduled a cadence out
    await _await_job(sched, "j")
    seen.clear()

    assert sched.reschedule_soon("j") is not None  # nudge → due now
    assert await sched.tick() == ["j"]  # the beat claims it
    await _await_job(sched, "j")
    assert seen == [{"connection": "db"}]  # gated — clean payload, no FORCE_RUN_KEY


async def test_tick_resets_a_dead_daemons_phantom_running_job(tmp_path: Path) -> None:
    # The "close + reopen the IDE" bug: one dbt job genuinely running under THIS scheduler +
    # one left "running" by a dead daemon (stale lease). A tick resets the phantom to scheduled
    # so it stops showing as Running, and coalescing keeps it deferred behind the live sibling
    # (it would render as Queued) — NOT re-claimed into a second concurrent run.
    import os
    import socket

    from alkera_cli.plugins.plugin_base.scheduler import JobLease

    now = T0 + timedelta(seconds=100)
    sched = _scheduler(tmp_path, now=now)  # lease_ttl 30s
    sched.register_runner("work", lambda job: _record([], job.job_id))
    live = new_job("live", IntervalTrigger(seconds=900), now=T0, kind="work", coalesce_key="g")
    live.state = "running"  # genuinely running here — lease is THIS live process
    live.lease = JobLease(
        pid=os.getpid(), host=socket.gethostname(), claimed_at=now, heartbeat_at=now
    )
    sched.register(live)
    phantom = new_job(
        "phantom", IntervalTrigger(seconds=900), now=T0, kind="work", coalesce_key="g"
    )
    phantom.state = "running"  # left "running" by a dead daemon — stale (cross-host, old heartbeat)
    phantom.lease = JobLease(pid=1, host="dead-host", claimed_at=T0, heartbeat_at=T0)
    sched.register(phantom)

    started = await sched.tick()
    assert "phantom" not in started  # NOT re-claimed (coalesced behind the live sibling)
    phantom_after = sched._store.get("phantom")
    assert phantom_after is not None and phantom_after.state == "scheduled"  # phantom → queued
    live_after = sched._store.get("live")
    assert live_after is not None and live_after.state == "running"  # the live one is untouched


async def _record(sink: list[str], value: str) -> None:
    sink.append(value)


async def test_tick_skips_a_job_with_no_runner(tmp_path: Path) -> None:
    # An orphaned/renamed kind (no runner) must NOT be claimed+failed by the beat — that
    # requeues a recurring job and re-fails it every cadence forever (the lineage_refresh
    # crash-loop). It stays scheduled, unclaimed, with NO error, so the beat is quiet and
    # it runs cleanly once a runner registers.
    sched = _scheduler(tmp_path, now=T0 + timedelta(seconds=20))
    sched.register(new_job("orphan", IntervalTrigger(seconds=10), now=T0, kind="gone"))

    assert await sched.tick() == []  # not dispatched
    job = sched._store.get("orphan")
    assert job is not None
    assert job.state == "scheduled" and job.lease is None and job.last_error is None

    # Once a runner for that kind exists, the next beat runs it normally.
    calls: list[str] = []
    sched.register_runner("gone", lambda j: _record(calls, j.job_id))
    assert await sched.tick() == ["orphan"]
    await _await_job(sched, "orphan")
    assert calls == ["orphan"]


async def test_tick_runs_known_kinds_even_when_an_orphan_is_also_due(tmp_path: Path) -> None:
    # A due orphan must not block the due jobs that DO have runners in the same tick.
    sched = _scheduler(tmp_path, now=T0 + timedelta(seconds=20))
    calls: list[str] = []
    sched.register_runner("work", lambda j: _record(calls, j.job_id))
    sched.register(new_job("orphan", IntervalTrigger(seconds=10), now=T0, kind="gone"))
    sched.register(new_job("real", IntervalTrigger(seconds=10), now=T0, kind="work"))

    assert await sched.tick() == ["real"]
    await _await_job(sched, "real")
    assert calls == ["real"]


# --- fire on activation (prime_due) -----------------------------------------


async def test_prime_due_fires_a_never_run_job_now(tmp_path: Path) -> None:
    # A fresh interval job is primed at now+cadence; prime_due makes it due NOW so a
    # refresh fires on activation instead of waiting 15 minutes.
    sched = _scheduler(tmp_path, now=T0)
    sched.register(new_job("j", IntervalTrigger(seconds=900), now=T0, kind="work"))
    assert sched._store.get("j").next_run_at == T0 + timedelta(seconds=900)  # type: ignore[union-attr]

    primed = sched.prime_due("j", now=T0)
    assert primed is not None and primed.next_run_at == T0

    calls: list[str] = []
    sched.register_runner("work", lambda j: _record(calls, j.job_id))
    assert await sched.tick() == ["j"]  # now due immediately
    await _await_job(sched, "j")
    assert calls == ["j"]


async def test_prime_due_never_primes_a_standing_event_job(tmp_path: Path) -> None:
    # A STANDING (EventTrigger) job is a MANUAL-ONLY, metered/billed source. prime_due must
    # REFUSE to prime it — otherwise activation / a daemon restart would auto-fire a billed
    # query. Money-safety defense in depth.
    sched = _scheduler(tmp_path, now=T0)
    sched.register(new_job("metered", EventTrigger(event="run.metered"), now=T0, kind="work"))
    assert sched.prime_due("metered", now=T0) is None  # standing → not primed
    assert sched._store.get("metered").next_run_at is None  # type: ignore[union-attr]


async def test_prime_due_respects_an_already_run_jobs_cadence(tmp_path: Path) -> None:
    # Once a job has run, prime_due is a no-op — it keeps its cadence (we only fire EARLY
    # on the FIRST activation, not re-fire a job that's already seeded and waiting).
    sched = _scheduler(tmp_path, now=T0)
    calls: list[str] = []
    sched.register_runner("work", lambda j: _record(calls, j.job_id))
    sched.register(new_job("j", IntervalTrigger(seconds=900), now=T0, kind="work"))
    assert await sched.run_now("j") is True
    await _await_job(sched, "j")
    after_run = sched._store.get("j").next_run_at  # type: ignore[union-attr]

    assert sched.prime_due("j", now=T0) is None  # last_run_at set → not re-primed
    assert sched._store.get("j").next_run_at == after_run  # type: ignore[union-attr]


# --- orphan GC --------------------------------------------------------------


async def test_prune_orphans_removes_runnerless_unprotected_jobs(tmp_path: Path) -> None:
    sched = _scheduler(tmp_path, now=T0)
    sched.register_runner("work", lambda j: _record([], j.job_id))
    sched.register(new_job("real", IntervalTrigger(seconds=60), now=T0, kind="work"))
    sched.register(new_job("orphan", IntervalTrigger(seconds=60), now=T0, kind="lineage_refresh"))
    sched.register(new_job("kb", IntervalTrigger(seconds=60), now=T0, kind="kb_seed"))

    removed = sched.prune_orphans(protected={"kb_seed"})
    assert removed == ["orphan"]  # runnerless + unprotected
    kinds = {j.kind for j in sched.list_jobs()}
    assert kinds == {"work", "kb_seed"}  # the registered + the protected survive


async def test_tick_runs_only_due_jobs(tmp_path: Path) -> None:
    sched = _scheduler(tmp_path, now=T0 + timedelta(seconds=20))
    calls: list[str] = []
    sched.register_runner("work", lambda job: _record(calls, job.job_id))
    sched.register(new_job("due", IntervalTrigger(seconds=10), now=T0, kind="work"))
    future = new_job("future", IntervalTrigger(seconds=10), now=T0, kind="work")
    future.next_run_at = T0 + timedelta(hours=1)
    sched.register(future)

    started = await sched.tick()
    assert started == ["due"]
    await _await_job(sched, "due")
    assert calls == ["due"]


# --- priority ordering + coalescing -----------------------------------------


async def test_tick_claims_due_jobs_in_priority_order(tmp_path: Path) -> None:
    # When several jobs are due in the SAME beat, the beat claims them highest-priority
    # first (ties broken by job_id) so a cheap/primary refresh lands before a slow one.
    sched = _scheduler(tmp_path, now=T0 + timedelta(seconds=20))
    sched.register_runner("work", lambda _job: asyncio.sleep(0))
    # Registered out of priority order — the beat must reorder, not honor insertion.
    sched.register(new_job("low", IntervalTrigger(seconds=10), now=T0, kind="work", priority=1))
    sched.register(new_job("high", IntervalTrigger(seconds=10), now=T0, kind="work", priority=9))
    sched.register(new_job("mid", IntervalTrigger(seconds=10), now=T0, kind="work", priority=5))

    assert await sched.tick() == ["high", "mid", "low"]  # -priority, then job_id
    for jid in ("high", "mid", "low"):
        await _await_job(sched, jid)


async def test_tick_coalesces_siblings_so_only_one_runs_at_a_time(tmp_path: Path) -> None:
    # Two jobs sharing a coalesce_key never BOTH start: the higher-priority one runs, the
    # sibling stays "scheduled" (NOT a fake "running" blocked on a lock) and is claimed on
    # a later beat once the first finishes — the family runs strictly one-at-a-time.
    sched = _scheduler(tmp_path, now=T0 + timedelta(seconds=20))
    gate = asyncio.Event()

    async def _blocking(_job: object) -> None:
        await gate.wait()

    sched.register_runner("seed", _blocking)
    sched.register(
        new_job("a", IntervalTrigger(seconds=10), now=T0, kind="seed", priority=1, coalesce_key="g")
    )
    sched.register(
        new_job("b", IntervalTrigger(seconds=10), now=T0, kind="seed", priority=9, coalesce_key="g")
    )

    # b (higher priority) wins the slot; a is DEFERRED — claimed by nobody, still scheduled.
    assert await sched.tick() == ["b"]
    assert sched._store.get("a").state == "scheduled" and sched._store.get("a").lease is None  # type: ignore[union-attr]
    assert sched._store.get("b").state == "running"  # type: ignore[union-attr]

    # A further beat while b is still in flight keeps deferring a (reads the persisted
    # running set — the guarantee holds across daemons, not just this process).
    assert await sched.tick() == []

    # b finishes → its key frees → a runs on the next beat.
    gate.set()
    await _await_job(sched, "b")
    assert await sched.tick() == ["a"]
    await _await_job(sched, "a")


async def test_tick_does_not_coalesce_jobs_without_a_key(tmp_path: Path) -> None:
    # The asymmetric case: coalesce_key=None (the default) means NO coalescing — two
    # keyless jobs both start in the same beat even though a keyed pair would not.
    sched = _scheduler(tmp_path, now=T0 + timedelta(seconds=20))
    sched.register_runner("work", lambda _job: asyncio.sleep(0))
    sched.register(new_job("x", IntervalTrigger(seconds=10), now=T0, kind="work"))
    sched.register(new_job("y", IntervalTrigger(seconds=10), now=T0, kind="work"))

    assert sorted(await sched.tick()) == ["x", "y"]  # both ran — no coalescing
    for jid in ("x", "y"):
        await _await_job(sched, jid)


async def test_run_now_false_when_already_running(tmp_path: Path) -> None:
    sched = _scheduler(tmp_path, now=T0 + timedelta(seconds=100))
    gate = asyncio.Event()

    async def _blocking(_job: object) -> None:
        await gate.wait()

    sched.register_runner("work", _blocking)
    sched.register(new_job("j", IntervalTrigger(seconds=60), now=T0, kind="work"))

    assert await sched.run_now("j") is True  # claims + starts (blocked on the gate)
    assert await sched.run_now("j") is False  # live lease blocks a second run
    gate.set()
    await _await_job(sched, "j")


async def test_run_now_overrides_coalescing_for_an_explicit_run(tmp_path: Path) -> None:
    # "Run now" is an explicit user action that must visibly DO something — so it claims
    # the job even while a coalesce sibling is in flight (unlike the BEAT, which defers).
    # It can't actually run two parses at once: the runtime's seed lock serializes the
    # heavy work, so a force-claimed sibling waits on the lock then runs. run_now returns
    # False ONLY when the job itself is already running.
    sched = _scheduler(tmp_path, now=T0 + timedelta(seconds=100))
    gate = asyncio.Event()

    async def _blocking(_job: object) -> None:
        await gate.wait()

    sched.register_runner("seed", _blocking)
    sched.register(new_job("a", IntervalTrigger(seconds=60), now=T0, kind="seed", coalesce_key="g"))
    sched.register(new_job("b", IntervalTrigger(seconds=60), now=T0, kind="seed", coalesce_key="g"))

    assert await sched.run_now("a") is True  # a starts (blocked on the gate)
    assert await sched.run_now("b") is True  # sibling in flight, but run_now FORCES b
    assert sched._store.get("b").state == "running"  # type: ignore[union-attr]
    assert await sched.run_now("a") is False  # a itself already running → False

    gate.set()
    await _await_job(sched, "a")
    await _await_job(sched, "b")


async def test_runner_error_marks_failed_and_emits(tmp_path: Path) -> None:
    sched = _scheduler(tmp_path, now=T0 + timedelta(seconds=10))
    events: list[JobEvent] = []
    sched.on_event(events.append)

    async def _boom(_job: object) -> None:
        raise RuntimeError("kaboom")

    sched.register_runner("work", _boom)
    sched.register(new_job("j", OnceTrigger(run_at=T0 + timedelta(seconds=5)), now=T0, kind="work"))

    await sched.run_now("j")
    await _await_job(sched, "j")

    job = sched._store.get("j")
    assert job is not None and job.state == "failed"
    assert job.last_error is not None and "kaboom" in job.last_error
    phases = [(e.phase, e.error) for e in events]
    assert phases[0][0] == "started"
    assert phases[-1][0] == "failed" and "kaboom" in (phases[-1][1] or "")


async def test_missing_runner_marks_failed(tmp_path: Path) -> None:
    sched = _scheduler(tmp_path, now=T0 + timedelta(seconds=10))
    sched.register(new_job("j", IntervalTrigger(seconds=10), now=T0, kind="nonexistent"))
    await sched.run_now("j")
    await _await_job(sched, "j")
    job = sched._store.get("j")
    assert job is not None and job.state == "scheduled"  # recurring requeues after a fail
    assert job.last_error is not None and "no runner" in job.last_error


async def test_emits_started_then_completed(tmp_path: Path) -> None:
    sched = _scheduler(tmp_path, now=T0 + timedelta(seconds=10))
    events: list[JobEvent] = []
    sched.on_event(events.append)
    sched.register_runner("work", lambda _job: asyncio.sleep(0))
    sched.register(new_job("j", IntervalTrigger(seconds=10), now=T0, kind="work"))

    await sched.run_now("j", reason="manual")
    await _await_job(sched, "j")

    assert [e.phase for e in events] == ["started", "completed"]
    assert all(e.reason == "manual" for e in events)


async def test_long_job_is_heartbeated(tmp_path: Path) -> None:
    # Real wall clock here so the heartbeat loop's sleeps advance; tiny interval.
    store = SchedulerStore(tmp_path / ".alkera" / "scheduler", lease_ttl_seconds=30.0)
    sched = Scheduler(store, heartbeat_interval_seconds=0.02)
    release = asyncio.Event()

    async def _long(_job: object) -> None:
        await release.wait()

    sched.register_runner("work", _long)
    sched.register(new_job("j", IntervalTrigger(seconds=10), now=datetime.now(UTC), kind="work"))

    assert await sched.run_now("j") is True
    claimed = store.get("j")
    assert claimed is not None and claimed.lease is not None
    first_hb = claimed.lease.heartbeat_at

    # Poll until the heartbeat advances (no fixed sleep) — proves the loop runs.
    for _ in range(200):
        await asyncio.sleep(0.02)
        current = store.get("j")
        assert current is not None and current.lease is not None
        if current.lease.heartbeat_at > first_hb:
            break
    else:  # pragma: no cover
        release.set()
        await _await_job(sched, "j")
        pytest.fail("heartbeat never advanced the lease")

    release.set()
    await _await_job(sched, "j")


async def test_stop_cancels_the_run_but_keeps_the_job(tmp_path: Path) -> None:
    # The user-facing "Cancel": stop the IN-FLIGHT run without deleting the job — it
    # reschedules for its next cadence. (Deletion is `remove`, internal-only.)
    sched = _scheduler(tmp_path)
    started = asyncio.Event()
    gate = asyncio.Event()

    async def _blocking(_job: object) -> None:
        started.set()
        await gate.wait()

    sched.register_runner("work", _blocking)
    sched.register(new_job("j", IntervalTrigger(seconds=10), now=T0, kind="work"))
    assert await sched.run_now("j") is True
    await started.wait()  # the run is genuinely in flight before we stop it

    assert sched.stop("j") is True  # stop the running task
    task = sched._tasks.get("j")
    if task is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await task  # let the cancellation + complete() settle

    job = sched._store.get("j")
    assert job is not None  # NOT deleted — the job survives
    assert job.state == "scheduled" and job.lease is None  # rescheduled for its next cadence
    assert job.last_outcome == "cancelled"


async def test_stop_is_a_noop_when_the_job_is_not_running(tmp_path: Path) -> None:
    sched = _scheduler(tmp_path)
    sched.register(new_job("j", IntervalTrigger(seconds=10), now=T0, kind="work"))
    assert sched.stop("j") is False  # nothing in flight to stop
    assert sched._store.get("j") is not None  # and the job is untouched


async def test_remove_deletes_a_job(tmp_path: Path) -> None:
    # `remove` is the INTERNAL delete (a connection removed / a stale job replaced) — it
    # drops the persisted job. It is NOT wired to the UI.
    sched = _scheduler(tmp_path)
    sched.register(new_job("j", IntervalTrigger(seconds=10), now=T0, kind="work"))
    assert sched.remove("j") is True
    assert sched._store.get("j") is None


async def test_complete_runs_even_when_the_heartbeat_task_raises(tmp_path: Path) -> None:
    # A heartbeat task that dies with its OWN (non-Cancelled) exception must NOT prevent the
    # job from being finalized: awaiting the cancelled heartbeat in _run's finally re-raises
    # that exception, and if it escaped, complete() would be skipped — leaving the job stuck
    # "running" until its lease goes stale, then a peer reclaims it and DOUBLE-RUNS it (a
    # seed running twice). Pin: the job is completed + its lease cleared regardless.
    sched = _scheduler(tmp_path, now=T0 + timedelta(seconds=10))

    hb_running = asyncio.Event()

    async def _boom_heartbeat(_job_id: str) -> None:
        hb_running.set()
        raise RuntimeError("heartbeat exploded")

    sched._heartbeat_loop = _boom_heartbeat  # type: ignore[method-assign]

    calls: list[str] = []

    async def _runner(job: ScheduledJob) -> None:
        await hb_running.wait()  # ensure the heartbeat task has run and is about to die...
        await asyncio.sleep(0)  # ...and let it transition to done-with-exception
        calls.append(job.job_id)

    sched.register_runner("work", _runner)
    sched.register(new_job("j", IntervalTrigger(seconds=60), now=T0, kind="work"))

    assert await sched.run_now("j") is True
    await _await_job(sched, "j")  # must NOT raise the heartbeat's RuntimeError

    assert calls == ["j"]  # the runner still completed
    job = sched._store.get("j")
    assert job is not None
    assert job.state == "scheduled" and job.lease is None  # finalized + lease released
