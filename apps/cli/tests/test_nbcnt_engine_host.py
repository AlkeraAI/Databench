"""The org worker's notebook engines: one per workspace, built from the box's
pieces, its state partitioned by workspace under the org's root, and taken down
with the workspace."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from _nbcnt_fakes import make_rig
from alkera_cli.files import folder_fence
from alkera_cli.harness import sandbox_steps, workspace_sandbox
from alkera_cli.harness.sandbox_layout import chat_slug
from alkera_cli.notebooks import engine_host as eh
from alkera_cli.notebooks.kernel_sandbox import DATA_SUBDIR
from alkera_notebook.document.file_store import FileDocumentStore
from alkera_notebook.envs.template import DEFAULT_ENV_PYPROJECT, default_env_template
from alkera_notebook.sql.provider import SqlProviderRegistry


class Engine:
    """An engine as the host holds it: records how it was taken down."""

    def __init__(self, parts: eh.WorkspaceParts) -> None:
        self.parts = parts
        self.calls: list[str] = []

    async def suspend(self) -> object:
        self.calls.append("suspend")
        return None

    async def close(self) -> None:
        self.calls.append("close")


def make_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **settings_: object
) -> eh.NotebookEngineHost:
    rig = make_rig(tmp_path, monkeypatch)
    # The host builds its kernel sandboxes with the default walk, which hands
    # trees to a kernel's uid: root's alone, so recorded here.
    monkeypatch.setattr(sandbox_steps, "repair_tree", rig.runner.repair)
    settings = eh.NotebookHostSettings(
        sandbox=rig.settings,
        capability=rig.cap,
        platform_mount=rig.config.platform_mount,
        memory_mb=1536,
        vcpu=2,
        cgroup_root=rig.config.trees.cgroup_root,
        runsc_root=rig.config.trees.runsc_root,
        **settings_,  # type: ignore[arg-type]
    )
    return eh.NotebookEngineHost(
        org_root=rig.short / "org",
        settings=settings,
        ensure_uid=rig.ensure_uid,
        engine_factory=Engine,
        run=rig.runner,
    )


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> eh.NotebookEngineHost:
    return make_host(tmp_path, monkeypatch)


def tenancy(tmp_path: Path, key: str = "ws:ws1") -> eh.WorkspaceTenancy:
    folder = tmp_path / key.replace(":", "_") / "files"
    folder.mkdir(parents=True, exist_ok=True)
    return eh.WorkspaceTenancy(key=key, folder=folder, envs_dir=tmp_path / "envs" / key[3:])


def test_every_kind_of_notebook_state_is_partitioned(host: eh.NotebookEngineHost) -> None:
    kinds = {k.name: k for k in eh.NOTEBOOK_STORE_KINDS}
    assert {k.partition for k in kinds.values()} <= {"workspace", "chat"}
    assert kinds["notebook_digest_cursor"].partition == "chat"
    root = host.org_root
    assert host.partition_dir(kinds["kernel_state"], "ws:ws1") == root / "kernel-state" / "ws1"
    assert host.partition_dir(kinds["workspace_envs"], "ws:ws1") == root / "envs" / "ws1"
    # The runtime directory holds sockets, whose paths must stay short.
    assert host.partition_dir(kinds["kernel_runtime"], "ws:ws1") == root / "run" / chat_slug(
        "ws:ws1"
    )
    with pytest.raises(ValueError, match="chat's"):
        host.partition_dir(kinds["notebook_digest_cursor"], "ws:ws1")
    with pytest.raises(KeyError):
        eh.store_kind("nothing")


def test_two_workspaces_share_no_directory(host: eh.NotebookEngineHost, tmp_path: Path) -> None:
    one = host.sandbox_config(tenancy(tmp_path, "ws:one"))
    two = host.sandbox_config(tenancy(tmp_path, "ws:two"))
    for a, b in ((one.state_dir, two.state_dir), (one.runtime_dir, two.runtime_dir)):
        assert a != b and a not in b.parents and b not in a.parents
    assert one.container != two.container


@pytest.mark.asyncio
async def test_one_engine_per_workspace_made_without_starting_anything(
    host: eh.NotebookEngineHost, tmp_path: Path
) -> None:
    ws = tenancy(tmp_path)
    first = await host.engine(ws)
    assert await host.engine(ws) is first
    assert isinstance(first, Engine)
    assert not first.parts.sandbox.running()  # the first kernel starts the sandbox
    other = await host.engine(tenancy(tmp_path, "ws:ws2"))
    assert other is not first
    assert host.held() == frozenset({"ws:ws1", "ws:ws2"})
    moved = eh.WorkspaceTenancy(key="ws:ws1", folder=tmp_path / "x", envs_dir=ws.envs_dir)
    with pytest.raises(ValueError, match="another tree"):
        host.parts(moved)


@pytest.mark.asyncio
async def test_putting_a_workspace_away_suspends_its_engine_and_stops_its_sandbox(
    host: eh.NotebookEngineHost, tmp_path: Path
) -> None:
    ws = tenancy(tmp_path)
    engine = await host.engine(ws)
    assert isinstance(engine, Engine)
    await asyncio.to_thread(engine.parts.sandbox.ensure_running)
    assert engine.parts.sandbox.running()
    host.register()
    try:
        await workspace_sandbox.release_workspace_sandbox("ws:ws1")
    finally:
        workspace_sandbox._TEARDOWNS.remove(host.put_away)
        folder_fence._HOLDERS.remove(host.follow_fences)
    assert engine.calls == ["suspend", "close"]
    assert not engine.parts.sandbox.running()
    assert host.held() == frozenset()
    assert await host.engine(ws) is not engine  # the next use makes a fresh one


@pytest.mark.asyncio
async def test_the_sweep_stops_only_idle_sandboxes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = make_host(tmp_path, monkeypatch, idle_stop_seconds=0.0)
    busy = await host.engine(tenancy(tmp_path, "ws:busy"))
    idle = await host.engine(tenancy(tmp_path, "ws:idle"))
    assert isinstance(busy, Engine) and isinstance(idle, Engine)
    await asyncio.to_thread(busy.parts.sandbox.claim, "krn_1")
    await asyncio.to_thread(idle.parts.sandbox.ensure_running)
    try:
        assert await host.sweep() == ["ws:idle"]
        assert busy.parts.sandbox.running() and not idle.parts.sandbox.running()
    finally:
        busy.parts.sandbox.stop()


def test_the_engine_is_built_from_the_boxs_pieces_and_its_paths_agree(
    host: eh.NotebookEngineHost, tmp_path: Path
) -> None:
    ws = tenancy(tmp_path)
    factory = eh.notebook_engine_factory(
        store_for=lambda t: FileDocumentStore(t.folder),
        sql=SqlProviderRegistry(),
        python_home="/opt/alkera/python/current",
        default_template=default_env_template(tmp_path / "template"),
    )
    parts = host.parts(ws)
    engine = factory(parts)
    config = engine.config  # type: ignore[attr-defined]
    assert engine.launcher is parts.launcher  # type: ignore[attr-defined]
    assert engine.transport is parts.transport  # type: ignore[attr-defined]
    assert engine.memory is parts.memory  # type: ignore[attr-defined]
    # Where the engine makes a kernel's data directory is where the launcher
    # hands it to the kernel, inside the runtime directory the sandbox binds.
    made = Path(config.data_root) / DATA_SUBDIR / "krn_0001"
    assert made == parts.sandbox.data_path("krn_0001")
    assert parts.sandbox.container_path(made) == "/opt/alkera/run/kernels/krn_0001"
    # The environments the engine builds are the ones the agents and the
    # kernels bind, and its kernels boot from the bound platform mount.
    assert Path(config.env_root) == ws.envs_dir
    assert config.kernel_mount == str(parts.sandbox.config.platform_mount)
    assert config.base_env["HOME"] == str(ws.folder)
    # Members and kernels share these environments, and the listing says so.
    assert config.shared_envs is True


def test_the_platform_mount_is_staged_as_real_files_by_content(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "_alkera_kernel").mkdir(parents=True)
    (source / "boot.py").write_text("# boot\n")
    (source / "_alkera_kernel" / "__init__.py").write_text("V = 1\n")
    (source / "_alkera_kernel" / "__pycache__").mkdir()
    public = tmp_path / "alkera"
    public.mkdir()
    (public / "__init__.py").write_text("")
    under = tmp_path / "py"
    first = eh.stage_platform_mount(source / "boot.py", source / "_alkera_kernel", public, under)
    assert (first / "boot.py").read_text() == "# boot\n"
    assert (first / "public" / "alkera" / "__init__.py").is_file()
    assert not (first / "_alkera_kernel" / "__pycache__").exists()
    assert not any(p.is_symlink() for p in first.rglob("*"))
    assert (
        eh.stage_platform_mount(source / "boot.py", source / "_alkera_kernel", public, under)
        == first
    )  # idempotent
    (source / "_alkera_kernel" / "__init__.py").write_text("V = 2\n")
    second = eh.stage_platform_mount(source / "boot.py", source / "_alkera_kernel", public, under)
    assert second != first and (first / "_alkera_kernel" / "__init__.py").read_text() == "V = 1\n"
    assert not any(name.endswith(".staging") for name in os.listdir(under))


def _platform_sources(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "src"
    (source / "_alkera_kernel" / "sub").mkdir(parents=True)
    (source / "boot.py").write_text("# boot\n")
    (source / "_alkera_kernel" / "__init__.py").write_text("V = 1\n")
    (source / "_alkera_kernel" / "sub" / "__init__.py").write_text("")
    public = tmp_path / "alkera"
    public.mkdir()
    (public / "__init__.py").write_text("")
    return source, public


def _modes(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_mode & 0o7777 for p in [root, *root.rglob("*")]}


def test_the_platform_mount_is_readable_by_kernels_and_writable_by_none(tmp_path: Path) -> None:
    """The worker runs under umask 077; kernels run as other uids and must
    read boot.py and the packages, never write them."""
    source, public = _platform_sources(tmp_path)
    old = os.umask(0o077)
    try:
        staged = eh.stage_platform_mount(
            source / "boot.py", source / "_alkera_kernel", public, tmp_path / "py"
        )
    finally:
        os.umask(old)
    modes = _modes(staged)
    dirs = {name for name in modes if (staged / name).is_dir()}
    assert {".", "_alkera_kernel", "_alkera_kernel/sub", "public", "public/alkera"} <= dirs
    assert {name: modes[name] for name in dirs} == dict.fromkeys(dirs, 0o755)
    files = set(modes) - dirs
    assert "boot.py" in files and "public/alkera/__init__.py" in files
    assert {name: modes[name] for name in files} == dict.fromkeys(files, 0o644)


def test_a_platform_mount_an_earlier_build_left_closed_is_opened(tmp_path: Path) -> None:
    source, public = _platform_sources(tmp_path)
    staged = eh.stage_platform_mount(
        source / "boot.py", source / "_alkera_kernel", public, tmp_path / "py"
    )
    for path in [staged, *staged.rglob("*")]:
        path.chmod(0o700 if path.is_dir() else 0o600)
    assert (
        eh.stage_platform_mount(
            source / "boot.py", source / "_alkera_kernel", public, tmp_path / "py"
        )
        == staged
    )
    assert set(_modes(staged).values()) == {0o755, 0o644}


def test_a_tenancy_finds_the_environments_its_agents_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alkera_cli.harness import sandbox_env

    monkeypatch.setenv(sandbox_env.ENV_WORKSPACE_ENVS_ROOT, str(tmp_path / "orgenvs"))
    member = eh.tenancy_of("ws:ws1", tmp_path / "files")
    assert member.envs_dir == tmp_path / "orgenvs" / "ws1"
    solo = eh.tenancy_of("chat_x", tmp_path / "scratch", state_dir=tmp_path / ".runtime")
    assert solo.envs_dir == tmp_path / ".runtime" / "envs"
    assert eh.store_kind("notebook_digest_cursor").subdir == "notebooks"


def test_the_sandbox_holds_each_kernel_s_data_where_the_engine_writes_it(
    host: eh.NotebookEngineHost, tmp_path: Path
) -> None:
    """The engine makes a kernel's data directory under its data root by
    the core's layout; the sandbox hands over and binds that same directory,
    so a file the engine writes is the file the kernel reads."""
    from alkera_notebook.kernels.launch_local import kernel_data_dir

    parts = host.parts(tenancy(tmp_path))
    engine_side = kernel_data_dir(parts.engine_paths["data_root"], "krn_0007")
    assert parts.sandbox.data_path("krn_0007") == engine_side
    assert parts.sandbox.container_path(engine_side) is not None


async def test_the_box_s_environment_builds_are_not_kept_offline(
    host: eh.NotebookEngineHost, tmp_path: Path
) -> None:
    """An install in the kernel sandbox reaches the index through the
    workspace's egress, so the engine's builds never carry ``UV_OFFLINE``."""
    import dataclasses

    from alkera_notebook.envs import CommandResult

    seen: list[dict[str, str]] = []

    class Recording:
        async def run(
            self,
            argv: Sequence[str],
            *,
            cwd: str,
            env: Mapping[str, str] | None = None,
            timeout_s: float | None = None,
        ) -> CommandResult:
            seen.append(dict(env or {}))
            return CommandResult(returncode=1, stdout="", stderr="refused here")

    ws = tenancy(tmp_path)
    factory = eh.notebook_engine_factory(
        store_for=lambda t: FileDocumentStore(t.folder),
        sql=SqlProviderRegistry(),
        python_home="/opt/alkera/python/current",
        default_template=default_env_template(tmp_path / "template"),
    )
    parts = dataclasses.replace(host.parts(ws), runner=Recording())  # type: ignore[arg-type]
    engine = factory(parts)
    envs = engine.envs  # type: ignore[attr-defined]
    (ws.folder / "pyproject.toml").write_text(
        '[project]\nname = "ws"\nversion = "0"\nrequires-python = ">=3.11"\n'
    )
    desc = await envs.resolve(str(ws.folder / "n.alknb.py"), None)
    assert desc.kind == "uv_project"
    with pytest.raises(Exception, match="failed"):
        await envs.install(desc.env_id, ["polars"], str(ws.folder / "n.alknb.py"))
    assert seen and all("UV_OFFLINE" not in env for env in seen)


#: The lock a scripted ``uv lock`` writes.
_LOCK = "version = 1\n# locked here\n"


class _RecordingRunner:
    """The sandbox's command runner: ``uv lock`` writes :data:`_LOCK` into
    the project it is run in (unless ``lock_fails``), and every other
    command is refused."""

    def __init__(self, *, lock_fails: bool = False) -> None:
        self.argvs: list[list[str]] = []
        self.lock_fails = lock_fails

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> object:
        from alkera_notebook.envs import CommandResult

        self.argvs.append(list(argv))
        if argv[1] == "lock" and not self.lock_fails:
            (Path(cwd) / "uv.lock").write_text(_LOCK)
            return CommandResult(returncode=0, stdout="", stderr="")
        return CommandResult(returncode=1, stdout="", stderr="refused here")


def _box_envs(
    host: eh.NotebookEngineHost, tmp_path: Path, *, lock_fails: bool = False
) -> tuple[object, Path, _RecordingRunner]:
    import dataclasses

    ws = tenancy(tmp_path)
    runner = _RecordingRunner(lock_fails=lock_fails)
    factory = eh.notebook_engine_factory(
        store_for=lambda t: FileDocumentStore(t.folder),
        sql=SqlProviderRegistry(),
        python_home="/opt/alkera/python/current",
        default_template=default_env_template(tmp_path / "template"),
    )
    parts = dataclasses.replace(host.parts(ws), runner=runner)  # type: ignore[arg-type]
    return factory(parts).envs, ws.folder, runner  # type: ignore[attr-defined]


async def test_a_workspace_s_first_default_notebook_locks_the_template_then_builds_it(
    host: eh.NotebookEngineHost, tmp_path: Path
) -> None:
    """The kernel's start asks for the environment and nobody approves it; a
    workspace with no default spec yet gets the platform's template, locked
    once in the sandbox and written into the workspace's spec, and its build
    runs frozen on that lock (here refused by the runner, after it was asked)."""
    from alkera_notebook.envs import EnvBuildError

    envs, folder, runner = _box_envs(host, tmp_path)
    desc = await envs.resolve(str(folder / "n.alknb.py"), "default")  # type: ignore[attr-defined]
    with pytest.raises(EnvBuildError, match="sync failed"):
        await envs.materialize(desc.env_id)  # type: ignore[attr-defined]
    spec = folder / ".alkera" / "envs" / "default"
    assert (spec / "pyproject.toml").read_text() == DEFAULT_ENV_PYPROJECT
    assert (spec / "uv.lock").read_text() == _LOCK
    assert [argv[:2] for argv in runner.argvs] == [["uv", "lock"], ["uv", "sync"]]
    assert runner.argvs[1][2] == "--frozen"
    assert sorted(os.listdir(spec.parent)) == ["default"]  # no staging left


async def test_a_lock_the_sandbox_refuses_seeds_nothing_and_says_so(
    host: eh.NotebookEngineHost, tmp_path: Path
) -> None:
    from alkera_notebook.envs import EnvBuildError

    envs, folder, runner = _box_envs(host, tmp_path, lock_fails=True)
    desc = await envs.resolve(str(folder / "n.alknb.py"), "default")  # type: ignore[attr-defined]
    with pytest.raises(EnvBuildError, match="Locking the default environment's packages failed"):
        await envs.materialize(desc.env_id)  # type: ignore[attr-defined]
    assert os.listdir(folder / ".alkera" / "envs") == []
    assert [argv[:2] for argv in runner.argvs] == [["uv", "lock"]]


async def test_a_default_spec_someone_changed_builds_as_it_is_without_an_approval(
    host: eh.NotebookEngineHost, tmp_path: Path
) -> None:
    """A changed spec went through whoever changed it (a person's edit, or an
    agent's tool permission); the kernel's start builds it frozen on its own
    lock, keeping the change."""
    from alkera_notebook.envs import EnvBuildError

    envs, folder, runner = _box_envs(host, tmp_path)
    spec = folder / ".alkera" / "envs" / "default"
    spec.mkdir(parents=True)
    (spec / "pyproject.toml").write_text(
        DEFAULT_ENV_PYPROJECT.replace('    "tqdm', '    "evil==1.0",\n    "tqdm')
    )
    (spec / "uv.lock").write_text(_LOCK)
    desc = await envs.resolve(str(folder / "n.alknb.py"), "default")  # type: ignore[attr-defined]
    with pytest.raises(EnvBuildError, match="sync failed"):
        await envs.materialize(desc.env_id)  # type: ignore[attr-defined]
    assert [argv[:3] for argv in runner.argvs] == [["uv", "sync", "--frozen"]]
    assert "evil==1.0" in (spec / "pyproject.toml").read_text()


def _modes_below(folder: Path, where: Path) -> dict[str, int]:
    """The modes of ``where``, what it holds and its ancestors below ``folder``."""
    import stat

    return {
        str(p.relative_to(folder)): stat.S_IMODE(p.lstat().st_mode)
        for p in [*where.parents, where, *where.iterdir()]
        if folder in p.parents
    }


async def test_a_seeded_default_spec_is_one_the_build_uid_can_work_in(
    host: eh.NotebookEngineHost, tmp_path: Path
) -> None:
    """The org worker seeds the default spec under its owner-only umask while
    the build runs in the kernel sandbox as the workspace's build uid, whose
    only tie to the tree is its files group. The seeded spec follows the
    shared tree's modes, so the ``runsc exec`` composed for the lock (in the
    staging project beside the spec) and for the build (in the spec
    directory), both as the build uid in the files group, finds a directory
    its group may enter and write and files its group may read and write."""
    import dataclasses

    from alkera_cli.notebooks.launch_container import SandboxCommandRunner
    from alkera_notebook.envs import CommandResult, EnvBuildError

    ws = tenancy(tmp_path)
    parts = host.parts(ws)
    composing = SandboxCommandRunner(parts.sandbox)
    met: list[tuple[list[str], dict[str, int]]] = []

    class Composing:
        async def run(
            self,
            argv: Sequence[str],
            *,
            cwd: str,
            env: Mapping[str, str] | None = None,
            timeout_s: float | None = None,
        ) -> CommandResult:
            modes = _modes_below(ws.folder, Path(cwd))
            met.append((composing.command(argv, cwd=cwd, env=env), modes))
            if argv[1] == "lock":
                (Path(cwd) / "uv.lock").write_text(_LOCK)
                return CommandResult(returncode=0, stdout="", stderr="")
            return CommandResult(returncode=1, stdout="", stderr="composed only")

    factory = eh.notebook_engine_factory(
        store_for=lambda t: FileDocumentStore(t.folder),
        sql=SqlProviderRegistry(),
        python_home="/opt/alkera/python/current",
        default_template=default_env_template(tmp_path / "template"),
    )
    envs = factory(dataclasses.replace(parts, runner=Composing())).envs  # type: ignore[arg-type,attr-defined]
    old = os.umask(0o077)
    try:
        desc = await envs.resolve(str(ws.folder / "n.alknb.py"), "default")
        with pytest.raises(EnvBuildError):
            await envs.materialize(desc.env_id)
    finally:
        os.umask(old)

    (lock_command, lock_modes), (command, modes) = met
    build = parts.sandbox.build_identity()
    at = lock_command.index(parts.sandbox.config.container)
    assert f"--user={build.uid}:{parts.sandbox.gid}" in lock_command[:at]
    (staging,) = [m for m in lock_modes if m.count("/") == 2 and m != ".alkera/envs/default"]
    assert f"--cwd=/home/alkera/{staging}" in lock_command[:at]
    assert lock_modes == {
        ".alkera": 0o2770,
        ".alkera/envs": 0o2770,
        staging: 0o2770,
        f"{staging}/pyproject.toml": 0o660,
    }
    at = command.index(parts.sandbox.config.container)
    assert f"--user={build.uid}:{parts.sandbox.gid}" in command[:at]
    assert "--cwd=/home/alkera/.alkera/envs/default" in command[:at]
    assert build.uid != parts.sandbox.tree_uid  # not the owner: the group decides
    assert modes == {
        ".alkera": 0o2770,
        ".alkera/envs": 0o2770,
        ".alkera/envs/default": 0o2770,
        ".alkera/envs/default/pyproject.toml": 0o660,
        ".alkera/envs/default/uv.lock": 0o660,
    }


@pytest.mark.asyncio
async def test_kernels_freeze_while_their_folder_lease_is_in_doubt_and_thaw_after(
    host: eh.NotebookEngineHost, tmp_path: Path
) -> None:
    """The custody's verdict, applied through the fence: only the doubtful
    workspace's kernels stop, and they go on once the lease is confirmed."""
    doubtful = await host.engine(tenancy(tmp_path, "ws:doubtful"))
    fine = await host.engine(tenancy(tmp_path, "ws:fine"))
    assert isinstance(doubtful, Engine) and isinstance(fine, Engine)
    await asyncio.to_thread(doubtful.parts.sandbox.claim, "krn_1")
    await asyncio.to_thread(fine.parts.sandbox.claim, "krn_2")
    cgroup = doubtful.parts.sandbox.cgroup_dir()
    assert cgroup is not None
    host.register()
    try:
        await folder_fence.apply(lambda key: key == "ws:doubtful")
        assert (cgroup / "cgroup.freeze").read_text() == "1"
        assert doubtful.parts.sandbox.fenced and not fine.parts.sandbox.fenced
        await folder_fence.apply(lambda key: False)
        assert (cgroup / "cgroup.freeze").read_text() == "0"
        assert not doubtful.parts.sandbox.fenced
    finally:
        workspace_sandbox._TEARDOWNS.remove(host.put_away)
        folder_fence._HOLDERS.remove(host.follow_fences)
        doubtful.parts.sandbox.stop()
        fine.parts.sandbox.stop()
