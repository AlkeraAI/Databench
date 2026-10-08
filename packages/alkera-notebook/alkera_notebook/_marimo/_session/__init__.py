# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
"""Session management for marimo server.

This module provides session management functionality including:
- Session lifecycle (creation, resumption, closure)
- Kernel management (process/thread management, interruption)
- Queue management (control, completion, input queues)
- Room management (broadcasting to multiple consumers)
- File change handling

All public APIs are re-exported from this module for backward compatibility.
"""

from __future__ import annotations

from alkera_notebook._marimo._session.types import (
    KernelManager,
    QueueManager,
    Session,
)
from alkera_notebook._marimo._session.utils import send_message_to_consumer

__all__ = [
    "KernelManager",
    "QueueManager",
    "Session",
    "send_message_to_consumer",
]
