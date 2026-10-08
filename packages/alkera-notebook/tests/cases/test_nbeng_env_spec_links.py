"""A link or a fifo at an environment's spec path is never read.

On a box the registry runs as the org worker, a uid that can read far more
than the workspace tree (another workspace, a private chat folder, the org's
credential; host files where the box serves one org), while any cell can put
a link in the tree. A spec file that is a link to a file outside the tree
must not bring that file's bytes back: not into the tree (a failed install
puts the spec back as it was read), not into the build record under the env
root, and not out through what the registry returns. A fifo there must not
hang the reader.

Real registry over a real workspace; only the build command runner is
scripted, so the tests need no ``uv``.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

import pytest
from alkera_notebook.envs import CommandResult, EnvError, LocalEnvRegistry
from alkera_notebook.envs.detect import Detector, detect_envs

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX links and fifos")

DEFAULT = "default:.alkera/envs/default"
SECRET = "host-secret-7f3a"
#: The outside file is valid TOML with a ``[project]`` table, so a reader
#: that followed the link would take it for a spec and use its bytes.
OUTSIDE = f'[project]\nname = "x"\nversion = "0"\ndependencies = ["{SECRET}"]\n'


class Runner:
    """Every command succeeds; the build probe reports a CPython build."""

    def __init__(self) -> None:
        self.ran: list[list[str]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> CommandResult:
        self.ran.append(list(argv))
        out = json.dumps({"version": "3.13.1", "build": "gil"}) if "-I" in argv else ""
        return CommandResult(returncode=0, stdout=out, stderr="")


class FailingRunner(Runner):
    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> CommandResult:
        self.ran.append(list(argv))
        return CommandResult(returncode=1, stdout="", stderr="stopped by the test")


def outside_file(tmp_path: Path) -> Path:
    host = tmp_path / "host"
    host.mkdir()
    secret = host / "node.env"
    secret.write_text(OUTSIDE, encoding="utf-8")
    return secret


def link_the_file(tmp_path: Path, spec_root: Path) -> None:
    """``pyproject.toml`` in the spec is a link to the outside file."""
    spec_root.mkdir(parents=True)
    (spec_root / "pyproject.toml").symlink_to(outside_file(tmp_path))


def link_the_directory(tmp_path: Path, spec_root: Path) -> None:
    """The spec directory itself is a link to the outside file's directory."""
    spec_root.parent.mkdir(parents=True)
    secret = outside_file(tmp_path)
    (secret.parent / "pyproject.toml").write_text(OUTSIDE, encoding="utf-8")
    spec_root.symlink_to(secret.parent, target_is_directory=True)


LINK_SHAPES = [
    pytest.param(link_the_file, id="spec-file-is-a-link"),
    pytest.param(link_the_directory, id="spec-directory-is-a-link"),
]


def workspace(tmp_path: Path, plant: Callable[[Path, Path], None]) -> tuple[Path, Path]:
    ws = tmp_path / "ws"
    spec_root = ws / ".alkera" / "envs" / "default"
    plant(tmp_path, spec_root)
    return ws, tmp_path / "ws-envs"


def registry(ws: Path, env_root: Path, runner: Runner) -> LocalEnvRegistry:
    return LocalEnvRegistry(ws, env_root, runner=runner, uv="uv", cache_dir=env_root / "cache")


def files_holding(root: Path, text: str) -> list[str]:
    """Every regular file under ``root`` (no link followed) whose bytes hold ``text``."""
    found: list[str] = []
    for directory, _, names in os.walk(root, followlinks=False):
        for name in names:
            path = Path(directory) / name
            if stat.S_ISREG(path.lstat().st_mode) and text.encode() in path.read_bytes():
                found.append(str(path.relative_to(root)))
    return found


@pytest.mark.parametrize("plant", LINK_SHAPES)
async def test_a_failed_install_never_writes_the_link_targets_bytes_into_the_tree(
    tmp_path: Path, plant: Callable[[Path, Path], None]
) -> None:
    """The install that fails puts the spec back as it read it: had it read
    through the link, the outside file's bytes would land in the tree as the
    restored ``pyproject.toml``."""
    ws, env_root = workspace(tmp_path, plant)
    runner = FailingRunner()
    reg = registry(ws, env_root, runner)
    with pytest.raises(EnvError):
        await reg.install(DEFAULT, ["tinypkg"], str(ws / "n.alknb.py"))
    assert files_holding(ws, SECRET) == []
    assert files_holding(env_root, SECRET) == []
    assert runner.ran == []


@pytest.mark.parametrize("plant", LINK_SHAPES)
async def test_a_build_never_records_the_link_targets_bytes(
    tmp_path: Path, plant: Callable[[Path, Path], None]
) -> None:
    """A build records the spec it was made from under the env root, which
    the workspace's cells can read too."""
    ws, env_root = workspace(tmp_path, plant)
    runner = Runner()
    reg = registry(ws, env_root, runner)
    with pytest.raises(EnvError, match="not followed"):
        await reg.materialize(DEFAULT)
    assert files_holding(env_root, SECRET) == []
    assert files_holding(ws, SECRET) == []
    assert runner.ran == []


async def test_requirements_refuse_a_linked_spec_file(tmp_path: Path) -> None:
    """The requirements a linked spec names (the outside file's) are never
    reported. A linked spec directory is refused earlier, as an environment
    outside the workspace."""
    ws, env_root = workspace(tmp_path, link_the_file)
    reg = registry(ws, env_root, Runner())
    with pytest.raises(EnvError, match="not followed"):
        await reg.requirements(DEFAULT)


@pytest.mark.parametrize("plant", LINK_SHAPES)
def test_detection_reads_a_linked_spec_as_absent(
    tmp_path: Path, plant: Callable[[Path, Path], None]
) -> None:
    """Detection describes the default environment as having no spec: its
    hash is the empty spec's, never the outside file's."""
    ws, env_root = workspace(tmp_path, plant)
    linked = Detector(ws, env_root).default()
    (tmp_path / "empty").mkdir()
    empty = Detector(tmp_path / "empty", env_root).default()
    assert linked.spec_hash == empty.spec_hash


def test_a_linked_project_file_is_no_project(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "pyproject.toml").symlink_to(outside_file(tmp_path))
    nb = ws / "n.alknb.py"
    nb.write_text("x = 1\n", encoding="utf-8")
    kinds = [d.kind for d in detect_envs(ws, nb, env_root=tmp_path / "ws-envs")]
    assert "uv_project" not in kinds


@pytest.fixture
def fifo_spec(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """A workspace whose default spec has a ``pyproject.toml`` and a fifo for
    its ``uv.lock``. A reader still blocked on the fifo is released at the
    end by opening its write end."""
    ws = tmp_path / "ws"
    spec_root = ws / ".alkera" / "envs" / "default"
    spec_root.mkdir(parents=True)
    (spec_root / "pyproject.toml").write_text(
        '[project]\nname = "d"\nversion = "0"\n', encoding="utf-8"
    )
    fifo = spec_root / "uv.lock"
    os.mkfifo(fifo)
    yield ws, tmp_path / "ws-envs"
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    os.close(fd)


def finishes(work: Callable[[], object], timeout_s: float = 5.0) -> bool:
    done = threading.Event()

    def target() -> None:
        try:
            work()
        finally:
            done.set()

    threading.Thread(target=target, daemon=True).start()
    return done.wait(timeout_s)


def test_detection_does_not_hang_on_a_fifo_spec(fifo_spec: tuple[Path, Path]) -> None:
    ws, env_root = fifo_spec
    assert finishes(lambda: Detector(ws, env_root).default())


def test_a_build_refuses_a_fifo_spec_without_hanging(fifo_spec: tuple[Path, Path]) -> None:
    ws, env_root = fifo_spec
    reg = registry(ws, env_root, Runner())
    raised: list[BaseException] = []

    def build() -> None:
        try:
            asyncio.run(reg.materialize(DEFAULT))
        except BaseException as exc:
            raised.append(exc)

    assert finishes(build)
    assert len(raised) == 1 and isinstance(raised[0], EnvError)
    assert "not a file" in str(raised[0])
