"""A workspace's default environment starts from the platform's template.

The template is only a ``pyproject.toml`` pinning a curated set of libraries;
the lock is made once, when a workspace's spec is seeded, and every later
build is ``uv sync --frozen`` on that workspace's own lock. A spec that is
still an earlier platform template is upgraded on its next build; a spec
anyone changed keeps its change, gains only the template packages it lacks,
and builds without anyone's approval; a lock that fails leaves the spec as it
was.

Builds are the real ``uv``, offline against hand-written wheels, except the
``live`` case, which locks and installs the real template from the index.
"""

from __future__ import annotations

import os
import socket
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from alkera_notebook.envs import CommandResult, CommandRunner, EnvBuildError
from alkera_notebook.envs.detect import project_spec, sha256_hex
from alkera_notebook.envs.registry import validate_requirement
from alkera_notebook.envs.template import (
    DEFAULT_ENV_PACKAGES,
    DEFAULT_ENV_PYPROJECT,
    PAST_TEMPLATE_PYPROJECTS,
    TemplatedEnvRegistry,
    default_env_template,
    template_pyproject,
)
from alkera_notebook.tree_io import Tree
from nbeng_env_support import PY, UV, RecordingRunner, build_wheel

ENV = "default:.alkera/envs/default"

#: What every workspace's first run seeded before the curated set: a project
#: with no dependencies and its one-package lock, byte for byte.
EMPTY_TEMPLATE = {
    "pyproject.toml": b"""\
[project]
name = "default"
version = "0.0.0"
description = "The workspace's default notebook environment."
requires-python = ">=3.10"
dependencies = []

[tool.uv]
package = false
""",
    "uv.lock": b"""\
version = 1
revision = 3
requires-python = ">=3.10"

[[package]]
name = "default"
version = "0.0.0"
source = { virtual = "." }
""",
}

needs_uv = pytest.mark.skipif(UV is None, reason="uv is not on PATH")


# -- the curated set -------------------------------------------------------------


def test_every_default_package_is_a_valid_exact_pin_named_once() -> None:
    names = []
    for requirement in DEFAULT_ENV_PACKAGES:
        validate_requirement(requirement)
        name, sep, version = requirement.partition("==")
        assert sep == "==" and version and "," not in version, requirement
        assert all(op not in name for op in "<>!~=[;"), requirement
        names.append(name.lower())
    assert len(names) == len(set(names))
    assert {"polars", "pandas", "pyarrow", "duckdb", "numpy", "ipywidgets", "anywidget"} <= set(
        names
    )


def test_the_template_is_a_project_naming_exactly_the_curated_set() -> None:
    project = tomllib.loads(DEFAULT_ENV_PYPROJECT)["project"]
    assert project["dependencies"] == list(DEFAULT_ENV_PACKAGES)
    assert project["requires-python"] == ">=3.10"


#: Every template pyproject a release has shipped, by sha256. A release that
#: changes the curated set adds the new template's digest here and the one it
#: replaces to ``PAST_TEMPLATE_PYPROJECTS``.
RELEASED_TEMPLATES = {
    "16c871f5d0080ae8d6946dad6bc66a05fa294f375483a05a4100eeddaaef7d39",  # dependencies = []
    "5beb5e201cf11af91b225fdb0d022c56b1683cb1ccf56a409d38360f3df5591d",  # the curated set
}


def test_the_current_template_is_a_released_one() -> None:
    """Changing the curated set without recording the template it replaces
    would strand every workspace on the old one: it would read as changed by
    a person, and never be upgraded."""
    assert sha256_hex(DEFAULT_ENV_PYPROJECT.encode()) in RELEASED_TEMPLATES, (
        "the template changed: add the digest it replaces to PAST_TEMPLATE_PYPROJECTS "
        "and the new one to RELEASED_TEMPLATES"
    )


def test_every_earlier_template_stays_known_and_the_current_one_is_not_past() -> None:
    """The list of past templates only grows: removing an entry strands
    every workspace still on it."""
    current = sha256_hex(DEFAULT_ENV_PYPROJECT.encode())
    assert RELEASED_TEMPLATES - {current} <= PAST_TEMPLATE_PYPROJECTS
    assert current not in PAST_TEMPLATE_PYPROJECTS
    assert sha256_hex(EMPTY_TEMPLATE["pyproject.toml"]) in PAST_TEMPLATE_PYPROJECTS


def test_the_template_directory_holds_the_pyproject_and_no_lock(tmp_path: Path) -> None:
    """A box keeps its template directory across releases; a lock an
    earlier release wrote there is removed, a changed pyproject restored."""
    directory = tmp_path / "template"
    directory.mkdir()
    (directory / "uv.lock").write_bytes(EMPTY_TEMPLATE["uv.lock"])
    (directory / "pyproject.toml").write_text("garbage")
    default_env_template(directory)
    assert sorted(os.listdir(directory)) == ["pyproject.toml"]
    assert (directory / "pyproject.toml").read_text() == DEFAULT_ENV_PYPROJECT


# -- seeding, upgrading, leaving alone ---------------------------------------------


@pytest.fixture
def wheels(tmp_path: Path) -> Path:
    d = tmp_path / "wheels"
    build_wheel(d, "tinypkg", "1.0.0")
    build_wheel(d, "tinypkg", "2.0.0")
    return d


#: The pins a release before the tests' "next" one shipped.
FIRST = ("tinypkg==1.0.0",)


def _registry(
    tmp_path: Path,
    wheels: Path,
    packages: tuple[str, ...] = FIRST,
    runs: CommandRunner | None = None,
) -> tuple[TemplatedEnvRegistry, RecordingRunner, Path]:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    runner = RecordingRunner()
    template = default_env_template(tmp_path / "template", packages)
    reg = TemplatedEnvRegistry(
        ws,
        tmp_path / "envs",
        runner=runs or runner,
        python=PY,
        uv=UV or "uv",
        index_args=["--no-index", "--find-links", str(wheels)],
        default_template=template,
        # The empty template and the tests' first release are past ones.
        past_templates=PAST_TEMPLATE_PYPROJECTS | {sha256_hex(template_pyproject(FIRST).encode())},
    )
    return reg, runner, ws / ".alkera" / "envs" / "default"


def _write_spec(spec_root: Path, files: dict[str, bytes]) -> None:
    spec_root.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (spec_root / name).write_bytes(data)


def _locked_versions(spec_root: Path, name: str) -> list[str]:
    lock = tomllib.loads((spec_root / "uv.lock").read_text())
    return [p["version"] for p in lock["package"] if p["name"] == name]


def _lose_build(prefix: Path) -> None:
    assert prefix.is_symlink()
    prefix.unlink()


def _uv_steps(runner: RecordingRunner) -> list[str]:
    """The ``uv`` subcommands the registry ran (not the interpreter probe)."""
    return [argv[1] for argv in runner.calls if argv[0] == (UV or "uv")]


def _no_staging_left(spec_root: Path) -> bool:
    return not [n for n in os.listdir(spec_root.parent) if n.startswith(".default-seed")]


@needs_uv
async def test_a_fresh_workspace_seeds_the_template_locks_once_and_builds_unasked(
    tmp_path: Path, wheels: Path
) -> None:
    reg, runner, spec_root = _registry(tmp_path, wheels)
    desc = await reg.materialize(ENV)
    assert desc.state == "ready"
    assert (spec_root / "pyproject.toml").read_text() == template_pyproject(["tinypkg==1.0.0"])
    assert _locked_versions(spec_root, "tinypkg") == ["1.0.0"]
    assert _uv_steps(runner) == ["lock", "sync"]
    lock = (spec_root / "uv.lock").read_bytes()
    # A lost build is rebuilt from the workspace's own lock, unasked.
    _lose_build(Path(desc.prefix))
    rebuilt = await reg.materialize(ENV)
    assert rebuilt.state == "ready"
    assert _uv_steps(runner) == ["lock", "sync", "sync"]
    assert (spec_root / "uv.lock").read_bytes() == lock
    assert _no_staging_left(spec_root)


@needs_uv
async def test_a_workspace_on_the_earlier_empty_template_is_upgraded(
    tmp_path: Path, wheels: Path
) -> None:
    reg, runner, spec_root = _registry(tmp_path, wheels)
    _write_spec(spec_root, EMPTY_TEMPLATE)
    desc = await reg.materialize(ENV)
    assert desc.state == "ready"
    assert (spec_root / "pyproject.toml").read_text() == template_pyproject(["tinypkg==1.0.0"])
    assert _locked_versions(spec_root, "tinypkg") == ["1.0.0"]
    assert _uv_steps(runner) == ["lock", "sync"]
    assert _no_staging_left(spec_root)


@needs_uv
async def test_a_workspace_on_a_template_this_registry_seeded_is_upgraded_to_the_next(
    tmp_path: Path, wheels: Path
) -> None:
    """A release that changes the pins upgrades every workspace still on
    the spec the platform seeded, its derived lock included."""
    reg, _, spec_root = _registry(tmp_path, wheels)
    await reg.materialize(ENV)
    reg, runner, _ = _registry(tmp_path, wheels, ("tinypkg==2.0.0",))
    desc = await reg.materialize(ENV)
    assert desc.state == "ready"
    assert _locked_versions(spec_root, "tinypkg") == ["2.0.0"]
    assert _uv_steps(runner) == ["lock", "sync"]


def _changed_pyproject() -> dict[str, bytes]:
    return {
        "pyproject.toml": EMPTY_TEMPLATE["pyproject.toml"].replace(
            b"dependencies = []", b'dependencies = ["tinypkg"]'
        ),
        "uv.lock": EMPTY_TEMPLATE["uv.lock"],
    }


def _lock_not_derived_here() -> dict[str, bytes]:
    """The current template's pyproject with a lock the registry did not make."""
    return {
        "pyproject.toml": template_pyproject(["tinypkg==1.0.0"]).encode(),
        "uv.lock": EMPTY_TEMPLATE["uv.lock"] + b"\n# edited\n",
    }


@needs_uv
@pytest.mark.parametrize(
    "spec",
    [
        pytest.param(_changed_pyproject(), id="pyproject_changed"),
        pytest.param(_lock_not_derived_here(), id="lock_not_derived_here"),
    ],
)
async def test_a_spec_someone_changed_is_left_alone_and_builds_unasked(
    tmp_path: Path, wheels: Path, spec: dict[str, bytes]
) -> None:
    """No approval stands between a changed spec and its build: whoever
    changed it was asked already (a person's own edit, an agent's tool
    permission). Its own lock is what builds, untouched."""
    reg, runner, spec_root = _registry(tmp_path, wheels)
    _write_spec(spec_root, spec)
    desc = await reg.materialize(ENV)
    assert desc.state == "ready"
    assert project_spec(Tree(spec_root), spec_root) == spec
    assert _uv_steps(runner) == ["sync"]


@needs_uv
async def test_a_changed_spec_after_seeding_is_never_upgraded(tmp_path: Path, wheels: Path) -> None:
    reg, _, spec_root = _registry(tmp_path, wheels)
    await reg.materialize(ENV)
    await reg.install(ENV, ["tinypkg>=1.0"], str(reg.root / "n.alknb.py"))
    pyproject = (spec_root / "pyproject.toml").read_bytes()
    assert pyproject != template_pyproject(["tinypkg==1.0.0"]).encode()
    reg, runner, _ = _registry(tmp_path, wheels, ("tinypkg==2.0.0",))
    await reg.materialize(ENV)
    assert (spec_root / "pyproject.toml").read_bytes() == pyproject
    assert _locked_versions(spec_root, "tinypkg") == ["1.0.0"]
    assert "lock" not in _uv_steps(runner)


@needs_uv
async def test_a_lock_that_fails_leaves_the_earlier_spec_and_names_the_cause(
    tmp_path: Path, wheels: Path
) -> None:
    reg, _, spec_root = _registry(tmp_path, wheels, ("missingpkg==1.0.0",))
    _write_spec(spec_root, EMPTY_TEMPLATE)
    with pytest.raises(EnvBuildError) as failed:
        await reg.materialize(ENV)
    assert "Locking the default environment" in str(failed.value)
    assert "missingpkg" in str(failed.value)
    assert project_spec(Tree(spec_root), spec_root) == EMPTY_TEMPLATE
    assert sorted(os.listdir(spec_root)) == ["pyproject.toml", "uv.lock"]
    assert _no_staging_left(spec_root)
    assert reg.describe(ENV).last_failure == str(failed.value)


#: What uv prints for an unresolvable lock where it guesses a narrow terminal
#: (a CI runner): one statement wrapped over several lines.
WRAPPED_LOCK_FAILURE = """\
  \u00d7 No solution found when resolving dependencies:
  ╰─▶ Because missingpkg was not found in the package registry and your
      project depends on missingpkg==1.0.0, we can conclude that your
      project's requirements are unsatisfiable.
"""


class _LockFailsWrapped:
    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> CommandResult:
        return CommandResult(returncode=1, stdout="", stderr=WRAPPED_LOCK_FAILURE)


async def test_a_lock_failure_uv_wrapped_over_lines_is_named_whole(tmp_path: Path) -> None:
    """uv wraps its message to the width it guesses; the failure names the
    whole statement, not the tail of its last line."""
    reg, _, _ = _registry(tmp_path, tmp_path / "wheels", runs=_LockFailsWrapped())
    with pytest.raises(EnvBuildError) as failed:
        await reg.materialize(ENV)
    assert str(failed.value) == (
        "Locking the default environment's packages failed. Because missingpkg was not found "
        "in the package registry and your project depends on missingpkg==1.0.0, we can "
        "conclude that your project's requirements are unsatisfiable."
    )


@needs_uv
async def test_a_lock_that_fails_in_a_fresh_workspace_leaves_no_spec(
    tmp_path: Path, wheels: Path
) -> None:
    reg, runner, spec_root = _registry(tmp_path, wheels, ("missingpkg==1.0.0",))
    with pytest.raises(EnvBuildError):
        await reg.materialize(ENV)
    assert not spec_root.exists()
    assert _no_staging_left(spec_root)
    assert reg.describe(ENV).state == "failed"
    assert _uv_steps(runner) == ["lock"]


# -- the real template, from the index ---------------------------------------------


def _index_reachable() -> bool:
    try:
        socket.create_connection(("pypi.org", 443), timeout=5).close()
    except OSError:
        return False
    return True


@pytest.mark.live
@pytest.mark.xdist_group("nbeng_env")
@pytest.mark.skipif(UV is None or sys.platform == "win32", reason="needs uv and a kernel")
async def test_a_fresh_workspace_runs_polars_from_the_real_template(tmp_path: Path) -> None:
    """The engine a box or a laptop builds by default: its first run seeds
    the curated template, locks it against the index once, and the kernel
    imports polars without anyone installing it."""
    if not _index_reachable():
        pytest.skip("the package index is not reachable")
    from alkera_notebook.sim.driver import SimDriver, StanceGatekeeper
    from alkera_notebook.sim.engine_target import EngineTarget

    target = EngineTarget(tmp_path / "ws")
    driver = SimDriver(target, gatekeeper=StanceGatekeeper("default"))  # type: ignore[arg-type]
    source = (
        "import anywidget, duckdb, ipywidgets, polars\n"
        "duckdb.sql('select 41 + 1 as v').pl()['v'][0]"
    )
    try:
        await driver.agent(
            "notebook.create", {"path": "nb.alknb.py", "cells": [{"source": source}]}
        )
        run = await driver.agent(
            "notebook.run", {"path": "nb.alknb.py", "target": {"kind": "all"}, "timeout_s": 900}
        )
        assert run.status == "finished", run  # type: ignore[union-attr]
        assert run.cells[-1].output.text.content.strip() == "42"  # type: ignore[union-attr]
    finally:
        await driver.close()
    spec_root = tmp_path / "ws" / ".alkera" / "envs" / "default"
    assert (spec_root / "pyproject.toml").read_text() == DEFAULT_ENV_PYPROJECT
    (polars,) = [p for p in DEFAULT_ENV_PACKAGES if p.startswith("polars==")]
    assert _locked_versions(spec_root, "polars") == [polars.partition("==")[2]]


# -- a changed spec is never left short of the template --------------------------------

#: A template with two packages, the second a spec may lack.
TWO = ("tinypkg==1.0.0", "widgetpkg==1.0.0")


@pytest.fixture
def more_wheels(wheels: Path) -> Path:
    build_wheel(wheels, "widgetpkg", "1.0.0")
    build_wheel(wheels, "otherpkg", "1.0.0")
    return wheels


def _grown_spec(deps: list[str]) -> dict[str, bytes]:
    """A spec that grew one install at a time from the empty template."""
    listed = ", ".join(f'"{d}"' for d in deps)
    return {
        "pyproject.toml": EMPTY_TEMPLATE["pyproject.toml"].replace(
            b"dependencies = []", f"dependencies = [{listed}]".encode()
        )
    }


def _deps(spec_root: Path) -> list[str]:
    return tomllib.loads((spec_root / "pyproject.toml").read_text())["project"]["dependencies"]


@needs_uv
async def test_a_spec_that_grew_from_the_empty_template_is_brought_up_to_it(
    tmp_path: Path, more_wheels: Path
) -> None:
    """A workspace whose first spec was the empty template and grew one
    install at a time gets every template package it lacks, at the template's
    pin, and the build that follows takes them at once."""
    reg, _, spec_root = _registry(tmp_path, more_wheels, TWO)
    _write_spec(spec_root, _grown_spec(["otherpkg"]))
    desc = await reg.materialize(ENV)
    assert sorted(_deps(spec_root)) == ["otherpkg", "tinypkg==1.0.0", "widgetpkg==1.0.0"]
    assert desc.state == "ready"
    assert _locked_versions(spec_root, "widgetpkg") == ["1.0.0"]


@needs_uv
async def test_bringing_a_spec_up_never_moves_what_it_names(
    tmp_path: Path, more_wheels: Path
) -> None:
    reg, _, spec_root = _registry(tmp_path, more_wheels, TWO)
    _write_spec(spec_root, _grown_spec(["tinypkg>=2.0"]))
    await reg.materialize(ENV)
    assert sorted(_deps(spec_root)) == ["tinypkg>=2.0", "widgetpkg==1.0.0"]
    assert _locked_versions(spec_root, "tinypkg") == ["2.0.0"]


@needs_uv
async def test_a_package_removed_after_the_spec_was_brought_up_stays_removed(
    tmp_path: Path, more_wheels: Path
) -> None:
    reg, _, spec_root = _registry(tmp_path, more_wheels, TWO)
    _write_spec(spec_root, _grown_spec(["otherpkg"]))
    await reg.materialize(ENV)
    await reg.remove(ENV, ["widgetpkg"], str(reg.root / "n.alknb.py"))
    await reg.materialize(ENV)
    assert "widgetpkg==1.0.0" not in _deps(spec_root)


@needs_uv
async def test_a_template_package_asked_for_by_name_gets_the_template_pin(
    tmp_path: Path, more_wheels: Path
) -> None:
    """``uv add tinypkg`` alone would take the newest (2.0.0): an install by
    name keeps the template's choice."""
    reg, _, spec_root = _registry(tmp_path, more_wheels, TWO)
    await reg.materialize(ENV)
    nb = str(reg.root / "n.alknb.py")
    await reg.remove(ENV, ["tinypkg"], nb)
    _, changed, _ = await reg.install(ENV, ["tinypkg"], nb)
    assert changed
    assert "tinypkg==1.0.0" in _deps(spec_root)
    assert _locked_versions(spec_root, "tinypkg") == ["1.0.0"]


@needs_uv
async def test_a_package_already_named_is_left_as_it_is(tmp_path: Path, more_wheels: Path) -> None:
    reg, _, spec_root = _registry(tmp_path, more_wheels, TWO)
    await reg.materialize(ENV)
    before = (spec_root / "pyproject.toml").read_bytes()
    _, changed, log = await reg.install(ENV, ["tinypkg"], str(reg.root / "n.alknb.py"))
    assert changed == []
    assert (spec_root / "pyproject.toml").read_bytes() == before
    assert "tinypkg is already in the environment as tinypkg==1.0.0." in log


@needs_uv
async def test_moving_a_template_pin_is_allowed_and_said(tmp_path: Path, more_wheels: Path) -> None:
    reg, _, spec_root = _registry(tmp_path, more_wheels, TWO)
    await reg.materialize(ENV)
    _, _, log = await reg.install(ENV, ["tinypkg==2.0.0"], str(reg.root / "n.alknb.py"))
    assert _locked_versions(spec_root, "tinypkg") == ["2.0.0"]
    assert log.startswith("tinypkg moves from the template's tinypkg==1.0.0 to tinypkg==2.0.0.")
