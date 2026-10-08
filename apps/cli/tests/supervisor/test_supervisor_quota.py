"""Each org's share of the box's disk."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from alkera_cli.org_root import OrgRoot
from alkera_cli.supervisor import service
from alkera_cli.supervisor.quota import (
    PROJECT_BASE,
    mountpoint_of,
    project_id,
    quota_steps,
)
from alkera_cli.supervisor.service import FileRoutingFeed, Supervisor
from alkera_cli.supervisor.slots import Slot, SlotTable
from alkera_core.compute.box_isolation import IsolationMechanism, IsolationReport
from alkera_core.host_resources import VolumeDevice

SINGLE_ORG = IsolationReport(frozenset())
NAMESPACED = IsolationReport(frozenset(IsolationMechanism) - {IsolationMechanism.SYSTEMD})

ORG = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"


def test_each_slot_is_a_project_of_its_own() -> None:
    ids = {project_id(Slot(i, ORG)) for i in range(64)}
    assert len(ids) == 64 and min(ids) == PROJECT_BASE


def test_the_root_inherits_the_slot_s_project_and_the_project_is_capped() -> None:
    root = Path("/opt/alkera-work/orgs/3")
    steps = quota_steps(
        Slot(3, ORG), root, limit_bytes=5 << 30, mountpoint=Path("/opt/alkera-work")
    )
    assert steps == (
        ("chattr", "+P", "-p", str(PROJECT_BASE + 3), str(root)),
        (
            "setquota",
            "-P",
            str(PROJECT_BASE + 3),
            "0",
            str((5 << 30) // 1024),
            "0",
            "0",
            "/opt/alkera-work",
        ),
    )


def test_the_mountpoint_is_found_above_the_root(tmp_path: Path) -> None:
    mount = mountpoint_of(tmp_path / "orgs" / "0")
    assert mount.is_dir() and (tmp_path.resolve() == mount or mount in tmp_path.resolve().parents)


def _supervisor(tmp_path: Path, isolation: IsolationReport, env: dict[str, str]) -> Supervisor:
    return Supervisor(
        api=None,  # type: ignore[arg-type]  # nothing here claims or beats
        feed=FileRoutingFeed(tmp_path / "routing.json"),
        slots=SlotTable(tmp_path / "slots.json"),
        orgs_root=tmp_path / "orgs",
        env=env,
        isolation=isolation,
        log_dir=tmp_path / "logs",
    )


@pytest.mark.parametrize(
    ("isolation", "tenancy", "limit"),
    [
        pytest.param(SINGLE_ORG, "pool", 500 << 30, id="single-org-box"),
        pytest.param(NAMESPACED, "dedicated", 500 << 30, id="a-box-bought-for-one-org"),
        pytest.param(NAMESPACED, "pool", 125 << 30, id="a-shared-box-splits-it"),
    ],
)
async def test_an_org_alone_on_its_box_may_fill_the_volume_it_paid_for(
    tmp_path: Path, isolation: IsolationReport, tenancy: str, limit: int
) -> None:
    """A 500 GB RunPod machine an org bought, with a four-worker memory budget,
    capped that org at 125 GB: the volume was split as if other orgs could
    come."""
    sup = _supervisor(tmp_path, isolation, {"ALKERA_MACHINE_TENANCY": tenancy})
    sup.resources = lambda: {"disk_total_bytes": 500 << 30, "org_worker_capacity": 4}  # type: ignore[method-assign,assignment,return-value]
    ran: list[tuple[str, ...]] = []

    async def run(argv: Sequence[str]) -> int:
        ran.append(tuple(argv))
        return 0

    sup._run = run  # type: ignore[method-assign,assignment]
    root = tmp_path / "orgs" / "0"
    root.mkdir(parents=True)
    await sup._cap_disk(Slot(0, ORG), root)
    (setquota,) = [argv for argv in ran if argv[0] == "setquota"]
    assert setquota[4] == str(limit // 1024)


# -- a volume grown under the running box ---------------------------------------


class _Box:
    """A box whose volume the provider can grow: the device size the host
    reports, the volume's filesystem size, and the commands the supervisor ran."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, resize: int) -> None:
        self.device_bytes: int | None = 100 << 30
        self.ran: list[tuple[str, ...]] = []
        self.sup = _supervisor(tmp_path, NAMESPACED, {"ALKERA_MACHINE_TENANCY": "dedicated"})
        # The filesystem is as big as its device once ``resize2fs`` has run.
        self.fs_bytes = 100 << 30
        self.sup.resources = lambda: {  # type: ignore[method-assign,assignment,return-value]
            "disk_total_bytes": self.fs_bytes,
            "org_worker_capacity": 4,
        }

        async def run(argv: Sequence[str]) -> int:
            self.ran.append(tuple(argv))
            if argv[0] == "resize2fs" and resize == 0 and self.device_bytes is not None:
                self.fs_bytes = self.device_bytes
            return resize if argv[0] == "resize2fs" else 0

        self.sup._run = run  # type: ignore[method-assign,assignment]
        monkeypatch.setattr(
            service.host_resources,
            "volume_device",
            lambda _path: (
                None
                if self.device_bytes is None
                else VolumeDevice("/dev/nvme1n1", self.device_bytes)
            ),
        )
        monkeypatch.setattr(
            service, "ensure_org_root", lambda root, slot: OrgRoot(root / str(slot.index))
        )

    def limits(self) -> list[int]:
        return [int(argv[4]) * 1024 for argv in self.ran if argv[0] == "setquota"]

    def resizes(self) -> int:
        return sum(1 for argv in self.ran if argv[0] == "resize2fs")


async def test_a_volume_grown_under_the_box_grows_its_filesystem_and_the_org_s_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = _Box(tmp_path, monkeypatch, resize=0)
    box.sup._slots.assign(ORG)
    await box.sup.follow_volume()
    # At start the filesystem is grown into whatever the device has (a volume
    # grown while the box was stopped), and the org's share set to it.
    assert (box.resizes(), box.limits()) == (1, [100 << 30])

    await box.sup.follow_volume()
    assert (box.resizes(), box.limits()) == (1, [100 << 30])

    box.device_bytes = 200 << 30
    await box.sup.follow_volume()
    assert box.ran[-3:] == [
        ("resize2fs", "/dev/nvme1n1"),
        ("chattr", "+P", "-p", str(PROJECT_BASE), str(tmp_path / "orgs" / "0")),
        box.ran[-1],
    ]
    assert box.limits() == [100 << 30, 200 << 30]


async def test_a_filesystem_that_does_not_grow_keeps_the_org_s_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = _Box(tmp_path, monkeypatch, resize=1)
    box.sup._slots.assign(ORG)
    box.device_bytes = 200 << 30
    await box.sup.follow_volume()
    await box.sup.follow_volume()
    # Tried once for this size, not on every beat; no share is set past what
    # the filesystem holds.
    assert (box.resizes(), box.limits()) == (1, [])


async def test_a_volume_on_no_block_device_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = _Box(tmp_path, monkeypatch, resize=0)
    box.device_bytes = None
    await box.sup.follow_volume()
    assert box.ran == []


async def test_each_beat_follows_the_volume_before_it_reports_the_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = _Box(tmp_path, monkeypatch, resize=0)
    beats: list[int] = []

    class _Api:
        async def heartbeat(self, _machine_id: str, body: dict[str, object]) -> None:
            resources = body["resources"]
            assert isinstance(resources, dict)
            beats.append(resources["disk_total_bytes"])

    box.sup._api = _Api()  # type: ignore[assignment]
    box.sup._machine_id = "m-1"
    box.sup.heartbeat_body = lambda: {"resources": box.sup.resources(), "draining": False}  # type: ignore[method-assign]
    box.device_bytes = 300 << 30
    await box.sup.beat()
    assert beats == [300 << 30]
