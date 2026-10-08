"""The conda side against a real micromamba: a captured explicit list is
recreated by ``micromamba create --file`` from a local channel, offline.

Skipped where micromamba is not installed (it is in every box sandbox, where
the conda round trip is also proven by hand; see the build brief)."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from _helpers.pyenv import MICROMAMBA, WINDOWS, offline_env
from alkera_cli.environment import LocalRunner, recreate
from alkera_cli.environment.spec import CondaPackage, CondaSpec, EnvironmentSpec, PlatformInfo

pytestmark = pytest.mark.skipif(
    MICROMAMBA is None or WINDOWS, reason="needs micromamba on a POSIX host"
)


def _conda_package(channel: Path, name: str, version: str) -> tuple[str, str]:
    """A noarch conda package holding one file; returns its file URL and md5."""
    payload = f"{name} {version}\n".encode()
    rel = f"share/{name}.txt"
    index = {
        "name": name,
        "version": version,
        "build": "0",
        "build_number": 0,
        "depends": [],
        "subdir": "noarch",
        "noarch": "generic",
        "license": "MIT",
    }
    paths = {
        "paths": [
            {
                "_path": rel,
                "path_type": "hardlink",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_in_bytes": len(payload),
            }
        ],
        "paths_version": 1,
    }
    members = {
        "info/index.json": json.dumps(index).encode(),
        "info/paths.json": json.dumps(paths).encode(),
        "info/files": (rel + "\n").encode(),
        rel: payload,
    }
    out = channel / "noarch" / f"{name}-{version}-0.tar.bz2"
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:bz2") as tar:
        for arc, data in members.items():
            info = tarfile.TarInfo(arc)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.as_uri(), hashlib.md5(out.read_bytes()).hexdigest()


def test_an_explicit_conda_list_is_recreated_by_micromamba(tmp_path: Path) -> None:
    url, md5 = _conda_package(tmp_path / "channel", "alkfixture", "1.0")
    env = offline_env(tmp_path, {"MAMBA_ROOT_PREFIX": str(tmp_path / "mamba")})
    spec = EnvironmentSpec(
        env_kind="conda",
        platform=PlatformInfo(sys_platform="linux", machine="x86_64"),
        conda=CondaSpec(
            subdir="",
            packages=[
                CondaPackage(
                    name="alkfixture", version="1.0", build="0", url=url, md5=md5, subdir="noarch"
                )
            ],
        ),
    )
    target = tmp_path / "conda-env"

    report = asyncio.run(
        recreate(spec, LocalRunner(base_env=env), target_env=str(target), target_root=str(tmp_path))
    )

    create = report.steps[0]
    assert create.purpose == "create_env" and create.exit_code == 0, create.output_tail
    assert "--file" in create.command
    assert create.inputs["explicit"].splitlines() == ["@EXPLICIT", f"{url}#{md5}"]
    assert (target / "share" / "alkfixture.txt").read_text(encoding="utf-8") == "alkfixture 1.0\n"
    assert (target / "conda-meta" / "alkfixture-1.0-0.json").is_file()
