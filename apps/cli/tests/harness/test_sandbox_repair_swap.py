"""Root's repair of a chat's trees is never redirected out of them.

Before every spawn and every command, root gives the chat's trees their owner
and modes while the agent, and every other process of the workspace, keep
running in them. A name the walk has judged a directory can be swapped for a
link to a host tree before root changes it. A change made by path (``find``
handing names to ``chmod``) follows the link and re-modes the host tree; a walk
that opens each directory refusing a link and changes each entry through its
own descriptor does not.

Each case swaps ``sub`` for a link to a tree outside, at the last moment
before root changes it, by whichever means root changes it: a ``chmod`` program
found on ``PATH`` gets the swap before it runs, and so does an open of the name
``sub``. The steps are the launch's own (:func:`uid_steps`), run by the
launch's runner. Handing a tree to the chat's uid is root's alone and the
suite runs as a person, so the owner change is a no-op at both of its seams
(a ``chown`` program, ``os.fchown``): what is asserted is the modes.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness import sandbox as sb

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX links and modes")

#: The swap, at a ``chmod`` about to change ``sub`` or a name below it.
SWAP = (
    'case " $* " in *" $SUB "*|*" $SUB/"*) '
    'if [ -d "$SUB" ] && [ ! -L "$SUB" ]; then mv "$SUB" "$SUB.away" && ln -s "$AWAY" "$SUB"; fi;; '
    "esac"
)


def _swap(sub: Path, away: Path) -> None:
    if sub.is_dir() and not sub.is_symlink():
        sub.rename(sub.with_name(sub.name + ".away"))
        sub.symlink_to(away)


def _no_owner_change(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Owner changes are no-ops at both seams; the directory of program shims,
    put first on ``PATH``."""
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    (shims / "chown").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (shims / "chown").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(os, "fchown", lambda *_: None)
    return shims


def _arm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sub: Path, away: Path) -> None:
    """Swap ``sub`` for a link to ``away`` right before root changes it."""
    real = shutil.which("chmod")
    assert real is not None
    shims = _no_owner_change(monkeypatch, tmp_path)
    shim = shims / "chmod"
    shim.write_text(f'#!/bin/sh\n{SWAP}\nexec "{real}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setenv("SUB", str(sub))
    monkeypatch.setenv("AWAY", str(away))
    opener: Callable[..., int] = os.open

    def open_after_swap(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if os.fsdecode(path) == sub.name and kwargs.get("dir_fd") is not None:
            _swap(sub, away)
        return opener(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_after_swap)


def _outside(tmp_path: Path) -> tuple[Path, dict[str, int]]:
    """A host tree with modes no repair would leave: what a redirect changes."""
    away = tmp_path / "host"
    away.mkdir()
    (away / "f").write_text("host", encoding="utf-8")
    os.chmod(away / "f", 0o600)
    os.chmod(away, 0o700)
    return away, _modes(away)


def _modes(tree: Path) -> dict[str, int]:
    return {
        "dir": stat.S_IMODE(tree.lstat().st_mode),
        "file": stat.S_IMODE((tree / "f").lstat().st_mode),
    }


def _tree(root: Path) -> Path:
    """A tree with a directory the agent will swap, and a file below it."""
    sub = root / "sub"
    sub.mkdir(parents=True)
    (sub / "f").write_text("chat", encoding="utf-8")
    os.chmod(sub / "f", 0o600)
    return sub


def _run(spec: sb.SandboxSpec) -> None:
    """The launch's ownership steps, but for the ancestors' ACLs (no
    ``setfacl`` on every host the suite runs on, and above the trees)."""
    steps = [
        s
        for s in sb.uid_steps(spec)
        if not (isinstance(s, sb.ShellStep) and s.argv[0] == "setfacl")
    ]
    sb.run_steps(steps)


def _spec(tmp_path: Path, **overrides: Any) -> sb.SandboxSpec:
    base: dict[str, Any] = {
        "chat_id": "chat_0123456789abcdef",
        "folder": tmp_path / "chat" / "folder",
        "uid": 20017,
        "vcpu": 1,
        "memory_mb": 512,
        "home": "/home/alkera",
        "mode": "none",
        "cgroup": "none",
    }
    base.update(overrides)
    return sb.SandboxSpec(**base)


@posix
def test_a_shared_tree_put_right_is_not_redirected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workspace folder on its first repair (its root not yet the
    workspace's), walked whole."""
    spec = _spec(tmp_path)
    sub = _tree(spec.folder)
    away, before = _outside(tmp_path)
    _arm(monkeypatch, tmp_path, sub, away)
    _run(spec)
    assert _modes(away) == before


@posix
def test_an_owned_tree_walked_every_launch_is_not_redirected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chat's own runtime state, walked before every spawn and command."""
    state = tmp_path / "chat" / ".runtime" / "agent"
    spec = _spec(tmp_path, binds=(sb.Bind(state, "/opt/alkera/agent"),))
    spec.folder.mkdir(parents=True)
    sub = _tree(state)
    away, before = _outside(tmp_path)
    _arm(monkeypatch, tmp_path, sub, away)
    _run(spec)
    assert _modes(away) == before


@posix
def test_a_members_setgid_pass_is_not_redirected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace member on a ``none`` box: every directory of the shared
    folder is made setgid on every launch, and its files are left as they are."""
    spec = _spec(tmp_path, share_gid=20017)
    setgid = [s for s in sb.uid_steps(spec) if isinstance(s, sb.RepairStep) and not s.files]
    assert setgid == [sb.RepairStep(spec.folder, None, files=False)]
    sub = _tree(spec.folder)
    os.chmod(sub, 0o700)
    away, before = _outside(tmp_path)
    _arm(monkeypatch, tmp_path, sub, away)
    sb.run_steps(setgid)
    assert _modes(away) == before
    assert stat.S_IMODE(spec.folder.lstat().st_mode) == 0o2770


@posix
def test_a_members_setgid_pass_leaves_the_files(tmp_path: Path) -> None:
    spec = _spec(tmp_path, share_gid=20017)
    sub = _tree(spec.folder)
    os.chmod(sub, 0o700)
    sb.run_steps([s for s in sb.uid_steps(spec) if isinstance(s, sb.RepairStep) and not s.files])
    assert stat.S_IMODE(sub.lstat().st_mode) == 0o2770
    assert stat.S_IMODE((sub / "f").lstat().st_mode) == 0o600


@posix
def test_the_repair_still_puts_the_tree_right(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: with nothing swapped the tree gets its modes, an
    executable keeps its bit for user and group, and a link is left as it is,
    its target untouched."""
    _no_owner_change(monkeypatch, tmp_path)
    spec = _spec(tmp_path)
    sub = _tree(spec.folder)
    tool = spec.folder / "run.sh"
    tool.write_text("x", encoding="utf-8")
    os.chmod(tool, 0o700)
    away, before = _outside(tmp_path)
    (spec.folder / "link").symlink_to(away)
    _run(spec)
    assert _modes(away) == before
    got = {p.name: stat.S_IMODE(p.lstat().st_mode) for p in (spec.folder, sub, sub / "f", tool)}
    assert got == {"folder": 0o2770, "sub": 0o2770, "f": 0o660, "run.sh": 0o770}
