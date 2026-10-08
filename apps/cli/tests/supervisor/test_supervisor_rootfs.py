"""Each org sees the staged rootfs owned as its own root: id-mapped where the
host can, a shifted copy where it cannot, outside the org's writable root."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from alkera_cli.org_root import ensure_org_root
from alkera_cli.supervisor.org_rootfs import (
    ROOTFS_STAMP,
    RootfsError,
    add_missing_dirs,
    ensure_org_rootfs,
    org_rootfs_path,
    shifted_copy,
)
from alkera_cli.supervisor.slots import ORG_UID_SPAN, SlotTable
from alkera_core.compute.box_isolation import IsolationMechanism, IsolationReport

#: A box whose probe proved every mechanism but systemd: namespaced workers,
#: spawned directly.
NAMESPACED = IsolationReport(frozenset(IsolationMechanism) - {IsolationMechanism.SYSTEMD})


ORG = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"


def _slot(tmp_path: Path, index_of: str = ORG):  # type: ignore[no-untyped-def]
    return SlotTable(tmp_path / "slots.json").assign(index_of)


def _staged(tmp_path: Path, build: str = "build-1") -> Path:
    root = tmp_path / "rootfs" / build
    (root / "var" / "lib" / "dpkg").mkdir(parents=True)
    (root / "var" / "lib" / "dpkg" / "lock").write_text("")
    (root / "bin").mkdir()
    (root / "bin" / "sh").write_text("#!")
    (root / "bin" / "sh").chmod(0o4755)
    (root / "usr").mkdir()
    (root / "usr" / "lib").symlink_to("../lib-elsewhere")
    (root / ROOTFS_STAMP).write_text(build)
    current = tmp_path / "rootfs" / "current"
    if current.is_symlink():
        current.unlink()
    current.symlink_to(build)
    return current


def test_an_orgs_rootfs_lies_outside_its_writable_root(tmp_path: Path) -> None:
    """The supervisor never opens a path beneath an org root, where the
    worker could have left a link to anywhere."""
    slot = _slot(tmp_path)
    orgs = tmp_path / "orgs"
    me = (os.getuid(), os.getgid())
    root = ensure_org_root(orgs, slot, root_owner=me, owner=me)
    path = org_rootfs_path(orgs, slot)
    assert root.path not in (path, *path.parents)
    assert orgs not in path.parents


def test_the_rootfs_is_id_mapped_where_the_host_can(tmp_path: Path) -> None:
    slot = _slot(tmp_path)
    source = _staged(tmp_path)
    target = tmp_path / "org-rootfs" / "0"
    bound: list[tuple[Path, Path, int]] = []
    copied: list[Path] = []

    outcome = ensure_org_rootfs(
        source,
        target,
        slot,
        is_mount=lambda _p: False,
        bind=lambda src, dst, s: bound.append((src, dst, s.uid_base)),
        copy=lambda src, dst, s: copied.append(dst),
    )

    assert outcome == "idmapped"
    assert bound == [(source.resolve(), target, slot.uid_base)]
    assert copied == []
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o711


def test_where_the_host_cannot_id_map_the_org_gets_a_copy(tmp_path: Path) -> None:
    slot = _slot(tmp_path)
    source = _staged(tmp_path)
    target = tmp_path / "org-rootfs" / "0"

    def refuse(src: Path, dst: Path, s: object) -> None:
        raise OSError(22, "Invalid argument")

    chowned: dict[str, tuple[int, int]] = {}

    def copy(src: Path, dst: Path, s):  # type: ignore[no-untyped-def]
        shifted_copy(src, dst, s, chown=lambda p, u, g: chowned.__setitem__(p, (u, g)))

    outcome = ensure_org_rootfs(
        source, target, slot, is_mount=lambda _p: False, bind=refuse, copy=copy
    )

    assert outcome == "copied"
    assert (target / ROOTFS_STAMP).read_text() == "build-1"
    assert (target / "var" / "lib" / "dpkg" / "lock").is_file()
    assert (target / "usr" / "lib").is_symlink()
    assert os.readlink(target / "usr" / "lib") == "../lib-elsewhere"
    # Every id shifted into the slot's range; nothing left at the host's.
    uid, gid = (i if i < ORG_UID_SPAN else ORG_UID_SPAN - 1 for i in (os.getuid(), os.getgid()))
    assert set(chowned.values()) == {(slot.uid_base + uid, slot.uid_base + gid)}
    staged = target.with_name(".0.copying")  # copied there whole, then renamed into place
    assert str(staged) in chowned and str(staged / "var" / "lib" / "dpkg" / "lock") in chowned
    # The set-id bit a chown would clear is kept.
    assert stat.S_IMODE((target / "bin" / "sh").stat().st_mode) == 0o4755
    assert not target.with_name(".0.copying").exists()


def test_a_rootfs_of_the_staged_build_is_kept(tmp_path: Path) -> None:
    slot = _slot(tmp_path)
    source = _staged(tmp_path)
    target = tmp_path / "org-rootfs" / "0"
    target.mkdir(parents=True)
    (target / ROOTFS_STAMP).write_text("build-1")

    def never(*_args: object) -> None:
        raise AssertionError("a current rootfs was made again")

    assert (
        ensure_org_rootfs(
            source, target, slot, is_mount=lambda p: p == target, bind=never, copy=never
        )
        == "present"
    )


def test_a_rootfs_of_an_earlier_build_is_replaced(tmp_path: Path) -> None:
    slot = _slot(tmp_path)
    _staged(tmp_path, "build-1")
    source = _staged(tmp_path, "build-2")
    target = tmp_path / "org-rootfs" / "0"
    target.mkdir(parents=True)
    (target / ROOTFS_STAMP).write_text("build-1")
    detached: list[Path] = []
    bound: list[Path] = []

    def detach(path: Path) -> None:
        detached.append(path)
        (path / ROOTFS_STAMP).unlink()

    outcome = ensure_org_rootfs(
        source,
        target,
        slot,
        is_mount=lambda p: p == target and (p / ROOTFS_STAMP).exists(),
        bind=lambda src, dst, s: bound.append(src),
        detach=detach,
    )
    assert outcome == "idmapped"
    assert detached == [target]
    assert bound == [source.resolve()]


def test_a_box_with_nothing_staged_gives_no_rootfs(tmp_path: Path) -> None:
    with pytest.raises(RootfsError, match="no staged rootfs"):
        ensure_org_rootfs(tmp_path / "missing", tmp_path / "t", _slot(tmp_path))


def test_an_id_past_the_span_maps_to_the_orgs_nobody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = _slot(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    (source / "f").write_text("x")
    seen: list[tuple[int, int]] = []
    real_lstat = os.lstat

    class Far:
        def __init__(self, info: os.stat_result) -> None:
            self._info = info

        def __getattr__(self, name: str) -> object:
            if name in ("st_uid", "st_gid"):
                return 10_000_000
            return getattr(self._info, name)

    monkeypatch.setattr(os, "lstat", lambda p: Far(real_lstat(p)))
    shifted_copy(source, tmp_path / "dst", slot, chown=lambda p, u, g: seen.append((u, g)))
    nobody = slot.uid_base + ORG_UID_SPAN - 1
    assert set(seen) == {(nobody, nobody)}


@pytest.mark.parametrize(
    ("mode", "outcome", "given"),
    [
        pytest.param("gvisor", "idmapped", True, id="gvisor-box"),
        pytest.param("none", "idmapped", False, id="no-gvisor-no-rootfs"),
        pytest.param("gvisor", OSError(5, "io"), False, id="failure-falls-back"),
    ],
)
def test_a_gvisor_box_points_each_worker_at_its_own_rootfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, outcome: object, given: bool
) -> None:
    import alkera_cli.supervisor.service as service_module
    from alkera_cli.supervisor.service import FileRoutingFeed, Supervisor

    def ensure(source: Path, target: Path, slot: object) -> object:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(service_module, "ensure_org_rootfs", ensure)
    table = SlotTable(tmp_path / "slots.json")
    sup = Supervisor(
        api=None,  # type: ignore[arg-type]
        feed=FileRoutingFeed(tmp_path / "routing.json"),
        slots=table,
        orgs_root=tmp_path / "orgs",
        env={"ALKERA_SANDBOX_MODE": mode},
        isolation=NAMESPACED,
    )
    slot = table.assign(ORG)
    found = sup._org_rootfs(slot)
    assert found == (org_rootfs_path(tmp_path / "orgs", slot) if given else None)


def test_a_copy_of_the_same_build_gains_the_directories_the_box_added_since(
    tmp_path: Path,
) -> None:
    """The box adds the chats' mountpoints to the staged tree without changing
    its stamp; a copy kept by its stamp lacked them, and every bind a chat
    mounts there failed in that org."""
    slot = _slot(tmp_path)
    source = _staged(tmp_path)
    target = tmp_path / "org-rootfs" / "0"
    target.parent.mkdir()
    shifted_copy(source.resolve(), target, slot, chown=lambda p, u, g: None)
    (source / "opt" / "alkera" / "harness" / "state").mkdir(parents=True)
    (source / "opt" / "alkera" / "harness" / "state").chmod(0o755)
    (source / "bin" / "new-tool-dir").mkdir()
    chowned: dict[str, tuple[int, int]] = {}

    def never(*_args: object) -> None:
        raise AssertionError("a current rootfs was made again")

    def complete(src: Path, dst: Path, s):  # type: ignore[no-untyped-def]
        return add_missing_dirs(src, dst, s, chown=lambda p, u, g: chowned.__setitem__(p, (u, g)))

    outcome = ensure_org_rootfs(
        source, target, slot, is_mount=lambda _p: False, bind=never, copy=never, complete=complete
    )

    assert outcome == "present"
    added = target / "opt" / "alkera" / "harness" / "state"
    assert added.is_dir() and stat.S_IMODE(added.stat().st_mode) == 0o755
    assert (target / "bin" / "new-tool-dir").is_dir()
    uid = os.getuid() if os.getuid() < ORG_UID_SPAN else ORG_UID_SPAN - 1
    assert chowned[str(added)][0] == slot.uid_base + uid
    # What the copy had is left alone; a link in the tree is not followed.
    assert str(target / "bin") not in chowned
    assert not (target / "lib-elsewhere").exists()
