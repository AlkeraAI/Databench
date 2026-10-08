"""The ``MaterializationTarget`` seam, driven over two stub targets.

Future callers each stub stands for:

* ``PosixTarget`` — ``alkera files pull`` and ``alkera files mount`` on a
  desktop (Windows/macOS/Linux) and a FUSE mount's write-through path: a real
  directory that gets real bytes, real pointer files and real symlinks.
* ``RunnerTarget`` — a CI runner and a RunPod box's stage-in: no filesystem of
  its own during planning, so it records what it was asked for and refuses the
  symlink kinds it cannot represent.

The materializer below is the tiny in-test consumer both are driven through: it
walks one fixture tree once and calls only the protocol, which is the claim —
a new host is a new target, not a new branch in ``pull``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from alkera_core.files.sync.target import (
    SYMLINK_KINDS,
    MaterializationTarget,
    SymlinkKind,
    TargetAttrs,
)

pytestmark = pytest.mark.asyncio


@dataclass(frozen=True, slots=True)
class Attrs:
    mode: int | None = None
    mtime_ns: int | None = None


@dataclass(frozen=True, slots=True)
class Entry:
    """One node of the fixture tree, in the shape a materializer sees."""

    path: str
    what: str
    payload: bytes = b""
    symlink_kind: SymlinkKind = "internal"
    symlink_target: str = ""
    attrs: Attrs = Attrs()


#: The fixture tree. It carries all three symlink kinds and both content
#: shapes on purpose: a target that only ever sees plain files proves nothing.
TREE: tuple[Entry, ...] = (
    Entry(
        "papers/report.txt",
        "bytes",
        payload=b"the bytes a host must get" * 8,
        attrs=Attrs(mode=0o640, mtime_ns=1_000_000_000_000_000_000),
    ),
    Entry("papers/weekly.alkerachat", "pointer", payload=b'{"kind":"chat"}\n'),
    Entry("papers/inside", "symlink", symlink_kind="internal", symlink_target="report.txt"),
    Entry("papers/outside", "symlink", symlink_kind="external", symlink_target="/etc/hosts"),
    Entry("papers/broken", "symlink", symlink_kind="dangling", symlink_target="nowhere"),
)


async def one_chunk(data: bytes) -> AsyncIterator[bytes]:
    """Content as a stream, because the seam never takes a whole body."""
    for start in range(0, len(data), 16):
        yield data[start : start + 16]


class UnsupportedSymlinkError(Exception):
    """A target refused a symlink kind it cannot represent."""


# ---- the two stub targets --------------------------------------------------


class PosixTarget:
    """A real directory. Stands for a desktop pull and a FUSE write-through."""

    def __init__(self, root: Path) -> None:
        self.root = str(root)

    def _abs(self, path: str) -> Path:
        full = Path(self.root) / path
        full.parent.mkdir(parents=True, exist_ok=True)
        return full

    async def write_bytes(
        self, path: str, stream: AsyncIterator[bytes], attrs: TargetAttrs
    ) -> None:
        full = self._abs(path)
        # Atomic from a reader's view: the name never holds a partial body.
        temp = full.with_name(full.name + ".partial")
        with temp.open("wb") as handle:
            async for chunk in stream:
                handle.write(chunk)
        temp.replace(full)
        await self.set_attrs(path, attrs)

    async def write_pointer(self, path: str, pointer: bytes) -> None:
        self._abs(path).write_bytes(pointer)

    async def write_symlink(self, path: str, kind: SymlinkKind, target: str) -> None:
        full = self._abs(path)
        if full.is_symlink():
            full.unlink()
        # A dangling link is a link, not an error: the node model stores the
        # kind verbatim and a host that resolved it would lose information.
        full.symlink_to(target)

    async def set_attrs(self, path: str, attrs: TargetAttrs) -> None:
        full = Path(self.root) / path
        if attrs.mode is not None:
            full.chmod(attrs.mode)
        if attrs.mtime_ns is not None:
            os.utime(full, ns=(attrs.mtime_ns, attrs.mtime_ns))


@dataclass
class RunnerTarget:
    """A CI runner / box stage-in plan: records, writes nothing, and refuses
    the symlink kinds that cannot leave the workspace."""

    root: str = "workspace://run-1"
    written: dict[str, bytes] = field(default_factory=dict)
    pointers: dict[str, bytes] = field(default_factory=dict)
    links: dict[str, tuple[str, str]] = field(default_factory=dict)
    applied: dict[str, Attrs] = field(default_factory=dict)
    refused: list[str] = field(default_factory=list)

    async def write_bytes(
        self, path: str, stream: AsyncIterator[bytes], attrs: TargetAttrs
    ) -> None:
        self.written[path] = b"".join([chunk async for chunk in stream])
        await self.set_attrs(path, attrs)

    async def write_pointer(self, path: str, pointer: bytes) -> None:
        self.pointers[path] = pointer

    async def write_symlink(self, path: str, kind: SymlinkKind, target: str) -> None:
        if kind == "external":
            self.refused.append(path)
            raise UnsupportedSymlinkError(path)
        self.links[path] = (kind, target)

    async def set_attrs(self, path: str, attrs: TargetAttrs) -> None:
        # A runner keeps mode and drops mtime: a build must not depend on it.
        self.applied[path] = Attrs(mode=attrs.mode, mtime_ns=None)


# ---- the in-test materializer every target is driven through ---------------


async def materialize(target: MaterializationTarget, tree: Sequence[Entry]) -> list[str]:
    """Walk the tree once through the protocol. Returns the paths it skipped."""
    skipped: list[str] = []
    for entry in tree:
        if entry.what == "bytes":
            await target.write_bytes(entry.path, one_chunk(entry.payload), entry.attrs)
        elif entry.what == "pointer":
            await target.write_pointer(entry.path, entry.payload)
        else:
            try:
                await target.write_symlink(entry.path, entry.symlink_kind, entry.symlink_target)
            except UnsupportedSymlinkError:
                skipped.append(entry.path)
    return skipped


def _partials(root: Path) -> list[str]:
    """Every leftover temp name under ``root``, as a plain walk."""
    found: list[str] = []
    for _folder, _dirs, names in os.walk(root):
        found.extend(name for name in names if name.endswith(".partial"))
    return sorted(found)


# ---- the seam's claims -----------------------------------------------------


async def test_both_stub_targets_satisfy_the_protocol_at_runtime(tmp_path: Path) -> None:
    """Registration alone: a target is one only if it has all five members."""
    assert isinstance(PosixTarget(tmp_path), MaterializationTarget)
    assert isinstance(RunnerTarget(), MaterializationTarget)


class _MissingSymlink:
    root = "x://"

    async def write_bytes(
        self, path: str, stream: AsyncIterator[bytes], attrs: TargetAttrs
    ) -> None:
        return None

    async def write_pointer(self, path: str, pointer: bytes) -> None:
        return None

    async def set_attrs(self, path: str, attrs: TargetAttrs) -> None:
        return None


async def test_a_target_missing_a_write_is_not_a_materialization_target() -> None:
    """The negative twin: the protocol is a real gate, not a label."""
    assert not isinstance(_MissingSymlink(), MaterializationTarget)


async def test_the_posix_target_lands_bytes_pointer_and_all_three_symlink_kinds(
    tmp_path: Path,
) -> None:
    target = PosixTarget(tmp_path / "pull")
    skipped = await materialize(target, TREE)

    root = Path(target.root)
    assert skipped == []
    assert (root / "papers/report.txt").read_bytes() == TREE[0].payload
    assert (root / "papers/weekly.alkerachat").read_bytes() == TREE[1].payload
    for entry in TREE[2:]:
        link = root / entry.path
        assert link.is_symlink()
        assert os.readlink(link) == entry.symlink_target
    # The dangling one is still dangling — the host did not resolve it away.
    assert not (root / "papers/broken").exists()
    # No temp file survived the atomic rename.
    assert _partials(root) == []


async def test_the_posix_target_restores_the_mtime(tmp_path: Path) -> None:
    target = PosixTarget(tmp_path / "pull")
    await materialize(target, TREE)

    stat = (Path(target.root) / "papers/report.txt").stat()
    assert stat.st_mtime_ns == TREE[0].attrs.mtime_ns


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="NTFS carries no POSIX permission bits: chmod there moves only the "
    "read-only flag, so st_mode & 0o777 can never read back 0o640",
)
async def test_the_posix_target_restores_the_mode(tmp_path: Path) -> None:
    target = PosixTarget(tmp_path / "pull")
    await materialize(target, TREE)

    stat = (Path(target.root) / "papers/report.txt").stat()
    assert stat.st_mode & 0o777 == 0o640


async def test_the_runner_target_records_the_same_walk_and_refuses_only_external() -> None:
    """The same materializer, a host with no filesystem: the plan is complete
    and the one kind it cannot represent is refused by name, not flattened."""
    target = RunnerTarget()
    skipped = await materialize(target, TREE)

    assert skipped == ["papers/outside"]
    assert target.refused == ["papers/outside"]
    assert target.written == {"papers/report.txt": TREE[0].payload}
    assert target.pointers == {"papers/weekly.alkerachat": TREE[1].payload}
    assert target.links == {
        "papers/inside": ("internal", "report.txt"),
        "papers/broken": ("dangling", "nowhere"),
    }
    # A refused symlink is not a silent copy: nothing landed under that path.
    assert "papers/outside" not in target.written
    assert "papers/outside" not in target.links


async def test_a_target_may_drop_an_attr_it_cannot_apply_without_a_flag() -> None:
    """``set_attrs`` is its own method precisely so this differs per target."""
    target = RunnerTarget()
    await materialize(target, TREE)

    assert target.applied["papers/report.txt"] == Attrs(mode=0o640, mtime_ns=None)


@pytest.mark.parametrize("kind", SYMLINK_KINDS, ids=SYMLINK_KINDS)
async def test_every_symlink_kind_reaches_the_seam(kind: SymlinkKind, tmp_path: Path) -> None:
    """A kind added to the model without a target case fails here."""
    target = PosixTarget(tmp_path / kind)
    await target.write_symlink("link", kind, "elsewhere")
    assert os.readlink(Path(target.root) / "link") == "elsewhere"
