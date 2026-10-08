# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
"""Queue and Kernel managers for session management.

This module contains the infrastructure components for managing
kernel processes/threads and their associated communication queues.

Standard implementations (QueueManagerImpl, KernelManagerImpl):
    Use multiprocessing.Process for edit mode and threading.Thread for run mode.
    Communicate via multiprocessing or threading queues.

IPC implementations (IPCQueueManagerImpl, IPCKernelManagerImpl):
    Launch kernel as subprocess with ZeroMQ IPC.
"""

from alkera_notebook._marimo._session.managers.ipc import (
    IPCKernelManagerImpl,
    IPCQueueManagerImpl,
)
from alkera_notebook._marimo._session.managers.kernel import KernelManagerImpl
from alkera_notebook._marimo._session.managers.queue import QueueManagerImpl

__all__ = [
    "IPCKernelManagerImpl",
    "IPCQueueManagerImpl",
    "KernelManagerImpl",
    "QueueManagerImpl",
]
