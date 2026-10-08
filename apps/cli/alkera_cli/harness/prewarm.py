"""First-chat pre-warm: pay the harness's one-time costs at daemon start.

The first chat on a fresh machine would otherwise pay two one-time costs inside
its own open: staging the bundled agent binary into
``~/.alkera/cache/runtime-<sha>/`` (a multi-hundred-MB copy), then opencode's
one-time database migration before its server binds. This module runs that
sequence once, in the background, at daemon startup: resolve (and stage) the
binary, launch it until it reports its listen URL (the migration runs), then
kill it. On a warm machine it is a cheap no-op.

The adapter awaits :func:`wait_for_prewarm` before spawning, so a chat opened
mid-migration waits instead of racing a second migrator against the same
database (the migration takes no cross-process lock). The wait is bounded; a
failed or wedged pre-warm never blocks a chat past the deadline.

On by default in a compiled binary, where the costs are real; a source checkout
and the e2e suites skip it. A caller whose whole job is running the agent (the
cloud mirror on a workspace box) asks for it explicitly.
``ALKERA_HARNESS_PREWARM=1|0`` forces it on or off and overrides the caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

from alkera_core.process import SpawnSpec, release, spawn_async

logger = logging.getLogger(__name__)

_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})
_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})

# One-shot module state: the background task and the "settled" latch the
# adapter waits on. Reset only by tests (via _reset_for_tests).
_task: asyncio.Task[None] | None = None
_settled: asyncio.Event | None = None


def _running_compiled() -> bool:
    """Inside a Nuitka build? Same ``__compiled__`` sentinel as
    ``alkera_cli.host.onefile_cache``."""
    return bool(getattr(sys.modules.get("__main__"), "__compiled__", None))


def prewarm_enabled(*, force: bool = False) -> bool:
    """Whether the startup pre-warm should run: an explicit
    ``ALKERA_HARNESS_PREWARM`` token wins both ways; otherwise a compiled
    binary pre-warms (a source daemon would spawn an extra agent on every dev
    restart for costs it doesn't pay), and so does a caller that asks for it.

    ``force`` is for a process whose whole job is answering questions with the
    agent — a workspace box — where paying those costs at boot is the point,
    however it was launched. The env token still wins, so a box can be told
    not to."""
    raw = (os.environ.get("ALKERA_HARNESS_PREWARM") or "").strip().lower()
    if raw in _TRUE_TOKENS:
        return True
    if raw in _FALSE_TOKENS:
        return False
    return force or _running_compiled()


def start_prewarm_in_background(*, force: bool = False) -> None:
    """Kick off the one-shot pre-warm on the running loop.

    Idempotent; a no-op when gated off or when no loop is running. Never
    raises — daemon startup must not depend on it."""
    global _task, _settled
    if _task is not None or not prewarm_enabled(force=force):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _settled = asyncio.Event()
    _task = loop.create_task(_run(), name="harness-prewarm")


def prewarm_started() -> bool:
    """Whether a pre-warm has been kicked off in this process — what a caller
    that front-loads the agent's one-time costs can be held to."""
    return _task is not None


async def wait_for_prewarm(timeout_seconds: float | None = None) -> None:
    """Block until an in-flight pre-warm settles, so a chat never races the
    pre-warm's migrator against the same opencode database.

    A no-op when no pre-warm was started (gated off / dev) or it already
    settled. Bounded (defaults to the adapter's listen deadline) and
    best-effort: on timeout the caller proceeds — worst case it waits on the
    migration itself, exactly as it would have with no pre-warm."""
    if _settled is None or _settled.is_set():
        return
    if timeout_seconds is None:
        from alkera_cli.harness.adapters.opencode_http import LISTEN_TIMEOUT_S

        timeout_seconds = LISTEN_TIMEOUT_S
    with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
        await asyncio.wait_for(_settled.wait(), timeout=timeout_seconds)


async def _run() -> None:
    """Stage the binary, run the agent to its listen URL once, tear it down.

    Everything is best-effort: any failure logs and settles — the pre-warm is
    an accelerator, never a gate on correctness."""
    assert _settled is not None
    settled = _settled
    tmp: str | None = None
    proc: asyncio.subprocess.Process | None = None
    try:
        # Late imports keep daemon startup light and avoid a module cycle with
        # the adapter (which lazily imports wait_for_prewarm from here).
        from alkera_cli.harness.adapters.opencode_http import LISTEN_TIMEOUT_S, build_agent_env
        from alkera_cli.harness.adapters.opencode_secrets import agent_secrets, write_agent_secrets
        from alkera_cli.harness.opencode_binary import resolve_opencode_binary
        from alkera_cli.harness.orphan_sweep import register_agent, write_pid_breadcrumb

        # Resolving stages the bundled agent + rg into the runtime cache — the
        # multi-hundred-MB copy a first chat would otherwise pay. Off-thread:
        # it's blocking file I/O.
        binary = await asyncio.to_thread(resolve_opencode_binary)

        tmp = tempfile.mkdtemp(prefix="alkera-prewarm-")
        listen_file = Path(tmp) / "listen-url"
        # The probe binds a real loopback HTTP server whose API can spawn a PTY
        # and run shell commands, so it gets the SAME isolation + lockdown a chat
        # spawn gets — above all a server password. Without one the agent's
        # authorization middleware short-circuits to a no-op (ServerAuth.required
        # is false when the option is absent) and ANY local process could drive
        # the probe's API for the lifetime of the process. We never issue a
        # request against it, so the value is generated and discarded; it is
        # handed over by file, as a chat's is.
        secrets_file = write_agent_secrets(Path(tmp), agent_secrets(secrets.token_urlsafe(32)))
        env = build_agent_env(
            data_home=tmp,
            config_home=tmp,
            listen_file=str(listen_file),
            secrets_file=str(secrets_file),
        )
        proc = await spawn_async(
            SpawnSpec(
                argv=[
                    str(binary.path),
                    *binary.prefix_args,
                    "serve",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    "0",
                ],
                env=env,
                stdout="devnull",
                stderr="devnull",
            )
        )
        # The same no-orphan plumbing as a real chat spawn: the seam binds it
        # to our lifetime, and a breadcrumb + registry entry let the startup
        # sweep reap the probe if this daemon dies mid-pre-warm.
        write_pid_breadcrumb(Path(tmp) / "pid", proc.pid)
        register_agent(proc.pid, pid_file=Path(tmp) / "pid")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + LISTEN_TIMEOUT_S
        ready = False
        while True:
            with contextlib.suppress(OSError):
                if listen_file.read_text(encoding="utf-8").strip().startswith("http"):
                    ready = True
                    break
            if proc.returncode is not None or loop.time() >= deadline:
                break
            await asyncio.sleep(0.25)
        if ready:
            logger.info("harness prewarm ready (source=%s)", binary.source)
        else:
            logger.warning(
                "harness prewarm did not reach ready (source=%s, exit=%s)",
                binary.source,
                proc.returncode,
            )
    except Exception:
        logger.warning("harness prewarm failed", exc_info=True)
    finally:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=10)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        if proc is not None:
            release(proc.pid)
        if proc is not None:
            # Drop the probe from the agent registry now that it is dead.
            # Deleting the tmp dir takes the pid breadcrumb with it, but the
            # registry entry lives at `<ALKERA_HOME>/agents/<pid>.json` and is
            # not in there — so without this every pre-warm this machine ever
            # ran leaves an entry naming a dead pid that each later sweep
            # re-reads. The day the OS recycles one of those pids onto a live
            # process that started within the sweep's creation-time tolerance,
            # the sweep terminates a stranger. The adapter unregisters its own
            # agent for exactly this reason.
            with contextlib.suppress(Exception):
                from alkera_cli.harness.orphan_sweep import unregister_agent

                unregister_agent(proc.pid)
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        settled.set()


def _reset_for_tests() -> None:
    """Clear the one-shot state so a test can drive a fresh pre-warm."""
    global _task, _settled
    _task = None
    _settled = None


__all__ = [
    "prewarm_enabled",
    "prewarm_started",
    "start_prewarm_in_background",
    "wait_for_prewarm",
]
