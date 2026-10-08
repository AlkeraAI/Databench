"""What the registry writes on a build's path follows the tree's shared modes.

On a box a build runs as the workspace's build uid, which shares the tree's
files group and nothing else, while the registry runs as the org worker under
an owner-only umask. A default spec seeded as ``0700``/``0600`` was a
directory the build could not even start in ("failed to find initial working
directory ... permission denied"). The runner here records, at the moment
each build command would start, whether a uid in the tree's group but not its
owner could enter its working directory and read and write the spec there.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from alkera_notebook.envs import EnvBuildError, EnvError, LocalEnvRegistry, TreeModes
from alkera_notebook.envs.models import CommandResult

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")

SHARED = TreeModes(directory=0o2770, file=0o660)
SPEC = {"pyproject.toml": '[project]\nname = "x"\nversion = "0"\n', "uv.lock": "version = 1\n"}
DEFAULT = "default:.alkera/envs/default"


def group_member_may(path: Path, want: int) -> bool:
    """Whether a uid other than ``path``'s owner, whose group is ``path``'s
    group, may do ``want`` (``os.R_OK`` / ``W_OK`` / ``X_OK`` bits) to it:
    the group bits decide, as they do for the build uid."""
    mode = path.lstat().st_mode
    granted = (mode >> 3) & 0o7
    return granted & want == want


@dataclass
class Seen:
    cwd: str
    enterable: bool
    readable: dict[str, bool] = field(default_factory=dict)
    writable: dict[str, bool] = field(default_factory=dict)


class GroupRunner:
    """Records what a build as a group member would meet, then fails the
    build (nothing here builds; the modes at the start are the subject)."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.seen: list[Seen] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> CommandResult:
        where = Path(cwd)
        chain = [where, *[p for p in where.parents if self.base in p.parents]]
        seen = Seen(cwd, all(group_member_may(d, os.X_OK) for d in chain))
        for name in SPEC:
            if (where / name).exists():
                seen.readable[name] = group_member_may(where / name, os.R_OK)
                seen.writable[name] = group_member_may(where / name, os.W_OK)
        self.seen.append(seen)
        return CommandResult(returncode=1, stdout="", stderr="stopped by the test")


@contextmanager
def owner_only_umask() -> Iterator[None]:
    """The org worker's umask: what it makes is its own alone."""
    old = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(old)


def _registry(
    tmp_path: Path, modes: TreeModes | None
) -> tuple[LocalEnvRegistry, GroupRunner, Path, Path]:
    ws = tmp_path / "ws"
    envs = tmp_path / "envs"
    template = tmp_path / "template"
    for d in (ws, envs, template):
        d.mkdir()
        os.chmod(d, 0o2770)
    for name, text in SPEC.items():
        (template / name).write_text(text)
    runner = GroupRunner(tmp_path)
    reg = LocalEnvRegistry(
        ws, envs, runner=runner, python=sys.executable, default_template=template, tree_modes=modes
    )
    return reg, runner, ws, envs


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


async def test_a_seeded_default_spec_is_the_group_s_to_build_in(tmp_path: Path) -> None:
    reg, runner, ws, _ = _registry(tmp_path, SHARED)
    with owner_only_umask(), pytest.raises(EnvBuildError):
        await reg.materialize(DEFAULT)
    (build,) = runner.seen
    assert build.cwd == str(ws / ".alkera" / "envs" / "default")
    assert build.enterable
    assert build.readable == {"pyproject.toml": True, "uv.lock": True}
    assert build.writable == {"pyproject.toml": True, "uv.lock": True}
    for d in (ws / ".alkera", ws / ".alkera" / "envs", ws / ".alkera" / "envs" / "default"):
        assert _mode(d) & 0o777 == 0o770, oct(_mode(d))
        assert _mode(d) & stat.S_ISGID, f"{d} lost the setgid bit"
    for name in SPEC:
        assert _mode(ws / ".alkera" / "envs" / "default" / name) == 0o660


async def test_without_tree_modes_the_umask_decides(tmp_path: Path) -> None:
    """A laptop's registry (one uid) keeps writing under its umask: the
    shared modes are the box's, not every registry's."""
    reg, runner, ws, _ = _registry(tmp_path, None)
    with owner_only_umask(), pytest.raises(EnvBuildError):
        await reg.materialize(DEFAULT)
    (build,) = runner.seen
    assert not build.enterable
    assert _mode(ws / ".alkera" / "envs" / "default" / "pyproject.toml") == 0o600


async def test_a_spec_an_earlier_build_left_owner_only_is_opened_to_the_group(
    tmp_path: Path,
) -> None:
    reg, runner, ws, _ = _registry(tmp_path, SHARED)
    spec = ws / ".alkera" / "envs" / "default"
    with owner_only_umask():
        spec.mkdir(parents=True)
        for name, text in SPEC.items():
            (spec / name).write_text(text)
    for d in (ws / ".alkera", ws / ".alkera" / "envs", spec):
        os.chmod(d, 0o2700)
    assert _mode(spec / "pyproject.toml") == 0o600
    with owner_only_umask(), pytest.raises(EnvBuildError):
        await reg.materialize(DEFAULT)
    (build,) = runner.seen
    assert build.enterable and all(build.readable.values()) and all(build.writable.values())


async def test_a_link_on_the_spec_s_way_is_refused_and_nothing_is_written_through_it(
    tmp_path: Path,
) -> None:
    """The tree is its people's and agents' to write: a link where the spec
    directory goes would have the registry make and re-mode a directory
    elsewhere as its own uid."""
    reg, runner, ws, _ = _registry(tmp_path, SHARED)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.chmod(elsewhere, 0o700)
    (ws / ".alkera" / "envs").mkdir(parents=True)
    (ws / ".alkera" / "envs" / "default").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(EnvError):
        await reg.materialize(DEFAULT)
    assert runner.seen == []
    assert list(elsewhere.iterdir()) == [] and _mode(elsewhere) == 0o700
