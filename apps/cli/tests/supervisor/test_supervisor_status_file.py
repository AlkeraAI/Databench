"""A supervised box writes the status file a roll reads, as the single daemon
does: without it every roll waits out its force deadline and then drains the
box, handing every chat back."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.org_worker_protocol import Status, encode
from alkera_cli.supervisor.http import ApiError
from alkera_cli.supervisor.service import FileRoutingFeed, RouteEntry, Supervisor, Worker
from alkera_cli.supervisor.slots import SlotTable
from alkera_core.compute.box_isolation import IsolationMechanism, IsolationReport

ORG_A = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
ORG_B = "be0a3393-082f-4acf-918d-6eac00904c9b"


#: A box whose orgs each get their own namespaces.
NAMESPACED = IsolationReport(frozenset(IsolationMechanism) - {IsolationMechanism.SYSTEMD})


class Api:
    def __init__(self) -> None:
        self.refuse = False

    async def claim(self, body: Mapping[str, object]) -> str:
        return "machine-1"

    async def heartbeat(self, machine_id: str, body: Mapping[str, object]) -> None:
        if self.refuse:
            raise ApiError(503, "down")


async def _supervisor(tmp_path: Path, *orgs: str) -> tuple[Supervisor, Api, Path]:
    table = SlotTable(tmp_path / "slots.json")

    async def start(org_id: str) -> Worker:
        async def send(frame: Any) -> None:
            return None

        return Worker(slot=table.assign(org_id), alive=lambda: True, send=send)

    api = Api()
    status = tmp_path / "home" / "cloud-mirror.status.json"
    sup = Supervisor(
        api=api,  # type: ignore[arg-type]
        feed=FileRoutingFeed(tmp_path / "routing.json"),
        slots=table,
        env={"ALKERA_RELEASE_VERSION": "0.6.1", "ALKERA_BUILD_ID": "abc1234"},
        start_worker=start,
        status_path=status,
        isolation=NAMESPACED,
    )
    sup._clock = lambda: 0.0
    await sup.claim()
    await sup.reconcile([RouteEntry(f"chat-{o[:4]}", o) for o in orgs], now=0)
    return sup, api, status


async def _report(sup: Supervisor, org_id: str, **counts: Any) -> None:
    reader = asyncio.StreamReader()
    frame = Status(rss_bytes=0, **{"chats_served": 0, "chats_busy": 0, **counts})
    reader.feed_data(encode(frame))
    reader.feed_eof()
    await sup._listen(sup.workers[org_id], reader)


async def test_an_accepted_beat_writes_the_workers_counts_summed(tmp_path: Path) -> None:
    sup, _api, status = await _supervisor(tmp_path, ORG_A, ORG_B)
    await _report(sup, ORG_A, chats_served=3, chats_busy=2, chats_working=1)
    await _report(sup, ORG_B, chats_served=2, chats_busy=1, chats_working=0)
    await sup.beat()
    written = json.loads(status.read_text(encoding="utf-8"))
    assert written["pid"] == os.getpid()
    assert written["build"] == "abc1234"
    assert (written["chats_held"], written["chats_busy"], written["chats_working"]) == (5, 3, 1)
    assert written["draining"] is False


async def test_a_refused_beat_writes_nothing(tmp_path: Path) -> None:
    sup, api, status = await _supervisor(tmp_path, ORG_A)
    await _report(sup, ORG_A, chats_working=0)
    api.refuse = True
    with pytest.raises(ApiError):
        await sup.beat()
    assert not status.exists()


@pytest.mark.parametrize(
    "unknown",
    [
        pytest.param("silent-worker", id="a-worker-that-has-not-reported"),
        pytest.param("restarting", id="a-restart-in-place"),
    ],
)
async def test_counts_it_cannot_vouch_for_are_left_out(tmp_path: Path, unknown: str) -> None:
    """The roll then reads the box as not idle and waits, rather than
    restarting it under a turn nobody counted."""
    sup, _api, status = await _supervisor(tmp_path, ORG_A, ORG_B)
    await _report(sup, ORG_A, chats_served=1, chats_busy=0, chats_working=0)
    if unknown == "restarting":
        await _report(sup, ORG_B, chats_working=0)
        sup._final = False
    await sup.beat()
    written = json.loads(status.read_text(encoding="utf-8"))
    assert "chats_working" not in written and "chats_busy" not in written
