"""What the account lifecycle has to tell a person, as plain data.

The lifecycle decides that a person is told something; it does not send it.
Sending mail is a delivery adapter (``alkera_core.email``, which pulls in its
templates and SMTP client), and a domain module never imports one: the
process that runs the lifecycle (the worker) hands it an
:data:`AccountNoticeSender` that delivers each notice.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class DeletionBlocked:
    """A due erasure waits, for the reason ``code`` names."""

    to: str
    name: str
    code: str


@dataclass(frozen=True)
class DeletionCompleted:
    """A person's account was erased."""

    to: str
    name: str


AccountNotice = DeletionBlocked | DeletionCompleted

#: Delivers one notice. Called after the rows it describes have committed.
AccountNoticeSender = Callable[[AccountNotice], Awaitable[None]]


__all__ = [
    "AccountNotice",
    "AccountNoticeSender",
    "DeletionBlocked",
    "DeletionCompleted",
]
