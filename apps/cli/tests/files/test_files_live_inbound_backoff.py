"""A node whose inbound download is refused is not asked for on every pass.

A link the drive keeps but the box will not materialize, or bytes another
holder has not sent yet, answer the download with the same refusal every time.
The box asked again on every inbound pass, two seconds apart, with a warning
each time, for as long as it held the folder. The refusal now waits a widening
interval, is said once per streak, and a newer entry for the node (the drive
changed it again) is asked for at once.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.files.inbound_backoff import INBOUND_RETRY_FIRST, INBOUND_RETRY_MAX
from alkera_cli.files.live_sync import InboundEntry, LiveEntry
from files._live_sync_fakes import FakeClock, FakeInboundApi, make_sync


@dataclass
class _CountingApi(FakeInboundApi):
    """A drive that counts every download it is asked for, refused or not."""

    asked: list[str] = field(default_factory=list)

    def download(self, node_id: str, into: Path, **kwargs: Any) -> None:
        self.asked.append(node_id)
        super().download(node_id, into, **kwargs)


def _owed(tmp_path: Path) -> tuple[Any, _CountingApi, FakeClock]:
    root = tmp_path / "scratch"
    root.mkdir()
    clock = FakeClock()
    api = _CountingApi(root=root, clock=clock)
    api.queued = [InboundEntry(node_id="link-node", state="inbound", seq=3)]
    api.paths = {"link-node": "Home/work/dl-host-auth"}
    api.contents = {"link-node": b"bytes"}
    api.refuse, api.refuse_on = "files.live_pending", "download"
    sync = make_sync(root, api, clock, root_path="Home/work")
    return sync, api, clock


def _pass(sync: Any) -> list[LiveEntry]:
    """One inbound pass under a lease whose beats are landing."""
    sync.fence.beat()
    answered: list[LiveEntry] = sync.pull_inbound()
    return answered


def test_a_refused_download_is_not_asked_for_again_until_its_wait_has_passed(
    tmp_path: Path,
) -> None:
    sync, api, clock = _owed(tmp_path)
    _pass(sync)
    assert api.asked == ["link-node"]

    clock.advance(INBOUND_RETRY_FIRST / 2)
    _pass(sync)
    assert api.asked == ["link-node"], "asked again inside the wait"

    clock.advance(INBOUND_RETRY_FIRST)
    _pass(sync)
    assert api.asked == ["link-node", "link-node"]


def test_the_wait_doubles_with_every_refusal_up_to_its_ceiling(tmp_path: Path) -> None:
    sync, api, clock = _owed(tmp_path)
    asked_at: list[float] = []
    for _ in range(int(INBOUND_RETRY_MAX * 4)):
        before = len(api.asked)
        _pass(sync)
        if len(api.asked) > before:
            asked_at.append(clock.now)
        clock.advance(1.0)
    waits = [later - earlier for earlier, later in itertools.pairwise(asked_at)]
    assert waits[:4] == [
        INBOUND_RETRY_FIRST,
        INBOUND_RETRY_FIRST * 2,
        INBOUND_RETRY_FIRST * 4,
        INBOUND_RETRY_FIRST * 8,
    ]
    assert max(waits) == INBOUND_RETRY_MAX
    assert waits[-1] == INBOUND_RETRY_MAX


def test_a_newer_entry_for_the_node_is_asked_for_at_once(tmp_path: Path) -> None:
    sync, api, _clock = _owed(tmp_path)
    _pass(sync)
    api.queued = [InboundEntry(node_id="link-node", state="inbound", seq=4)]
    _pass(sync)
    assert api.asked == ["link-node", "link-node"]


def test_a_download_that_lands_clears_the_wait(tmp_path: Path) -> None:
    sync, api, clock = _owed(tmp_path)
    _pass(sync)
    clock.advance(INBOUND_RETRY_FIRST)
    api.refuse = None
    assert _pass(sync) == [LiveEntry(node_id="link-node", state="applied")]
    # The drive changes the node again and this time refuses: the wait starts
    # over from the first one, not from where the old streak left off.
    api.refuse = "files.live_pending"
    api.queued = [InboundEntry(node_id="link-node", state="inbound", seq=5)]
    _pass(sync)
    clock.advance(INBOUND_RETRY_FIRST)
    _pass(sync)
    assert api.asked == ["link-node", "link-node", "link-node", "link-node"]


def test_a_streak_of_refusals_is_said_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sync, _api, clock = _owed(tmp_path)
    with caplog.at_level(logging.WARNING, logger="alkera_cli.files.live_sync"):
        for _ in range(5):
            _pass(sync)
            clock.advance(INBOUND_RETRY_MAX)
    said = [r for r in caplog.records if "inbound download of link-node failed" in r.message]
    assert len(said) == 1
    assert len(_api.asked) == 5, "every pass past its wait did ask again"
