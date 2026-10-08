"""How the environment library runs a command: the one seam between its logic
and the machine.

Capture and recreate never spawn a process themselves. They describe each
command as a :class:`Command` (argv, the files it reads, extra environment, a
time limit) and hand it to a :class:`CommandRunner`. :class:`LocalRunner` runs
it on this machine. A cloud chat's runner (in the environment tool) runs the
same command inside the chat's sandbox as the chat's own user, so nothing the
chat controls is ever read, run or written by the box's root.

A file a command needs (the probe script, a requirements list) travels as
content in :attr:`Command.files`; the argv names it as ``{name}`` and the
runner substitutes a path it made. So the planner never chooses a temporary
path on a machine it cannot see.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from alkera_core.process import SpawnSpec, kill_tree_async, spawn_async

#: ``{name}`` in an argv element: a file from :attr:`Command.files`.
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")
#: How long a command may run when its caller names no limit.
DEFAULT_TIMEOUT_S = 900.0
#: The most output a command keeps; the tail survives.
MAX_OUTPUT_CHARS = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Command:
    argv: tuple[str, ...]
    files: Mapping[str, str] = field(default_factory=dict)
    """Content by name; ``{name}`` in ``argv`` becomes the path of a file
    holding it."""
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S

    def display(self) -> str:
        """The command as a person reads it, with each file named ``<name>``."""
        shown = [PLACEHOLDER_RE.sub(lambda m: f"<{m.group(1)}>", arg) for arg in self.argv]
        prefix = [f"{k}={v}" for k, v in sorted(self.env.items())]
        return shlex.join([*prefix, *shown])


@dataclass(frozen=True, slots=True)
class CommandResult:
    exit_code: int | None
    """``None`` when the command was killed at its time limit."""
    output: str
    """stdout and stderr together."""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class CommandRunner(Protocol):
    async def run(self, command: Command) -> CommandResult: ...

    async def which(self, name: str) -> str | None:
        """The program ``name`` resolves to where commands run, or ``None``."""
        ...


def substitute(argv: tuple[str, ...], paths: Mapping[str, str]) -> list[str]:
    """``argv`` with every ``{name}`` replaced by ``paths[name]``. A name with
    no file is an error, not a literal: a planner bug must not run."""

    def _one(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in paths:
            raise KeyError(f"command names a file it does not carry: {name}")
        return paths[name]

    return [PLACEHOLDER_RE.sub(_one, arg) for arg in argv]


class LocalRunner:
    """Runs commands on this machine, as this user."""

    def __init__(self, *, base_env: Mapping[str, str] | None = None) -> None:
        self._base_env = dict(os.environ if base_env is None else base_env)

    async def which(self, name: str) -> str | None:
        return shutil.which(name, path=self._base_env.get("PATH"))

    async def run(self, command: Command) -> CommandResult:
        with tempfile.TemporaryDirectory(prefix="alkera-env-") as tmp:
            paths: dict[str, str] = {}
            for name, content in command.files.items():
                target = Path(tmp) / name
                target.write_text(content, encoding="utf-8")
                paths[name] = str(target)
            argv = substitute(command.argv, paths)
            env = {**self._base_env, **command.env}
            return await _run_process(argv, env=env, cwd=command.cwd, timeout_s=command.timeout_s)


async def _run_process(
    argv: list[str], *, env: Mapping[str, str], cwd: str | None, timeout_s: float
) -> CommandResult:
    try:
        proc = await spawn_async(
            SpawnSpec(argv=argv, env=env, cwd=cwd, stdout="pipe", stderr="stdout")
        )
    except OSError as exc:
        return CommandResult(exit_code=127, output=f"could not start {argv[0]}: {exc}")
    try:
        out = await asyncio.wait_for(_read_tail(proc, MAX_OUTPUT_CHARS), timeout=timeout_s)
    except TimeoutError:
        await _kill(proc)
        return CommandResult(exit_code=None, output=f"timed out after {timeout_s:g} s")
    except asyncio.CancelledError:
        await _kill(proc)
        raise
    text = out.decode("utf-8", "replace")
    return CommandResult(exit_code=proc.returncode, output=text[-MAX_OUTPUT_CHARS:])


#: How much of a command's output is read at a time.
_READ_CHUNK = 64 * 1024


async def _read_tail(proc: asyncio.subprocess.Process, keep: int) -> bytes:
    """The last ``keep`` bytes of the command's output, read as it arrives,
    once it has exited. Only the tail is ever held: an installer that prints
    gigabytes costs this process ``keep`` bytes, not everything it printed."""
    stream = proc.stdout
    tail = bytearray()
    if stream is not None:
        while chunk := await stream.read(_READ_CHUNK):
            tail += chunk
            if len(tail) > 2 * keep:
                del tail[: len(tail) - keep]
    await proc.wait()
    return bytes(tail[-keep:]) if keep > 0 else b""


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill the command and everything it started (an installer's builds are
    its children)."""
    with contextlib.suppress(Exception):
        await kill_tree_async(proc, grace=0)


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "PLACEHOLDER_RE",
    "Command",
    "CommandResult",
    "CommandRunner",
    "LocalRunner",
    "substitute",
]
