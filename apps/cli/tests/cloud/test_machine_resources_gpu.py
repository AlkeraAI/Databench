"""The box's GPUs, as ``nvidia-smi`` reports them, and what it can do with them.

A box with no driver, or a driver that refuses, has no GPUs: the heartbeat that
carries the sample must never fail for it. The parser reads the exact columns
the query asks for, from output in the shape ``nvidia-smi`` prints.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_cli.cloud.machine_resources import (
    NVIDIA_SMI_ARGS,
    LastActivity,
    gpu_capabilities,
    parse_nvidia_smi,
    sample_gpus,
    sample_resources,
)
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.schemas.compute_machines import MachineResources

MIB = 1024 * 1024

#: ``nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu
#: --format=csv,noheader,nounits`` on a two-A40 pod, one busy.
TWO_A40 = "0, NVIDIA A40, 1, 46068, 0\n1, NVIDIA A40, 40112, 46068, 97\n"

#: A MIG slice reports no utilization of its own.
MIG_SLICE = "0, NVIDIA H100 80GB HBM3 MIG 1g.10gb, 13, 9728, [N/A]\n"


def test_two_gpus_are_read_column_by_column() -> None:
    assert parse_nvidia_smi(TWO_A40) == [
        {
            "index": 0,
            "name": "NVIDIA A40",
            "memory_used_bytes": 1 * MIB,
            "memory_total_bytes": 46068 * MIB,
            "utilization_percent": 0.0,
        },
        {
            "index": 1,
            "name": "NVIDIA A40",
            "memory_used_bytes": 40112 * MIB,
            "memory_total_bytes": 46068 * MIB,
            "utilization_percent": 97.0,
        },
    ]


def test_a_utilization_the_driver_does_not_report_reads_zero() -> None:
    (gpu,) = parse_nvidia_smi(MIG_SLICE)
    assert gpu["utilization_percent"] == 0.0
    assert gpu["memory_total_bytes"] == 9728 * MIB


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty"),
        pytest.param("No devices were found\n", id="no-devices-sentence"),
        pytest.param("0, NVIDIA A40, 1, 46068\n", id="a-column-short"),
        pytest.param("x, NVIDIA A40, 1, 46068, 0\n", id="index-not-a-number"),
        pytest.param("0, NVIDIA A40, [N/A], 46068, 0\n", id="memory-not-a-number"),
        pytest.param("0, , 1, 46068, 0\n", id="no-name"),
    ],
)
def test_a_row_that_cannot_be_read_is_skipped_not_guessed(text: str) -> None:
    assert parse_nvidia_smi(text) == []


def test_every_parsed_sample_is_what_the_backend_accepts() -> None:
    MachineResources.model_validate({"gpus": parse_nvidia_smi(TWO_A40 + MIG_SLICE)})


def _runner(result: subprocess.CompletedProcess[str] | Exception, seen: list[list[str]]) -> Any:
    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        if isinstance(result, Exception):
            raise result
        return result

    return run


def test_the_binary_on_the_path_is_asked_the_one_query() -> None:
    seen: list[list[str]] = []
    gpus = sample_gpus(
        which=lambda _name: "/usr/bin/nvidia-smi",
        run=_runner(subprocess.CompletedProcess([], 0, stdout=TWO_A40, stderr=""), seen),
    )
    assert [g["index"] for g in gpus] == [0, 1]
    assert seen == [["/usr/bin/nvidia-smi", *NVIDIA_SMI_ARGS]]


def test_a_missing_binary_is_no_gpus_and_runs_nothing() -> None:
    seen: list[list[str]] = []
    assert sample_gpus(which=lambda _name: None, run=_runner(OSError("unused"), seen)) == []
    assert seen == []


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(
            subprocess.CompletedProcess(
                [], 9, stdout="", stderr="NVIDIA-SMI has failed because it couldn't communicate"
            ),
            id="nonzero-exit",
        ),
        pytest.param(
            subprocess.CompletedProcess([], 255, stdout=TWO_A40, stderr=""),
            id="nonzero-exit-with-output-is-not-trusted",
        ),
        pytest.param(subprocess.TimeoutExpired(["nvidia-smi"], 5), id="wedged-driver-times-out"),
        pytest.param(PermissionError("denied"), id="cannot-execute"),
    ],
)
def test_a_failing_call_is_no_gpus(outcome: Any) -> None:
    seen: list[list[str]] = []
    gpus = sample_gpus(which=lambda _name: "/usr/bin/nvidia-smi", run=_runner(outcome, seen))
    assert gpus == []
    assert seen  # it was asked


def test_the_heartbeat_sample_carries_the_gpus(tmp_path: Any) -> None:
    sample = sample_resources(
        work_dir=tmp_path, cgroup_root=tmp_path / "none", gpus=lambda: parse_nvidia_smi(TWO_A40)
    )
    assert sample is not None
    assert len(sample["gpus"]) == 2
    MachineResources.model_validate(sample)


@pytest.mark.parametrize(
    ("gpus", "mode", "devices", "nvproxy", "expected"),
    [
        pytest.param([], "none", True, False, [], id="no-gpu-nothing"),
        pytest.param(
            [{"index": 0}],
            "none",
            True,
            False,
            [BoxCapability.GPU, BoxCapability.GPU_PASSTHROUGH],
            id="unsandboxed-with-devices-passes-through",
        ),
        pytest.param(
            [{"index": 0}],
            "none",
            False,
            False,
            [BoxCapability.GPU],
            id="unsandboxed-without-device-nodes-does-not",
        ),
        pytest.param(
            [{"index": 0}], "gvisor", True, False, [BoxCapability.GPU], id="gvisor-without-nvproxy"
        ),
        pytest.param(
            [{"index": 0}],
            "gvisor",
            True,
            True,
            [BoxCapability.GPU, BoxCapability.GPU_PASSTHROUGH],
            id="gvisor-with-nvproxy",
        ),
    ],
)
def test_what_a_box_can_do_with_its_gpus(
    gpus: list[dict[str, Any]], mode: str, devices: bool, nvproxy: bool, expected: list[str]
) -> None:
    assert (
        gpu_capabilities(gpus, sandbox_mode=mode, nvidia_devices=devices, gvisor_nvproxy=nvproxy)
        == expected
    )


def test_last_activity_moves_only_while_a_chat_works() -> None:
    now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    clock = [now]
    activity = LastActivity(clock=lambda: clock[0])
    assert activity.observe([False, False]) is None
    assert activity.observe([False, True]) == now
    clock[0] = now + timedelta(minutes=10)
    # Nothing working: the stamp stays where the work was.
    assert activity.observe([False]) == now
    assert activity.note() == now + timedelta(minutes=10)
