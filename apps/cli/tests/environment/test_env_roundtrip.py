"""Capture and recreate round trips against real environments: real uv, real
venvs, real editable installs, real hash checking. Only the network is faked
(an offline ``--find-links`` folder of wheels built by the test).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest
from _helpers.pyenv import (
    UV,
    WINDOWS,
    bin_python,
    build_wheel,
    editable_project,
    make_venv,
    offline_env,
    run_py,
    uv_install,
)
from alkera_cli.environment import (
    LocalRunner,
    capture,
    read_spec,
    recreate,
    render_spec,
    write_spec,
)
from alkera_cli.environment.spec import EnvironmentSpec, PackageSpec

needs_uv = pytest.mark.skipif(UV is None, reason="needs uv on PATH")
pytestmark = needs_uv


class Workspace:
    """A workspace with a venv: alpha 1.0 (needs beta, held at 2.0 though 3.0
    exists) from the offline index, and mylib (needs beta) installed editable
    from libs/mylib."""

    def __init__(self, tmp_path: Path, *, hashed: bool = False) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "ws"
        self.wheels = tmp_path / "wheels"
        self.env = offline_env(tmp_path)
        _, alpha_hash = build_wheel(self.wheels, "alpha", "1.0", deps=["beta"])
        _, beta_hash = build_wheel(self.wheels, "beta", "2.0")
        build_wheel(self.wheels, "beta", "3.0")
        self.root.mkdir()
        editable_project(self.root / "libs" / "mylib", "mylib", deps=["beta"])
        lines = [f"--find-links {self.wheels.as_posix()}"]
        if hashed:
            lines += [f"alpha==1.0 --hash={alpha_hash}", f"beta==2.0 --hash={beta_hash}"]
        else:
            lines += ["alpha==1.0"]
        (self.root / "requirements.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        # beta arrives as alpha's dependency, held below its newest version.
        (tmp_path / "constraints.txt").write_text("beta==2.0\n", encoding="utf-8")
        self.python = make_venv(self.root / ".venv", self.env)
        uv_install(
            self.python,
            self.env,
            "-r",
            str(self.root / "requirements.txt"),
            "-c",
            str(tmp_path / "constraints.txt"),
        )
        uv_install(
            self.python,
            self.env,
            "--find-links",
            str(self.wheels),
            "-e",
            str(self.root / "libs" / "mylib"),
        )

    @property
    def runner(self) -> LocalRunner:
        return LocalRunner(base_env=self.env)

    def capture(self) -> EnvironmentSpec:
        return asyncio.run(capture(self.runner, root=str(self.root), python=str(self.python)))


def _recreate(ws: Workspace, spec: EnvironmentSpec, target: Path, root: Path, **kw: object):  # type: ignore[no-untyped-def]
    return asyncio.run(
        recreate(
            spec,
            ws.runner,
            target_env=str(target),
            target_root=str(root),
            windows=WINDOWS,
            fallback_python=sys.executable,
            **kw,  # type: ignore[arg-type]
        )
    )


def test_capture_records_versions_editable_path_and_requested(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    spec = ws.capture()

    assert spec.env_kind == "venv"
    by_name = {p.name: p for p in spec.packages}
    assert by_name["alpha"].version == "1.0"
    assert by_name["alpha"].source == "index"
    assert by_name["beta"].version == "2.0"
    # alpha and mylib are leaves; beta is only a dependency.
    assert by_name["alpha"].requested is True
    assert by_name["beta"].requested is False
    editable = by_name["mylib"]
    assert editable.source == "editable"
    assert editable.path == "libs/mylib"
    # The workspace's own location never reaches the spec.
    assert str(ws.root) not in render_spec(spec)
    assert any(i.kind == "find_links" for i in spec.indexes)
    assert spec.not_portable == []


def test_editable_outside_the_workspace_is_reported_not_recorded(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    outside = editable_project(tmp_path / "elsewhere" / "extlib", "extlib")
    uv_install(ws.python, ws.env, "-e", str(outside))

    spec = ws.capture()

    assert spec.package("extlib") is None
    issues = [i for i in spec.not_portable if i.name == "extlib"]
    assert [i.kind for i in issues] == ["editable_outside_workspace"]
    # The editable inside the workspace is still recorded relative to it.
    assert spec.package("mylib") is not None
    assert spec.package("mylib").path == "libs/mylib"  # type: ignore[union-attr]


def test_recreate_into_a_copied_tree_keeps_the_editable_relative(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    write_spec(ws.root, ws.capture())
    copy = tmp_path / "moved"
    shutil.copytree(ws.root, copy, ignore=shutil.ignore_patterns(".venv"))
    spec = read_spec(copy)
    assert spec is not None
    target = tmp_path / "clone-env"

    report = _recreate(ws, spec, target, copy)

    assert report.not_recreated == []
    assert [s.purpose for s in report.steps] == ["create_env", "install_packages", "install_local"]
    assert all(s.exit_code == 0 for s in report.steps)
    clone = bin_python(target)
    where = run_py(clone, "import mylib, alpha, beta; print(mylib.__file__, beta.VERSION)", ws.env)
    path, beta_version = where.rsplit(" ", 1)
    assert Path(path).resolve().is_relative_to((copy / "libs" / "mylib").resolve())
    assert beta_version == "2.0"


def test_recreate_again_is_a_no_op(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    spec = ws.capture()
    target = tmp_path / "clone-env"
    first = _recreate(ws, spec, target, ws.root)
    assert first.not_recreated == []

    second = _recreate(ws, spec, target, ws.root)

    assert second.already_satisfied is True
    assert second.steps == []


def test_dry_run_plans_without_touching_the_target(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    spec = ws.capture()
    target = tmp_path / "clone-env"

    report = _recreate(ws, spec, target, ws.root, dry_run=True)

    assert report.dry_run is True
    assert [s.purpose for s in report.steps] == ["create_env", "install_packages", "install_local"]
    assert not any(s.ran for s in report.steps)
    assert not target.exists()


def test_recreate_only_installs_what_is_missing(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    spec = ws.capture()
    target = tmp_path / "clone-env"
    _recreate(ws, spec, target, ws.root)
    subprocess_env = ws.env
    uv_install(bin_python(target), subprocess_env, "--find-links", str(ws.wheels), "beta==3.0")

    plan = _recreate(ws, spec, target, ws.root, dry_run=True)

    assert [s.purpose for s in plan.steps] == ["install_packages"]
    healed = _recreate(ws, spec, target, ws.root)
    assert healed.not_recreated == []
    assert run_py(bin_python(target), "import beta; print(beta.VERSION)", ws.env) == "2.0"


def test_hashed_requirements_are_captured_and_enforced(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, hashed=True)
    spec = ws.capture()
    alpha = spec.package("alpha")
    assert alpha is not None and alpha.hashes and alpha.hashes[0].startswith("sha256:")
    target = tmp_path / "clone-env"

    report = _recreate(ws, spec, target, ws.root)

    assert report.not_recreated == []
    install = next(s for s in report.steps if s.purpose == "install_packages")
    assert "--require-hashes" in install.command


def test_a_wrong_hash_fails_the_install_and_is_reported(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, hashed=True)
    spec = ws.capture()
    bad = [
        p.model_copy(update={"hashes": ["sha256:" + "0" * 64]}) if p.name == "alpha" else p
        for p in spec.packages
    ]
    spec = spec.model_copy(update={"packages": bad})

    report = _recreate(ws, spec, tmp_path / "clone-env", ws.root)

    kinds = {(g.kind, g.name) for g in report.not_recreated}
    assert ("not_installed", "alpha") in kinds
    assert any(g.kind == "step_failed" for g in report.not_recreated)


def test_a_missing_editable_source_is_reported_before_anything_runs(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    spec = ws.capture()
    copy = tmp_path / "moved"
    shutil.copytree(ws.root, copy, ignore=shutil.ignore_patterns(".venv", "libs"))

    report = _recreate(ws, spec, tmp_path / "clone-env", copy, dry_run=True)

    assert {(g.kind, g.name) for g in report.not_recreated} == {("local_source_missing", "mylib")}
    assert "install_local" not in [s.purpose for s in report.steps]


def test_a_target_on_another_python_is_refused_not_replaced(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    spec = ws.capture()
    assert spec.python is not None
    other = spec.model_copy(update={"python": spec.python.model_copy(update={"version": "2.7.18"})})

    report = _recreate(ws, other, ws.root / ".venv", ws.root)

    assert [g.kind for g in report.not_recreated] == ["python_mismatch"]
    assert report.steps == []


def test_path_entries_in_the_workspace_are_recreated(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    tools = ws.root / "tools"
    tools.mkdir()
    (tools / "helper.py").write_text("NAME = 'helper'\n", encoding="utf-8")
    site = run_py(ws.python, "import sysconfig; print(sysconfig.get_paths()['purelib'])", ws.env)
    (Path(site) / "conda.pth").write_text(str(tools) + "\n", encoding="utf-8")
    spec = ws.capture()
    assert spec.path_entries == ["tools"]
    target = tmp_path / "clone-env"

    report = _recreate(ws, spec, target, ws.root)

    assert report.not_recreated == []
    assert run_py(bin_python(target), "import helper; print(helper.NAME)", ws.env) == "helper"


def test_project_only_capture_installs_requirements_txt(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    spec = asyncio.run(capture(ws.runner, root=str(ws.root), python=None))
    assert spec.env_kind == "none"
    assert spec.packages == []
    target = tmp_path / "clone-env"

    report = _recreate(ws, spec, target, ws.root)

    assert [s.purpose for s in report.steps] == ["create_env", "install_requirements"]
    assert report.steps[-1].exit_code == 0
    assert run_py(bin_python(target), "import alpha; print(alpha.VERSION)", ws.env) == "1.0"


def test_uv_project_with_an_unchanged_lock_syncs_from_it(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    (ws.root / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.0.0"\nrequires-python = ">=3.8"\n'
        'dependencies = ["alpha==1.0"]\n\n[tool.uv]\npackage = false\n'
        f'find-links = ["{ws.wheels.as_posix()}"]\n',
        encoding="utf-8",
    )
    assert UV is not None
    import subprocess

    subprocess.run(
        [UV, "lock", "--quiet", "--project", str(ws.root)], check=True, env=ws.env, cwd=ws.root
    )
    spec = ws.capture()
    assert spec.project_file("uv_lock") is not None
    target = tmp_path / "clone-env"

    report = _recreate(ws, spec, target, ws.root)

    assert report.not_recreated == []
    assert "uv_sync" in [s.purpose for s in report.steps]
    assert run_py(bin_python(target), "import alpha; print(alpha.VERSION)", ws.env) == "1.0"


def test_capture_spec_file_round_trips(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    spec = ws.capture()
    path = write_spec(ws.root, spec)

    loaded = read_spec(ws.root)

    assert loaded == spec
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == "1.0.0"
    assert isinstance(loaded.packages[0], PackageSpec)  # type: ignore[union-attr]


def test_a_timed_out_command_takes_its_children_with_it(tmp_path: Path) -> None:
    from alkera_cli.environment import Command
    from alkera_core.process import process_alive

    pidfile = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time; "
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
        f"open({str(pidfile)!r}, 'w').write(str(c.pid)); time.sleep(120)"
    )
    result = asyncio.run(
        LocalRunner().run(Command(argv=(sys.executable, "-c", script), timeout_s=5))
    )
    assert result.exit_code is None
    child = int(pidfile.read_text())
    for _ in range(100):
        if not process_alive(child):
            break
        import time

        time.sleep(0.05)
    assert not process_alive(child)
