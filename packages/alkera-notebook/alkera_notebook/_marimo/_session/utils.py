# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
"""Utility functions for session management."""

from __future__ import annotations

from typing import TYPE_CHECKING

from alkera_notebook._marimo._messaging.serde import serialize_kernel_message
from alkera_notebook._marimo._session.model import ConnectionState
from alkera_notebook._marimo._types.ids import ConsumerId

if TYPE_CHECKING:
    from alkera_notebook._marimo._messaging.notification import NotificationMessage
    from alkera_notebook._marimo._session.session import Session


def send_message_to_consumer(
    session: Session,
    operation: NotificationMessage,
    consumer_id: ConsumerId | None,
) -> None:
    """Send a message operation to a specific consumer in a session."""
    if consumer_id is None:
        return
    if session.connection_state() != ConnectionState.OPEN:
        return
    consumer = session.room.get_consumer(consumer_id)
    if consumer is not None:
        consumer.notify(serialize_kernel_message(operation))
