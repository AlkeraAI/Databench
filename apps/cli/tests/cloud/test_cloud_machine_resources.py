"""A box's heartbeat carries what it is using, read the way the box is limited.

The cgroup reader is driven against a fake cgroup tree in ``tmp_path`` (v2, v1,
no limit), the sampler against that tree and a real directory, and the REST
client through an httpx ``MockTransport`` so the body on the wire is the one
the backend's schema validates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from alkera_cli.cloud.machine_resources import cgroup_memory, chats_memory, sample_resources
from alkera_cli.cloud.rest import CloudRestClient
from alkera_core.schemas.compute import MachineHeartbeatRequest

GIB = 1 << 30


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, value in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    return tmp_path


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param(
            {"memory.max": f"{4 * GIB}\n", "memory.current": f"{GIB}\n"},
            (GIB, 4 * GIB),
            id="v2-limited",
        ),
        pytest.param({"memory.max": "max\n", "memory.current": "5"}, None, id="v2-unlimited"),
        pytest.param(
            {
                "memory/memory.limit_in_bytes": f"{2 * GIB}",
                "memory/memory.usage_in_bytes": f"{GIB // 2}",
            },
            (GIB // 2, 2 * GIB),
            id="v1-limited",
        ),
        pytest.param({"memory/memory.limit_in_bytes": str(1 << 63 - 1)}, None, id="v1-unlimited"),
        pytest.param({}, None, id="no-cgroup"),
        pytest.param({"memory.max": "garbage"}, None, id="unreadable"),
    ],
)
def test_the_cgroup_limit_is_read_when_there_is_one(
    tmp_path: Path, files: dict[str, str], expected: tuple[int, int] | None
) -> None:
    assert cgroup_memory(_tree(tmp_path, files)) == expected


def test_a_sample_reports_the_cgroup_memory_and_the_work_volume(tmp_path: Path) -> None:
    root = _tree(tmp_path / "cg", {"memory.max": str(4 * GIB), "memory.current": str(GIB)})
    work = tmp_path / "work"
    work.mkdir()
    sample = sample_resources(work_dir=work, cgroup_root=root)
    assert sample is not None
    assert (sample["memory_used_bytes"], sample["memory_limit_bytes"]) == (GIB, 4 * GIB)
    assert 0 < sample["disk_used_bytes"] <= sample["disk_total_bytes"]
    assert sample["cpu_percent"] >= 0
    # Whatever the sampler says is what the backend accepts.
    MachineHeartbeatRequest.model_validate({"resources": sample})


def test_with_no_cgroup_the_host_memory_is_reported(tmp_path: Path) -> None:
    sample = sample_resources(work_dir=tmp_path / "absent", cgroup_root=tmp_path / "none")
    assert sample is not None
    assert 0 < sample["memory_used_bytes"] <= sample["memory_limit_bytes"]


@pytest.mark.parametrize("resources", [None, {"cpu_percent": 12.5, "memory_used_bytes": 7}])
async def test_the_heartbeat_body_carries_the_sample_only_when_there_is_one(
    resources: dict[str, float] | None,
) -> None:
    bodies: list[dict[str, object]] = []

    def reply(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(204)

    client = CloudRestClient(
        api_url="http://api.test/",
        token="t",
        agent_id="box",
        transport=httpx.MockTransport(reply),
    )
    await client.heartbeat_machine("m1", chats_served=2, resources=resources)
    (body,) = bodies
    if resources is None:
        assert body["resources"] is None
    else:
        assert body["resources"] == {
            "cpu_percent": 12.5,
            "memory_used_bytes": 7,
            "memory_limit_bytes": 0,
            "disk_used_bytes": 0,
            "disk_total_bytes": 0,
            "org_workers": 0,
            "org_worker_capacity": 0,
            "org_worker_memory_bytes": 0,
            "org_workers_failing": 0,
            "org_slots_free": None,
        }
    assert body["chats_served"] == 2


# -- the memory the chats compete for ------------------------------------------


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param(
            {
                "alkera.slice/memory.max": f"{6 * GIB}\n",
                "alkera.slice/memory.current": f"{5 * GIB}\n",
                "alkera.slice/memory.stat": f"anon {2 * GIB}\ninactive_file {3 * GIB}\n",
                "memory.max": f"{8 * GIB}\n",
                "memory.current": f"{7 * GIB}\n",
            },
            (2 * GIB, 6 * GIB),
            id="the-chats-slice-less-its-reclaimable-cache",
        ),
        pytest.param(
            {
                "alkera.slice/memory.max": "max\n",
                "memory.max": f"{8 * GIB}\n",
                "memory.current": f"{7 * GIB}\n",
                "memory.stat": f"inactive_file {GIB}\nactive_file {GIB}\n",
            },
            (6 * GIB, 8 * GIB),
            id="an-unbounded-slice-falls-to-the-container",
        ),
        pytest.param(
            {
                "memory/memory.limit_in_bytes": f"{4 * GIB}\n",
                "memory/memory.usage_in_bytes": f"{3 * GIB}\n",
                "memory/memory.stat": f"total_inactive_file {GIB}\n",
            },
            (2 * GIB, 4 * GIB),
            id="cgroup-v1",
        ),
    ],
)
def test_the_chats_memory_is_their_working_set_against_the_tightest_limit(
    tmp_path: Path, files: dict[str, str], expected: tuple[int, int]
) -> None:
    """A box that copied a large folder has filled its page cache to the
    limit; counting that cache as pressure would sleep every idle chat for
    memory the kernel gives back on demand."""
    assert chats_memory(_tree(tmp_path, files)) == expected


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the host fallback is Linux's")
def test_a_linux_box_with_no_cgroup_reads_the_hosts_memory(tmp_path: Path) -> None:
    found = chats_memory(tmp_path)
    assert found is not None
    used, limit = found
    assert 0 <= used <= limit


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="the host fallback is Linux's")
def test_a_mac_with_no_cgroup_reports_no_pressure_at_all(tmp_path: Path) -> None:
    """macOS counts compressed and cached pages so that "available" runs low
    on an idle machine; read as pressure, it would sleep every chat."""
    assert chats_memory(tmp_path) is None
