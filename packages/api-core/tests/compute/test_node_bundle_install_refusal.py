"""Which hosts a deployment's node bundles can install, and the sentence an
admin reads when one cannot be."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from alkera_core.compute.node_bundle import held_targets, install_refusal

REBUILD_X64 = (
    "Rebuild the backend and worker images with NODE_BUNDLE_ARCHES=all, "
    "or build the linux-x64 bundle into NODE_BUNDLE_DIR on an amd64 computer."
)


def _bundles(tmp: Path, *targets: str) -> str:
    entries = {}
    for target in targets:
        body = f"bundle for {target}".encode()
        name = f"alkera-{target}.tar.gz"
        (tmp / name).write_bytes(body)
        entries[target] = {
            "file": name,
            "sha256": hashlib.sha256(body).hexdigest(),
            "size": len(body),
        }
    (tmp / "manifest.json").write_text(
        json.dumps({"version": "1.0.0+abc", "targets": entries}), encoding="utf-8"
    )
    return str(tmp)


@pytest.mark.parametrize(
    ("held", "machine"),
    [
        pytest.param(("linux-x64",), "x86_64", id="x64-host-x64-bundle"),
        pytest.param(("linux-arm64",), "aarch64", id="arm-host-arm-bundle"),
        pytest.param(("linux-arm64",), "arm64", id="arm-host-named-arm64"),
        pytest.param(("linux-x64", "linux-arm64"), "x86_64", id="both-x64-host"),
        pytest.param(("linux-x64", "linux-arm64"), "aarch64\n", id="both-arm-host"),
    ],
)
def test_a_host_with_a_bundle_for_its_architecture_is_not_refused(
    tmp_path: Path, held: tuple[str, ...], machine: str
) -> None:
    assert install_refusal(_bundles(tmp_path, *held), machine) is None


def test_an_x86_host_against_an_arm_only_install_names_both_and_the_fix(tmp_path: Path) -> None:
    refusal = install_refusal(_bundles(tmp_path, "linux-arm64"), "x86_64")
    assert refusal == (
        "This server is x86_64, but this install only has the arm64 machine bundle. " + REBUILD_X64
    )


def test_an_arm_host_against_an_x64_only_install_is_refused(tmp_path: Path) -> None:
    refusal = install_refusal(_bundles(tmp_path, "linux-x64"), "aarch64")
    assert refusal is not None
    assert refusal.startswith(
        "This server is aarch64, but this install only has the amd64 machine bundle."
    )
    assert "build the linux-arm64 bundle into NODE_BUNDLE_DIR on an arm64 computer" in refusal


def test_an_install_with_no_usable_bundle_says_so(tmp_path: Path) -> None:
    refusal = install_refusal(str(tmp_path), "x86_64")
    assert refusal == (
        "This server is x86_64, but this install has no machine bundle. " + REBUILD_X64
    )


def test_a_listed_bundle_whose_file_is_gone_does_not_count(tmp_path: Path) -> None:
    directory = _bundles(tmp_path, "linux-x64", "linux-arm64")
    (tmp_path / "alkera-linux-x64.tar.gz").unlink()
    assert held_targets(directory) == ["linux-arm64"]
    assert install_refusal(directory, "x86_64") is not None


@pytest.mark.parametrize("machine", ["riscv64", "ppc64le", ""])
def test_an_architecture_no_bundle_is_built_for_is_refused(tmp_path: Path, machine: str) -> None:
    refusal = install_refusal(_bundles(tmp_path, "linux-x64", "linux-arm64"), machine)
    assert refusal is not None
    assert refusal.endswith("Machines run on x86_64 or arm64 (aarch64) only.")


def test_a_deployment_that_serves_no_bundles_refuses_nothing() -> None:
    """A release-download deployment (no NODE_BUNDLE_DIR) is not judged here."""
    assert install_refusal("", "x86_64") is None
    assert install_refusal("", "riscv64") is None
