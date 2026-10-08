"""A decisions log the permission gate can always write to.

The gate suites need a sink that takes every record, so a case is never about the
audit failing. What lands in the log is ``test_permissions_audit.py``'s subject;
here the sink only has to exist.
"""

from __future__ import annotations

from typing import Any


class MemorySink:
    def __init__(self) -> None:
        self.records: list[Any] = []

    def record(self, rec: Any) -> None:
        self.records.append(rec)
