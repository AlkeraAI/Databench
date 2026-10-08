"""There is no fixed environment. A notebook with no project environment of
its own runs in the workspace's shared default environment, which is built
on first use and takes installs; the only environments that refuse one are
those Alkera does not manage, and each says why and offers the default.

Real ``uv`` builds, offline against a hand-built wheel."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alkera_notebook.envs import EnvNotMaterializableError, LocalCommandRunner, LocalEnvRegistry
from alkera_notebook.envs.models import PACKAGE_MANAGED, EnvDescriptor, env_actions
from alkera_notebook.envs.static import StaticEnvRegistry
from alkera_notebook.envs.template import default_env_template
from alkera_notebook.sim.driver import SimDriver, StanceGatekeeper
from alkera_notebook.sim.engine_target import EngineTarget
from nbeng_env_support import UV, build_wheel

pytestmark = [
    pytest.mark.xdist_group("nbeng_env"),
    pytest.mark.skipif(UV is None or sys.platform == "win32", reason="needs uv and a kernel"),
]

PATH = "nb.alknb.py"


async def test_a_new_notebook_in_a_fresh_workspace_installs_and_imports_a_package(
    tmp_path: Path,
) -> None:
    """The agent's notebook.env install on the engine a box or a laptop
    builds by default (no registry handed in)."""
    wheels = tmp_path / "wheels"
    build_wheel(wheels, "tinypkg", "4.5.6")
    target = EngineTarget(
        tmp_path / "ws",
        index_args=["--no-index", "--find-links", str(wheels)],
        # A template the local index can lock (the platform's names the index's).
        default_template=default_env_template(tmp_path / "template", packages=()),
    )
    driver = SimDriver(target, gatekeeper=StanceGatekeeper("default"))  # type: ignore[arg-type]
    try:
        await driver.agent(
            "notebook.create",
            {"path": PATH, "cells": [{"source": "import tinypkg\ntinypkg.VERSION"}]},
        )
        installed = await driver.agent(
            "notebook.env", {"path": PATH, "action": "install", "packages": ["tinypkg"]}
        )
        assert installed.env.kind == "default" and installed.env.state == "ready"  # type: ignore[union-attr]
        run = await driver.agent(
            "notebook.run", {"path": PATH, "target": {"kind": "all"}, "timeout_s": 120}
        )
        assert run.status == "finished", run  # type: ignore[union-attr]
        assert run.cells[-1].output.text.content.strip() == "'4.5.6'"  # type: ignore[union-attr]
    finally:
        await driver.close()


def _desc(kind: str) -> EnvDescriptor:
    return EnvDescriptor(
        env_id=f"{kind}:x",
        kind=kind,  # type: ignore[arg-type]
        spec_root="/w/x",
        prefix="/w/x/.venv",
        interpreter="/w/x/.venv/bin/python",
        python_version="3.13",
        spec_hash="h",
        built_spec_hash="h",
        state="ready",
        recorded="./x",
    )


@pytest.mark.parametrize(
    "kind", ["default", "uv_project", "script", "venv", "requirements", "conda"]
)
def test_every_kind_takes_installs_unless_alkera_does_not_manage_it(kind: str) -> None:
    admits = "install" in env_actions(_desc(kind))
    assert admits == (kind in PACKAGE_MANAGED)
    assert PACKAGE_MANAGED == {"default", "uv_project", "script"}


async def test_an_environment_alkera_does_not_manage_says_why_and_offers_the_default(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    (ws / "own-venv").mkdir(parents=True)
    (ws / "own-venv" / "pyvenv.cfg").write_text("version_info = 3.13.1\n")
    reg = LocalEnvRegistry(ws, tmp_path / "envs", runner=LocalCommandRunner(), uv=UV or "uv")
    refusals = []
    for env_id in ("venv:own-venv",):
        with pytest.raises(EnvNotMaterializableError) as refused:
            await reg.install(env_id, ["six"], str(ws / PATH))
        refusals.append(str(refused.value))
    with pytest.raises(EnvNotMaterializableError) as given:
        await StaticEnvRegistry().install("static", ["six"], PATH)
    refusals.append(str(given.value))
    for message in refusals:
        assert "default environment" in message
        assert "fixed" not in message
