"""The editor daemon's per-project runtime pool.

One production runtime per project path, built on the first RPC that touches
the project and reused by every later one.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from alkera_cli.app.compose import production_runtime
from alkera_cli.harness.extension_points import prepare_opened_project
from alkera_cli.harness.runtime import HarnessRuntime

if TYPE_CHECKING:
    from alkera_cli.daemon.server import JsonRpcServer


def runtime_for(server: JsonRpcServer, project_path: str) -> HarnessRuntime:
    registry: dict[str, HarnessRuntime] | None = getattr(server, "harness_runtimes", None)
    if registry is None:
        registry = {}
        server.harness_runtimes = registry  # type: ignore[attr-defined]
    abs_path = str(Path(project_path).resolve())
    if abs_path not in registry:
        # Same shared runtime the CLI uses: route opencode (agent_config) AND
        # Claude Code (claude_env) through the gateway, rebuilding each from the
        # chat's pinned model + a fresh token at every open (the token is never
        # persisted to the manifest).
        runtime = production_runtime("daemon", Path(abs_path))
        registry[abs_path] = runtime
        # First open in this daemon: register the project's standing KB jobs so the
        # scheduler beat self-seeds + prunes the knowledge base without a manual
        # trigger. Best-effort + idempotent across re-opens/restarts; being inside
        # the daemon-only ``runtime_for`` IS the running-daemon guard (the CLI path
        # builds its own runtime and has no beat to run them).
        with contextlib.suppress(Exception):
            runtime.schedule_context_jobs()
        # Bring the project's stores to head eagerly so they are ready the moment
        # a producer writes (idempotent; each store's lazy open also covers the CLI
        # path, which never reaches this daemon-only init).
        prepare_opened_project(runtime.project)
        # Eagerly build the plugin registry in the BACKGROUND so the connector REFRESH
        # jobs (dbt_refresh, …) are scheduled the moment the daemon first touches the
        # project — on ANY RPC (the Background Jobs view's scheduler.list, an auth poll),
        # NOT only when the user opens a chat or the Plugins & Connections page. The build
        # (discover + activate) runs off the event loop inside plugin_registry(), which
        # calls schedule_refresh + primes the jobs due-now so the beat starts them.
        _kick_eager_registry_build(server, runtime)
    return registry[abs_path]


def _kick_eager_registry_build(server: JsonRpcServer, runtime: HarnessRuntime) -> None:
    """Fire-and-forget the registry build so refresh jobs schedule without manual
    interaction. Tracked on the server so the task isn't GC'd; best-effort (no running
    loop → skip; the next RPC that needs the registry builds it lazily as before)."""

    async def _build() -> None:
        with contextlib.suppress(Exception):
            await runtime.plugin_registry()
        # Start the file watcher once the registry (hence the connections) is known, so a
        # source-file edit re-seeds without manual interaction. OUTSIDE the registry build —
        # restart_file_watcher re-reads the (now cached) registry, so it can't re-enter the
        # init lock. Mirrors the CLI, which starts it from its beat loop.
        with contextlib.suppress(Exception):
            await runtime.restart_file_watcher()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    tasks: set[asyncio.Task[None]] = getattr(server, "_eager_registry_tasks", None) or set()
    task = loop.create_task(_build())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    server._eager_registry_tasks = tasks  # type: ignore[attr-defined]
