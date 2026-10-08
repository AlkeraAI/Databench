"""What a box is using, sampled for its heartbeat: cpu, memory, work-volume
disk, GPUs, and when it last did any work.

CPU is the percentage since the previous sample (``psutil.cpu_percent`` with
no interval), so one sample per beat reads as the load over the beat interval;
the first sample of a process is 0. Memory is read against the cgroup limit
when the box runs under one (cgroup v2 ``memory.max`` / ``memory.current``,
else v1 ``memory.limit_in_bytes`` / ``memory.usage_in_bytes``) — a container
on a large host is out of memory at its limit, not at the host's total — and
against the host otherwise. Disk is the work volume's (``/opt/alkera-work``),
or the root filesystem when it is not mounted.

GPUs are read from ``nvidia-smi`` when the binary is on the box; a box with no
binary, or one whose call fails or times out, has no GPUs (never an error: a
heartbeat must not fail because a driver is wedged). The box also says what
its GPUs are good for: ``gpu`` when it has one, and ``gpu_passthrough`` when
its sandbox can hand one to a chat or a kernel.

:class:`LastActivity` is the box's own record of the last moment any chat did
work (a turn running or ending, a background process busy), which the
platform reads to stop an idle machine.
"""

from __future__ import annotations

import glob
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.host_resources import host_memory_in_use
from alkera_core.process import run_captured

WORK_DIR = Path("/opt/alkera-work")
CGROUP_ROOT = Path("/sys/fs/cgroup")

#: The parent cgroup every sandboxed chat hangs under (``sandbox-prereqs.sh``).
CHAT_SLICE_NAME = "alkera.slice"

#: v1 reports "no limit" as a number near 2**63.
_UNLIMITED_V1 = 1 << 60

#: The one query the box asks its GPUs, in the order :func:`parse_nvidia_smi`
#: reads the columns. Memory comes in MiB, utilization in percent.
NVIDIA_SMI_ARGS = (
    "--query-gpu=index,name,memory.used,memory.total,utilization.gpu",
    "--format=csv,noheader,nounits",
)
#: The longest a heartbeat waits on ``nvidia-smi``.
NVIDIA_SMI_TIMEOUT_SECONDS = 5.0
_MIB = 1024 * 1024

#: The device nodes a GPU is reached through on the host.
NVIDIA_DEVICE_GLOB = "/dev/nvidia[0-9]*"


def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw or raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def cgroup_memory(root: Path = CGROUP_ROOT) -> tuple[int, int] | None:
    """``(used, limit)`` from the cgroup this process runs under, or ``None``
    when there is no finite limit to read."""
    limit = _read_int(root / "memory.max")
    if limit is not None:
        used = _read_int(root / "memory.current")
        return (used or 0, limit)
    limit = _read_int(root / "memory" / "memory.limit_in_bytes")
    if limit is not None and limit < _UNLIMITED_V1:
        used = _read_int(root / "memory" / "memory.usage_in_bytes")
        return (used or 0, limit)
    return None


def _stat_field(path: Path, name: str) -> int:
    """One counter from a cgroup ``memory.stat``, ``0`` when absent."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == name and value.strip().isdigit():
            return int(value.strip())
    return 0


def working_set(root: Path) -> tuple[int, int] | None:
    """``(working set, limit)`` of the cgroup v2 group at ``root``, or ``None``
    when it has no finite limit to read. The working set is ``memory.current``
    less the inactive file cache (``memory.stat`` ``inactive_file``), which is
    what the kernel gives back before it OOM-kills anything: a box that copied
    a large folder fills its page cache to the limit, and reading that as
    pressure would put every idle chat to sleep for memory nobody is using."""
    limit = _read_int(root / "memory.max")
    if limit is None:
        return None
    used = _read_int(root / "memory.current") or 0
    inactive = _stat_field(root / "memory.stat", "inactive_file")
    return (max(0, used - inactive), limit)


def chats_memory(cgroup_root: Path = CGROUP_ROOT) -> tuple[int, int] | None:
    """``(working set, limit)`` for the memory the chats compete for.

    The chats' shared slice when it is bounded (the slice the kernel OOM-kills
    a chat in), else the cgroup this process runs under (a container's own
    limit), else, on Linux, the host (total less what it reports available).
    ``None`` when none of them can be read."""
    for root in (cgroup_root / CHAT_SLICE_NAME, cgroup_root):
        found = working_set(root)
        if found is not None:
            return found
    v1 = cgroup_root / "memory"
    limit = _read_int(v1 / "memory.limit_in_bytes")
    if limit is not None and limit < _UNLIMITED_V1:
        used = _read_int(v1 / "memory.usage_in_bytes") or 0
        inactive = _stat_field(v1 / "memory.stat", "total_inactive_file")
        return (max(0, used - inactive), limit)
    if not sys.platform.startswith("linux"):
        # macOS counts compressed and cached pages so that "available" runs low
        # on an idle machine; read there, the valve would sleep every chat.
        return None
    return host_memory_in_use()


def _number(raw: str) -> float | None:
    try:
        return float(raw.strip())
    except ValueError:
        return None


def parse_nvidia_smi(text: str) -> list[dict[str, Any]]:
    """``nvidia-smi`` CSV (:data:`NVIDIA_SMI_ARGS`) as heartbeat GPU samples.

    A row that does not have the five columns, or whose index or memory is not
    a number, is skipped rather than guessed at. A utilization the driver does
    not report (``[N/A]``) reads as 0."""
    gpus: list[dict[str, Any]] = []
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) != 5:
            continue
        index, name, used, total, utilization = cells
        index_n, used_n, total_n = _number(index), _number(used), _number(total)
        if index_n is None or used_n is None or total_n is None or not name:
            continue
        gpus.append(
            {
                "index": int(index_n),
                "name": name,
                "memory_used_bytes": int(used_n * _MIB),
                "memory_total_bytes": int(total_n * _MIB),
                "utilization_percent": min(100.0, max(0.0, _number(utilization) or 0.0)),
            }
        )
    return gpus


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def sample_gpus(
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Runner = run_captured,
    timeout: float = NVIDIA_SMI_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """The box's GPUs now, or ``[]`` when it has none it can read."""
    binary = which("nvidia-smi")
    if not binary:
        return []
    try:
        result = run(
            [binary, *NVIDIA_SMI_ARGS],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return parse_nvidia_smi(result.stdout or "")


def gpu_capabilities(
    gpus: list[dict[str, Any]],
    *,
    sandbox_mode: str,
    nvidia_devices: bool,
    gvisor_nvproxy: bool = False,
) -> list[BoxCapability]:
    """What the box can do with its GPUs: ``gpu`` with at least one, and
    ``gpu_passthrough`` when its sandbox can hand one to a chat or a kernel —
    no sandbox with the device nodes on the host, or gVisor started with
    ``--nvproxy``. A box with no GPU passes nothing through."""
    if not gpus:
        return []
    capabilities = [BoxCapability.GPU]
    passes = (sandbox_mode == "none" and nvidia_devices) or (
        sandbox_mode == "gvisor" and gvisor_nvproxy
    )
    if passes:
        capabilities.append(BoxCapability.GPU_PASSTHROUGH)
    return capabilities


def nvidia_devices_present(pattern: str = NVIDIA_DEVICE_GLOB) -> bool:
    """Whether the host exposes an NVIDIA device node."""
    return bool(glob.glob(pattern))


class LastActivity:
    """The last moment any chat on the box did work, on the wall clock.

    Noted when a turn ends, and on every beat while a chat has something in
    flight (a turn running, a background shell, query or subagent): a turn
    that started between beats is seen at the next one. ``None`` until the box
    has done any work, which the platform reads as "nothing since it came up"."""

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._clock = clock
        self._at: datetime | None = None

    @property
    def at(self) -> datetime | None:
        return self._at

    def note(self) -> datetime:
        self._at = self._clock()
        return self._at

    def observe(self, working: Iterable[bool]) -> datetime | None:
        """Stamp now when any chat is working; return the latest stamp."""
        if any(working):
            self.note()
        return self._at


def sample_resources(
    *,
    work_dir: Path = WORK_DIR,
    cgroup_root: Path = CGROUP_ROOT,
    gpus: Callable[[], list[dict[str, Any]]] = sample_gpus,
) -> dict[str, Any] | None:
    """One heartbeat's sample, or ``None`` when the host refuses to say.
    Blocking (``nvidia-smi`` is a subprocess): call it off the event loop."""
    try:
        cpu = float(psutil.cpu_percent(interval=None))
        memory = cgroup_memory(cgroup_root) or host_memory_in_use()
        if memory is None:
            return None
        used, limit = memory
        disk = psutil.disk_usage(str(work_dir if work_dir.is_dir() else Path("/")))
    except (OSError, RuntimeError):
        return None
    return {
        "cpu_percent": max(0.0, cpu),
        "memory_used_bytes": max(0, used),
        "memory_limit_bytes": max(0, limit),
        "disk_used_bytes": int(disk.used),
        "disk_total_bytes": int(disk.total),
        "gpus": gpus(),
    }


__all__ = [
    "CGROUP_ROOT",
    "CHAT_SLICE_NAME",
    "NVIDIA_SMI_ARGS",
    "WORK_DIR",
    "LastActivity",
    "cgroup_memory",
    "chats_memory",
    "gpu_capabilities",
    "nvidia_devices_present",
    "parse_nvidia_smi",
    "sample_gpus",
    "sample_resources",
    "working_set",
]
