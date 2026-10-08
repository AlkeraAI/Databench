"""AtomicWriter's seams, ordering, failure behaviour and platform barrier."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from alkera_core.files.sync import AtomicWriter, atomic, write_bytes_atomic
from alkera_core.files.sync.atomic import CHECKPOINTS

KIB = 1024


class _Recorder:
    """Records the durability steps in the order the writer performs them."""

    def __init__(self) -> None:
        self.trace: list[str] = []
        self.renames: list[tuple[Path, Path]] = []

    def fsync_file(self, fd: int) -> None:
        self.trace.append("fsync_file")

    def replace(self, source: Path, target: Path) -> None:
        self.trace.append("replace")
        self.renames.append((Path(source), Path(target)))
        Path(source).replace(target)

    def fsync_dir(self, directory: Path) -> None:
        self.trace.append(f"fsync_dir:{Path(directory).name}")

    def checkpoint(self, name: str) -> None:
        self.trace.append(name)

    def seams(self) -> dict[str, Any]:
        return {
            "fsync_file": self.fsync_file,
            "replace": self.replace,
            "fsync_dir": self.fsync_dir,
            "on_checkpoint": self.checkpoint,
        }


def test_durability_steps_run_in_the_write_fsync_rename_fsyncdir_order(tmp_path: Path) -> None:
    recorder = _Recorder()
    target = tmp_path / "data.bin"

    write_bytes_atomic(target, b"payload", **recorder.seams())

    assert recorder.trace == [
        "atomic.after_write",
        "fsync_file",
        "atomic.after_fsync_file",
        "replace",
        "atomic.after_rename",
        f"fsync_dir:{tmp_path.name}",
        "atomic.after_fsync_dir",
    ]
    assert target.read_bytes() == b"payload"


def test_every_checkpoint_constant_is_emitted_exactly_once(tmp_path: Path) -> None:
    recorder = _Recorder()

    write_bytes_atomic(tmp_path / "data.bin", b"x", **recorder.seams())

    emitted = [step for step in recorder.trace if step.startswith("atomic.")]
    assert emitted == list(CHECKPOINTS)


def test_the_temp_file_is_a_sibling_of_the_target_and_is_renamed_onto_it(
    tmp_path: Path,
) -> None:
    recorder = _Recorder()
    target = tmp_path / "data.bin"

    write_bytes_atomic(target, b"payload", **recorder.seams())

    (source, renamed_to) = recorder.renames[0]
    assert renamed_to == target
    assert source.parent == target.parent, "the temp file must share the target's directory"
    assert source.name.startswith(".data.bin.") and source.name.endswith(".tmp")


def test_an_existing_target_is_replaced_rather_than_modified_in_place(tmp_path: Path) -> None:
    target = tmp_path / "data.bin"
    write_bytes_atomic(target, b"A" * 4096)
    inode_before = target.stat().st_ino

    write_bytes_atomic(target, b"B" * 4096)

    assert target.read_bytes() == b"B" * 4096
    assert target.stat().st_ino != inode_before, "the old file was written in place"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"\x00", id="one-byte"),
        pytest.param(b"z" * (64 * KIB - 1), id="64KiB-1"),
        pytest.param(b"z" * (64 * KIB), id="64KiB"),
        pytest.param(b"z" * (64 * KIB + 1), id="64KiB+1"),
        pytest.param(bytes(range(256)) * 4096, id="1MiB-binary"),
    ],
)
def test_write_bytes_atomic_round_trips_the_payload(tmp_path: Path, payload: bytes) -> None:
    target = tmp_path / "data.bin"

    write_bytes_atomic(target, payload)

    assert target.read_bytes() == payload


def test_an_exception_in_the_block_leaves_an_existing_target_byte_identical(
    tmp_path: Path,
) -> None:
    target = tmp_path / "data.bin"
    write_bytes_atomic(target, b"A" * 4096)
    inode_before = target.stat().st_ino

    with pytest.raises(RuntimeError, match="boom"), AtomicWriter(target) as handle:
        handle.write(b"B" * 4096)
        raise RuntimeError("boom")

    assert target.read_bytes() == b"A" * 4096
    assert target.stat().st_ino == inode_before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["data.bin"]


def test_an_exception_in_the_block_leaves_an_absent_target_absent(tmp_path: Path) -> None:
    target = tmp_path / "data.bin"

    with pytest.raises(ValueError, match="boom"), AtomicWriter(target) as handle:
        handle.write(b"partial")
        raise ValueError("boom")

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_failing_rename_seam_leaves_no_temp_file_and_no_target(tmp_path: Path) -> None:
    target = tmp_path / "data.bin"

    def refuse(source: Path, destination: Path) -> None:
        raise OSError("rename refused")

    with (
        pytest.raises(OSError, match="rename refused"),
        AtomicWriter(target, replace=refuse) as handle,
    ):
        handle.write(b"payload")

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="F_FULLFSYNC is a macOS barrier")
def test_darwin_issues_f_fullfsync_before_the_injected_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    commands: list[int] = []
    real_fcntl = fcntl.fcntl

    def recording_fcntl(fd: Any, cmd: int, *args: Any) -> Any:
        commands.append(cmd)
        return real_fcntl(fd, cmd, *args)

    monkeypatch.setattr(fcntl, "fcntl", recording_fcntl)
    recorder = _Recorder()
    target = tmp_path / "data.bin"

    write_bytes_atomic(target, b"payload", **recorder.seams())

    assert fcntl.F_FULLFSYNC in commands
    assert recorder.trace.index("fsync_file") < recorder.trace.index("atomic.after_fsync_file")
    assert target.read_bytes() == b"payload"


@pytest.mark.skipif(sys.platform != "darwin", reason="F_FULLFSYNC is a macOS barrier")
def test_without_f_fullfsync_the_write_still_completes_through_the_injected_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    monkeypatch.delattr(fcntl, "F_FULLFSYNC", raising=False)
    recorder = _Recorder()
    target = tmp_path / "data.bin"

    write_bytes_atomic(target, b"payload", **recorder.seams())

    assert "fsync_file" in recorder.trace
    assert target.read_bytes() == b"payload"


def test_the_default_directory_fsync_runs_against_a_real_directory(tmp_path: Path) -> None:
    nested = tmp_path / "sub"
    nested.mkdir()
    target = nested / "data.bin"

    write_bytes_atomic(target, b"payload")

    assert target.read_bytes() == b"payload"


def test_the_default_directory_fsync_is_a_no_op_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows has no directory file descriptor to flush.

    ``os.open`` on a directory fails there outright, so a writer that tried
    would raise where a POSIX one durably committed. The module's ``os`` name is
    swapped for one that refuses every call, which is the strongest statement of
    "it did not go near the filesystem" available from another platform.
    """

    class _Refusing:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the Windows branch reached os.{name}")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(atomic, "os", _Refusing())

    assert atomic.default_fsync_dir(tmp_path) is None


def test_a_windows_write_completes_with_no_fcntl_in_the_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``import fcntl`` never runs on Windows, so no path may reach the name.

    The module-level import is already guarded, and this is what holds it there:
    deleting the name reproduces the only namespace a Windows interpreter ever
    has, so a barrier that stopped checking the platform first — or any new
    caller of ``fcntl`` — fails here with ``NameError`` instead of on a runner.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delattr(atomic, "fcntl", raising=False)
    recorder = _Recorder()
    target = tmp_path / "data.bin"

    write_bytes_atomic(target, b"payload", **recorder.seams())

    assert target.read_bytes() == b"payload"
    assert recorder.trace == [
        "atomic.after_write",
        "fsync_file",
        "atomic.after_fsync_file",
        "replace",
        "atomic.after_rename",
        f"fsync_dir:{tmp_path.name}",
        "atomic.after_fsync_dir",
    ]


@pytest.mark.parametrize(
    ("platform", "expects_barrier"),
    [
        pytest.param("darwin", True, id="darwin-issues-the-barrier"),
        pytest.param("win32", False, id="windows-has-no-fcntl"),
        pytest.param("linux", False, id="linux-fsync-is-already-the-barrier"),
    ],
)
def test_only_darwin_reaches_fcntl_for_the_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    expects_barrier: bool,
) -> None:
    """One table for the whole branch: exactly one platform touches ``fcntl``."""
    reached: list[int] = []

    class _Fcntl:
        F_FULLFSYNC = 51

        @staticmethod
        def fcntl(fd: int, cmd: int, *args: Any) -> int:
            reached.append(cmd)
            return 0

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(atomic, "fcntl", _Fcntl, raising=False)
    recorder = _Recorder()
    target = tmp_path / "data.bin"

    write_bytes_atomic(target, b"payload", **recorder.seams())

    assert reached == ([_Fcntl.F_FULLFSYNC] if expects_barrier else [])
    assert target.read_bytes() == b"payload"
