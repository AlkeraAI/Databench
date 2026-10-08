# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
"""Experimental IPC implementation (using ZeroMQ)."""

from __future__ import annotations

from alkera_notebook._marimo._ipc.queue_manager import QueueManager
from alkera_notebook._marimo._ipc.types import KernelArgs

__all__ = [
    "KernelArgs",
    "QueueManager",
]
