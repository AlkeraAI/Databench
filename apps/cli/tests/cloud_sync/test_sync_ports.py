"""The open cloud-sync engine and its ports, on their own and with a stand-in
registered: what the open platform does when no distribution supplies a lane,
a member's team actions, a lease cache or a box's connections, and what it
hands to the one that does."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.cloud_sync.job import (
    CLOUD_SYNC_KIND,
    LaneRun,
    SyncLane,
    lane_job_id,
    make_cloud_sync_runner,
    nudge_after_sign_in,
    schedule_cloud_sync,
)
from alkera_cli.cloud_sync.team_actions import (
    NO_TEAM_CONNECTIONS,
    TeamConnectionActionError,
    TeamConnectionActions,
    team_connection_actions,
)
from alkera_cli.plugins.plugin_base.scheduler import (
    IntervalTrigger,
    Scheduler,
    SchedulerStore,
    new_job,
)
from alkera_core.extensions import ExtensionError, ExtensionPoint
from alkera_core.project.directory import ProjectDirectory

#: Ahead of the wall clock, so a sign-in nudge (which reads the real clock) is
#: the only thing that can make a lane due.
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


def _scheduler(tmp_path: Path) -> tuple[Scheduler, ProjectDirectory]:
    project = ProjectDirectory(tmp_path / ".alkera")
    return Scheduler(SchedulerStore(project.scheduler_path)), project


def _lane_job(name: str) -> Any:
    return new_job(
        lane_job_id(name),
        IntervalTrigger(seconds=60),
        now=NOW,
        kind=CLOUD_SYNC_KIND,
        payload={"lane": name},
    )


def _recording_lane(
    name: str, runs: list[tuple[str, LaneRun]], *, due_on_open: bool, after_sign_in: bool = False
) -> SyncLane:
    async def run(lane_run: LaneRun) -> None:
        runs.append((name, lane_run))

    return SyncLane(
        name=name,
        cadence_seconds=600.0,
        due_on_open=due_on_open,
        run=run,
        after_sign_in=after_sign_in,
    )


# -- the lanes -----------------------------------------------------------------------


async def test_with_no_lane_registered_the_job_schedules_nothing(tmp_path: Path) -> None:
    """The open platform alone ships no lane: registration arms no job and a
    persisted lane job from a product build is answered without running."""
    scheduler, project = _scheduler(tmp_path)
    point: ExtensionPoint[SyncLane] = ExtensionPoint("lanes")

    assert schedule_cloud_sync(scheduler, project, now=NOW, point=point) == CLOUD_SYNC_KIND
    assert scheduler.list_jobs() == []

    runner = make_cloud_sync_runner(project, point=point)
    await runner(_lane_job("connections"))  # a product build's lane: logged, not run


async def test_a_registered_lane_is_scheduled_on_its_cadence_and_primed_when_due_on_open(
    tmp_path: Path,
) -> None:
    scheduler, project = _scheduler(tmp_path)
    runs: list[tuple[str, LaneRun]] = []
    point: ExtensionPoint[SyncLane] = ExtensionPoint("lanes")
    point.register(_recording_lane("fresh", runs, due_on_open=True))
    point.register(_recording_lane("sweep", runs, due_on_open=False))

    schedule_cloud_sync(scheduler, project, now=NOW, point=point)

    by_id = {job.job_id: job for job in scheduler.list_jobs()}
    assert set(by_id) == {lane_job_id("fresh"), lane_job_id("sweep")}
    assert by_id[lane_job_id("fresh")].next_run_at == NOW
    assert by_id[lane_job_id("sweep")].next_run_at == NOW + timedelta(seconds=600)
    assert by_id[lane_job_id("sweep")].payload == {"lane": "sweep"}


async def test_the_runner_hands_the_named_lane_its_project_and_the_announcer(
    tmp_path: Path,
) -> None:
    _, project = _scheduler(tmp_path)
    runs: list[tuple[str, LaneRun]] = []
    point: ExtensionPoint[SyncLane] = ExtensionPoint("lanes")
    point.register(_recording_lane("one", runs, due_on_open=True))
    point.register(_recording_lane("two", runs, due_on_open=True))

    def announce(record_id: str, removed: bool) -> None:
        return None

    runner = make_cloud_sync_runner(project, announce=announce, point=point)
    await runner(_lane_job("two"))
    await runner(_lane_job("nobody"))

    assert [(name, run.project.path, run.announce) for name, run in runs] == [
        ("two", project.path, announce)
    ]


async def test_two_lanes_under_one_name_are_refused(tmp_path: Path) -> None:
    """They would share one job id, and one would never run."""
    scheduler, project = _scheduler(tmp_path)
    runs: list[tuple[str, LaneRun]] = []
    point: ExtensionPoint[SyncLane] = ExtensionPoint("lanes")
    point.register(_recording_lane("same", runs, due_on_open=True))
    point.register(_recording_lane("same", runs, due_on_open=False))

    with pytest.raises(ExtensionError, match="'same'"):
        schedule_cloud_sync(scheduler, project, now=NOW, point=point)


async def test_a_sign_in_makes_due_only_the_lanes_that_follow_it(tmp_path: Path) -> None:
    scheduler, project = _scheduler(tmp_path)
    runs: list[tuple[str, LaneRun]] = []
    point: ExtensionPoint[SyncLane] = ExtensionPoint("lanes")
    point.register(_recording_lane("follows", runs, due_on_open=False, after_sign_in=True))
    point.register(_recording_lane("ignores", runs, due_on_open=False))
    schedule_cloud_sync(scheduler, project, now=NOW, point=point)

    nudge_after_sign_in(scheduler, point=point)

    due = {job.job_id for job in scheduler.list_jobs() if job.next_run_at <= datetime.now(UTC)}
    assert due == {lane_job_id("follows")}


# -- a member's team actions -----------------------------------------------------------


class _Actions:
    def __init__(self) -> None:
        self.removed: list[str] = []

    async def accept(
        self,
        project: ProjectDirectory,
        registry: Any,
        record_id: str,
        *,
        member_values: dict[str, str] | None = None,
    ) -> Any:
        return f"accepted {record_id}"

    async def authorize(
        self,
        project: ProjectDirectory,
        registry: Any,
        record_id: str,
        *,
        open_browser: Callable[[str], None] | None = None,
    ) -> Any:
        return f"authorized {record_id}"

    def set_muted(self, project: ProjectDirectory, record_id: str, muted: bool) -> bool:
        return True

    def remove(self, project: ProjectDirectory, record_id: str) -> bool:
        self.removed.append(record_id)
        return True

    def dismiss(self, project: ProjectDirectory, record_id: str) -> bool:
        return True


async def test_with_no_team_connections_an_accept_is_refused_and_nothing_changes(
    tmp_path: Path,
) -> None:
    project = ProjectDirectory(tmp_path / ".alkera")
    actions = team_connection_actions(ExtensionPoint("actions"))

    with pytest.raises(TeamConnectionActionError, match=NO_TEAM_CONNECTIONS):
        await actions.accept(project, None, "r1")
    with pytest.raises(TeamConnectionActionError, match=NO_TEAM_CONNECTIONS):
        await actions.authorize(project, None, "r1")
    assert actions.set_muted(project, "r1", True) is False
    assert actions.remove(project, "r1") is False
    assert actions.dismiss(project, "r1") is False


async def test_the_registered_team_actions_are_the_ones_carried_out(tmp_path: Path) -> None:
    project = ProjectDirectory(tmp_path / ".alkera")
    registered = _Actions()
    point: ExtensionPoint[TeamConnectionActions] = ExtensionPoint("actions")
    point.register(registered)
    actions = team_connection_actions(point)

    assert await actions.accept(project, None, "r1") == "accepted r1"
    assert actions.remove(project, "r2") is True
    assert registered.removed == ["r2"]


def test_two_team_action_implementations_are_refused() -> None:
    point: ExtensionPoint[TeamConnectionActions] = ExtensionPoint("actions")
    point.register(_Actions())
    point.register(_Actions())
    with pytest.raises(ExtensionError):
        team_connection_actions(point)
