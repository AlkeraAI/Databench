"""The box's status file: what the process serving a box said on the last beat
the server took.

A deploy rolls each node onto a new build and must then learn, from the node,
that the new build is what serves and whether a turn is in flight that a
restart would cut. The deploy's node upgrade and box roll scripts read this
file for both answers. Whichever
process serves the box writes it: the single daemon, or the supervisor with
its workers' counts summed. Both build it here, so the shape the roll reads
has one owner.

The file is written whole (temp and rename) only after a beat was accepted,
so a reader sees the last beat or the one before, and the platform's row
already says the same build. A write that fails is logged and dropped: the
file is a report, never a reason to stop serving.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final, NotRequired, TypedDict

from alkera_core.atomic_io import write_json_atomic

from alkera_cli.host import paths as alkera_paths

logger = logging.getLogger(__name__)

#: Where the file goes when the environment names a place, and its name in
#: the Alkera home otherwise (on a node, ``/opt/alkera-home``, where a roll
#: reads it).
ENV_DAEMON_STATUS_FILE: Final = "ALKERA_DAEMON_STATUS_FILE"
DAEMON_STATUS_FILE_NAME: Final = "cloud-mirror.status.json"
#: The shape's version; a reader that does not know it reads no field.
STATUS_SCHEMA: Final = 1


class BoxStatus(TypedDict):
    """What the roll reads. ``pid`` must be the unit's main process: a file
    another process left behind is never taken for the one serving now."""

    schema: int
    pid: int
    daemon_instance_id: str
    daemon_version: str | None
    build: str
    machine_id: str | None
    beat_at: float
    chats_held: int
    #: Anything in flight, a parked ask on a person included. Left out when
    #: the process cannot vouch for it, and the roll never reads the box as
    #: idle then.
    chats_busy: NotRequired[int]
    #: What a drain waits for: turns, tools and jobs. Left out when the
    #: process cannot count it, and the roll then waits on ``chats_busy``,
    #: which never counts fewer.
    chats_working: NotRequired[int]
    chats_awaiting_user: NotRequired[int]
    chats_idle: NotRequired[int]
    draining: bool
    restarting: bool | None


def status_file_from_env(env: Mapping[str, str] | None = None) -> Path:
    """Where the serving process writes its last accepted beat."""
    source = os.environ if env is None else env
    raw = (source.get(ENV_DAEMON_STATUS_FILE) or "").strip()
    return Path(raw) if raw else alkera_paths.ALKERA_HOME / DAEMON_STATUS_FILE_NAME


def write_status(path: Path | None, report: BoxStatus) -> None:
    """Write ``report`` whole to ``path``; nothing when there is no path.
    Owner-only (the default): the roll reads it as root."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, dict(report))
    except OSError as exc:
        logger.debug("could not write the box status file %s: %s", path, exc)
