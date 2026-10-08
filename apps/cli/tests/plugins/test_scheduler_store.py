"""SchedulerStore: the atomic-claim + lease machinery.

The headline invariant: two daemons over the same workspace NEVER double-run a
job. That rests on a compare-and-set claim under the store's own FileLock, plus a
per-run lease whose staleness (dead PID / heartbeat past TTL) lets a crashed
runner be reclaimed. Covered here in-process (deterministic) AND across two real
processes (the cross-process proof).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alkera_cli.plugins.plugin_base.scheduler import (
    CronTrigger,
    EventTrigger,
    IntervalTrigger,
    JobLease,
    OnceTrigger,
    RawTrigger,
    ScheduledJob,
    SchedulerStore,
    new_job,
)

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _dead_pid() -> int:
    """A guaranteed-dead, reaped PID."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _store(tmp_path: Path, *, ttl: float = 60.0) -> SchedulerStore:
    return SchedulerStore(tmp_path / ".alkera" / "scheduler", lease_ttl_seconds=ttl)


# --- triggers --------------------------------------------------------------


def test_cron_trigger_next_after() -> None:
    nxt = CronTrigger(expr="0 * * * *").next_after(T0 + timedelta(minutes=5))
    assert nxt == datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)


def test_interval_trigger_next_after() -> None:
    assert IntervalTrigger(seconds=300).next_after(T0) == T0 + timedelta(seconds=300)


def test_once_trigger_fires_once() -> None:
    future = T0 + timedelta(days=1)
    once = OnceTrigger(run_at=future)
    assert once.next_after(T0) == future  # still ahead → fires
    assert once.next_after(future + timedelta(seconds=1)) is None  # already past → never again


def test_event_and_raw_triggers_are_not_time_driven() -> None:
    assert EventTrigger(event="workspace.open").next_after(T0) is None
    assert RawTrigger(type="solar_eclipse").next_after(T0) is None


# --- claim exclusivity + lease reclaim (in-process) ------------------------


def test_claim_is_exclusive_across_two_stores(tmp_path: Path) -> None:
    a = _store(tmp_path)
    b = _store(tmp_path)  # a second "daemon" over the same dir
    a.register(new_job("j", IntervalTrigger(seconds=60), now=T0))

    first = a.claim("j", now=T0 + timedelta(seconds=61))
    second = b.claim("j", now=T0 + timedelta(seconds=61))
    assert first is not None
    assert second is None  # the live lease blocks the second claimer
    assert first.state == "running" and first.lease is not None


def test_live_lease_blocks_claim(tmp_path: Path) -> None:
    s = _store(tmp_path, ttl=60.0)
    s.register(new_job("j", IntervalTrigger(seconds=10), now=T0))
    now = T0 + timedelta(seconds=11)
    assert s.claim("j", now=now) is not None
    # A fresh lease (our live pid, heartbeat just now) → not reclaimable.
    assert s.claim("j", now=now + timedelta(seconds=1)) is None


def test_stale_lease_dead_pid_is_reclaimed(tmp_path: Path) -> None:
    s = _store(tmp_path)
    job = new_job("j", IntervalTrigger(seconds=10), now=T0)
    job.state = "running"
    job.lease = JobLease(pid=_dead_pid(), host=socket.gethostname(), claimed_at=T0, heartbeat_at=T0)
    s.register(job)
    reclaimed = s.claim("j", now=T0 + timedelta(seconds=1))
    assert reclaimed is not None
    assert reclaimed.lease is not None and reclaimed.lease.pid == os.getpid()


def test_live_local_lease_survives_an_old_heartbeat(tmp_path: Path) -> None:
    # The laptop-suspend regression: after a resume EVERY local daemon's lease
    # has an hours-old heartbeat, but the holder PID is alive and still running
    # the job. Same-host PID liveness is authoritative — an age-based reclaim
    # here would double-run the job. (The holder refreshes within its ~20s
    # heartbeat loop; a contender must NOT win that race.)
    s = _store(tmp_path, ttl=30.0)
    s.register(new_job("j", IntervalTrigger(seconds=10), now=T0))
    claimed = s.claim("j", now=T0 + timedelta(seconds=11))
    assert claimed is not None  # heartbeat stamped at T0+11, OUR live pid
    assert s.claim("j", now=T0 + timedelta(hours=3)) is None, (
        "a live local holder was reclaimed on heartbeat age alone"
    )


def test_a_running_job_is_not_re_run_when_its_interval_refires(tmp_path: Path) -> None:
    # A long seed (a 20-min dbt column pass) outlives its 15-min interval: next_run_at goes
    # into the PAST while it's still running, so the row looks "overdue". But it must NOT be
    # reported due / be reclaimable while its holder (this daemon's pid) is alive — else the
    # beat would start a SECOND run on top of the first. PID-liveness keeps a same-host live
    # lease valid no matter how long the run takes (past next_run_at AND past the TTL).
    s = _store(tmp_path, ttl=60.0)
    job = new_job("j", IntervalTrigger(seconds=900), now=T0)  # every 15m
    job.state = "running"
    job.next_run_at = T0  # the interval already re-fired — overdue
    job.lease = JobLease(pid=os.getpid(), host=socket.gethostname(), claimed_at=T0, heartbeat_at=T0)
    s.register(job)
    # 20+ min later (well past next_run_at AND the 60s TTL) it's neither due nor claimable.
    later = T0 + timedelta(minutes=25)
    assert s.due(now=later) == []
    assert s.claim("j", now=later) is None


def test_active_coalesce_keys_tracks_only_live_running_jobs(tmp_path: Path) -> None:
    # The beat reads this to defer a due job whose coalesce sibling is in flight. A key is
    # ACTIVE only while its job is RUNNING with a LIVE lease: a scheduled (idle) job, a
    # keyless job, and a CRASHED worker (stale cross-host lease) all contribute nothing —
    # so a dead worker can't block its siblings forever (its key frees the moment the
    # lease ages past TTL and the job becomes reclaimable).
    s = _store(tmp_path, ttl=30.0)
    here = socket.gethostname()

    live = new_job("live", IntervalTrigger(seconds=10), now=T0, coalesce_key="g1")
    live.state = "running"
    live.lease = JobLease(pid=os.getpid(), host=here, claimed_at=T0, heartbeat_at=T0)
    s.register(live)

    dead = new_job("dead", IntervalTrigger(seconds=10), now=T0, coalesce_key="g2")
    dead.state = "running"  # a crashed cross-host worker → TTL-expired lease
    dead.lease = JobLease(pid=os.getpid(), host="other-host", claimed_at=T0, heartbeat_at=T0)
    s.register(dead)

    s.register(new_job("idle", IntervalTrigger(seconds=10), now=T0, coalesce_key="g3"))

    keyless = new_job("keyless", IntervalTrigger(seconds=10), now=T0)  # running, but no key
    keyless.state = "running"
    keyless.lease = JobLease(pid=os.getpid(), host=here, claimed_at=T0, heartbeat_at=T0)
    s.register(keyless)

    # Past the cross-host TTL (T0+31s > T0+30s): only the live LOCAL holder's key is active
    # (its own pid is alive, so an old heartbeat doesn't make it stale).
    assert s.active_coalesce_keys(now=T0 + timedelta(seconds=31)) == {"g1"}


def test_claim_respect_coalesce_defers_a_sibling_under_the_lock(tmp_path: Path) -> None:
    # The cross-daemon coalesce guarantee: with respect_coalesce, claim refuses while a LIVE
    # sibling (same coalesce_key) runs — the check happens under the SAME store lock as the
    # claim, so two daemons racing can't both claim different siblings of one key (the beat's
    # in-memory pre-filter alone has a TOCTOU). run_now's explicit path leaves it off and wins.
    s = _store(tmp_path, ttl=30.0)
    here = socket.gethostname()

    running = new_job("a", IntervalTrigger(seconds=10), now=T0, coalesce_key="dbt")
    running.state = "running"
    running.lease = JobLease(pid=os.getpid(), host=here, claimed_at=T0, heartbeat_at=T0)
    s.register(running)
    s.register(new_job("b", IntervalTrigger(seconds=10), now=T0, coalesce_key="dbt"))

    assert s.claim("b", now=T0, respect_coalesce=True) is None  # sibling in flight → deferred
    b = s.get("b")
    assert b is not None and b.state == "scheduled"  # left unclaimed, not stamped running

    claimed = s.claim("b", now=T0, respect_coalesce=False)  # run_now bypass → wins anyway
    assert claimed is not None and claimed.state == "running"


def test_claim_respect_coalesce_ignores_a_dead_sibling(tmp_path: Path) -> None:
    # A CRASHED sibling (stale lease) must not wedge the family forever — once the holder's
    # lease ages past TTL its key frees up and the sibling claims, even with respect_coalesce.
    s = _store(tmp_path, ttl=30.0)
    dead = new_job("a", IntervalTrigger(seconds=10), now=T0, coalesce_key="dbt")
    dead.state = "running"  # crashed cross-host worker → TTL-expired lease
    dead.lease = JobLease(pid=os.getpid(), host="other-host", claimed_at=T0, heartbeat_at=T0)
    s.register(dead)
    s.register(new_job("b", IntervalTrigger(seconds=10), now=T0, coalesce_key="dbt"))

    claimed = s.claim("b", now=T0 + timedelta(seconds=31), respect_coalesce=True)
    assert claimed is not None and claimed.state == "running"


def test_cross_host_lease_ttl_expired_is_reclaimed(tmp_path: Path) -> None:
    # Cross-host there is no PID to check — heartbeat age is all we have, so an
    # expired TTL reclaims (a crashed remote daemon can't release its lease).
    s = _store(tmp_path, ttl=30.0)
    job = new_job("j", IntervalTrigger(seconds=10), now=T0)
    job.state = "running"
    job.lease = JobLease(pid=os.getpid(), host="some-other-host", claimed_at=T0, heartbeat_at=T0)
    s.register(job)
    assert s.claim("j", now=T0 + timedelta(seconds=71)) is not None


def test_cross_host_lease_fresh_heartbeat_blocks(tmp_path: Path) -> None:
    s = _store(tmp_path, ttl=30.0)
    job = new_job("j", IntervalTrigger(seconds=10), now=T0)
    job.state = "running"
    job.lease = JobLease(pid=os.getpid(), host="some-other-host", claimed_at=T0, heartbeat_at=T0)
    s.register(job)
    assert s.claim("j", now=T0 + timedelta(seconds=10)) is None  # within TTL → blocked


# --- heartbeat -------------------------------------------------------------


def test_heartbeat_refreshes_and_prevents_reclaim(tmp_path: Path) -> None:
    s = _store(tmp_path, ttl=30.0)
    s.register(new_job("j", IntervalTrigger(seconds=10), now=T0))
    s.claim("j", now=T0 + timedelta(seconds=11))
    # Heartbeat at +40s keeps it alive...
    assert s.heartbeat("j", now=T0 + timedelta(seconds=40)) is True
    # ...so at +60s the lease age is only 20s (< 30s ttl) → not reclaimable.
    assert s.claim("j", now=T0 + timedelta(seconds=60)) is None


def test_heartbeat_from_non_owner_is_rejected(tmp_path: Path) -> None:
    s = _store(tmp_path)
    job = new_job("j", IntervalTrigger(seconds=10), now=T0)
    job.state = "running"
    job.lease = JobLease(pid=_dead_pid(), host=socket.gethostname(), claimed_at=T0, heartbeat_at=T0)
    s.register(job)
    assert s.heartbeat("j", now=T0 + timedelta(seconds=1)) is False  # not our lease


# --- complete --------------------------------------------------------------


def test_complete_recurring_reschedules(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.register(new_job("j", IntervalTrigger(seconds=300), now=T0))
    s.claim("j", now=T0 + timedelta(seconds=300))
    done = s.complete("j", now=T0 + timedelta(seconds=301), ok=True)
    assert done is not None
    assert done.state == "scheduled"  # recurring → back in the queue
    assert done.lease is None
    assert done.next_run_at == T0 + timedelta(seconds=301) + timedelta(seconds=300)


def test_complete_once_is_terminal(tmp_path: Path) -> None:
    s = _store(tmp_path)
    run_at = T0 + timedelta(seconds=10)
    s.register(new_job("j", OnceTrigger(run_at=run_at), now=T0))
    s.claim("j", now=run_at)
    done = s.complete("j", now=run_at + timedelta(seconds=1), ok=True)
    assert done is not None
    assert done.state == "ok" and done.next_run_at is None and done.lease is None


def test_complete_failed_records_error(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.register(new_job("j", OnceTrigger(run_at=T0 + timedelta(seconds=1)), now=T0))
    s.claim("j", now=T0 + timedelta(seconds=1))
    done = s.complete("j", now=T0 + timedelta(seconds=2), ok=False, error="boom")
    assert done is not None
    assert done.state == "failed" and done.last_error == "boom"


def test_event_job_is_re_runnable_not_terminal(tmp_path: Path) -> None:
    # A metered/manual refresh (an EventTrigger) is STANDING: after a run it returns to idle
    # ``scheduled`` (NOT terminal ``ok``/``failed`` like a one-shot), with no ``next_run_at`` so
    # it never auto-runs — and a second run_now can claim it again. Without is_standing() the job
    # went terminal after one run and claim() refused it forever (runnable exactly once).
    s = _store(tmp_path)
    s.register(new_job("j", EventTrigger(event="snowflake_access:metered"), now=T0))
    assert s.get("j").next_run_at is None  # never auto-due

    claimed = s.claim("j", now=T0 + timedelta(seconds=1))
    assert claimed is not None  # first manual run claims fine
    done = s.complete("j", now=T0 + timedelta(seconds=2), ok=True)
    assert done is not None
    assert done.state == "scheduled"  # idle + re-runnable, NOT terminal "ok"
    assert done.next_run_at is None  # but never auto-due (is_due needs a time)
    assert done.last_run_at == T0 + timedelta(seconds=2)  # last run still recorded

    # the run never went auto-due, AND a second run_now can claim it again (the bug fix).
    assert not done.is_due(now=T0 + timedelta(days=365), ttl_seconds=60.0)
    reclaimed = s.claim("j", now=T0 + timedelta(seconds=3))
    assert reclaimed is not None  # second manual run — would be None before the fix


def test_failed_event_job_returns_to_scheduled_with_error_visible(tmp_path: Path) -> None:
    # A failed manual run records the error but stays re-runnable (idle scheduled), so the user
    # can fix the cause and run it again — it does not get stuck in a terminal "failed".
    s = _store(tmp_path)
    s.register(new_job("j", EventTrigger(event="bigquery_query_history:metered"), now=T0))
    s.claim("j", now=T0 + timedelta(seconds=1))
    done = s.complete("j", now=T0 + timedelta(seconds=2), ok=False, error="boom")
    assert done is not None
    assert done.state == "scheduled"  # re-runnable, not terminal "failed"
    assert done.last_error == "boom" and done.last_outcome == "failed"  # error still surfaced
    assert s.claim("j", now=T0 + timedelta(seconds=3)) is not None  # can retry


def test_complete_from_non_owner_does_not_clobber(tmp_path: Path) -> None:
    # A2: this is the post-reclaim state — the lease belongs to ANOTHER daemon
    # (a different pid). A slow runner finishing here must NOT finalize the job,
    # else it would reset a job the reclaimer is actively running → double-run.
    s = _store(tmp_path)
    foreign = JobLease(
        pid=os.getpid() + 1, host=socket.gethostname(), claimed_at=T0, heartbeat_at=T0
    )
    job = new_job("j", IntervalTrigger(seconds=300), now=T0)
    job.state = "running"
    job.lease = foreign
    s.register(job)

    assert s.complete("j", now=T0 + timedelta(seconds=301), ok=True) is None
    after = s.get("j")
    assert after is not None
    # Untouched: still running, the reclaimer's lease intact.
    assert after.state == "running"
    assert after.lease is not None and after.lease.pid == os.getpid() + 1


# --- cron timezone ---------------------------------------------------------


def test_cron_trigger_honors_timezone() -> None:
    # "0 9 * * *" at 09:00 America/New_York. 2026-06-15 is EDT (UTC-4), so the
    # next fire is 13:00 UTC — NOT 09:00 UTC (the pre-fix behavior).
    after = datetime(2026, 6, 15, 0, 0, tzinfo=UTC)
    et = CronTrigger(expr="0 9 * * *", timezone="America/New_York").next_after(after)
    assert et == datetime(2026, 6, 15, 13, 0, tzinfo=UTC)
    # The same expr in UTC fires at 09:00 UTC.
    utc = CronTrigger(expr="0 9 * * *", timezone="UTC").next_after(after)
    assert utc == datetime(2026, 6, 15, 9, 0, tzinfo=UTC)


def test_cron_trigger_bad_timezone_degrades_to_utc() -> None:
    after = datetime(2026, 6, 15, 0, 0, tzinfo=UTC)
    nxt = CronTrigger(expr="0 9 * * *", timezone="Not/AZone").next_after(after)
    assert nxt == datetime(2026, 6, 15, 9, 0, tzinfo=UTC)  # fail-safe → UTC


# --- register / remove / due / list ---------------------------------------


def test_register_is_idempotent(tmp_path: Path) -> None:
    s = _store(tmp_path)
    first = s.register(new_job("j", IntervalTrigger(seconds=10), now=T0, kind="a"))
    second = s.register(new_job("j", IntervalTrigger(seconds=10), now=T0, kind="b"))
    assert first.kind == "a" and second.kind == "a"  # existing wins
    assert len(s.list_jobs()) == 1


def test_remove(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.register(new_job("j", IntervalTrigger(seconds=10), now=T0))
    assert s.remove("j") is True
    assert s.get("j") is None
    assert s.remove("j") is False


def test_job_id_with_colon_round_trips_on_every_platform(tmp_path: Path) -> None:
    """Job ids are `<plugin>:<connection>` by convention, but `:` is invalid in
    a Windows filename (the NTFS alternate-data-stream separator — os.replace
    fails with WinError 87). The store's filename mapping must encode it while
    register/get/claim/remove keep addressing the raw id."""
    s = _store(tmp_path)
    job_id = "fake_refresh:fake_db"
    s.register(new_job(job_id, IntervalTrigger(seconds=10), now=T0))

    loaded = s.get(job_id)
    assert loaded is not None and loaded.job_id == job_id
    assert [j.job_id for j in s.list_jobs()] == [job_id]

    claimed = s.claim(job_id, now=T0 + timedelta(seconds=11))
    assert claimed is not None and claimed.state == "running"

    # The on-disk name carries no raw colon (and is identical cross-platform).
    (job_file,) = (tmp_path / ".alkera" / "scheduler" / "jobs").glob("*.json")
    assert ":" not in job_file.name
    assert "%3A" in job_file.name

    assert s.remove(job_id) is True
    assert s.get(job_id) is None


def test_due_selects_arrived_idle_and_stale_running(tmp_path: Path) -> None:
    s = _store(tmp_path, ttl=30.0)
    # arrived + scheduled → due
    s.register(new_job("due", IntervalTrigger(seconds=10), now=T0))
    # future → not due
    future = new_job("future", IntervalTrigger(seconds=10), now=T0)
    future.next_run_at = T0 + timedelta(hours=1)
    s.register(future)
    # running with a live (our-pid, fresh) lease → not due
    s.register(new_job("live", IntervalTrigger(seconds=10), now=T0))
    s.claim("live", now=T0 + timedelta(seconds=11))
    # running with a dead-pid lease → due (reclaimable)
    stale = new_job("stale", IntervalTrigger(seconds=10), now=T0)
    stale.state = "running"
    stale.lease = JobLease(
        pid=_dead_pid(), host=socket.gethostname(), claimed_at=T0, heartbeat_at=T0
    )
    s.register(stale)

    due_ids = {j.job_id for j in s.due(now=T0 + timedelta(seconds=20))}
    assert due_ids == {"due", "stale"}


def test_list_skips_corrupt_file(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.register(new_job("good", IntervalTrigger(seconds=10), now=T0))
    (tmp_path / ".alkera" / "scheduler" / "jobs" / "bad.json").write_text("{ not json")
    jobs = s.list_jobs()
    assert [j.job_id for j in jobs] == ["good"]  # torn file skipped, not fatal


def test_job_round_trips_through_disk(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.register(new_job("j", CronTrigger(expr="*/5 * * * *"), now=T0, payload={"k": 1}))
    loaded = s.get("j")
    assert isinstance(loaded, ScheduledJob)
    assert isinstance(loaded.trigger, CronTrigger)
    assert loaded.payload == {"k": 1}


# --- the cross-process proof ----------------------------------------------


def test_two_processes_exactly_one_claims(tmp_path: Path) -> None:
    """Two REAL daemon processes race to claim one due job; exactly one wins.

    Reliability (lesson from the chat-lock de-flake): no timing assumption. The
    winner HOLDS (stays alive, lease live) until the parent observes that every
    loser has provably bounced; only then does the winner exit. A starved loser
    just makes the parent wait — it can never find the lease stale while the
    winner lives, so it can never become a false second winner.
    """
    scheduler_dir = tmp_path / ".alkera" / "scheduler"
    SchedulerStore(scheduler_dir).register(new_job("race", IntervalTrigger(seconds=1), now=T0))

    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    bounced_dir = tmp_path / "bounced"
    bounced_dir.mkdir()
    go_file = tmp_path / "GO"
    winner_file = tmp_path / "winner"
    release_file = tmp_path / "RELEASE"

    script = textwrap.dedent(
        """
        import json, os, sys, time
        from datetime import UTC, datetime
        from pathlib import Path
        from alkera_cli.plugins.plugin_base.scheduler import SchedulerStore

        (sched_dir, go_path, ready_path, bounced_dir,
         winner_path, release_path) = sys.argv[1:7]

        Path(ready_path).touch()
        gp = Path(go_path)
        while not gp.exists():
            time.sleep(0.005)

        store = SchedulerStore(Path(sched_dir))
        # Far-future 'now' so the job is unambiguously due.
        job = store.claim("race", now=datetime(2030, 1, 1, tzinfo=UTC))
        if job is None:
            Path(bounced_dir, str(os.getpid())).write_text("none")
            print(json.dumps({"result": "none"}), flush=True)
            sys.exit(0)
        print(json.dumps({"result": "claimed", "pid": os.getpid()}), flush=True)
        Path(winner_path).write_text(str(os.getpid()))
        rp = Path(release_path)
        deadline = time.monotonic() + 120.0
        while not rp.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        """
    )

    n = 2
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(scheduler_dir),
                str(go_file),
                str(ready_dir / str(i)),
                str(bounced_dir),
                str(winner_file),
                str(release_file),
            ],
            env={**os.environ},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(n)
    ]

    import time as _time

    def _await(cond, *, timeout: float, message: str) -> None:  # type: ignore[no-untyped-def]
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if cond():
                return
            _time.sleep(0.02)
        raise AssertionError(message)

    _await(lambda: len(list(ready_dir.iterdir())) == n, timeout=60.0, message="children not ready")
    go_file.touch()
    _await(lambda: winner_file.exists(), timeout=60.0, message="no claimer reported winning")
    _await(
        lambda: len(list(bounced_dir.iterdir())) == n - 1,
        timeout=60.0,
        message="loser never bounced while winner held the claim",
    )
    release_file.touch()

    outputs = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=60)
        assert p.returncode == 0, f"subprocess crashed: {stderr}"
        for line in stdout.splitlines():
            if line.strip():
                outputs.append(json.loads(line))

    claimed = [o for o in outputs if o["result"] == "claimed"]
    none = [o for o in outputs if o["result"] == "none"]
    assert len(claimed) == 1, f"expected exactly one claimer, got {outputs}"
    assert len(none) == n - 1, f"expected {n - 1} bounced, got {outputs}"


def test_reschedule_soon_makes_an_already_run_job_due_now(tmp_path: Path) -> None:
    # The file-watcher nudge: an already-run recurring job (next run a cadence away) is pulled
    # forward to NOW so the next beat re-claims it. Unlike prime_due, it works AFTER the job has
    # run; unlike run_now, it doesn't force (the beat-claimed run still hits the skip-gate).
    s = _store(tmp_path)
    s.register(new_job("j", IntervalTrigger(seconds=900), now=T0))
    s.claim("j", now=T0)
    done = s.complete("j", now=T0, ok=True)  # → scheduled, next_run_at = T0+900, last_run_at set
    assert done is not None and done.next_run_at == T0 + timedelta(seconds=900)

    later = T0 + timedelta(seconds=60)
    nudged = s.reschedule_soon("j", now=later)
    assert nudged is not None and nudged.next_run_at == later  # pulled forward
    reread = s.get("j")
    assert reread is not None and reread.last_run_at == T0  # still already-run (prime_due refuses)


def test_reschedule_soon_marks_a_running_job_to_rerun(tmp_path: Path) -> None:
    # A nudge arriving mid-run must not disturb the in-flight run, but must NOT be lost: it sets
    # a one-shot rerun marker (reschedule_soon still returns None while running) so completion
    # re-runs the job promptly instead of stranding the mid-run work until the next cadence.
    s = _store(tmp_path)
    s.register(new_job("j", IntervalTrigger(seconds=900), now=T0))
    s.claim("j", now=T0)  # → running
    assert s.reschedule_soon("j", now=T0 + timedelta(seconds=5)) is None
    running = s.get("j")
    assert running is not None and running.state == "running"
    assert running.metadata.get("rerun_requested") is True  # signal kept, not dropped


def test_complete_reruns_now_when_nudged_mid_run_then_returns_to_cadence(tmp_path: Path) -> None:
    # The robustness payoff: a job nudged while running becomes due NOW on completion (not at the
    # cadence), and the one-shot marker is cleared so the run after it returns to normal cadence.
    s = _store(tmp_path)
    s.register(new_job("j", IntervalTrigger(seconds=900), now=T0))
    s.claim("j", now=T0)  # → running
    s.reschedule_soon("j", now=T0 + timedelta(seconds=5))  # nudge mid-run → marker set

    done = s.complete("j", now=T0 + timedelta(seconds=10), ok=True)
    assert done is not None
    assert done.next_run_at == T0 + timedelta(seconds=10)  # due NOW, not +900
    assert not done.metadata.get("rerun_requested")  # one-shot marker cleared

    # The re-run completes with NO new nudge → back to the normal cadence.
    s.claim("j", now=T0 + timedelta(seconds=10))
    done2 = s.complete("j", now=T0 + timedelta(seconds=10), ok=True)
    assert done2 is not None and done2.next_run_at == T0 + timedelta(seconds=910)  # cadence


def test_a_cancel_overrides_a_pending_rerun(tmp_path: Path) -> None:
    # The "I cancelled but it came right back" glitch: a nudge set rerun_requested mid-run, then
    # the user CANCELLED. The cancel must STICK — reschedule for the cadence, NOT due-now — else
    # the just-cancelled job restarts on the next beat and the cancel looks ignored.
    s = _store(tmp_path)
    s.register(new_job("j", IntervalTrigger(seconds=900), now=T0))
    s.claim("j", now=T0)  # → running
    s.reschedule_soon("j", now=T0 + timedelta(seconds=5))  # nudge mid-run → rerun marker set

    done = s.complete("j", now=T0 + timedelta(seconds=10), ok=False, error="cancelled")
    assert done is not None
    assert done.last_outcome == "cancelled"
    assert done.last_error is None  # a Cancel is intentional, NOT a red-alert error
    assert done.next_run_at == T0 + timedelta(seconds=910)  # cadence, NOT due-now
    assert not done.metadata.get("rerun_requested")  # one-shot marker cleared, not carried over


def test_claim_clears_the_previous_runs_outcome_and_error(tmp_path: Path) -> None:
    # A RUNNING job must not show a stale outcome badge OR red error ("it says cancelled on the
    # running job"): claiming for a NEW run drops the previous run's outcome/message/error;
    # last_run_at stays for "ran Xm ago".
    s = _store(tmp_path)
    s.register(new_job("j", IntervalTrigger(seconds=10), now=T0))
    s.claim("j", now=T0)
    s.complete("j", now=T0, ok=False, error="boom")  # a real failure → outcome + error set
    after = s.get("j")
    assert after is not None and after.last_outcome == "failed" and after.last_error == "boom"

    running = s.claim("j", now=T0 + timedelta(seconds=20))  # next run
    assert running is not None and running.state == "running"
    assert running.last_outcome is None and running.last_message is None  # stale badge gone
    assert running.last_error is None  # stale red error gone too
    assert running.last_run_at == T0  # "ran Xm ago" still true


def test_a_due_running_job_with_a_live_lease_stays_running(tmp_path: Path) -> None:
    # The lifecycle guarantee for a long (hours-long, spellbook-scale) seed: a job that's RUNNING
    # with a heartbeated (live) lease is NEVER reset or re-claimed when its own schedule fires
    # again — it stays running to completion. (Paired with the raised subprocess cap, this is
    # what stops a long KB/column seed being killed + restarted by its hourly cadence.)
    s = _store(tmp_path, ttl=60.0)
    s.register(new_job("j", IntervalTrigger(seconds=10), now=T0))
    claimed = s.claim("j", now=T0)  # → running, lease heartbeat_at=T0
    assert claimed is not None and claimed.state == "running"

    # Its 10s cadence fired long ago, but the lease is owned by THIS (alive, same-host) process,
    # so even hours later — well past the 60s TTL — the job is neither re-claimable nor reset:
    # for a same-host lease, staleness is PID-LIVENESS, not the clock. So a multi-hour seed on a
    # healthy daemon runs to completion; only a DEAD daemon's job is reclaimed (covered by the
    # reset-orphaned-on-dead-pid tests above).
    for t in (T0 + timedelta(seconds=30), T0 + timedelta(hours=2)):
        assert s.claim("j", now=t, respect_coalesce=True) is None  # live lease → not re-claimed
        assert s.reset_orphaned_running(now=t) == []  # PID alive → not orphaned → left running
        still = s.get("j")
        assert still is not None and still.state == "running"


def test_reschedule_soon_none_for_missing_or_non_recurring(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.reschedule_soon("nope", now=T0) is None  # missing
    # An event-driven job has no next fire time → not re-schedulable.
    s.register(new_job("ev", EventTrigger(event="x"), now=T0))
    assert s.reschedule_soon("ev", now=T0) is None


def test_reschedule_soon_is_idempotent_once_due(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.register(new_job("j", IntervalTrigger(seconds=900), now=T0))  # next_run_at = T0+900
    first = s.reschedule_soon("j", now=T0 + timedelta(seconds=60))  # → next_run_at = T0+60
    assert first is not None and first.next_run_at == T0 + timedelta(seconds=60)
    # Already due → returns the job without moving next_run_at again.
    second = s.reschedule_soon("j", now=T0 + timedelta(seconds=120))
    assert second is not None and second.next_run_at == T0 + timedelta(seconds=60)


def test_reset_orphaned_running_clears_a_dead_daemons_phantom(tmp_path: Path) -> None:
    # The "close + reopen the IDE" bug: a job left running by a DEAD daemon (stale lease) is a
    # phantom — it shows as Running but no one runs it, and coalescing keeps it deferred behind
    # a live sibling forever. The crash-recovery sweep resets it to scheduled + due; the live
    # sibling (this process) is left alone.
    s = _store(tmp_path)
    here = socket.gethostname()
    live = new_job("live", IntervalTrigger(seconds=900), now=T0, kind="dbt_refresh")
    live.state = "running"
    live.lease = JobLease(pid=os.getpid(), host=here, claimed_at=T0, heartbeat_at=T0)
    s.register(live)
    phantom = new_job("phantom", IntervalTrigger(seconds=900), now=T0, kind="dbt_refresh")
    phantom.state = "running"
    phantom.lease = JobLease(pid=_dead_pid(), host=here, claimed_at=T0, heartbeat_at=T0)
    s.register(phantom)

    now = T0 + timedelta(seconds=1)
    assert s.reset_orphaned_running(now=now) == ["phantom"]  # only the dead-daemon one
    p = s.get("phantom")
    assert p is not None and p.state == "scheduled" and p.lease is None and p.next_run_at == now
    live_after = s.get("live")
    assert live_after is not None and live_after.state == "running"  # the live daemon's job stays


def test_reset_orphaned_standing_metered_job_stays_idle_never_auto_runs(tmp_path: Path) -> None:
    # The cost-tier safety invariant: a METERED (manual, event-triggered) job interrupted by a
    # daemon crash must NOT be re-armed due-now — else the next beat would AUTO-RUN a billed
    # warehouse query on restart, with no user action. Crash recovery clears the phantom but
    # leaves it IDLE (next_run_at=None) so it can only run via an explicit Run-now.
    s = _store(tmp_path)
    here = socket.gethostname()
    metered = new_job(
        "snowflake_access:metered:snow",
        EventTrigger(event="snowflake_access:metered"),
        now=T0,
        kind="snowflake_access:metered",
        payload={"connection": "snow", "metered": True},
    )
    metered.state = "running"  # was mid-run when the daemon died
    metered.lease = JobLease(pid=_dead_pid(), host=here, claimed_at=T0, heartbeat_at=T0)
    s.register(metered)

    now = T0 + timedelta(seconds=1)
    assert s.reset_orphaned_running(now=now) == ["snowflake_access:metered:snow"]
    job = s.get("snowflake_access:metered:snow")
    assert job is not None
    assert job.state == "scheduled" and job.lease is None  # phantom cleared
    assert job.next_run_at is None  # NOT due-now — the metered source can't auto-run
    # the beat will never claim it, even far in the future (is_due needs a time).
    assert not job.is_due(now=now + timedelta(days=365), ttl_seconds=60.0)
    # but the user can still Run-now it (claim succeeds from idle scheduled).
    assert s.claim("snowflake_access:metered:snow", now=now) is not None
