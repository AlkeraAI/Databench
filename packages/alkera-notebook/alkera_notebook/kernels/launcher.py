"""The seams between the engine and a kernel process.

The launcher seam (``LaunchSpec``, ``LaunchedKernel``, ``KernelLauncher``)
lives in ``launch_local.py``: the launcher alone knows the pid
and process group; signals and kills go to the whole group; ``rss_bytes`` is
measured by the launcher, never reported by the kernel. A launcher may also
offer ``kill_all()`` (every kernel it started, or a whole sandbox), which the
memory guard's second stage uses.

:class:`KernelTransport` is this package's: it hands the engine a fresh
endpoint per kernel (``ALKERA_RPC_ENDPOINT``). The core uses a private Unix
socket directory; a container transport places the socket where the kernel's
sandbox can reach it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from alkera_notebook.kernels.launch_local import (
    ENV_ALLOWLIST,
    KernelLauncher,
    LaunchedKernel,
    LaunchSpec,
    LocalSubprocessLauncher,
)
from alkera_notebook.rpc.service import UnixEndpoint

_RESERVED = frozenset(
    {"ALKERA_RPC_ENDPOINT", "ALKERA_NOTEBOOK_DIR", "ALKERA_KERNEL_ID", "ALKERA_DATA_DIR"}
)


class KernelTransport(Protocol):
    def endpoint(self, kernel_id: str) -> UnixEndpoint: ...


class UnixSocketTransport:
    """A private directory under ``base`` (short, for macOS) per kernel."""

    def __init__(self, base: str = "/tmp") -> None:  # noqa: S108 - macOS socket path limit
        self._base = base

    def endpoint(self, kernel_id: str) -> UnixEndpoint:
        return UnixEndpoint.create(base=self._base)


def kernel_env(base: Mapping[str, str], extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The allowlisted subset of ``base`` (then ``extra``), with
    ``PYTHONHASHSEED=0``. The ``ALKERA_*`` variables are the launcher's."""
    env = {k: v for k, v in base.items() if k in ENV_ALLOWLIST and k not in _RESERVED}
    for k, v in (extra or {}).items():
        if k in ENV_ALLOWLIST and k not in _RESERVED:
            env[k] = v
    env["PYTHONHASHSEED"] = "0"
    return env


__all__ = [
    "ENV_ALLOWLIST",
    "KernelLauncher",
    "KernelTransport",
    "LaunchSpec",
    "LaunchedKernel",
    "LocalSubprocessLauncher",
    "UnixSocketTransport",
    "kernel_env",
]
