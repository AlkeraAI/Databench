"""The core's :class:`CommandRunner`: local subprocesses."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Mapping, Sequence

from alkera_notebook.envs.models import CommandResult


class LocalCommandRunner:
    """Runs a command as a local subprocess in its own session.

    stdin is ``/dev/null``; stdout and stderr are captured. On timeout the
    whole process group is killed and the result says ``timed_out``.
    """

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> CommandResult:
        if not argv:
            raise ValueError("argv is empty")
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=sys.platform != "win32",
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        except TimeoutError:
            _kill_group(proc)
            out, err = await proc.communicate()
            return CommandResult(
                returncode=proc.returncode if proc.returncode is not None else -9,
                stdout=out.decode("utf-8", "replace"),
                stderr=err.decode("utf-8", "replace") + f"\ntimed out after {timeout_s} s",
                timed_out=True,
            )
        except asyncio.CancelledError:
            _kill_group(proc)
            with contextlib.suppress(Exception):
                await proc.wait()
            raise
        return CommandResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
        )


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
