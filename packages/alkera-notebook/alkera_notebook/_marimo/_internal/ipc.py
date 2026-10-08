# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
"""Internal API for inter-process communication (IPC)."""

from alkera_notebook._marimo._ipc.connection import Channel, Connection
from alkera_notebook._marimo._ipc.queue_manager import QueueManager
from alkera_notebook._marimo._ipc.types import ConnectionInfo, KernelArgs

__all__ = [
    "Channel",
    "Connection",
    "ConnectionInfo",
    "KernelArgs",
    "QueueManager",
]
