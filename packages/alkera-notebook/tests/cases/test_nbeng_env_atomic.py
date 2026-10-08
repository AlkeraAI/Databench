"""An environment build changes what an environment is only when it succeeds.

A failed or cancelled install leaves the spec byte for byte as it was and the
previous build in use; a failed rebuild of a stale environment keeps running
the last good build; only an environment that never built reads ``failed``.
A package can be removed, and the spec's own requirements can be read.

Builds are the real ``uv``, offline, against hand-written wheels. What fails
a build is real too: the wheel the lock names disappears from the index
between the lock and the sync (a runner hook deletes it), and a cancel kills
a sync that the hook holds open.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path

import pytest
from alkera_notebook.envs import (
    CommandResult,
    EnvBuildError,
    LocalEnvRegistry,
)
from alkera_notebook.envs.detect import interpreter_in
from alkera_notebook.envs.registry import EnvBuildCancelledError
from nbeng_env_support import DEFAULT_PYPROJECT, PY, UV, RecordingRunner, build_wheel, lock_project

pytestmark = [
    pytest.mark.xdist_group("nbeng_env"),
    pytest.mark.skipif(UV is None, reason="uv is not on PATH"),
]

ENV = "default:.alkera/envs/default"
NOTEBOOK = "n.alknb.py"


class HookedRunner(RecordingRunner):
    """Runs every command for real; ``before_sync`` runs first when armed."""

    def __init__(self) -> None:
        super().__init__()
        self.before_sync: Callable[[], Awaitable[None]] | None = None

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> CommandResult:
        hook = self.before_sync
        if hook is not None and len(argv) > 1 and argv[1] == "sync":
            self.before_sync = None
            await hook()
        return await super().run(argv, cwd=cwd, env=env, timeout_s=timeout_s)


@pytest.fixture
def wheels(tmp_path: Path) -> Path:
    d = tmp_path / "wheels"
    build_wheel(d, "tinypkg", "1.0.0")
    build_wheel(d, "otherpkg", "0.1.0")
    return d


@pytest.fixture
def template(tmp_path: Path, wheels: Path) -> Path:
    d = tmp_path / "template"
    d.mkdir()
    (d / "pyproject.toml").write_text(DEFAULT_PYPROJECT)
    lock_project(d, wheels, tmp_path / "lock-cache")
    return d


def _registry(
    tmp_path: Path, wheels: Path, template: Path
) -> tuple[LocalEnvRegistry, HookedRunner]:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    runner = HookedRunner()
    reg = LocalEnvRegistry(
        ws,
        tmp_path / "envs",
        runner=runner,
        python=PY,
        uv=UV or "uv",
        index_args=["--no-index", "--find-links", str(wheels)],
        default_template=template,
    )
    return reg, runner


def _spec(reg: LocalEnvRegistry) -> dict[str, bytes]:
    root = reg.root / ".alkera" / "envs" / "default"
    return {n: (root / n).read_bytes() for n in ("pyproject.toml", "uv.lock")}


def _losing(wheel: Path) -> Callable[[], Awaitable[None]]:
    async def lose() -> None:
        await asyncio.to_thread(wheel.unlink)

    return lose


def _build_in_use(prefix: str) -> Path:
    return Path(prefix).resolve()


def _exists(path: Path | str) -> bool:
    return Path(path).exists()


async def _names(reg: LocalEnvRegistry) -> set[str]:
    return {name for name, _ in await reg.packages(ENV)}


async def test_a_failed_install_leaves_the_spec_and_the_build_as_they_were(
    tmp_path: Path, wheels: Path, template: Path
) -> None:
    reg, runner = _registry(tmp_path, wheels, template)
    await reg.materialize(ENV)
    before = _spec(reg)
    interpreter = reg.describe(ENV).interpreter

    runner.before_sync = _losing(wheels / "otherpkg-0.1.0-py3-none-any.whl")
    with pytest.raises(EnvBuildError):
        await reg.install(ENV, ["otherpkg==0.1.0"], NOTEBOOK)

    assert _spec(reg) == before
    after = reg.describe(ENV)
    assert after.state == "ready" and after.interpreter == interpreter
    assert _exists(after.interpreter)
    assert await _names(reg) == {"tinypkg"}
    assert after.last_failure == "Installing otherpkg==0.1.0 failed."
    # The next install goes through as if the failed one had never been asked.
    desc, _, _ = await reg.install(ENV, ["tinypkg"], NOTEBOOK)
    assert desc.state == "ready" and desc.last_failure == ""


async def test_a_failed_rebuild_of_a_changed_spec_keeps_the_last_good_build(
    tmp_path: Path, wheels: Path, template: Path
) -> None:
    reg, runner = _registry(tmp_path, wheels, template)
    await reg.materialize(ENV)
    spec_dir = reg.root / ".alkera" / "envs" / "default"
    (spec_dir / "pyproject.toml").write_text(DEFAULT_PYPROJECT.replace('"tinypkg"', '"otherpkg"'))
    lock_project(spec_dir, wheels, tmp_path / "relock-cache")
    assert reg.describe(ENV).state == "stale"

    runner.before_sync = _losing(wheels / "otherpkg-0.1.0-py3-none-any.whl")
    with pytest.raises(EnvBuildError):
        await reg.materialize(ENV)

    after = reg.describe(ENV)
    assert after.state == "stale"
    assert await _names(reg) == {"tinypkg"}
    assert after.last_failure != ""


async def test_an_environment_that_never_built_reads_failed(
    tmp_path: Path, wheels: Path, template: Path
) -> None:
    reg, runner = _registry(tmp_path, wheels, template)
    runner.before_sync = _losing(wheels / "tinypkg-1.0.0-py3-none-any.whl")
    with pytest.raises(EnvBuildError):
        await reg.materialize(ENV)
    after = reg.describe(ENV)
    assert after.state == "failed"
    assert not _exists(interpreter_in(Path(after.prefix)))


async def test_a_cancelled_install_stops_its_build_and_puts_the_spec_back(
    tmp_path: Path, wheels: Path, template: Path
) -> None:
    reg, runner = _registry(tmp_path, wheels, template)
    await reg.materialize(ENV)
    before = _spec(reg)
    holding = asyncio.Event()

    async def hold() -> None:
        holding.set()
        await asyncio.Event().wait()

    runner.before_sync = hold
    install = asyncio.ensure_future(reg.install(ENV, ["otherpkg==0.1.0"], NOTEBOOK))
    await asyncio.wait_for(holding.wait(), 60)
    assert reg.describe(ENV).state == "building"
    assert await reg.cancel(ENV) is True
    with pytest.raises(EnvBuildCancelledError):
        await asyncio.wait_for(install, 30)
    assert _spec(reg) == before
    assert reg.describe(ENV).state == "ready"
    assert reg.describe(ENV).last_failure == "Installing otherpkg==0.1.0 was cancelled."
    assert await _names(reg) == {"tinypkg"}
    assert await reg.cancel(ENV) is False, "nothing is building now"


async def test_a_package_is_removed_from_the_spec_and_the_build(
    tmp_path: Path, wheels: Path, template: Path
) -> None:
    reg, _ = _registry(tmp_path, wheels, template)
    await reg.materialize(ENV)
    await reg.install(ENV, ["otherpkg==0.1.0"], NOTEBOOK)
    assert await reg.requirements(ENV) == ["otherpkg==0.1.0", "tinypkg"]

    desc, changed, _ = await reg.remove(ENV, ["otherpkg"], NOTEBOOK)

    assert desc.state == "ready"
    assert ".alkera/envs/default/pyproject.toml" in changed
    assert await reg.requirements(ENV) == ["tinypkg"]
    assert await _names(reg) == {"tinypkg"}


async def test_a_running_kernel_keeps_its_interpreter_while_a_new_build_replaces_it(
    tmp_path: Path, wheels: Path, template: Path
) -> None:
    """The build in use is never written into: the old one stays whole on
    disk for a kernel that loaded it until the next build replaces it."""
    reg, _ = _registry(tmp_path, wheels, template)
    first = await reg.materialize(ENV)
    loaded = _build_in_use(first.prefix)
    await reg.install(ENV, ["otherpkg==0.1.0"], NOTEBOOK)
    now = reg.describe(ENV)
    assert _build_in_use(now.prefix) != loaded
    assert _exists(loaded / "pyvenv.cfg"), "the previous build stays for a kernel still on it"
    assert now.interpreter == first.interpreter, "the path a kernel starts from does not move"
