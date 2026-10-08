"""The steps a sandbox launch runs around the agent's spawn, and their runner.

A launch is composed as data by :mod:`alkera_cli.harness.sandbox`: shell
steps (an argv, run to completion), write steps (a file's content) and repair
steps (a tree the agent writes, given its owner and modes), before the spawn
and after the exit. They run here, through the one seam every step
goes through: a checked step that fails refuses the chat
(:class:`SandboxRefusedError`), since the chat is not started less bounded
than its spec says; an unchecked one logs and the rest carry on.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from alkera_core.process import SpawnSpec, reclaim_children, run

from alkera_cli.files.chat_fs import ChatTree, TreeIdentity

logger = logging.getLogger(__name__)


class SandboxRefusedError(RuntimeError):
    """The box cannot provide the sandbox it is configured for; the chat is
    refused rather than run less bounded than the box requires. A box set to
    ``gvisor`` with no working ``runsc`` (or no staged rootfs, or no resolver
    for the container) refuses every chat instead of running them unsandboxed."""


@dataclass(frozen=True, slots=True)
class ShellStep:
    argv: tuple[str, ...]
    check: bool = True
    """Whether a non-zero exit aborts the launch. A cleanup step never does."""


@dataclass(frozen=True, slots=True)
class WriteStep:
    path: Path
    content: str
    check: bool = True
    mode: int | None = None
    """The file's mode after the write, whatever the daemon's umask; ``None``
    leaves the umask's. A file the chat's uid must read is given one: the
    daemon runs under 077."""


@dataclass(frozen=True, slots=True)
class RepairStep:
    """Give a tree the agent writes its owner and the chat tree's modes
    (:meth:`~alkera_cli.files.chat_fs.ChatTree.repair`), as root, while the
    agent and every other process of the workspace may be renaming inside it.
    The walk goes from opened directories and changes each entry through its
    own descriptor, so a name swapped for a link mid-walk is never followed
    out of the tree, which a ``chmod`` or ``chown`` by path would do. The root
    is opened refusing a link."""

    tree: Path
    owner: TreeIdentity | None
    """Who the tree's entries go to; ``None`` changes modes only."""
    files: bool = True
    """``False`` repairs the directories alone."""
    when_wrong: bool = False
    """Walk only when the root is not already the owner's with
    :data:`~alkera_cli.files.chat_fs.DIR_MODE`. Once it is, what is made below
    inherits the group and the setgid bit, so a tree that grows with every
    spawn is walked only to put it right."""
    check: bool = True


Step = ShellStep | WriteStep | RepairStep

Runner = Callable[[Sequence[str]], int]
"""Runs an argv to completion and returns its exit status."""


def argv_text(argv: Sequence[str]) -> str:
    """A step for a log line or a refusal: a program passed inline (the
    relocation script) reads as ``<script>`` rather than as its every line."""
    return " ".join("<script>" if "\n" in arg else arg for arg in argv)


def run_argv(argv: Sequence[str], *, quiet: bool = False) -> int:
    """Run one step's argv to completion; its exit status, 127 when it could
    not run at all. The default :data:`Runner`. ``quiet``: a cleanup step,
    whose failure (taking down what is already gone) is the usual case, said
    at debug rather than as a warning on every sleep."""
    # A step's verdict is its exit status; a daemon that inherited an ignored
    # SIGCHLD would read every one as 0 and start a chat on steps that failed.
    reclaim_children()
    try:
        done = run(
            SpawnSpec(argv=list(argv), env=os.environ, stdout="pipe", stderr="pipe"),
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # A binary the box lacks (the default env's python when `uv venv` could
        # not make it) or one that hangs is a failed step like any other: an
        # unchecked step is skipped, a checked one refuses the chat by name.
        logger.warning("sandbox step failed to run: %s: %s", argv_text(argv), exc)
        return 127
    if done.returncode != 0:
        logger.log(
            logging.DEBUG if quiet else logging.WARNING,
            "sandbox step failed (%d): %s: %s",
            done.returncode,
            argv_text(argv),
            done.stderr.decode("utf-8", errors="replace").strip()[-2000:],
        )
    return done.returncode


def _default_write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def repair_tree(step: RepairStep) -> None:
    """Run one :class:`RepairStep`. Raises :class:`OSError` when the root is
    missing or a link, or a change is refused."""
    ChatTree(step.tree, step.owner).hand_over(files=step.files, when_wrong=step.when_wrong)


def run_steps(
    steps: Iterable[Step],
    *,
    run: Runner | None = None,
    write: Callable[[Path, str], None] | None = None,
    repair: Callable[[RepairStep], None] | None = None,
) -> None:
    """Run each step in order. A checked step that fails raises
    :class:`SandboxRefusedError`, since the chat is not started less bounded
    than its spec says; an unchecked one logs and the rest carry on. The defaults are
    resolved here, at call time, so this stays the one seam every shell step
    goes through."""
    run = run or run_argv
    write = write or _default_write
    repair = repair or repair_tree
    for step in steps:
        if isinstance(step, ShellStep):
            status = (
                run_argv(step.argv, quiet=not step.check) if run is run_argv else run(step.argv)
            )
            if status != 0 and step.check:
                raise SandboxRefusedError(
                    f"sandbox step failed: {argv_text(step.argv)} (exit {status})"
                )
            continue
        if isinstance(step, RepairStep):
            try:
                repair(step)
            except OSError as exc:
                if step.check:
                    raise SandboxRefusedError(
                        f"sandbox step failed: repair {step.tree}: {exc}"
                    ) from exc
                logger.warning("sandbox step skipped: repair %s: %s", step.tree, exc)
            continue
        try:
            write(step.path, step.content)
            if step.mode is not None:
                os.chmod(step.path, step.mode)
        except OSError as exc:
            if step.check:
                raise SandboxRefusedError(f"sandbox step failed: write {step.path}: {exc}") from exc
            logger.warning("sandbox step skipped: write %s: %s", step.path, exc)


__all__ = [
    "RepairStep",
    "Runner",
    "SandboxRefusedError",
    "ShellStep",
    "Step",
    "WriteStep",
    "argv_text",
    "repair_tree",
    "run_argv",
    "run_steps",
]
