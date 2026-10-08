"""The recreate planner, as plain data: spec + target state + tools in, the
commands (and what cannot be recreated) out."""

from __future__ import annotations

import pytest
from alkera_cli.environment.recreate import (
    Installed,
    Step,
    TargetState,
    Tools,
    is_satisfied,
    plan_recreate,
)
from alkera_cli.environment.spec import (
    CondaPackage,
    CondaSpec,
    EnvironmentSpec,
    IndexRef,
    PackageSpec,
    PlatformInfo,
    ProjectFile,
    PythonInfo,
    VcsRef,
)

ENV = "/envs/target"
ROOT = "/work/ws"
UV = Tools(uv="/usr/bin/uv", micromamba="/usr/bin/micromamba", python="/usr/bin/python3")


def _spec(**kw: object) -> EnvironmentSpec:
    base: dict[str, object] = {
        "env_kind": "venv",
        "python": PythonInfo(version="3.12.4", implementation="cpython"),
        "platform": PlatformInfo(sys_platform="linux", machine="x86_64"),
    }
    base.update(kw)
    return EnvironmentSpec(**base)  # type: ignore[arg-type]


def _state(**kw: object) -> TargetState:
    base: dict[str, object] = {
        "exists": True,
        "python_version": "3.12.1",
        "implementation": "cpython",
        "sys_platform": "linux",
        "machine": "x86_64",
    }
    base.update(kw)
    return TargetState(**base)  # type: ignore[arg-type]


def _plan(spec: EnvironmentSpec, state: TargetState, tools: Tools = UV):  # type: ignore[no-untyped-def]
    return plan_recreate(spec, state, target_env=ENV, target_root=ROOT, tools=tools)


def _step(steps: list[Step], purpose: str) -> Step:
    return next(s for s in steps if s.purpose == purpose)


PKGS = [
    PackageSpec(name="pandas", version="2.2.3", requested=True),
    PackageSpec(name="numpy", version="2.1.0"),
]


def test_a_satisfied_target_plans_nothing() -> None:
    state = _state(installed={"pandas": Installed("2.2.3"), "numpy": Installed("2.1.0")})
    plan = _plan(_spec(packages=PKGS), state)
    assert plan.steps == []
    assert plan.gaps == []
    assert plan.satisfied == 2


def test_a_missing_env_is_made_from_the_host_interpreter_when_its_version_fits() -> None:
    state = _state(
        exists=False, host_python="/envs/alkera/bin/python3", host_python_version="3.12.9"
    )
    plan = _plan(_spec(packages=PKGS), state)
    create = _step(plan.steps, "create_env")
    assert create.command.argv == (
        "/usr/bin/uv",
        "venv",
        "--quiet",
        "--python",
        "/envs/alkera/bin/python3",
        ENV,
    )


def test_a_missing_env_names_the_minor_version_when_the_host_does_not_fit() -> None:
    state = _state(exists=False, host_python="/usr/bin/python3", host_python_version="3.11.2")
    plan = _plan(_spec(packages=PKGS), state)
    assert _step(plan.steps, "create_env").command.argv[3:5] == ("--python", "3.12")


def test_without_uv_a_plain_venv_and_pip_are_used_and_the_version_is_noted() -> None:
    tools = Tools(uv=None, micromamba=None, python="/usr/bin/python3")
    plan = _plan(_spec(packages=PKGS), _state(exists=False), tools)
    assert _step(plan.steps, "create_env").command.argv == ("/usr/bin/python3", "-m", "venv", ENV)
    install = _step(plan.steps, "install_packages")
    assert install.command.argv[:4] == (f"{ENV}/bin/python", "-m", "pip", "install")
    assert any("3.12.4" in n for n in plan.notes)


def test_with_no_installer_at_all_nothing_runs_and_it_says_why() -> None:
    plan = _plan(_spec(packages=PKGS), _state(exists=False), Tools())
    assert plan.steps == []
    assert [g.kind for g in plan.gaps] == ["no_installer"]


def test_another_python_minor_blocks_everything() -> None:
    plan = _plan(_spec(packages=PKGS), _state(python_version="3.11.9"))
    assert plan.steps == []
    assert [g.kind for g in plan.gaps] == ["python_mismatch"]


def test_another_implementation_blocks_everything() -> None:
    plan = _plan(_spec(packages=PKGS), _state(implementation="pypy"))
    assert [g.kind for g in plan.gaps] == ["python_mismatch"]


def test_a_patch_difference_is_not_a_mismatch() -> None:
    plan = _plan(_spec(packages=PKGS), _state(python_version="3.12.0"))
    assert [g.kind for g in plan.gaps] == []


def test_same_platform_installs_every_missing_pin_without_constraints() -> None:
    plan = _plan(_spec(packages=PKGS), _state())
    install = _step(plan.steps, "install_packages")
    assert install.command.files["requirements"].splitlines() == ["pandas==2.2.3", "numpy==2.1.0"]
    assert "constraints" not in install.command.files
    assert "--require-hashes" not in install.command.argv


def test_another_platform_installs_what_was_asked_for_held_to_the_captured_pins() -> None:
    plan = _plan(_spec(packages=PKGS), _state(sys_platform="darwin", machine="arm64"))
    install = _step(plan.steps, "install_packages")
    assert install.command.files["requirements"].splitlines() == ["pandas==2.2.3"]
    assert install.command.files["constraints"].splitlines() == ["pandas==2.2.3", "numpy==2.1.0"]
    assert "{constraints}" in install.command.argv


def test_another_platform_with_nothing_requested_missing_installs_the_missing_pins() -> None:
    state = _state(sys_platform="darwin", machine="arm64", installed={"pandas": Installed("2.2.3")})
    plan = _plan(_spec(packages=PKGS), state)
    assert _step(plan.steps, "install_packages").command.files["requirements"].splitlines() == [
        "numpy==2.1.0"
    ]


HASHED = [
    PackageSpec(name="pandas", version="2.2.3", requested=True, hashes=["sha256:aa", "sha256:bb"]),
    PackageSpec(name="numpy", version="2.1.0", hashes=["sha256:cc"]),
]


def test_fully_hashed_on_the_same_platform_installs_hash_checked_without_resolving() -> None:
    install = _step(_plan(_spec(packages=HASHED), _state()).steps, "install_packages")
    assert install.command.files["requirements"].splitlines() == [
        "pandas==2.2.3 --hash=sha256:aa --hash=sha256:bb",
        "numpy==2.1.0 --hash=sha256:cc",
    ]
    assert install.command.argv[-2:] == ("--require-hashes", "--no-deps")


@pytest.mark.parametrize(
    "packages,state",
    [
        pytest.param([HASHED[0], PKGS[1]], _state(), id="one-unhashed"),
        pytest.param(HASHED, _state(sys_platform="darwin", machine="arm64"), id="other-platform"),
        pytest.param(
            [
                *HASHED,
                PackageSpec(
                    name="lib",
                    source="vcs",
                    vcs=VcsRef(url="https://g/x", commit="c"),
                    hashes=["sha256:dd"],
                ),
            ],
            _state(),
            id="vcs-cannot-be-hashed",
        ),
    ],
)
def test_hash_checking_needs_every_package_hashed_on_the_same_platform(
    packages: list[PackageSpec], state: TargetState
) -> None:
    install = _step(_plan(_spec(packages=packages), state).steps, "install_packages")
    assert "--require-hashes" not in install.command.argv


def test_vcs_and_url_requirements_are_spelled_for_the_installer() -> None:
    packages = [
        PackageSpec(
            name="tool",
            source="vcs",
            requested=True,
            vcs=VcsRef(vcs="git", url="https://github.com/o/tool.git", commit="abc123"),
            subdirectory="py",
        ),
        PackageSpec(
            name="wheelpkg", source="url", url="https://files.example/w-1.0-py3-none-any.whl"
        ),
    ]
    lines = (
        _step(_plan(_spec(packages=packages), _state()).steps, "install_packages")
        .command.files["requirements"]
        .splitlines()
    )
    assert lines == [
        "tool @ git+https://github.com/o/tool.git@abc123#subdirectory=py",
        "wheelpkg @ https://files.example/w-1.0-py3-none-any.whl",
    ]


def test_the_indexes_lead_the_requirements() -> None:
    spec = _spec(
        packages=PKGS,
        indexes=[
            IndexRef(url="https://pkgs.example/simple", kind="index"),
            IndexRef(url="https://extra.example/simple", kind="extra_index"),
            IndexRef(url="/srv/wheels", kind="find_links"),
        ],
    )
    lines = (
        _step(_plan(spec, _state()).steps, "install_packages")
        .command.files["requirements"]
        .splitlines()
    )
    assert lines[:3] == [
        "--index-url https://pkgs.example/simple",
        "--extra-index-url https://extra.example/simple",
        "--find-links /srv/wheels",
    ]


def test_editables_install_from_the_target_tree_without_dependencies() -> None:
    packages = [
        PackageSpec(name="mylib", version="0.1", source="editable", path="libs/mylib"),
        PackageSpec(
            name="sub", version="1", source="editable", path="pkgs/sub", subdirectory="python"
        ),
        PackageSpec(name="plain", version="1", source="path", path="vendor/plain"),
    ]
    state = _state(dirs={"libs/mylib": True, "pkgs/sub": True, "vendor/plain": True})
    local = _step(_plan(_spec(packages=packages), state).steps, "install_local")
    assert "--no-deps" in local.command.argv
    assert local.command.files["local"].splitlines() == [
        f"-e {ROOT}/libs/mylib",
        f"-e {ROOT}/pkgs/sub/python",
        f"{ROOT}/vendor/plain",
    ]


def test_an_editable_whose_folder_is_missing_is_a_gap_not_a_step() -> None:
    packages = [PackageSpec(name="mylib", version="0.1", source="editable", path="libs/mylib")]
    plan = _plan(_spec(packages=packages), _state(dirs={"libs/mylib": False}))
    assert not any(s.purpose == "install_local" for s in plan.steps)
    assert [(g.kind, g.name) for g in plan.gaps] == [("local_source_missing", "mylib")]


def test_workspace_folders_on_sys_path_are_written_as_one_pth() -> None:
    spec = _spec(path_entries=["tools", "gone"])
    plan = _plan(spec, _state(dirs={"tools": True, "gone": False}))
    pth = _step(plan.steps, "path_entries")
    assert pth.command.argv[0] == f"{ENV}/bin/python"
    assert pth.command.argv[4] == f"{ROOT}/tools\n"
    assert [(g.kind, g.detail) for g in plan.gaps] == [("local_source_missing", "gone")]


def test_path_entries_already_present_plan_nothing() -> None:
    plan = _plan(
        _spec(path_entries=["tools"]), _state(path_entries={"tools"}, dirs={"tools": True})
    )
    assert plan.steps == []


UV_FILES = [
    ProjectFile(path="pyproject.toml", kind="pyproject", sha256="p"),
    ProjectFile(path="uv.lock", kind="uv_lock", sha256="lock-1"),
]


def test_an_unchanged_uv_lock_syncs_the_target_from_it() -> None:
    spec = _spec(packages=PKGS, project_files=UV_FILES)
    plan = _plan(spec, _state(exists=False, files={"uv.lock": "lock-1", "pyproject.toml": "p"}))
    sync = _step(plan.steps, "uv_sync")
    assert sync.command.argv[:4] == ("/usr/bin/uv", "sync", "--frozen", "--inexact")
    assert sync.command.env["UV_PROJECT_ENVIRONMENT"] == ENV
    assert [s.purpose for s in plan.steps] == ["create_env", "uv_sync", "install_packages"]


def test_a_changed_uv_lock_is_not_synced_and_is_noted() -> None:
    spec = _spec(packages=PKGS, project_files=UV_FILES)
    plan = _plan(spec, _state(files={"uv.lock": "lock-2"}))
    assert "uv_sync" not in [s.purpose for s in plan.steps]
    assert any("uv.lock" in n for n in plan.notes)


def test_a_project_only_spec_without_a_lock_installs_requirements_txt() -> None:
    spec = _spec(
        project_files=[ProjectFile(path="requirements.txt", kind="requirements", sha256="r")]
    )
    plan = _plan(spec, _state(exists=False, files={"requirements.txt": "r"}))
    req = _step(plan.steps, "install_requirements")
    assert req.command.argv[-2:] == ("-r", f"{ROOT}/requirements.txt")


CONDA = CondaSpec(
    subdir="linux-64",
    channels=["https://conda.anaconda.org/conda-forge"],
    specs=["numpy", "python=3.12"],
    packages=[
        CondaPackage(
            name="numpy",
            version="2.1.0",
            build="py312h1",
            url="https://conda.anaconda.org/conda-forge/linux-64/numpy-2.1.0-py312h1.conda",
            md5="m1",
            subdir="linux-64",
        ),
        CondaPackage(
            name="python",
            version="3.12.4",
            build="h2",
            url="https://conda.anaconda.org/conda-forge/linux-64/python-3.12.4-h2.conda",
            md5="m2",
            subdir="linux-64",
        ),
    ],
)


def test_conda_on_the_same_platform_creates_from_the_explicit_list() -> None:
    plan = _plan(_spec(env_kind="conda", conda=CONDA), _state(exists=False))
    create = _step(plan.steps, "create_env")
    assert create.command.argv[:5] == ("/usr/bin/micromamba", "create", "--yes", "--prefix", ENV)
    assert create.command.files["explicit"].splitlines() == [
        "@EXPLICIT",
        CONDA.packages[0].url + "#m1",
        CONDA.packages[1].url + "#m2",
    ]


def test_conda_on_another_platform_creates_from_the_requested_specs() -> None:
    plan = _plan(
        _spec(env_kind="conda", conda=CONDA),
        _state(exists=False, sys_platform="darwin", machine="arm64"),
    )
    argv = _step(plan.steps, "create_env").command.argv
    assert "--override-channels" in argv
    assert argv[-2:] == ("numpy", "python=3.12")
    assert ("--channel", "https://conda.anaconda.org/conda-forge") == argv[
        argv.index("--channel") : argv.index("--channel") + 2
    ]


def test_conda_specs_without_python_get_the_captured_python() -> None:
    conda = CONDA.model_copy(update={"specs": ["numpy"], "subdir": "osx-arm64"})
    argv = _step(
        _plan(_spec(env_kind="conda", conda=conda), _state(exists=False)).steps, "create_env"
    ).command.argv
    assert argv[-1] == "python=3.12.4"


def test_conda_without_micromamba_falls_back_to_a_venv_and_says_so() -> None:
    tools = Tools(uv="/usr/bin/uv", micromamba=None)
    plan = _plan(_spec(env_kind="conda", conda=CONDA), _state(exists=False), tools)
    assert _step(plan.steps, "create_env").command.argv[1] == "venv"
    assert [g.kind for g in plan.gaps] == ["conda_unavailable"]


def test_an_existing_conda_env_gets_only_its_missing_packages() -> None:
    state = _state(conda={"python": ("3.12.4", "h2")}, python_version="3.12.4")
    install = _step(_plan(_spec(env_kind="conda", conda=CONDA), state).steps, "conda_install")
    assert install.command.argv[-1] == "numpy=2.1.0=py312h1"


def test_a_conda_spec_against_a_plain_venv_is_refused() -> None:
    plan = _plan(_spec(env_kind="conda", conda=CONDA), _state(conda=None))
    assert [g.kind for g in plan.gaps] == ["target_not_conda"]
    assert plan.steps == []


@pytest.mark.parametrize(
    "pkg,have,expected",
    [
        pytest.param(
            PackageSpec(name="A_b", version="1"),
            {"a-b": Installed("1")},
            True,
            id="normalized-name",
        ),
        pytest.param(
            PackageSpec(name="a", version="1"), {"a": Installed("2")}, False, id="other-version"
        ),
        pytest.param(PackageSpec(name="a", version=""), {"a": Installed("9")}, True, id="unpinned"),
        pytest.param(PackageSpec(name="a", version="1"), {}, False, id="absent"),
        pytest.param(
            PackageSpec(name="e", version="1", source="editable", path="libs/e"),
            {"e": Installed("1", editable="libs/e")},
            True,
            id="editable-same-folder",
        ),
        pytest.param(
            PackageSpec(name="e", version="1", source="editable", path="libs/e"),
            {"e": Installed("1", editable=None)},
            False,
            id="editable-installed-plain",
        ),
        pytest.param(
            PackageSpec(name="v", version="1", source="vcs", vcs=VcsRef(url="u", commit="c1")),
            {"v": Installed("1", commit="c2")},
            False,
            id="vcs-other-commit",
        ),
        pytest.param(
            PackageSpec(name="v", version="1", source="vcs", vcs=VcsRef(url="u", commit="c1")),
            {"v": Installed("0", commit="c1")},
            True,
            id="vcs-same-commit",
        ),
        pytest.param(
            PackageSpec(name="w", version="1", source="url", url="https://x/w.whl"),
            {"w": Installed("0", url="https://x/w.whl")},
            True,
            id="url-same",
        ),
    ],
)
def test_satisfaction(pkg: PackageSpec, have: dict[str, Installed], expected: bool) -> None:
    assert is_satisfied(pkg, have) is expected


def test_a_noarch_only_explicit_list_is_used_on_any_platform() -> None:
    noarch = CondaSpec(
        subdir="",
        packages=[
            CondaPackage(
                name="tz", version="1", url="https://c/noarch/tz-1-0.conda", subdir="noarch"
            )
        ],
    )
    state = _state(exists=False, sys_platform="darwin", machine="arm64")
    create = _step(_plan(_spec(env_kind="conda", conda=noarch), state).steps, "create_env")
    assert create.command.files["explicit"].splitlines() == [
        "@EXPLICIT",
        "https://c/noarch/tz-1-0.conda",
    ]


@pytest.mark.parametrize(
    "windows,conda,expected",
    [
        pytest.param(False, False, "/e/bin/python", id="posix-venv"),
        pytest.param(False, True, "/e/bin/python", id="posix-conda"),
        pytest.param(True, False, "C:\\e\\Scripts\\python.exe", id="windows-venv"),
        pytest.param(True, True, "C:\\e\\python.exe", id="windows-conda-at-the-prefix"),
    ],
)
def test_the_environment_interpreter_is_where_each_kind_puts_it(
    windows: bool, conda: bool, expected: str
) -> None:
    from alkera_cli.environment.recreate import env_python

    assert env_python("C:\\e" if windows else "/e", windows=windows, conda=conda) == expected


# --- an unattended restore runs only what the spec pins ------------------------


def _pinned(spec: EnvironmentSpec, state: TargetState):  # type: ignore[no-untyped-def]
    return plan_recreate(
        spec, state, target_env=ENV, target_root=ROOT, tools=UV, pinned_inputs=True
    )


REQ = [ProjectFile(path="requirements.txt", kind="requirements", sha256="r")]
ROOT_EDITABLE = [PackageSpec(name="app", version="0.1", source="editable", path=".")]
ROOT_BUILD = [ProjectFile(path="pyproject.toml", kind="pyproject", sha256="p")]


@pytest.mark.parametrize(
    ("spec", "state", "purpose"),
    [
        pytest.param(
            _spec(project_files=REQ),
            _state(exists=False, files={"requirements.txt": "r"}),
            "install_requirements",
            id="requirements-as-captured",
        ),
        pytest.param(
            _spec(packages=ROOT_EDITABLE, project_files=ROOT_BUILD),
            _state(dirs={".": True}, files={"pyproject.toml": "p"}),
            "install_local",
            id="root-editable-as-captured",
        ),
        pytest.param(
            _spec(packages=PKGS, project_files=UV_FILES),
            _state(exists=False, files={"uv.lock": "lock-1", "pyproject.toml": "p"}),
            "uv_sync",
            id="uv-sync-as-captured",
        ),
    ],
)
def test_a_pinned_restore_runs_a_tree_step_whose_input_is_the_captured_one(
    spec: EnvironmentSpec, state: TargetState, purpose: str
) -> None:
    plan = _pinned(spec, state)
    assert purpose in [s.purpose for s in plan.steps]
    assert not [g for g in plan.gaps if g.kind == "unpinned_input"]


@pytest.mark.parametrize(
    ("spec", "state", "purpose", "why"),
    [
        pytest.param(
            _spec(project_files=REQ),
            _state(exists=False, files={"requirements.txt": "edited"}),
            "install_requirements",
            "requirements.txt changed",
            id="requirements-edited",
        ),
        pytest.param(
            _spec(packages=ROOT_EDITABLE, project_files=ROOT_BUILD),
            _state(dirs={".": True}, files={"pyproject.toml": "edited"}),
            "install_local",
            "pyproject.toml changed",
            id="root-build-hooks-edited",
        ),
        pytest.param(
            _spec(packages=ROOT_EDITABLE, project_files=ROOT_BUILD),
            _state(dirs={".": True}, files={"pyproject.toml": "p", "setup.py": "planted"}),
            "install_local",
            "setup.py changed",
            id="setup-py-planted",
        ),
        pytest.param(
            _spec(
                packages=[PackageSpec(name="lib", version="1", source="editable", path="libs/lib")]
            ),
            _state(dirs={"libs/lib": True}),
            "install_local",
            "not recorded",
            id="folder-below-the-root-is-unpinned",
        ),
        pytest.param(
            _spec(packages=PKGS, project_files=UV_FILES),
            _state(exists=False, files={"uv.lock": "lock-1", "pyproject.toml": "edited"}),
            "uv_sync",
            "pyproject.toml changed",
            id="uv-sync-project-edited",
        ),
    ],
)
def test_a_pinned_restore_skips_a_tree_step_whose_input_changed(
    spec: EnvironmentSpec, state: TargetState, purpose: str, why: str
) -> None:
    """Another member who can write the tree cannot change what runs in a
    chat's sandbox at its wake: the step is left out and said, and the same
    plan made by a person (unpinned) still runs it."""
    plan = _pinned(spec, state)
    assert purpose not in [s.purpose for s in plan.steps]
    gaps = [g for g in plan.gaps if g.kind == "unpinned_input"]
    assert len(gaps) == 1 and why in gaps[0].detail
    assert purpose in [s.purpose for s in _plan(spec, state).steps]
