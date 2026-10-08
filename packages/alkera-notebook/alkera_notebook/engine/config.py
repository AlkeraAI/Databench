"""Engine configuration and the clock and id seams."""

from __future__ import annotations

import asyncio
import base64
import secrets
import time
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field, field_validator

from alkera_notebook.format.settings import NOTEBOOK_SETTINGS

MiB = 1024 * 1024


class MemoryPolicy(BaseModel):
    """The memory guard's tuning.

    Threshold = limit minus a reserve of ``max(10% of limit, max_alloc_rate x
    (interval + kill latency + jitter))``, unless ``reserve_bytes`` pins it.
    """

    interval_s: float = 0.1
    max_alloc_rate_bytes_s: float = 1.5e9
    kill_latency_s: float = 0.15
    jitter_s: float = 0.15
    reserve_bytes: int | None = None
    # Still over the threshold this long after killing the largest kernel:
    # kill every kernel (the sandbox, in the platform).
    second_stage_s: float = 0.3
    warn_fraction: float = 0.8
    # Admission control: a new kernel is refused when usage plus this much
    # would pass the threshold.
    kernel_start_bytes: int = 64 * MiB


class OutputLimits(BaseModel):
    rich_bytes_per_cell: int = 8 * MiB
    stream_bytes_per_cell: int = 1 * MiB
    blob_threshold_bytes: int = 256 * 1024
    snapshot_debounce_s: float = 1.0


class EngineConfig(BaseModel):
    workspace_root: str
    env_root: str
    data_root: str
    workspace_id: str = "local"
    # The organization the workspace belongs to (empty in the open core).
    org_id: str = ""
    # Whether the workspace's members share its environments on this machine
    # (they do where each runs in a container as the workspace; on a machine
    # with no container each member keeps its own). Reported on the listing.
    shared_envs: bool = True
    # Connections whose queries cost money: a SQL cell on one, run as an
    # implicit (upstream or descendant) step, needs confirmation.
    metered_connections: list[str] = Field(default_factory=list)
    max_kernels: int = 4
    interrupt_escalation_s: tuple[float, float] = (3.0, 7.0)
    kernel_idle_seconds: float | None = None
    memory: MemoryPolicy = Field(default_factory=MemoryPolicy)
    limits: OutputLimits = Field(default_factory=OutputLimits)
    cost_guard_seconds: float = 60.0
    # Rows kept from a SQL query without its own LIMIT (marimo's default
    # limit); None keeps the whole result. A notebook may override it.
    sql_row_limit: int | None = None
    # The workspace's defaults for the notebook's settings: what a notebook
    # whose file does not set one runs with (``source: workspace``).
    workspace_settings: dict[str, Any] = Field(default_factory=dict)
    # Where boot.py and _alkera_kernel live (the platform mount).
    kernel_mount: str | None = None
    # How often a cell whose re-run waits for someone to stop editing it is
    # looked at again.
    editing_recheck_s: float = 2.0
    # How long a run waits for the store to include the client's frontier.
    frontier_wait_s: float = 2.0
    # How long a kernel has to connect after launch.
    connect_timeout_s: float = 30.0
    # Per-client event queue bound.
    client_queue_max: int = 1000
    # Base environment the kernel's allowlisted variables are taken from.
    base_env: dict[str, str] = Field(default_factory=dict)
    # The output frame page widgets and rich HTML render in (an absolute URL
    # on the content origin, which embeds the allowed parent origins); None
    # where no frame host exists. Reported on every ``NotebookView``.
    output_frame_url: str | None = None
    # ``alkera.args()`` for kernels this engine starts (``alkera-notebook run
    # file -- --key value``); sent in the hello settings as ``args``.
    kernel_args: dict[str, str] = Field(default_factory=dict)

    @field_validator("workspace_settings")
    @classmethod
    def _settings_only(cls, value: dict[str, Any]) -> dict[str, Any]:
        """The workspace layer names notebook settings and nothing else (a
        settings read's ``sources`` is never one)."""
        stray = sorted(set(value) - {s.name for s in NOTEBOOK_SETTINGS})
        if stray:
            raise ValueError(f"not notebook settings: {', '.join(stray)}")
        return value

    def setting_defaults(self) -> dict[str, Any]:
        """The workspace layer of each setting's value (a notebook's own wins)."""
        found = dict(self.workspace_settings)
        if self.sql_row_limit is not None:
            found.setdefault("sql_row_limit", self.sql_row_limit)
        return found


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class IdSource(Protocol):
    def run_id(self) -> str: ...

    def kernel_id(self) -> str: ...

    def token(self) -> str: ...

    def message_id(self) -> str: ...


class RandomIds:
    def run_id(self) -> str:
        return "run_" + secrets.token_hex(8)

    def kernel_id(self) -> str:
        return "krn_" + secrets.token_hex(8)

    def token(self) -> str:
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")

    def message_id(self) -> str:
        return "msg_" + secrets.token_hex(8)


class SequentialIds:
    """Deterministic ids for tests and reproducible plans (tokens stay random)."""

    def __init__(self) -> None:
        self._n = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}_{self._n:06d}"

    def run_id(self) -> str:
        return self._next("run")

    def kernel_id(self) -> str:
        return self._next("krn")

    def token(self) -> str:
        return RandomIds().token()

    def message_id(self) -> str:
        return self._next("msg")
