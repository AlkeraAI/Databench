"""Environment cases: detection, recorded forms, materialization, installs.

Builds use the real ``uv`` offline against a local wheel index
(``--no-index --find-links``) of wheels written by hand in the test.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alkera_notebook.envs import (
    DistributionRequiredError,
    EnvBuildError,
    EnvNotFoundError,
    EnvNotMaterializableError,
    LocalEnvRegistry,
    ScriptInstallViaDocumentError,
    add_script_dependencies,
    detect_envs,
)
from alkera_notebook.envs.detect import interpreter_in
from nbeng_env_support import (
    DEFAULT_PYPROJECT,
    PY,
    UV,
    RecordingRunner,
    build_wheel,
    lock_project,
)

pytestmark = pytest.mark.xdist_group("nbeng_env")

needs_uv = pytest.mark.skipif(UV is None, reason="uv is not on PATH")

NOTEBOOK = """# /// script
# dependencies = ["tinypkg"]
# ///
# >>> alkera
# format = "1.0"
# <<< alkera

import marimo

app = marimo.App()
"""


def _fake_venv(prefix: Path, version: str = "3.13.1") -> None:
    interp = interpreter_in(prefix)
    interp.parent.mkdir(parents=True, exist_ok=True)
    interp.write_text("")
    (prefix / "pyvenv.cfg").write_text(f"home = /usr/bin\nversion_info = {version}\n")


def _wipe_build(prefix: str) -> None:
    """The build a prefix stands for is gone (a managed prefix is a link)."""
    shutil.rmtree(Path(prefix).resolve())


def _tree(ws: Path) -> Path:
    """Every kind around one notebook at ``proj/nb/note.alknb.py``."""
    (ws / ".alkera" / "envs" / "default").mkdir(parents=True)
    (ws / ".alkera" / "envs" / "default" / "pyproject.toml").write_text(DEFAULT_PYPROJECT)
    (ws / "proj" / "nb").mkdir(parents=True)
    (ws / "proj" / "pyproject.toml").write_text('[project]\nname = "proj"\nversion = "0"\n')
    (ws / "proj" / "nb" / "note.alknb.py").write_text(NOTEBOOK)
    _fake_venv(ws / "proj" / "nb" / "myenv")
    _fake_venv(ws / "proj" / ".venv")  # the project's own env, not a separate venv
    (ws / "requirements.txt").write_text("tinypkg\n")
    (ws / "environment.yml").write_text("name: x\n")
    # A pyproject without [project] is not a uv project.
    (ws / "proj" / "nb" / "pyproject.toml").write_text("[tool.ruff]\n")
    return ws / "proj" / "nb" / "note.alknb.py"


@pytest.mark.parametrize("case", [pytest.param("copy", id="env.detect_every_kind_stable_ids")])
def test_detection_of_every_kind(tmp_path: Path, case: str) -> None:
    nb = _tree(tmp_path / "a" / "ws")
    found = detect_envs(tmp_path / "a" / "ws", nb, env_root=tmp_path / "a" / "envs")
    summary = [(d.kind, d.env_id, d.recorded, d.state) for d in found]
    assert summary == [
        ("uv_project", "uv_project:proj", "../", "stale"),
        ("venv", "venv:proj/nb/myenv", "./myenv", "ready"),
        ("script", "script:proj/nb/note.alknb.py", "script", "missing"),
        ("default", "default:.alkera/envs/default", "default", "missing"),
        ("requirements", "requirements:.", "../../", "missing"),
        ("conda", "conda:.", "../../", "missing"),
    ]
    by_kind = {d.kind: d for d in found}
    assert by_kind["venv"].python_version == "3.13.1"
    assert not Path(by_kind["default"].prefix).is_relative_to(tmp_path / "a" / "ws")
    assert not Path(by_kind["script"].prefix).is_relative_to(tmp_path / "a" / "ws")
    assert by_kind["uv_project"].prefix.endswith("/proj/.venv")

    # The same tree elsewhere gets the same ids and spec hashes.
    shutil.copytree(tmp_path / "a" / "ws", tmp_path / "b" / "elsewhere")
    copied = detect_envs(
        tmp_path / "b" / "elsewhere",
        tmp_path / "b" / "elsewhere" / "proj" / "nb" / "note.alknb.py",
        env_root=tmp_path / "b" / "envs",
    )
    assert [(d.env_id, d.spec_hash) for d in copied] == [(d.env_id, d.spec_hash) for d in found]


def test_detection_without_specs_lists_only_default(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    nb = tmp_path / "ws" / "n.alknb.py"
    nb.write_text("import marimo\n")
    found = detect_envs(tmp_path / "ws", nb, env_root=tmp_path / "envs")
    assert [d.kind for d in found] == ["default"]


def test_detection_stops_at_the_workspace_root(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "outer"\nversion = "0"\n')
    _fake_venv(tmp_path / ".venv")
    (tmp_path / "ws").mkdir()
    nb = tmp_path / "ws" / "n.alknb.py"
    nb.write_text("")
    found = detect_envs(tmp_path / "ws", nb, env_root=tmp_path / "envs")
    assert [d.kind for d in found] == ["default"]


RECORDED = [
    pytest.param("default", "default:.alkera/envs/default", id="env.recorded_default"),
    pytest.param("script", "script:proj/nb/note.alknb.py", id="env.recorded_script"),
    pytest.param("./myenv", "venv:proj/nb/myenv", id="env.recorded_venv_path"),
    pytest.param("../", "uv_project:proj", id="env.recorded_project_path"),
    pytest.param("../../other", "uv_project:other", id="env.recorded_sibling_project"),
    pytest.param(None, "uv_project:proj", id="env.absent_detects_uv_project_first"),
]


@pytest.mark.parametrize(("recorded", "env_id"), RECORDED)
async def test_recorded_env_forms(tmp_path: Path, recorded: str | None, env_id: str) -> None:
    ws = tmp_path / "ws"
    nb = _tree(ws)
    (ws / "other").mkdir()
    (ws / "other" / "pyproject.toml").write_text('[project]\nname = "o"\nversion = "0"\n')
    reg = LocalEnvRegistry(ws, tmp_path / "envs", runner=RecordingRunner())
    desc = await reg.resolve(str(nb), recorded)
    assert desc.env_id == env_id
    if recorded is not None:
        assert desc.recorded == recorded.rstrip("/") + ("/" if recorded.endswith("/") else "")


@pytest.mark.parametrize(
    "recorded",
    [
        pytest.param("../../../outside", id="outside-workspace"),
        pytest.param("./missing", id="nothing-there"),
        pytest.param("conda", id="unknown-word"),
        pytest.param("/abs/path", id="absolute"),
    ],
)
async def test_bad_recorded_env_is_refused(tmp_path: Path, recorded: str) -> None:
    ws = tmp_path / "ws"
    nb = _tree(ws)
    (tmp_path / "outside").mkdir()
    _fake_venv(tmp_path / "outside")
    reg = LocalEnvRegistry(ws, tmp_path / "envs", runner=RecordingRunner())
    with pytest.raises(EnvNotFoundError):
        await reg.resolve(str(nb), recorded)


async def test_detection_never_changes_a_recorded_env(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    nb = _tree(ws)
    reg = LocalEnvRegistry(ws, tmp_path / "envs", runner=RecordingRunner())
    before = nb.read_text()
    assert (await reg.resolve(str(nb), "default")).kind == "default"
    await reg.detect(str(nb))
    assert nb.read_text() == before


async def test_script_recorded_without_a_block_is_refused(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    nb = tmp_path / "ws" / "n.alknb.py"
    nb.write_text("import marimo\n")
    reg = LocalEnvRegistry(tmp_path / "ws", tmp_path / "envs", runner=RecordingRunner())
    with pytest.raises(EnvNotFoundError, match="script block"):
        await reg.resolve(str(nb), "script")


# Building ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wheels(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("wheels")
    build_wheel(d, "tinypkg", "1.0.0")
    build_wheel(d, "otherpkg", "0.1.0")
    return d


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory, wheels: Path) -> Path:
    if UV is None:
        pytest.skip("uv is not on PATH")
    d = tmp_path_factory.mktemp("template")
    (d / "pyproject.toml").write_text(DEFAULT_PYPROJECT)
    lock_project(d, wheels, tmp_path_factory.mktemp("cache"))
    return d


def _registry(
    tmp_path: Path, wheels: Path, template: Path | None
) -> tuple[LocalEnvRegistry, RecordingRunner, Path]:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    runner = RecordingRunner()
    reg = LocalEnvRegistry(
        ws,
        tmp_path / "envs",
        runner=runner,
        python=PY,
        uv=UV or "uv",
        index_args=["--no-index", "--find-links", str(wheels)],
        default_template=template,
    )
    return reg, runner, ws


def _no_network(runner: RecordingRunner) -> None:
    for argv, env in zip(runner.calls, runner.envs, strict=True):
        if argv[0] != (UV or "uv"):
            continue  # the built interpreter reporting its build
        assert env["UV_OFFLINE"] == "1" and env["UV_PYTHON_DOWNLOADS"] == "never"
        assert "--python" in argv
        if argv[1] in ("sync", "add") or argv[1:3] == ["pip", "install"]:
            assert "--no-index" in argv and "--find-links" in argv


@needs_uv
@pytest.mark.parametrize("case", [pytest.param(1, id="env.materialize_default_ready")])
async def test_materialize_default_into_an_empty_env_root(
    tmp_path: Path, wheels: Path, template: Path, case: int
) -> None:
    reg, runner, ws = _registry(tmp_path, wheels, template)
    desc = await reg.resolve(str(ws / "n.alknb.py"), None)
    assert desc.kind == "default" and desc.state == "missing"
    built = await reg.materialize(desc.env_id)
    assert built.state == "ready"
    # The engine's own interpreter is a GIL build, and the build reports so.
    assert built.python_build == "gil"
    assert built.built_spec_hash == built.spec_hash
    assert interpreter_in(Path(built.prefix)).name.startswith("python")
    assert "tinypkg" in [n for n, _ in await reg.packages(built.env_id)]
    assert (ws / ".alkera" / "envs" / "default" / "uv.lock").is_file()
    assert ("tinypkg", "1.0.0") in await reg.packages(built.env_id)
    _no_network(runner)
    # Ready means nothing more to do.
    n = len(runner.calls)
    assert (await reg.materialize(built.env_id)).state == "ready"
    assert len(runner.calls) == n
    assert len(reg.fingerprint(built)) == 64


@needs_uv
async def test_a_never_built_env_builds_unasked_and_records_who_and_when(
    tmp_path: Path, wheels: Path, template: Path
) -> None:
    reg, _, ws = _registry(tmp_path, wheels, template)
    desc = await reg.resolve(str(ws / "n.alknb.py"), "default")
    asked = datetime.now(UTC)
    built = await reg.materialize(desc.env_id, by="Ada Lovelace")
    assert built.state == "ready"
    record = reg._detector.records.get(desc.env_id)
    assert (record.built_spec_hash, record.built_by) == (built.spec_hash, "Ada Lovelace")
    assert asked <= datetime.fromisoformat(record.built_at) <= datetime.now(UTC)


@needs_uv
async def test_default_without_spec_or_template_fails_clearly(tmp_path: Path, wheels: Path) -> None:
    reg, _, _ = _registry(tmp_path, wheels, None)
    with pytest.raises(Exception, match="no template"):
        await reg.materialize("default:.alkera/envs/default")


@needs_uv
@pytest.mark.parametrize("case", [pytest.param(1, id="env.spec_edit_stale_rebuild_on_request")])
async def test_spec_edit_makes_stale_and_the_next_ask_rebuilds_it(
    tmp_path: Path, wheels: Path, template: Path, case: int
) -> None:
    reg, _, ws = _registry(tmp_path, wheels, template)
    env_id = "default:.alkera/envs/default"
    first = await reg.materialize(env_id)
    marker = Path(first.prefix) / "marker"
    marker.write_text("built once")
    pyproject = ws / ".alkera" / "envs" / "default" / "pyproject.toml"
    pyproject.write_text(pyproject.read_text() + "# edited\n")

    assert reg.describe(env_id).state == "stale"
    assert marker.exists()

    rebuilt = await reg.materialize(env_id)
    assert rebuilt.state == "ready" and rebuilt.spec_hash != first.spec_hash
    assert rebuilt.built_spec_hash == rebuilt.spec_hash


@needs_uv
async def test_a_wiped_machine_rebuilds_from_the_current_spec(
    tmp_path: Path, wheels: Path, template: Path
) -> None:
    reg, _, ws = _registry(tmp_path, wheels, template)
    env_id = "default:.alkera/envs/default"
    first = await reg.materialize(env_id)
    spec_dir = ws / ".alkera" / "envs" / "default"
    # Someone edits the spec to want otherpkg, then the machine is wiped.
    (spec_dir / "pyproject.toml").write_text(
        DEFAULT_PYPROJECT.replace('["tinypkg"]', '["tinypkg", "otherpkg"]')
    )
    lock_project(spec_dir, wheels, tmp_path / "lockcache")
    # The build in use is gone (the prefix is a link to it).
    _wipe_build(first.prefix)

    woke = await reg.materialize(env_id)
    assert woke.state == "ready"
    assert woke.built_spec_hash == woke.spec_hash != first.spec_hash
    names = [n for n, _ in await reg.packages(env_id)]
    assert "tinypkg" in names and "otherpkg" in names


@needs_uv
async def test_failed_build_is_recorded_with_its_log(
    tmp_path: Path, wheels: Path, template: Path
) -> None:
    lonely = tmp_path / "lonely-wheels"
    build_wheel(lonely, "tinypkg", "1.0.0")
    tpl = tmp_path / "tpl"
    tpl.mkdir()
    (tpl / "pyproject.toml").write_text(DEFAULT_PYPROJECT)
    lock_project(tpl, lonely, tmp_path / "c")
    next(lonely.iterdir()).unlink()  # the locked wheel is gone
    reg, _, _ = _registry(tmp_path, lonely, tpl)
    with pytest.raises(EnvBuildError) as info:
        await reg.materialize("default:.alkera/envs/default")
    assert "uv sync --frozen" in info.value.log
    assert reg.describe("default:.alkera/envs/default").state == "failed"


@needs_uv
@pytest.mark.parametrize("case", [pytest.param(1, id="env.install_edits_spec")])
async def test_install_edits_the_spec(
    tmp_path: Path, wheels: Path, template: Path, case: int
) -> None:
    reg, runner, ws = _registry(tmp_path, wheels, template)
    env_id = "default:.alkera/envs/default"
    await reg.materialize(env_id)
    runner.calls.clear()
    runner.envs.clear()
    spec_dir = ws / ".alkera" / "envs" / "default"

    desc, changed, log = await reg.install(env_id, ["otherpkg==0.1.0"], str(ws / "n.alknb.py"))

    find = ["--no-index", "--find-links", str(wheels)]
    assert runner.calls[:2] == [
        [
            UV,
            "add",
            "--no-sync",
            "--project",
            str(spec_dir.resolve()),
            "--python",
            PY,
            *find,
            "otherpkg==0.1.0",
        ],
        [UV, "sync", "--frozen", "--project", str(spec_dir.resolve()), "--python", PY, *find],
    ]
    assert changed == [
        ".alkera/envs/default/pyproject.toml",
        ".alkera/envs/default/uv.lock",
    ]
    assert "otherpkg==0.1.0" in (spec_dir / "pyproject.toml").read_text()
    assert 'name = "otherpkg"' in (spec_dir / "uv.lock").read_text()
    assert desc.state == "ready"
    assert ("otherpkg", "0.1.0") in await reg.packages(env_id)
    assert "uv add" not in log or "$ " in log
    _no_network(runner)


@needs_uv
async def test_install_into_a_uv_project(tmp_path: Path, wheels: Path) -> None:
    reg, _, ws = _registry(tmp_path, wheels, None)
    proj = ws / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "proj"\nversion = "0"\nrequires-python = ">=3.11"\ndependencies = []\n'
    )
    lock_project(proj, wheels, tmp_path / "c")
    desc, changed, _ = await reg.install("uv_project:proj", ["tinypkg"], str(proj / "n.alknb.py"))
    assert changed == ["proj/pyproject.toml", "proj/uv.lock"]
    assert desc.state == "ready" and desc.prefix.endswith("proj/.venv")
    assert ("tinypkg", "1.0.0") in await reg.packages("uv_project:proj")


@needs_uv
async def test_install_refuses_bad_requirements_before_running_anything(
    tmp_path: Path, wheels: Path, template: Path
) -> None:
    reg, runner, ws = _registry(tmp_path, wheels, template)
    with pytest.raises(ValueError, match="not a package requirement"):
        await reg.install(
            "default:.alkera/envs/default", ["ok", "--index-url=http://x"], str(ws / "n.py")
        )
    assert runner.calls == []


@pytest.mark.parametrize(
    ("module", "distribution", "expected"),
    [
        pytest.param("mylib", None, None, id="env.unknown_module_requires_distribution"),
        pytest.param("mylib", "my-lib", "my-lib", id="env.explicit_distribution_accepted"),
        pytest.param("sklearn", None, "scikit-learn", id="env.known_module_mapped"),
    ],
)
def test_install_for_module(
    tmp_path: Path, module: str, distribution: str | None, expected: str | None
) -> None:
    reg = LocalEnvRegistry(tmp_path, tmp_path / "envs", runner=RecordingRunner())
    if expected is None:
        with pytest.raises(DistributionRequiredError, match="name the distribution"):
            reg.install_for_module(module, distribution)
    else:
        assert reg.install_for_module(module, distribution) == expected


@needs_uv
@pytest.mark.parametrize("case", [pytest.param(1, id="env.script_install_block")])
async def test_script_env_build_and_install_through_the_document(
    tmp_path: Path, wheels: Path, case: int
) -> None:
    reg, runner, ws = _registry(tmp_path, wheels, None)
    nb = ws / "n.alknb.py"
    nb.write_text(NOTEBOOK)
    desc = await reg.resolve(str(nb), "script")
    built = await reg.materialize(desc.env_id)
    assert built.state == "ready"
    assert [n for n, _ in await reg.packages(built.env_id)] == ["tinypkg"]

    with pytest.raises(ScriptInstallViaDocumentError):
        await reg.install(built.env_id, ["otherpkg"], str(nb))
    assert nb.read_text() == NOTEBOOK  # the registry never writes the notebook

    header, rest = NOTEBOOK.split("# >>> alkera", 1)
    new_header = add_script_dependencies(header, ["otherpkg"])
    assert new_header == (
        '# /// script\n# dependencies = [\n#     "tinypkg",\n#     "otherpkg",\n# ]\n# ///\n'
    )
    nb.write_text(new_header + "# >>> alkera" + rest)  # what the document op does
    assert reg.describe(built.env_id).state == "stale"
    rebuilt = await reg.materialize(built.env_id)
    assert rebuilt.state == "ready"
    assert [n for n, _ in await reg.packages(built.env_id)] == ["otherpkg", "tinypkg"]
    _no_network(runner)


async def test_venv_is_used_as_is_and_listed_kinds_are_not_built(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    nb = _tree(ws)
    runner = RecordingRunner()
    reg = LocalEnvRegistry(ws, tmp_path / "envs", runner=runner)
    venv = await reg.resolve(str(nb), "./myenv")
    assert (await reg.materialize(venv.env_id)).state == "ready"
    with pytest.raises(EnvNotFoundError):
        reg.describe("requirements:.")
    with pytest.raises(EnvNotMaterializableError):
        await reg.install(venv.env_id, ["tinypkg"], str(nb))
    assert runner.calls == []
    shutil.rmtree(ws / "proj" / "nb" / "myenv" / "bin")
    with pytest.raises(EnvNotFoundError):
        await reg.materialize(venv.env_id)


@pytest.mark.parametrize(
    "env_id",
    [
        pytest.param("nonsense", id="no-kind"),
        pytest.param("venv:../../etc", id="escapes-root"),
        pytest.param("default:elsewhere", id="wrong-default-root"),
        pytest.param("uv_project:missing", id="no-project"),
    ],
)
def test_unknown_env_ids_are_refused(tmp_path: Path, env_id: str) -> None:
    reg = LocalEnvRegistry(tmp_path, tmp_path / "envs", runner=RecordingRunner())
    with pytest.raises(EnvNotFoundError):
        reg.describe(env_id)


@needs_uv
async def test_a_registry_told_its_builds_reach_the_index_installs_without_uv_offline(
    tmp_path: Path, wheels: Path, template: Path
) -> None:
    """The platform's kernel sandbox builds through the workspace's egress:
    there an install must not inherit ``UV_OFFLINE`` (it would block every
    package uv has not cached), while Python downloads stay off."""
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = RecordingRunner()
    reg = LocalEnvRegistry(
        ws,
        tmp_path / "envs",
        runner=runner,
        python=PY,
        uv=UV or "uv",
        index_args=["--no-index", "--find-links", str(wheels)],
        default_template=template,
        offline=False,
    )
    desc = await reg.resolve(str(ws / "n.alknb.py"), None)
    built, _changed, _log = await reg.install(desc.env_id, ["otherpkg"], str(ws / "n.alknb.py"))
    assert ("otherpkg", "0.1.0") in await reg.packages(built.env_id)
    uv_runs = [env for argv, env in zip(runner.calls, runner.envs, strict=True) if argv[0] == UV]
    assert uv_runs
    assert all("UV_OFFLINE" not in env for env in uv_runs)
    assert all(env["UV_PYTHON_DOWNLOADS"] == "never" for env in uv_runs)
