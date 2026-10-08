"""The worker's command line (``python -m worker``; the composition root dispatches here).

    python -m worker run [--queues money,email,sync,default] [--health-host H]
                         [--health-port N] [--no-schedule-sync]
    python -m worker schedules sync [--dry-run]
    python -m worker schedules list
    python -m worker files gc [--dry-run]
    python -m worker account reerase
    python -m worker health [--host H] [--port N]

``run`` serves the task queues; ``schedules`` reconciles or lists the catalog
against the server; ``files gc`` runs the daily Files collection once, now,
through the same workflow the schedule starts (so an operator reclaiming a
bucket by hand and the nightly tick are the same code path, and ``--dry-run``
reports what a real pass would take without moving a byte); ``account reerase``
erases again every identity the erasure ledger names that a restore brought
back (the restore runbook's step, run in-process so it works with Temporal
down); ``health`` probes
THIS container's liveness endpoint and exits 0 / 1 — the command every
container health check runs. The dispatcher
answers ``health`` from ``worker.health_probe`` before this module (and the
runtime it imports) is loaded; the subcommand is kept here so ``--help`` is
complete and the parser is one place.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from uuid import uuid4

from alkera_core.config import settings
from alkera_core.temporal import QUEUE_FOR, TaskQueue, WorkflowType, drain_workflow_id
from temporalio.client import Client

from worker import health_probe
from worker.schedules import ScheduleSyncReport, catalog, sync_schedules
from worker.temporal import runner
from worker.temporal.runner import TemporalUnavailableError


def _queue_list(raw: str) -> list[TaskQueue]:
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if not names:
        raise argparse.ArgumentTypeError("at least one queue is required")
    queues: list[TaskQueue] = []
    for name in names:
        try:
            queue = TaskQueue(name)
        except ValueError:
            valid = ", ".join(q.value for q in TaskQueue)
            raise argparse.ArgumentTypeError(f"unknown queue {name!r} (valid: {valid})") from None
        if queue not in queues:
            queues.append(queue)
    return queues


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m worker", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="serve task queues")
    run.add_argument(
        "--queues",
        type=_queue_list,
        default=_queue_list(settings.alkera_temporal_task_queues),
        help="comma-separated subset of money,email,sync,default "
        "(default: ALKERA_TEMPORAL_TASK_QUEUES)",
    )
    run.add_argument("--health-host", default=settings.alkera_worker_health_host)
    run.add_argument("--health-port", type=int, default=settings.alkera_worker_health_port)
    run.add_argument(
        "--no-schedule-sync",
        action="store_true",
        help="do not reconcile the schedule catalog at boot (default-queue workers do)",
    )

    schedules = sub.add_parser("schedules", help="reconcile or list the schedule catalog")
    schedules_sub = schedules.add_subparsers(dest="schedules_command", required=True)
    sync = schedules_sub.add_parser("sync", help="make the server match the catalog")
    sync.add_argument("--dry-run", action="store_true", help="report changes, write nothing")
    schedules_sub.add_parser("list", help="show every catalog entry's server state")

    files = sub.add_parser("files", help="run a Files job once, now")
    files_sub = files.add_subparsers(dest="files_command", required=True)
    files_gc = files_sub.add_parser(
        "gc", help="collect the bytes of dedup domains no drive row names"
    )
    files_gc.add_argument(
        "--dry-run", action="store_true", help="report what would be collected, move nothing"
    )
    files_gc.add_argument(
        "--allow-mass-collect",
        action="store_true",
        help="for this run only, collect even when the share of orphaned domains "
        "is over FILES_GC_MAX_ORPHAN_FRACTION",
    )
    files_adopt = files_sub.add_parser(
        "gc-adopt",
        help="stamp unmarked domain prefixes as this deployment's, so the collector may "
        "consider them",
    )
    files_adopt.add_argument(
        "--known", action="store_true", help="every domain this database's drive rows name"
    )
    files_adopt.add_argument(
        "--domain", action="append", default=[], help="one domain id; repeatable"
    )

    account = sub.add_parser("account", help="account lifecycle operations")
    account_sub = account.add_subparsers(dest="account_command", required=True)
    account_sub.add_parser(
        "reerase", help="re-erase every ledgered identity a database restore brought back"
    )

    health = sub.add_parser("health", help="probe this container's /health/live; exit 0 or 1")
    health.add_argument("--host", default=health_probe.default_host())
    health.add_argument("--port", type=int, default=health_probe.default_port())
    return parser


async def _connect() -> Client:
    return await runner.connect_with_retry(
        timeout_s=settings.alkera_worker_connect_timeout_seconds, component="worker-cli"
    )


async def _schedules_sync(*, dry_run: bool) -> ScheduleSyncReport:
    client = await _connect()
    return await sync_schedules(client, dry_run=dry_run)


async def _schedules_list() -> list[dict[str, object]]:
    client = await _connect()
    rows: list[dict[str, object]] = []
    for entry in catalog():
        row: dict[str, object] = {
            "id": entry.id,
            "workflow": entry.workflow.value,
            "spec": entry.spec_text,
            "queue": entry.queue.value,
        }
        try:
            described = await client.get_schedule_handle(entry.id).describe()
        except Exception as exc:
            row.update(state="missing", error=str(exc) or type(exc).__name__)
        else:
            nxt = described.info.next_action_times
            row.update(
                state="paused" if described.schedule.state.paused else "active",
                next_run=nxt[0].isoformat() if nxt else None,
                skipped_overlap=described.info.num_actions_skipped_overlap,
            )
        rows.append(row)
    return rows


async def _files_gc(*, dry_run: bool, allow_mass_collect: bool = False) -> dict[str, Any]:
    """Run the collection workflow once and answer with what it reported."""
    client = await _connect()
    result: dict[str, Any] = await client.execute_workflow(
        WorkflowType.FILES_GC.value,
        args=[None, dry_run, allow_mass_collect],
        id=f"{drain_workflow_id(WorkflowType.FILES_GC)}-manual-{uuid4().hex[:8]}",
        task_queue=QUEUE_FOR[WorkflowType.FILES_GC].value,
        execution_timeout=timedelta(hours=6),
    )
    return result


async def _files_gc_adopt(*, known: bool, domains: Sequence[str]) -> dict[str, Any]:
    """Stamp domain prefixes in-process: adopting is a store write, not a job."""
    from worker import files_bootstrap
    from worker.tasks import files as files_tasks

    if not files_bootstrap.wire_files_jobs():
        return {"error": "FILES_ENABLED is off; there is no bucket to adopt from"}
    report = await files_tasks.files_gc_adopt(tuple(domains), known=known)
    return {
        "stamped": list(report.stamped),
        "already_ours": list(report.already_ours),
        "foreign": list(report.foreign),
    }


def _print_sync_report(report: ScheduleSyncReport, *, dry_run: bool) -> None:
    prefix = "would " if dry_run else ""
    for label, ids in report.as_dict().items():
        # "pending" and "foreign" describe what the ids ARE, not something the
        # reconciler would do to them, so they never take the dry-run prefix.
        verb = label if label in {"pending", "foreign"} else f"{prefix}{label}"
        print(f"{verb:>14}: {', '.join(ids) if ids else '-'}")


def _print_schedule_rows(rows: Sequence[dict[str, object]]) -> None:
    for row in rows:
        print(json.dumps(row, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    # Only `run` configures logging (in the runner's boot): the one-off commands
    # print their result and leave the process's logging as they found it.
    if args.command == "run":
        return runner.main_run(
            args.queues,
            health_host=args.health_host,
            health_port=args.health_port,
            sync_schedules_on_boot=not args.no_schedule_sync,
        )
    if args.command == "schedules":
        try:
            if args.schedules_command == "sync":
                report = asyncio.run(_schedules_sync(dry_run=args.dry_run))
                _print_sync_report(report, dry_run=args.dry_run)
            else:
                _print_schedule_rows(asyncio.run(_schedules_list()))
        except TemporalUnavailableError as exc:
            # An operator's one-off command: a plain line, not a traceback.
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.command == "files" and args.files_command == "gc-adopt":
        if not args.known and not args.domain:
            print("error: name --known, --domain <id>, or both", file=sys.stderr)
            return 2
        adopted = asyncio.run(_files_gc_adopt(known=args.known, domains=args.domain))
        print(json.dumps(adopted, sort_keys=True))
        return 1 if "error" in adopted else 0
    if args.command == "files":
        try:
            outcome = asyncio.run(
                _files_gc(dry_run=args.dry_run, allow_mass_collect=args.allow_mass_collect)
            )
        except TemporalUnavailableError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        verb = "would park" if args.dry_run else "parked"
        verdict = outcome.get("verdict", "ok")
        print(
            json.dumps(
                {
                    "job": WorkflowType.FILES_GC.value,
                    "verdict": verdict,
                    verb: outcome.get("parked", 0),
                },
                sort_keys=True,
            )
        )
        # A refusal is the run declining to erase, and the operator has to see
        # it as one; a pass that is merely switched off is not an error.
        return 0 if verdict in ("ok", "disabled") else 3
    if args.command == "account":
        from alkera_core.account import ledger

        reerased = asyncio.run(ledger.reerase())
        print(
            json.dumps(
                {
                    "job": WorkflowType.ACCOUNT_REERASE.value,
                    "ledgered": reerased.ledgered,
                    "resurrected": reerased.resurrected,
                    "erased": [str(u) for u in reerased.erased],
                    "failed": [str(u) for u in reerased.failed],
                },
                sort_keys=True,
            )
        )
        return 1 if reerased.failed else 0
    if args.command == "health":
        code, body = health_probe.probe_health(args.host, args.port)
        print(body)
        return 0 if code == 200 else 1
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
