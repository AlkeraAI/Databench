"""How a box's supervisor stops and comes back: the kind of stop, how long it
waits for its workers, and the orgs a restart in place stopped workers for.

The routing reads a chat on a restarting box as asleep, so a supervisor
started after a restart would give no worker to a chat whose lease the restart
kept and whose work it still owes. The stopping supervisor names the orgs it
stopped (:func:`remember`); the next one reads the list (:func:`read`), adds
the orgs whose worker units were still running when it started (a supervisor
that crashed named nothing), and starts those orgs' workers whatever the
routing says. Each org leaves the list once its new worker is ready, so a
crash before that leaves it named for the supervisor after. A final stop names
nothing: every chat was handed back.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

from alkera_core.compute.liveness import (
    RESTART_DRAIN_CEILING_SECONDS,
    STOP_EXIT_SECONDS,
    drain_ceiling_seconds,
)

from alkera_cli.supervisor.slots import canonical_org

logger = logging.getLogger(__name__)

#: Beside the slot table.
RESUME_FILE: Final = "resume.json"
#: How long a restart in place waits for a chat's background job: the job is
#: the process's and dies with it, and the next process reports it to its chat
#: as interrupted (``harness/interrupted_jobs.py``). Minutes, not the drain
#: ceiling: a restart that waited hours for a dev server kept the box's fix
#: from going live and took no new chat meanwhile.
RESTART_JOB_CEILING_SECONDS: Final = 10 * 60
#: How long an org a restart named stays wanted while its worker is not yet
#: ready: past it the routing alone decides.
RESUME_HOLD_SECONDS: Final = 3600.0


def stop_is_final(env: Mapping[str, str]) -> bool:
    """Whether a stop of the supervisor is final (every chat handed back) or
    a restart in place (every lease kept), by the same rule the single
    daemon followed: unsupervised, every stop is final; supervised, a stop is
    a restart unless the unit's stop step wrote the final-stop file."""
    if env.get("ALKERA_DAEMON_SUPERVISED", "").strip() != "1":
        return True
    final = env.get("ALKERA_DAEMON_FINAL_STOP_FILE", "").strip()
    return bool(final) and Path(final).exists()


def drain_window(env: Mapping[str, str]) -> float:
    """How long a final stop waits for its workers to drain and exit: the
    drain ceiling and the margin a worker takes past it to end its own
    process. A worker still running after that is killed; nothing short of it
    is. A restart in place waits :func:`restart_window`."""
    return drain_ceiling_seconds(env) + STOP_EXIT_SECONDS


def restart_window(env: Mapping[str, str]) -> float:
    """How long workers whose supervisor went away (they drain as a restart)
    are given to end on their own: a restart's wait for a job (never past the
    drain ceiling), the short wait for the turn after it, and the margin a
    worker takes to end its process (``cloud/restart_wait.py``)."""
    wait = min(float(RESTART_JOB_CEILING_SECONDS), drain_ceiling_seconds(env))
    short = float(RESTART_DRAIN_CEILING_SECONDS)
    return max(wait, short) + short + STOP_EXIT_SECONDS


def remember(path: Path, orgs: Iterable[str]) -> None:
    """Name ``orgs`` at ``path`` for the next supervisor; none removes the file."""
    names = sorted(orgs)
    try:
        if not names:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staged = path.with_suffix(".tmp")
        staged.write_text(json.dumps({"orgs": names}), encoding="utf-8")
        staged.chmod(0o600)
        staged.replace(path)
    except OSError as exc:
        logger.warning("could not record the orgs to resume after the restart: %s", exc)


def read(path: Path) -> set[str]:
    """The orgs named at ``path``; the file stays until :func:`remember`
    rewrites it. A file that does not parse, or an org id that is not one,
    names nothing: the worst case is a worker started when its chats are next
    asked for."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    orgs = raw.get("orgs") if isinstance(raw, Mapping) else None
    found: set[str] = set()
    for org in orgs if isinstance(orgs, list) else []:
        with contextlib.suppress(RuntimeError, TypeError):
            found.add(canonical_org(org))
    return found


__all__ = [
    "RESTART_JOB_CEILING_SECONDS",
    "RESUME_FILE",
    "RESUME_HOLD_SECONDS",
    "drain_window",
    "read",
    "remember",
    "restart_window",
    "stop_is_final",
]
