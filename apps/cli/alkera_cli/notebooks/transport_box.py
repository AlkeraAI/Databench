"""How a kernel in the kernel sandbox reaches the engine: a Unix socket of its own.

Measured against TCP on the sandbox's veth (``NOTEBOOK_PLATFORM_DESIGN.md``,
S3b), a Unix socket in the sandbox's runtime directory connects ten times
faster, needs no firewall grant, and is the only transport RPC v1 has. The
sandbox runs with ``--host-uds=open``: a kernel can open a host socket that
sits in a tree bound into it, and can make none.

The engine asks for an endpoint per kernel (``endpoint(kernel_id)``), binds
the socket itself and passes its path to the kernel. This transport places it
in a directory of its own under the runtime directory,
``<runtime>/sock/<kernel id>/k.sock``, which the runtime directory's binding
shows the kernel at the same path. The directory is the worker's (0700) until
the launcher has the kernel's uid and hands it over
(:func:`hand_over`): the directory to the kernel's uid alone (0700), the
socket to its uid and the workspace's files group. Another kernel of the
workspace cannot even reach the socket's directory, so the only process
that can present the kernel's token on its socket is that kernel.

Endpoints are made without starting anything: the engine calls this inside
its event loop, and the sandbox starts with the kernel's launch.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Final

from alkera_notebook.rpc.service import SOCKET_PATH_MAX, UnixEndpoint

from alkera_cli.notebooks.kernel_sandbox import (
    SOCKET_SUBDIR,
    KernelIdentity,
    checked_kernel_id,
)

#: The socket's name in its kernel's directory.
SOCKET_NAME: Final = "k.sock"

Chown = Callable[[Path, int, int], None]


def chown_nofollow(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid, follow_symlinks=False)


def socket_dir(runtime_dir: Path, kernel_id: str) -> Path:
    """The directory ``kernel_id``'s socket is made in."""
    return runtime_dir / SOCKET_SUBDIR / checked_kernel_id(kernel_id)


class BoxKernelTransport:
    """``KernelTransport`` for one workspace's kernel sandbox."""

    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir

    def endpoint(self, kernel_id: str) -> UnixEndpoint:
        """A fresh directory for ``kernel_id``'s socket, the worker's own until
        the kernel's launch hands it over. Refuses a socket path the host's
        ``sockaddr_un`` cannot hold."""
        directory = socket_dir(self.runtime_dir, kernel_id)
        path = directory / SOCKET_NAME
        if len(os.fsencode(str(path))) >= SOCKET_PATH_MAX:
            raise ValueError(f"the kernel socket path is too long: {path}")
        shutil.rmtree(directory, ignore_errors=True)
        (self.runtime_dir / SOCKET_SUBDIR).mkdir(parents=True, exist_ok=True)
        directory.mkdir(mode=0o700)
        return UnixEndpoint(directory, path)


def hand_over(
    endpoint_path: Path,
    data_dir: Path,
    identity: KernelIdentity,
    *,
    chown: Chown = chown_nofollow,
) -> None:
    """Make the kernel's socket and data directory its own: the socket's
    directory and the data directory to the kernel's uid alone (0700), the
    socket (already bound by the engine) to its uid and the files group."""
    directory = endpoint_path.parent
    for owned in (directory, data_dir):
        chown(owned, identity.uid, identity.gid)
        os.chmod(owned, 0o700)
    if endpoint_path.exists():
        chown(endpoint_path, identity.uid, identity.gid)


__all__ = [
    "SOCKET_NAME",
    "BoxKernelTransport",
    "Chown",
    "chown_nofollow",
    "hand_over",
    "socket_dir",
]
