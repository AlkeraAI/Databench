# apps/worker

The background worker. Every background job is a Temporal workflow that runs one activity, and the activity calls an async core in `worker/tasks/<family>.py`. One worker process serves one or more task queues, and periodic jobs are schedules on the Temporal server rather than a separate scheduler.

## Run locally

```bash
make infra-up        # Postgres, a local Temporal server and its UI, Mailpit, SeaweedFS
make dev-worker      # python -m worker run --queues money,email,sync,default
```

`make dev-all` runs it beside the backend, the gateway and the web app. The worker reads the same env files as the backend. The settings that matter to it are `TEMPORAL_ADDRESS` and `TEMPORAL_NAMESPACE` (plus `TEMPORAL_API_KEY` and the `TEMPORAL_TLS*` values for an external cluster), `ALKERA_TEMPORAL_TASK_QUEUES`, `ALKERA_WORKER_HEALTH_PORT`, `ALKERA_WORKER_MAX_CONCURRENT_ACTIVITIES` and `ALKERA_WORKER_CONNECT_TIMEOUT_SECONDS`.

## Command line

```
python -m worker run [--queues money,email,sync,default] [--health-host H] [--health-port N] [--no-schedule-sync]
python -m worker schedules sync [--dry-run]     # make the server match the schedule catalog
python -m worker schedules list                 # each catalog entry's state on the server
python -m worker files gc [--dry-run]           # run the Files collection once, now
python -m worker account reerase                # erase again identities a restore brought back
python -m worker health [--host H] [--port N]   # probe GET /health/live, exit 0 or 1
```

`run` connects (retrying for `ALKERA_WORKER_CONNECT_TIMEOUT_SECONDS`, then exiting 1), serves one Temporal worker per queue, and answers `GET /health/live` with 200 while every queue's poller runs and 503 once one has stopped. When it serves the `default` queue it reconciles the schedule catalog at boot. The container image runs `python -m worker run` with the health server on port 9000 and uses `python -m worker health` as its `HEALTHCHECK`. A deployment may run one service per queue or all four in one process.

`python -m worker` runs the worker with no extensions installed. A distribution that adds job families installs them before any command except `health`: each family registers its modules in `JOB_MODULES` (`worker/temporal/queues.py`) and its activity policies in `ACTIVITY_POLICY_SETS` (`worker/temporal/retry.py`). A queue that no registered job uses is logged as idle and not polled, and a `run` whose queues are all idle exits 1.

## Queues

The queue for each job is `QUEUE_FOR` in `alkera_core.temporal.contract`, and a test keeps it exhaustive over `WorkflowType`. The jobs this worker ships run on two of the four queues:

| Queue | Jobs |
| --- | --- |
| `money` | the compute meter, compute reconcile, organisation machine reconcile |
| `default` | auth, device code, login lockout and security event prunes; account export, lifecycle sweep and re-erase; Files promote, janitor, collection, ACL rewrite, large move, copy, bulk and queued-job recovery; the compute catalog and reachability sweep; notebook run sweep; chat spare reaping; workspace reconcile, deletion and machine moves; the entitlements watchdog; deployment health |

`email` and `sync` serve jobs that extensions register. With none installed they are idle.

## Layout

- `worker/tasks/<family>.py`: the async cores (logic and database), with no Temporal imports.
- `worker/activities/<family>.py`: `@activity.defn(name=...)` wrappers that take the job's advisory lock (`run_locked`) and call the core.
- `worker/workflows/<family>.py`: `@workflow.defn(name=...)`, one class per job.
- `worker/temporal/`: `retry.py` (an explicit policy for every activity), `interceptors.py` (transient and permanent failures, error reporting, log context), `queues.py` (the discovery registries), `runner.py` (boot, the health server, one worker per queue), `sandbox.py`.
- `worker/schedules.py`: the schedule catalog (`SCHEDULES`) and `sync_schedules()`.

## Adding a job

1. Write the async core in `worker/tasks/<family>.py`.
2. Add the job to `WorkflowType` and `QUEUE_FOR` in `packages/api-core/alkera_core/temporal/contract.py`.
3. Add the activity (`worker/activities/<family>.py`) and the workflow (`worker/workflows/<family>.py`) under that name. The registries in `worker/temporal/queues.py` find them by walking the modules.
4. Add its policy to the table in `worker/temporal/retry.py`. Use `NO_RETRY` when the activity is not idempotent.
5. If it is periodic, add a `SCHEDULES` entry in `worker/schedules.py`. If the backend starts it, add the call in `apps/backend/backend/services/infra/task_queue.py`.
6. Test the core in `apps/worker/tests/test_<family>.py` and the workflow through a real worker in `apps/worker/tests/test_<family>_workflow.py` (`@pytest.mark.temporal`, the `temporal_worker` fixture).

## Tests

```bash
make test-py
```

or, for this app only:

```bash
eval "$(bash ops/scripts/ensure-temporal-cli.sh)"; export ALKERA_TEMPORAL_BIN
uv run pytest apps/worker/tests
```

Tests marked `temporal` start a local Temporal dev server through the root `conftest.py` fixtures. The script finds or downloads the pinned `temporal` CLI.

## Restarting during development

The worker does not reload on change. Stop it with Ctrl-C and run `make dev-worker` again, or use a file watcher such as `watchexec -r -e py -- make dev-worker`.
