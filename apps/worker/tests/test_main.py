"""``python -m worker``: argument parsing, dispatch, and the health probe's exit code."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from alkera_core.config import settings
from alkera_core.temporal import TaskQueue
from worker import cli, health_probe
from worker.schedules import SCHEDULES, ScheduleSyncReport
from worker.temporal import runner
from worker.temporal.runner import HealthServer, TemporalUnavailableError


def test_run_defaults_come_from_settings() -> None:
    args = cli.build_parser().parse_args(["run"])
    assert args.queues == [TaskQueue(q) for q in settings.temporal_task_queue_list]
    assert args.health_host == settings.alkera_worker_health_host
    assert args.health_port == settings.alkera_worker_health_port
    assert args.no_schedule_sync is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("sync,money", [TaskQueue.SYNC, TaskQueue.MONEY], id="two-ordered"),
        pytest.param(" default ", [TaskQueue.DEFAULT], id="trimmed"),
        pytest.param("money,money,email", [TaskQueue.MONEY, TaskQueue.EMAIL], id="deduped"),
    ],
)
def test_run_queues_are_parsed(raw: str, expected: list[TaskQueue]) -> None:
    assert cli.build_parser().parse_args(["run", "--queues", raw]).queues == expected


@pytest.mark.parametrize(
    "raw", ["payments", "money,Sync", "", ","], ids=["unknown", "case", "empty", "comma"]
)
def test_an_unknown_or_empty_queue_list_is_a_usage_error(
    raw: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["run", "--queues", raw])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "queue" in err


def test_no_arguments_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2


def test_run_dispatches_to_the_runner_with_the_parsed_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_main_run(queues: list[TaskQueue], **kwargs: Any) -> int:
        calls.append({"queues": queues, **kwargs})
        return 7

    monkeypatch.setattr(runner, "main_run", fake_main_run)
    code = cli.main(
        [
            "run",
            "--queues",
            "sync,default",
            "--health-host",
            "0.0.0.0",
            "--health-port",
            "0",
            "--no-schedule-sync",
        ]
    )
    assert code == 7
    assert calls == [
        {
            "queues": [TaskQueue.SYNC, TaskQueue.DEFAULT],
            "health_host": "0.0.0.0",
            "health_port": 0,
            "sync_schedules_on_boot": False,
        }
    ]


def test_run_syncs_schedules_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(runner, "main_run", lambda q, **kw: seen.update(kw) or 0)
    assert cli.main(["run"]) == 0
    assert seen["sync_schedules_on_boot"] is True


# --- schedules -----------------------------------------------------------------


class _FakeSchedulesClient:
    """Answers describe() for a chosen subset of ids and refuses every write."""

    def __init__(self, present: set[str]) -> None:
        self.present = present

    def get_schedule_handle(self, id: str) -> Any:
        client = self

        class _Handle:
            async def describe(self) -> Any:
                if id not in client.present:
                    raise RuntimeError("schedule not found")
                return SimpleNamespace(
                    schedule=SimpleNamespace(state=SimpleNamespace(paused=id.endswith("prices"))),
                    info=SimpleNamespace(
                        next_action_times=[datetime(2026, 9, 6, 1, 0, tzinfo=UTC)],
                        num_actions_skipped_overlap=2,
                    ),
                )

        return _Handle()

    async def create_schedule(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a read-only command must not create schedules")


@pytest.fixture
def fake_connect(monkeypatch: pytest.MonkeyPatch) -> _FakeSchedulesClient:
    fake = _FakeSchedulesClient(present={"roll-due-cycles", "reconcile-prices"})

    async def _connect() -> _FakeSchedulesClient:
        return fake

    monkeypatch.setattr(cli, "_connect", _connect)
    return fake


def test_schedules_sync_dry_run_prints_the_report_and_passes_the_flag(
    monkeypatch: pytest.MonkeyPatch,
    fake_connect: _FakeSchedulesClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, Any] = {}

    async def fake_sync(client: Any, *, dry_run: bool) -> ScheduleSyncReport:
        seen["client"] = client
        seen["dry_run"] = dry_run
        return ScheduleSyncReport(
            created=("roll-due-cycles",),
            unchanged=("reconcile-prices",),
            pending=("deployment-health",),
            foreign=("sweep-gate-checks",),
        )

    monkeypatch.setattr(cli, "sync_schedules", fake_sync)
    assert cli.main(["schedules", "sync", "--dry-run"]) == 0
    assert seen == {"client": fake_connect, "dry_run": True}
    out = capsys.readouterr().out
    assert "would created: roll-due-cycles" in out
    assert "would unchanged: reconcile-prices" in out
    assert "pending: deployment-health" in out
    # A state, not an action the reconciler would take: no "would".
    assert "foreign: sweep-gate-checks" in out
    assert "would foreign" not in out
    assert "would deleted: -" in out


def test_schedules_sync_without_the_flag_is_a_real_sync(
    monkeypatch: pytest.MonkeyPatch,
    fake_connect: _FakeSchedulesClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, Any] = {}

    async def fake_sync(client: Any, *, dry_run: bool) -> ScheduleSyncReport:
        seen["dry_run"] = dry_run
        return ScheduleSyncReport(updated=("roll-due-cycles",))

    monkeypatch.setattr(cli, "sync_schedules", fake_sync)
    assert cli.main(["schedules", "sync"]) == 0
    assert seen == {"dry_run": False}
    assert "       updated: roll-due-cycles" in capsys.readouterr().out


def test_schedules_list_reports_every_catalog_entry(
    fake_connect: _FakeSchedulesClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["schedules", "list"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["id"] for r in rows] == [e.id for e in SCHEDULES]
    by_id = {r["id"]: r for r in rows}
    assert by_id["roll-due-cycles"] == {
        "id": "roll-due-cycles",
        "workflow": "billing.roll_due_cycles",
        "spec": "10 * * * *",
        "queue": "money",
        "state": "active",
        "next_run": "2026-09-06T01:00:00+00:00",
        "skipped_overlap": 2,
    }
    assert by_id["reconcile-prices"]["state"] == "paused"
    assert by_id["process-stripe-events"]["state"] == "missing"
    assert by_id["process-stripe-events"]["spec"] == "every 30s"
    assert "error" in by_id["process-stripe-events"]


@pytest.mark.parametrize(
    "argv", [["schedules", "sync"], ["schedules", "list"]], ids=["sync", "list"]
)
def test_schedules_commands_report_an_unreachable_server_in_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    async def _connect() -> Any:
        raise TemporalUnavailableError("Temporal at nowhere:7233 did not answer within 3s")

    monkeypatch.setattr(cli, "_connect", _connect)
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "error: Temporal at nowhere:7233 did not answer within 3s"
    assert "Traceback" not in captured.err
    assert captured.out == ""


# --- health ----------------------------------------------------------------------


async def test_health_exits_zero_only_while_every_queue_is_running(
    capsys: pytest.CaptureFixture[str],
) -> None:
    states = {"money": "running", "email": "running"}
    server = HealthServer(host="127.0.0.1", port=0, liveness=lambda: states)
    await server.start()
    try:
        port = server.port
        assert (
            await asyncio.to_thread(
                cli.main, ["health", "--host", "127.0.0.1", "--port", str(port)]
            )
            == 0
        )
        assert '"status": "ok"' in capsys.readouterr().out
        states["email"] = "failed"
        assert (
            await asyncio.to_thread(
                cli.main, ["health", "--host", "127.0.0.1", "--port", str(port)]
            )
            == 1
        )
        assert '"degraded"' in capsys.readouterr().out
    finally:
        await server.stop()
    # A stopped server (nothing listening) is unhealthy too.
    assert (
        await asyncio.to_thread(cli.main, ["health", "--host", "127.0.0.1", "--port", str(port)])
        == 1
    )


def test_health_defaults_come_from_settings() -> None:
    args = cli.build_parser().parse_args(["health"])
    assert args.host == settings.alkera_worker_health_host
    assert args.port == settings.alkera_worker_health_port


def test_health_probe_loads_no_runtime_module() -> None:
    """ECS runs ``python -m worker health`` in a fresh interpreter every 30 s
    with a 5 s budget on a 0.5 vCPU task that is also running the worker.
    Importing the worker runtime for one GET blew that budget in production
    and got healthy tasks replaced. Pin the real ``-m`` entry: after a probe,
    alkera_core, temporalio, sqlalchemy and worker.temporal are unloaded."""
    script = textwrap.dedent(
        """
        import json, runpy, sys
        sys.argv = ["worker", "health", "--host", "127.0.0.1", "--port", "1"]
        try:
            runpy.run_module("worker", run_name="__main__", alter_sys=True)
        except SystemExit as exc:
            rc = exc.code
        heavy = sorted(
            m for m in sys.modules
            if m.split(".")[0] in {"alkera_core", "temporalio", "sqlalchemy"}
            or m.startswith("worker.temporal") or m == "worker.cli"
        )
        print(json.dumps({"rc": rc, "heavy": heavy}))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["rc"] == 1, proc.stdout  # nothing listens on port 1
    assert result["heavy"] == []


def test_health_probe_defaults_track_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe cannot read Settings (that import is the cost it exists to
    avoid), so it mirrors the two fields: same defaults, same env names."""
    fields = type(settings).model_fields
    assert health_probe.DEFAULT_HOST == fields["alkera_worker_health_host"].default
    assert health_probe.DEFAULT_PORT == fields["alkera_worker_health_port"].default
    monkeypatch.setenv("ALKERA_WORKER_HEALTH_HOST", "0.0.0.0")
    monkeypatch.setenv("ALKERA_WORKER_HEALTH_PORT", "9123")
    args = health_probe.build_parser().parse_args([])
    assert (args.host, args.port) == ("0.0.0.0", 9123)
    assert cli.build_parser().parse_args(["health"]).port == 9123
    monkeypatch.setenv("ALKERA_WORKER_HEALTH_PORT", "not-a-port")
    assert health_probe.default_port() == health_probe.DEFAULT_PORT


@pytest.mark.parametrize(
    ("failed", "code"), [pytest.param([], 0, id="clean"), pytest.param(["x"], 1, id="a-failure")]
)
def test_account_reerase_runs_the_ledger_pass_and_reports_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failed: list[str],
    code: int,
) -> None:
    import uuid

    from alkera_core.account import ledger

    erased = uuid.uuid4()

    async def fake_reerase() -> ledger.ReerasureReport:
        return ledger.ReerasureReport(
            ledgered=3, resurrected=1, erased=[erased], failed=[uuid.uuid4() for _ in failed]
        )

    monkeypatch.setattr(ledger, "reerase", fake_reerase)
    assert cli.main(["account", "reerase"]) == code
    out = json.loads(capsys.readouterr().out)
    assert out["job"] == "account.reerase"
    assert (out["ledgered"], out["resurrected"], out["erased"]) == (3, 1, [str(erased)])
