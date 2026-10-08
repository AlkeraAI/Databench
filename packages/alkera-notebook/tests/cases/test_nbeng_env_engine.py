"""Environments through the engine: switching restarts the kernel and records
the choice in the file; installing into a script environment edits the
notebook's PEP 723 block through a document op. Real ``uv``, offline, against
a local wheel directory."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.document.ops import SetSetting
from alkera_notebook.engine import Actor, EnvAction, ForbiddenError, SettingsChange
from alkera_notebook.envs import EnvBuildError, LocalCommandRunner, LocalEnvRegistry
from nbeng_env_support import DEFAULT_PYPROJECT, PY, UV, build_wheel, lock_project, uv_env
from nbeng_harness import ANN, engine_for, notebook, run_cells, text_of

pytestmark = pytest.mark.skipif(UV is None, reason="uv is not on PATH")


def make_venv(path: Path, cache: Path) -> None:
    assert UV is not None
    subprocess.run(
        [UV, "venv", str(path), "--python", PY],
        check=True,
        env=uv_env(cache),
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


def registry(tmp_path: Path, wheels: Path | None = None) -> LocalEnvRegistry:
    index = ["--no-index", "--find-links", str(wheels)] if wheels else []
    return LocalEnvRegistry(
        tmp_path / "ws",
        tmp_path / "ws-envs",
        runner=LocalCommandRunner(),
        python=PY,
        uv=UV or "uv",
        index_args=index,
        cache_dir=tmp_path / "uv-cache",
    )


async def test_env_switching_restarts_the_kernel_and_records_env(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    make_venv(ws / "env-a", tmp_path / "uv-cache")
    make_venv(ws / "env-b", tmp_path / "uv-cache")
    async with engine_for(tmp_path, envs=registry(tmp_path)) as engine:
        _, ann, (a,) = await notebook(
            engine, ["import sys\nsys.prefix"], settings={"env": "./env-a"}
        )
        await run_cells(ann, a)
        first = await ann.kernel("status")
        assert first.env is not None and first.env.kind == "venv"
        assert (await text_of(ann, a)).strip("'").endswith("env-a")
        await ann.settings(SettingsChange(env="./env-b"))
        second = await ann.kernel("status")
        assert second.kernel_id != first.kernel_id and second.state == "idle"
        assert second.env is not None and second.env.env_id != first.env.env_id
        assert 'env = "./env-b"' in (ws / "nb.alknb.py").read_text()
        await run_cells(ann, a)
        assert (await text_of(ann, a)).strip("'").endswith("env-b")


async def test_env_install_into_a_script_env_edits_the_pep723_block(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    build_wheel(wheels, "tinypkg", "1.2.3")
    async with engine_for(tmp_path, envs=registry(tmp_path, wheels)) as engine:
        _, ann, (a,) = await notebook(
            engine, ["import tinypkg\ntinypkg.VERSION"], settings={"env": "script"}
        )
        await ann.apply(
            [SetSetting(key="header", value="# /// script\n# dependencies = []\n# ///")], None
        )
        result = await ann.env(EnvAction(action="install", packages=["tinypkg"]))
        assert result.env is not None and result.env.kind == "script"
        assert result.env.state == "ready"
        text = (tmp_path / "ws" / "nb.alknb.py").read_text()
        assert '"tinypkg"' in text.split("# ///")[1]
        record = await run_cells(ann, a, timeout_s=60)
        assert record.status == "ok", record
        assert await text_of(ann, a) == "'1.2.3'"


async def test_env_unknown_module_requires_an_explicit_distribution(tmp_path: Path) -> None:
    from alkera_notebook.envs import DistributionRequiredError

    reg = registry(tmp_path)
    assert reg.install_for_module("sklearn") == "scikit-learn"
    with pytest.raises(DistributionRequiredError):
        reg.install_for_module("some_internal_module")
    assert reg.install_for_module("some_internal_module", "internal-dist") == "internal-dist"


def _env_states(client: object) -> list[object]:
    queue = client.queue  # type: ignore[attr-defined]
    found = [e for e in queue._items if getattr(e, "type", None) == "env.state"]
    queue._items.clear()
    return found


async def test_a_spec_appearing_beside_a_notebook_without_env_is_announced(
    tmp_path: Path,
) -> None:
    """A notebook that records no ``env`` follows detection, so a
    ``pyproject.toml`` uploaded beside it makes the project its environment:
    that change is announced (``env.state`` naming the project), while the
    running kernel keeps the venv it started in, as its status says, until it
    restarts. A notebook that records its ``env`` is not moved by the spec."""
    from nbeng_harness import BOB

    ws = tmp_path / "ws"
    make_venv(ws / "env-a", tmp_path / "uv-cache")
    async with engine_for(tmp_path, envs=registry(tmp_path)) as engine:
        session, ann, (a,) = await notebook(engine, ["import sys\nsys.prefix"])
        watcher = session.attach(BOB)
        assert (await run_cells(ann, a)).status == "ok"
        first = await ann.kernel("status")
        assert first.env is not None and first.env.kind == "venv"
        assert _env_states(watcher) == []
        (ws / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0"\n')
        assert (await run_cells(ann, a)).status == "ok"
        (announced,) = _env_states(watcher)
        assert announced.env.kind == "uv_project"  # type: ignore[attr-defined]
        assert announced.env.recorded_in_file is False  # type: ignore[attr-defined]
        still = await ann.kernel("status")
        assert still.kernel_id == first.kernel_id
        assert still.env is not None and still.env.env_id == first.env.env_id
        assert (await text_of(ann, a)).strip("'").endswith("env-a")
        # Asked again, the same selection is not announced twice.
        assert (await run_cells(ann, a)).status == "ok"
        assert _env_states(watcher) == []


async def test_a_recorded_env_is_not_moved_by_a_spec_appearing(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    make_venv(ws / "env-a", tmp_path / "uv-cache")
    async with engine_for(tmp_path, envs=registry(tmp_path)) as engine:
        from nbeng_harness import BOB

        session, ann, (a,) = await notebook(
            engine, ["import sys\nsys.prefix"], settings={"env": "./env-a"}
        )
        watcher = session.attach(BOB)
        assert (await run_cells(ann, a)).status == "ok"
        (ws / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0"\n')
        assert (await run_cells(ann, a)).status == "ok"
        listing = await engine.env_listing(session.runtime)
        assert listing.current.kind == "venv"
        assert _env_states(watcher) == []


SCRIPT_HEADER = "# /// script\n# dependencies = []\n# ///"


async def _script_notebook(engine: object, actor: Actor = ANN) -> tuple[Any, Any]:
    session, owner, _ = await notebook(engine, ["1"], settings={"env": "script"})  # type: ignore[arg-type]
    await owner.apply([SetSetting(key="header", value=SCRIPT_HEADER)], None)
    return session, session.attach(actor)


@pytest.mark.parametrize(
    "action", [pytest.param("install", id="install"), pytest.param("remove", id="remove")]
)
async def test_a_runner_without_edit_rights_cannot_change_a_notebooks_own_packages(
    tmp_path: Path, action: str
) -> None:
    """The script environment's spec is the notebook itself, so a person who
    may run but not edit it is refused, in words that say why."""
    runner_only = Actor(kind="person", id="u-run", display_name="Rui", can_edit=False, can_run=True)
    async with engine_for(tmp_path, envs=registry(tmp_path)) as engine:
        _, rui = await _script_notebook(engine, runner_only)
        with pytest.raises(ForbiddenError, match="needs edit rights"):
            await rui.env(EnvAction(action=action, packages=["tinypkg"]))  # type: ignore[arg-type]
        assert SCRIPT_HEADER in (tmp_path / "ws" / "nb.alknb.py").read_text()


async def test_a_script_install_whose_build_fails_puts_the_block_back(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    build_wheel(wheels, "tinypkg", "1.2.3")
    async with engine_for(tmp_path, envs=registry(tmp_path, wheels)) as engine:
        _, ann = await _script_notebook(engine)
        with pytest.raises(EnvBuildError):
            await ann.env(EnvAction(action="install", packages=["nosuchpkg"]))
        text = (tmp_path / "ws" / "nb.alknb.py").read_text()
        assert "nosuchpkg" not in text and SCRIPT_HEADER in text


async def test_a_person_removes_a_package_from_the_default_environment(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    build_wheel(wheels, "tinypkg", "1.2.3")
    lock_dir = tmp_path / "tpl"
    lock_dir.mkdir()
    (lock_dir / "pyproject.toml").write_text(DEFAULT_PYPROJECT)
    lock_project(lock_dir, wheels, tmp_path / "lock-cache")
    reg = LocalEnvRegistry(
        tmp_path / "ws",
        tmp_path / "ws-envs",
        runner=LocalCommandRunner(),
        python=PY,
        uv=UV or "uv",
        index_args=["--no-index", "--find-links", str(wheels)],
        cache_dir=tmp_path / "uv-cache",
        default_template=lock_dir,
    )
    async with engine_for(tmp_path, envs=reg) as engine:
        _, ann, _ = await notebook(engine, ["1"], settings={"env": "default"})
        built = await ann.env(EnvAction(action="materialize"))
        assert built.env is not None and built.env.allowed_actions == ["install", "remove"]
        listed = await ann.env_packages(built.env.env_id)
        assert listed.requirements == ["tinypkg"]
        removed = await ann.env(EnvAction(action="remove", packages=["tinypkg"]))
        assert removed.env is not None and removed.env.state == "ready"
        after = await ann.env_packages(built.env.env_id)
        assert after.requirements == [] and "tinypkg" not in [p.name for p in after.packages]


async def test_a_kernel_on_a_replaced_build_says_a_newer_environment_is_ready(
    tmp_path: Path,
) -> None:
    """An install builds a new generation while the kernel keeps running on
    the one it started on, where the new package does not import. The
    notebook says a newer environment is ready, and a restart takes it."""
    wheels = tmp_path / "wheels"
    build_wheel(wheels, "tinypkg", "1.2.3")
    build_wheel(wheels, "otherpkg", "2.0.0")
    lock_dir = tmp_path / "tpl"
    lock_dir.mkdir()
    (lock_dir / "pyproject.toml").write_text(DEFAULT_PYPROJECT)
    lock_project(lock_dir, wheels, tmp_path / "lock-cache")
    reg = LocalEnvRegistry(
        tmp_path / "ws",
        tmp_path / "ws-envs",
        runner=LocalCommandRunner(),
        python=PY,
        uv=UV or "uv",
        index_args=["--no-index", "--find-links", str(wheels)],
        cache_dir=tmp_path / "uv-cache",
        default_template=lock_dir,
    )
    async with engine_for(tmp_path, envs=reg) as engine:
        _, ann, (a,) = await notebook(
            engine, ["import tinypkg\ntinypkg.VERSION"], settings={"env": "default"}
        )
        await ann.env(EnvAction(action="materialize"))
        assert (await run_cells(ann, a, timeout_s=60)).status == "ok"
        assert (await ann.kernel("status")).env_outdated is False

        await ann.env(EnvAction(action="install", packages=["otherpkg"]))
        assert (await ann.kernel("status")).env_outdated is True
        view = await ann.read()
        said = [n for n in view.notices if n.kind == "env_newer"]
        assert [n.message for n in said] == [
            "A newer environment is ready. Restart the kernel to use it."
        ]

        await ann.kernel("restart")
        assert (await ann.kernel("status")).env_outdated is False
