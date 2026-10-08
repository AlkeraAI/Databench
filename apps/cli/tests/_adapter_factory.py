"""The one FakeAdapter factory the harness and daemon suites share.

Every suite that drives a `HarnessRuntime` over fakes needs the same shim: build
an adapter per session, rewire its bus to the one the runtime built (so the
persist + permission pumps see the events), and record what was built so the
test can drive it. ``make`` swaps in a scripted adapter (an auto-reply, a
programmed native state, a crash); ``available`` answers the availability probe
for suites whose flows consult it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.runtime import AdapterFactory
from alkera_cli.plugins.plugin_base.delivery import SqlQueryResult
from alkera_core.project.directory import ProjectDirectory

if TYPE_CHECKING:
    from alkera_cli.harness import EventBus
    from alkera_cli.harness.adapter import SessionConfig


class FakeAdapterFactory(AdapterFactory):
    """Yields one ``make()`` adapter per session on the runtime's own bus and
    records every adapter + config in creation order."""

    def __init__(
        self,
        make: Callable[[], FakeAdapter] = FakeAdapter,
        *,
        available: bool = False,
    ) -> None:
        super().__init__(binary=None)
        self._make = make
        self._available = available
        self.adapters: list[FakeAdapter] = []
        self.configs: list[SessionConfig] = []

    def __call__(  # type: ignore[override]
        self, config: SessionConfig, *, bus: EventBus, harness_type: str = "agent"
    ) -> FakeAdapter:
        adapter = self._make()
        adapter._bus = bus
        adapter._harness_type = harness_type
        self.adapters.append(adapter)
        self.configs.append(config)
        return adapter

    def is_available(self, harness_type: str = "agent") -> bool:
        return self._available or super().is_available(harness_type)


#: How the runtime frames a finished background job when it wakes the model.
WAKE_TAG = "<backgrounded_tool_finished"


def fake_runtime(
    tmp_path: Path, make: Callable[[], FakeAdapter] = FakeAdapter
) -> tuple[HarnessRuntime, FakeAdapterFactory]:
    """A runtime over fakes in this test's workspace, with the factory that built
    them so a test can drive the adapter it got."""
    factory = FakeAdapterFactory(make)
    return HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), adapter_factory=factory), factory


async def finished_sql_job() -> SqlQueryResult:
    """A background job that completes immediately, for the queue-and-wake paths."""
    return SqlQueryResult(columns=["n"], preview_rows=[[1]], row_count=1)
