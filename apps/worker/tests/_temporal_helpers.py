"""Shared helpers for the worker suite's dev-server tests.

``AttemptLog`` plugs into the production interceptor's recorder seam (the
``temporal_worker`` fixture's ``recorder=``), so a test can watch individual
activity attempts — and wait for one to finish — without instrumenting the
workflow or the activity under test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AttemptLog:
    """An ``ActivityRecorder`` that keeps every attempt's start and outcome, in order."""

    starts: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)

    def record_start(
        self, *, activity_type: str, workflow_id: str, attempt: int, tool_call: Any
    ) -> None:
        self.starts.append(
            {
                "activity_type": activity_type,
                "workflow_id": workflow_id,
                "attempt": attempt,
                "tool_call": tool_call,
            }
        )

    def record_result(
        self,
        *,
        activity_type: str,
        workflow_id: str,
        attempt: int,
        outcome: str,
        duration_ms: int,
    ) -> None:
        self.results.append(
            {
                "activity_type": activity_type,
                "workflow_id": workflow_id,
                "attempt": attempt,
                "outcome": outcome,
                "duration_ms": duration_ms,
            }
        )

    @property
    def attempts(self) -> list[int]:
        """The attempt number of every finished attempt, in the order they finished."""
        return [r["attempt"] for r in self.results]

    @property
    def outcomes(self) -> list[str]:
        return [r["outcome"] for r in self.results]

    async def wait_for_results(self, count: int, *, max_wait: float = 30.0) -> None:
        """Return once at least ``count`` attempts have finished."""
        await wait_until(
            lambda: len(self.results) >= count,
            max_wait=max_wait,
            what=f"{count} finished attempt(s)",
        )


async def wait_until(
    predicate: Callable[[], bool], *, max_wait: float = 30.0, what: str = "the condition"
) -> None:
    """Poll ``predicate()`` on the running loop until it holds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"{what} did not happen within {max_wait}s")
        await asyncio.sleep(0.02)
