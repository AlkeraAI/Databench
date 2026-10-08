"""The absolute path of the ``alkera`` binary the supervisor is running from.

Every process the supervisor spawns (an org's worker, under ``unshare`` and
``systemd-run``) runs this same build, named by its absolute path. A bare
``alkera`` is looked up on the spawned process's ``PATH``, which an install
that put the build under ``/opt/alkera/alkera.dist`` never touched: the worker
then fails with exit 127 while the supervisor, started by its full path, runs.

:func:`resolve_launcher` decides from what it is handed (pure, for tests);
:func:`running_launcher` hands it this process's facts.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

#: Set by Nuitka in every module it compiled; absent when run from source.
_COMPILED = "__compiled__" in globals()


class LauncherNotFoundError(RuntimeError):
    """No absolute path to the running ``alkera`` could be found."""


def _runnable(path: str | os.PathLike[str]) -> Path | None:
    candidate = Path(path)
    if not candidate.is_absolute():
        return None
    resolved = candidate.resolve()
    if resolved.is_file() and os.access(resolved, os.X_OK):
        return resolved
    return None


def resolve_launcher(
    *,
    argv0: str,
    executable: str,
    compiled: bool,
    which: Callable[[str], str | None] = shutil.which,
) -> Path:
    """The absolute path that starts this same build.

    A compiled build is its own executable. From source, ``argv0`` is the
    console script that started it: taken as given when it names a path,
    looked up on ``PATH`` when it is a bare name. ``alkera`` on ``PATH`` is the
    last resort. Refused when none is an existing executable file."""
    candidates: list[str | None] = []
    if compiled:
        candidates.append(executable)
    if argv0:
        has_dir = os.sep in argv0 or (os.altsep is not None and os.altsep in argv0)
        candidates.append(os.path.abspath(argv0) if has_dir else which(argv0))
    candidates.append(which("alkera"))
    for candidate in candidates:
        if candidate and (found := _runnable(candidate)) is not None:
            return found
    raise LauncherNotFoundError(
        f"cannot find the alkera binary this supervisor runs from (argv[0] {argv0!r})"
    )


def running_launcher(*, only_alkera_argv0: bool = False) -> Path:
    """:func:`resolve_launcher` for this process, resolved at each call (a few
    stats) so it holds no module state. ``only_alkera_argv0`` ignores an
    ``argv[0]`` that is not an ``alkera`` (a test runner's wrapper)."""
    argv0 = sys.argv[0] if sys.argv else ""
    if only_alkera_argv0 and not Path(argv0).name.startswith("alkera"):
        argv0 = ""
    return resolve_launcher(argv0=argv0, executable=sys.executable, compiled=_COMPILED)


def worker_argv(launcher: Sequence[str], org_root: Path) -> tuple[str, ...]:
    """The command that starts one org's worker, ``launcher`` first."""
    return (
        *launcher,
        "cloud-mirror",
        "worker",
        "--org-root",
        str(org_root),
        "--control-fd",
        "0",
    )


__all__ = ["LauncherNotFoundError", "resolve_launcher", "running_launcher", "worker_argv"]
