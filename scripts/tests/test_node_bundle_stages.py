"""The node bundle stages of the backend and worker images: every step runs
inside a Docker stage pinned to its platform, so a Windows, macOS or Linux host
builds them alike, and the merge of two architectures yields one manifest."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

OPEN_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = [
    OPEN_ROOT / "apps" / "backend" / "Dockerfile",
    OPEN_ROOT / "apps" / "worker" / "Dockerfile",
]
BUNDLE_SCRIPTS = ("build-node-bundle.sh", "merge-node-bundles.py")


def _script(name: str) -> Path:
    """The script in the open tree; before the move it sits at the repository
    root's ``scripts/`` beside this folder."""
    for root in (OPEN_ROOT, OPEN_ROOT.parent):
        candidate = root / "scripts" / name
        if candidate.is_file():
            return candidate
    raise AssertionError(f"no scripts/{name}")


def _stages(dockerfile: Path) -> dict[str, tuple[str, list[str]]]:
    """Each named stage: its FROM line and the instructions under it."""
    stages: dict[str, tuple[str, list[str]]] = {}
    current: list[str] | None = None
    joined = re.sub(r"\\\n", " ", dockerfile.read_text(encoding="utf-8"))
    for line in joined.splitlines():
        if line.startswith("FROM "):
            match = re.search(r"\bAS (\S+)$", line)
            current = []
            if match:
                stages[match.group(1)] = (line, current)
        elif current is not None and line and not line.startswith("#"):
            current.append(line)
    return stages


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.parent.name)
def test_each_architecture_builds_in_a_stage_pinned_to_its_platform(dockerfile: Path) -> None:
    stages = _stages(dockerfile)
    for arch in ("amd64", "arm64"):
        line, body = stages[f"node-bundle-{arch}"]
        assert line.startswith(f"FROM --platform=linux/{arch} "), line
        assert any("bash scripts/build-node-bundle.sh" in step for step in body)


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.parent.name)
def test_the_bundle_scripts_run_only_inside_bundle_stages(dockerfile: Path) -> None:
    """Nothing outside the bundle stages (and so nothing on the host) runs
    the bundle build or the merge."""
    for name, (_line, body) in _stages(dockerfile).items():
        runs = [step for step in body if step.startswith("RUN ")]
        if any(script in step for step in runs for script in BUNDLE_SCRIPTS):
            assert name in {"node-bundle-amd64", "node-bundle-arm64", "node-bundles-all"}, name


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.parent.name)
def test_the_default_carries_only_the_native_bundle(dockerfile: Path) -> None:
    text = dockerfile.read_text(encoding="utf-8")
    assert "ARG NODE_BUNDLE_ARCHES=native\n" in text
    stages = _stages(dockerfile)
    assert (
        stages["node-bundles-native"][0] == "FROM node-bundles-${TARGETARCH} AS node-bundles-native"
    )
    assert stages["node-bundle"][0] == "FROM node-bundles-${NODE_BUNDLE_ARCHES} AS node-bundle"
    _merge_line, merge_body = stages["node-bundles-all"]
    assert "COPY --from=node-bundle-amd64 /out /parts/amd64" in merge_body
    assert "COPY --from=node-bundle-arm64 /out /parts/arm64" in merge_body


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.parent.name)
def test_a_missing_emulator_fails_on_a_line_that_says_what_to_enable(dockerfile: Path) -> None:
    for arch in ("amd64", "arm64"):
        _line, body = _stages(dockerfile)[f"node-bundle-{arch}"]
        first = body[0]
        assert first.startswith("RUN echo"), first
        assert "enable emulation" in first and "NODE_BUNDLE_ARCHES=native" in first


def _part(root: Path, target: str, version: str) -> Path:
    root.mkdir()
    body = f"bundle {target}".encode()
    name = f"alkera-{target}.tar.gz"
    sha = hashlib.sha256(body).hexdigest()
    (root / name).write_bytes(body)
    (root / f"{name}.sha256").write_text(f"{sha}  {name}\n", encoding="utf-8")
    manifest = {
        "version": version,
        "targets": {target: {"file": name, "sha256": sha, "size": len(body)}},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _merge(out: Path, *parts: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(_script("merge-node-bundles.py")), str(out), *map(str, parts)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_merge_holds_both_bundles_under_one_manifest(tmp_path: Path) -> None:
    amd = _part(tmp_path / "amd64", "linux-x64", "1.2.3+b")
    arm = _part(tmp_path / "arm64", "linux-arm64", "1.2.3+b")
    done = _merge(tmp_path / "out", amd, arm)
    assert done.returncode == 0, done.stderr
    out = tmp_path / "out"
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "1.2.3+b"
    assert sorted(manifest["targets"]) == ["linux-arm64", "linux-x64"]
    for target in ("linux-x64", "linux-arm64"):
        name = f"alkera-{target}.tar.gz"
        assert (out / name).read_bytes() == f"bundle {target}".encode()
        assert (
            manifest["targets"][target]["sha256"]
            == hashlib.sha256((out / name).read_bytes()).hexdigest()
        )
        assert (out / f"{name}.sha256").is_file()


def test_the_merge_refuses_two_versions(tmp_path: Path) -> None:
    amd = _part(tmp_path / "amd64", "linux-x64", "1.2.3+a")
    arm = _part(tmp_path / "arm64", "linux-arm64", "1.2.3+b")
    done = _merge(tmp_path / "out", amd, arm)
    assert done.returncode != 0
    assert "holds version 1.2.3+b, not 1.2.3+a" in done.stderr
    assert not (tmp_path / "out" / "manifest.json").exists()
