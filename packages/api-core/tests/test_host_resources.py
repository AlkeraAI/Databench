"""What a process may use of its machine, read from a fake ``/proc`` and
``/sys/fs/cgroup`` laid out in a temporary directory."""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_core.host_resources import (
    HostRoots,
    VolumeDevice,
    cgroup_memory_limit_bytes,
    disk_bytes,
    effective_cpus,
    effective_memory_bytes,
    host_memory_bytes,
    host_memory_in_use,
    volume_device,
)

GIB = 1024**3
#: A 93 GiB host, in ``/proc/meminfo``'s kB.
HOST_MEMINFO = "MemTotal:       97710592 kB\nMemFree:  1 kB\nMemAvailable:   87710592 kB\n"


def _tree(
    tmp_path: Path,
    *,
    self_cgroup: str | None = "0::/docker/abc123\n",
    meminfo: str | None = HOST_MEMINFO,
    files: dict[str, str] | None = None,
) -> HostRoots:
    roots = HostRoots(proc=tmp_path / "proc", cgroup=tmp_path / "cgroup")
    (roots.proc / "self").mkdir(parents=True)
    roots.cgroup.mkdir()
    if self_cgroup is not None:
        (roots.proc / "self" / "cgroup").write_text(self_cgroup)
    if meminfo is not None:
        (roots.proc / "meminfo").write_text(meminfo)
    for rel, text in (files or {}).items():
        path = roots.cgroup / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return roots


def test_effective_memory_reads_cgroup(tmp_path: Path) -> None:
    """A container with no private cgroup namespace sits in a nested group;
    its 6 GiB limit is there, not at the root, and the host's 93 GiB is not
    what it may use."""
    roots = _tree(
        tmp_path,
        files={"memory.max": "max\n", "docker/abc123/memory.max": "6442450944\n"},
    )
    assert effective_memory_bytes(roots) == 6 * GIB


def test_the_tightest_ancestor_limit_wins(tmp_path: Path) -> None:
    roots = _tree(
        tmp_path,
        self_cgroup="0::/a/b\n",
        files={"a/memory.max": str(4 * GIB), "a/b/memory.max": str(8 * GIB)},
    )
    assert cgroup_memory_limit_bytes(roots) == 4 * GIB


def test_cgroup_v1_names_its_memory_group(tmp_path: Path) -> None:
    roots = _tree(
        tmp_path,
        self_cgroup="12:cpu,cpuacct:/x\n11:memory:/docker/abc\n",
        files={
            "memory/memory.limit_in_bytes": "9223372036854771712\n",
            "memory/docker/abc/memory.limit_in_bytes": str(2 * GIB),
        },
    )
    assert effective_memory_bytes(roots) == 2 * GIB


def test_a_limit_above_the_host_is_the_host(tmp_path: Path) -> None:
    roots = _tree(tmp_path, files={"docker/abc123/memory.max": str(512 * GIB)})
    assert effective_memory_bytes(roots) == 97710592 * 1024


@pytest.mark.parametrize("text", ["max\n", "lots\n", "-1\n", "0\n", ""])
def test_a_file_that_states_no_limit_is_not_one(tmp_path: Path, text: str) -> None:
    roots = _tree(tmp_path, files={"docker/abc123/memory.max": text})
    assert cgroup_memory_limit_bytes(roots) is None
    assert effective_memory_bytes(roots) == host_memory_bytes(roots) == 97710592 * 1024


def test_nothing_readable_is_unknown(tmp_path: Path) -> None:
    roots = _tree(tmp_path, self_cgroup=None, meminfo=None)
    assert effective_memory_bytes(roots) is None
    assert host_memory_in_use(roots) is None


def test_memory_in_use_is_total_less_available(tmp_path: Path) -> None:
    roots = _tree(tmp_path)
    assert host_memory_in_use(roots) == (10_000_000 * 1024, 97710592 * 1024)


@pytest.mark.parametrize(
    ("files", "visible", "expected"),
    [
        pytest.param({"docker/abc123/cpu.max": "200000 100000\n"}, 16, 2.0, id="quota"),
        pytest.param({"docker/abc123/cpu.max": "150000 100000\n"}, 16, 1.5, id="fractional"),
        pytest.param({"cpu.max": "max 100000\n"}, 16, 16.0, id="no-quota"),
        pytest.param({"docker/abc123/cpu.max": "800000 100000\n"}, 4, 4.0, id="quota-above"),
        pytest.param(
            {"docker/cpu.max": "100000 100000\n", "docker/abc123/cpu.max": "400000 100000\n"},
            8,
            1.0,
            id="ancestor-quota",
        ),
        pytest.param({"docker/abc123/cpu.max": "junk\n"}, 3, 3.0, id="garbled"),
    ],
)
def test_effective_cpus_reads_the_quota(
    tmp_path: Path, files: dict[str, str], visible: int, expected: float
) -> None:
    roots = _tree(tmp_path, files=files)
    assert effective_cpus(roots, visible=visible) == expected


def test_disk_reads_the_nearest_existing_ancestor(tmp_path: Path) -> None:
    used, total = disk_bytes(tmp_path / "not" / "yet" / "made")
    assert total > 0
    assert 0 <= used <= total
    assert (used, total)[1] == disk_bytes(tmp_path)[1]


# -- the device under the volume ----------------------------------------------------

MOUNTS = (
    "/dev/nvme0n1p1 / ext4 rw,relatime 0 0\n"
    "proc /proc proc rw 0 0\n"
    "/dev/nvme1n1 /opt/alkera-work ext4 rw,prjquota 0 0\n"
    "overlay /var/lib/docker/overlay2/x/merged overlay rw 0 0\n"
)


def _block_tree(tmp_path: Path, mounts: str, sizes: dict[str, str]) -> HostRoots:
    roots = HostRoots(proc=tmp_path / "proc", cgroup=tmp_path / "cgroup", block=tmp_path / "block")
    (roots.proc / "self").mkdir(parents=True)
    (roots.proc / "self" / "mounts").write_text(mounts)
    for name, sectors in sizes.items():
        (roots.block / name).mkdir(parents=True)
        (roots.block / name / "size").write_text(sectors)
    return roots


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        pytest.param(
            "/opt/alkera-work/orgs/3",
            VolumeDevice("/dev/nvme1n1", 200 * GIB),
            id="the-deepest-mount-above-the-path",
        ),
        pytest.param("/opt/alkera-work", VolumeDevice("/dev/nvme1n1", 200 * GIB), id="the-mount"),
        pytest.param(
            "/opt/alkera-workshop", VolumeDevice("/dev/nvme0n1p1", 8 * GIB), id="a-prefix-only"
        ),
        pytest.param("/var/lib/docker/overlay2/x/merged/a", None, id="not-a-block-device"),
    ],
)
def test_the_volume_device_is_the_deepest_mount_and_its_size_in_sectors(
    tmp_path: Path, path: str, expected: VolumeDevice | None
) -> None:
    roots = _block_tree(
        tmp_path,
        MOUNTS,
        {"nvme1n1": f"{200 * GIB // 512}\n", "nvme0n1p1": f"{8 * GIB // 512}\n"},
    )
    assert volume_device(Path(path), roots) == expected


@pytest.mark.parametrize(
    ("mounts", "sizes"),
    [
        pytest.param("", {}, id="no-mounts-file"),
        pytest.param(MOUNTS, {}, id="no-size-for-the-device"),
        pytest.param(MOUNTS, {"nvme1n1": "lots\n"}, id="a-size-that-is-not-a-number"),
    ],
)
def test_a_volume_device_that_cannot_be_read_is_none(
    tmp_path: Path, mounts: str, sizes: dict[str, str]
) -> None:
    roots = _block_tree(tmp_path, mounts, sizes)
    if not mounts:
        (roots.proc / "self" / "mounts").unlink()
    assert volume_device(Path("/opt/alkera-work/orgs"), roots) is None
