"""The store conformance suite bound to the drivers that must pass it by default.

The filesystem driver is the reference implementation every other driver is held
against, so it runs the whole suite here; the fault-injecting wrapper runs it
again with an empty schedule, which proves the wrapper is transparent when it is
not asked to break anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_core.files.clock import FakeClock
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_test_support.files.faulty_store import FaultSchedule, FaultyStore
from conformance import StoreConformance


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> FilesystemStore:
    return FilesystemStore(tmp_path / "store", clock=clock.now)


class TestFilesystemStoreConformance(StoreConformance):
    """The filesystem driver against the portable contract."""


@pytest.fixture
def faulty_store(store: FilesystemStore) -> FaultyStore:
    return FaultyStore(store, FaultSchedule([]))


class TestFaultyStoreConformance(TestFilesystemStoreConformance):
    """The same driver behind the fault injector, with nothing scheduled.

    It subclasses the filesystem binding rather than the bare contract so the
    two stay in lockstep: a case the reference driver is known to fail is failed
    here for the same reason, and the day the driver is fixed both bindings pass
    together instead of one of them turning into a strict xpass.
    """

    @pytest.fixture
    def store(self, faulty_store: FaultyStore) -> FaultyStore:
        return faulty_store
