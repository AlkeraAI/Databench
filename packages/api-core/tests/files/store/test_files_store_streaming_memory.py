"""A 1 GiB object moves through the store without being held in memory.

The bound is only meaningful if it is measured in a process that did nothing
else, so the transfer runs in a subprocess and the child prints its own peaks
as its last lines. An in-process assertion would measure the test session's
high-water mark — including every fixture and import — and would pass even if
the driver buffered the whole body.

Two instruments, because they answer two different questions and only one of
them is the contract:

* ``tracemalloc`` counts bytes Python itself allocated, so its peak is exactly
  the claim — "the body is never held" — and it is the same number on every
  platform and every allocator. A ``BytesIO``, a ``b"".join`` or a
  ``chunk`` list anywhere in the path blows past the budget on it immediately.
  This is the binding assertion.
* Peak RSS is the process's high-water mark, which includes whatever the C
  allocator chose to keep after Python freed it, and whatever a C extension
  allocated where the tracer cannot see it. That is what it is here: a smoke
  alarm for a buffer outside Python's allocator, held to a coarse ceiling, not
  the contract. The child pins glibc's thresholds low *for itself only* — an
  env var read at its own startup, no product change — so streaming churn is
  returned to the OS instead of growing an arena that is only trimmed from the
  top. It reads its OWN high-water mark to do it (see ``peak_rss_bytes``);
  ``ru_maxrss`` in a child is the parent's on Linux.
"""

from __future__ import annotations

import ast
import inspect
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import IO, Any

import psutil
import pytest
from alkera_core.files.clock import SystemClock
from alkera_core.files.hashing import StreamHasher
from alkera_core.files.store.filesystem import CHUNK_BYTES as DRIVER_CHUNK_BYTES
from alkera_core.files.store.filesystem import FilesystemStore

GIB = 1 << 30
MIB = 1 << 20
CHUNK_BYTES = MIB
TOTAL_BYTES = GIB
#: What Python may allocate across the whole transfer. The contract.
TRACED_LIMIT_BYTES = 128 * MIB
#: What the process may be resident at. Deliberately far above the traced
#: budget: it is bounding allocator behaviour, not the code's.
RSS_LIMIT_BYTES = 512 * MIB
FREE_SPACE_FLOOR = 2 * GIB

#: Keep glibc returning the streaming churn instead of growing its arena. Both
#: default to an adaptive mode that raises itself as soon as it sees mmap'd
#: blocks freed, which is the behaviour that makes RSS an unusable instrument
#: here. Set in the CHILD's env only; nothing the product ships reads these.
CHILD_MALLOC_ENV = {
    "MALLOC_MMAP_THRESHOLD_": "131072",
    "MALLOC_TRIM_THRESHOLD_": "131072",
}


# ---------------------------------------------------------------------------
# The RSS probe. Defined here rather than inside the child template so the test
# session can drive each platform branch, and injected into the child by source
# so what runs there is exactly what is pinned below.
# ---------------------------------------------------------------------------


def _linux_peak_rss_bytes(status_path: str = "/proc/self/status") -> int:
    """THIS process's high-water mark, from /proc — not ``ru_maxrss``.

    CPython spawns a child with fork/vfork on Linux, so the child's mm starts as
    the parent's and carries the parent's peak into ``signal->maxrss``, which
    ``getrusage`` then reports forever: a pytest worker holding a gibibyte makes
    every child it starts look like it held one too. ``VmHWM`` belongs to the mm
    created by execve, so it counts this transfer and nothing before it.
    """
    with open(status_path, encoding="utf-8") as status:
        for line in status:
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    raise AssertionError(f"no VmHWM in {status_path}")


def _windows_peak_rss_bytes() -> int:
    """``peak_wset`` for this process, through psutil.

    Windows has neither /proc nor the ``resource`` module, so the POSIX branch
    below cannot even be imported there, let alone called. ``peak_wset`` is the
    platform's own high-water mark — the same ``PeakWorkingSetSize`` psapi
    reports — and is already in bytes.

    Called through psutil rather than ctypes because the handle and argument
    widths are psutil's problem, not this suite's: ``GetCurrentProcess``
    returns a pseudo-handle that an undeclared ``restype`` truncates to a
    32-bit int, and psapi then refuses it with ``ERROR_INVALID_HANDLE``, which
    took the measuring child down before it measured anything.
    """
    import psutil

    return int(psutil.Process().memory_info().peak_wset)


def _posix_peak_rss_bytes() -> int:
    """macOS and other POSIX hosts: ``ru_maxrss``, which is already this child's own.

    Imported inside the branch: ``resource`` does not exist on Windows, and a
    module-level import of it takes the whole child down before it measures
    anything.
    """
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def peak_rss_bytes() -> int:
    """This process's peak resident set, in bytes, on whatever platform it is."""
    system = platform.system()
    if system == "Linux":
        return _linux_peak_rss_bytes()
    if system == "Windows":
        return _windows_peak_rss_bytes()
    return _posix_peak_rss_bytes()


PROBE_SOURCE = "\n".join(
    inspect.getsource(fn)
    for fn in (
        _linux_peak_rss_bytes,
        _windows_peak_rss_bytes,
        _posix_peak_rss_bytes,
        peak_rss_bytes,
    )
)


CHILD = r"""
import asyncio
import platform
import sys
import tracemalloc
from collections.abc import AsyncIterator

from pathlib import Path

from alkera_core.files.clock import SystemClock
from alkera_core.files.hashing import StreamHasher
from alkera_core.files.store.filesystem import CHUNK_BYTES as DRIVER_CHUNK_BYTES
from alkera_core.files.store.filesystem import FilesystemStore

CHUNK_BYTES = {chunk_bytes}
TOTAL_BYTES = {total_bytes}
KEY = "objects/aa/bb/onegib"

# A repeating pattern whose period is coprime with the chunk size, so a driver
# that silently truncated or reordered the stream could not still hash right.
PATTERN = bytes(range(251))
CHUNK = (PATTERN * (CHUNK_BYTES // len(PATTERN) + 1))[:CHUNK_BYTES]
CHUNKS = TOTAL_BYTES // CHUNK_BYTES


async def source() -> AsyncIterator[bytes]:
    for _ in range(CHUNKS):
        # A fresh object per chunk, the way a body arriving off a socket is.
        # Yielding one shared buffer would let a store that simply KEPT every
        # chunk it was handed trace as a few kilobytes of references.
        yield bytes(memoryview(CHUNK))


def expected_hash() -> bytes:
    hasher = StreamHasher()
    for _ in range(CHUNKS):
        hasher.update(CHUNK)
    return hasher.finalize().content_hash


{probe_source}


async def main(root: str) -> None:
    store = FilesystemStore(Path(root), clock=SystemClock().now)
    checksum = expected_hash()

    # Started here, so the pattern buffer and every import are already behind
    # us: what it counts from now on is the transfer's own allocation.
    tracemalloc.start()

    put = await store.put(KEY, source(), size=TOTAL_BYTES, checksum=checksum)
    assert put.size == TOTAL_BYTES, put.size
    assert put.checksum == checksum
    put_traced = tracemalloc.get_traced_memory()[1]
    put_rss = peak_rss_bytes()

    tracemalloc.reset_peak()
    read = 0
    async for chunk in await store.get(KEY):
        read += len(chunk)
    assert read == TOTAL_BYTES, read
    get_traced = tracemalloc.get_traced_memory()[1]
    get_rss = peak_rss_bytes()
    tracemalloc.stop()

    print(put_traced)
    print(get_traced)
    print(put_rss)
    print(get_rss)


asyncio.run(main(sys.argv[1]))
"""


def child_script() -> str:
    """The child program: the template above with this module's probe inlined."""
    return CHILD.format(
        chunk_bytes=CHUNK_BYTES,
        total_bytes=TOTAL_BYTES,
        probe_source=PROBE_SOURCE,
    )


def _module_level_imports(script: str) -> set[str]:
    """Every module the child imports before it runs a single statement of work."""
    names: set[str] = set()
    for node in ast.parse(script).body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def test_the_child_imports_no_module_a_supported_platform_lacks() -> None:
    """``resource`` does not exist on Windows; importing it up front kills the child.

    The child then dies during its imports, before it measures anything, and the
    failure surfaces as an unreadable traceback in the parent's assertion rather
    than as "this platform cannot be measured".
    """
    imported = _module_level_imports(child_script())

    assert "resource" not in imported
    assert "asyncio" in imported, "the parse found no imports at all"


def test_the_linux_probe_reads_vmhwm_as_kibibytes(tmp_path: Path) -> None:
    """The /proc branch, driven from a host with no /proc."""
    status = tmp_path / "status"
    status.write_text(
        "Name:\tpython3\nVmPeak:\t 9999999 kB\nVmHWM:\t  123456 kB\nVmRSS:\t 1024 kB\n",
        encoding="utf-8",
    )

    assert _linux_peak_rss_bytes(str(status)) == 123456 * 1024


def test_the_linux_probe_refuses_a_status_with_no_high_water_mark(tmp_path: Path) -> None:
    status = tmp_path / "status"
    status.write_text("Name:\tpython3\nVmRSS:\t 1024 kB\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="no VmHWM"):
        _linux_peak_rss_bytes(str(status))


def test_the_windows_branch_never_reaches_for_the_resource_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows has no ``resource`` and no /proc: it must take neither path.

    ``resource`` is made unimportable for the duration, so a probe that fell
    through to the POSIX branch raises here instead of quietly answering.
    """
    module = sys.modules[__name__]
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setitem(sys.modules, "resource", None)
    monkeypatch.setattr(module, "_windows_peak_rss_bytes", lambda: 4096)
    monkeypatch.setattr(
        module,
        "_linux_peak_rss_bytes",
        lambda *_: pytest.fail("Windows took the /proc branch"),
    )

    assert peak_rss_bytes() == 4096


def test_the_windows_probe_reads_the_platforms_own_high_water_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows branch's body, driven from a host that has no psapi.

    Two things are wrong if this fails. Reading ``rss`` instead of
    ``peak_wset`` would answer with the working set at the moment of the call,
    which a transfer that had already freed its buffers reports as small — the
    bound would then hold for a store that had held the whole gibibyte. And
    reaching for the handle through ctypes, as this used to, made the call
    itself the failure: an undeclared ``restype`` truncates the pseudo-handle
    ``GetCurrentProcess`` returns, psapi answers ``ERROR_INVALID_HANDLE``, and
    the measuring child died before it measured anything.
    """

    class FakeMemoryInfo:
        rss = 3 * MIB
        peak_wset = 512 * MIB

    class FakeProcess:
        def memory_info(self) -> FakeMemoryInfo:
            return FakeMemoryInfo()

    monkeypatch.setattr(psutil, "Process", FakeProcess)

    assert _windows_peak_rss_bytes() == 512 * MIB


def test_the_probe_answers_this_processes_own_peak_on_this_host() -> None:
    """The branch this host actually takes returns a plausible byte count."""
    peak = peak_rss_bytes()

    assert peak > 8 * MIB, peak
    assert peak < 64 * GIB, peak


def _free_bytes(directory: Path) -> int:
    return shutil.disk_usage(directory).free


def test_a_1_gib_object_streams_through_the_store_under_a_128_mib_bound(
    tmp_path: Path,
) -> None:
    """Neither the put nor the get may buffer the body."""
    if _free_bytes(Path(tempfile.gettempdir())) < FREE_SPACE_FLOOR:
        pytest.skip("the temp filesystem has under 2 GiB free")
    if _free_bytes(tmp_path) < FREE_SPACE_FLOOR:
        pytest.skip("the test's own filesystem has under 2 GiB free")

    script = child_script()
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "store")],
        capture_output=True,
        text=True,
        timeout=280,
        check=False,
        env={**os.environ, **CHILD_MALLOC_ENV},
    )
    assert completed.returncode == 0, completed.stderr

    lines = completed.stdout.strip().splitlines()
    assert len(lines) == 4, completed.stdout
    put_traced, get_traced, put_rss, get_rss = (int(line) for line in lines)

    # The contract: a gibibyte moved through Python without a gibibyte of it
    # ever being allocated at once.
    assert put_traced < TRACED_LIMIT_BYTES, f"put traced {put_traced / MIB:.1f} MiB"
    assert get_traced < TRACED_LIMIT_BYTES, f"get traced {get_traced / MIB:.1f} MiB"
    # The smoke alarm: a buffer the tracer cannot see would still show here.
    assert put_rss < RSS_LIMIT_BYTES, f"put was resident at {put_rss / MIB:.1f} MiB"
    assert get_rss < RSS_LIMIT_BYTES, f"get was resident at {get_rss / MIB:.1f} MiB"


# ---------------------------------------------------------------------------
# The same claim, in-process and in a second: what the driver asks the file for.
# ---------------------------------------------------------------------------
#
# The gibibyte above is the honest measurement, and it is slow and only as
# sharp as the platform's instruments. This pair is the cheap guard that runs
# everywhere: every read and every write the driver issues goes through the
# file object, so a body held whole — a ``read()`` with no size, a joined
# stream, a mmap of the file — shows up as one oversized call.


class _Recorded:
    """A file handle that remembers the size of every read and write through it."""

    def __init__(self, inner: IO[bytes]) -> None:
        self._inner = inner
        self.reads: list[int] = []
        self.writes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        data = self._inner.read(size)
        self.reads.append(len(data))
        return data

    def write(self, data: Any) -> int:
        self.writes.append(len(data))
        return self._inner.write(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __enter__(self) -> _Recorded:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._inner.__exit__(*exc)


def _record_handles(monkeypatch: pytest.MonkeyPatch) -> list[_Recorded]:
    """Every file the driver opens from here on, wrapped and collected."""
    opened: list[_Recorded] = []
    real_open = Path.open

    def recording_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, *args, **kwargs)
        if "b" not in str(args[0] if args else kwargs.get("mode", "r")):
            return handle
        recorded = _Recorded(handle)
        opened.append(recorded)
        return recorded

    monkeypatch.setattr(Path, "open", recording_open)
    return opened


async def _source(total: int, chunk: int) -> AsyncIterator[bytes]:
    remaining = total
    while remaining > 0:
        take = min(chunk, remaining)
        remaining -= take
        yield bytes(take)


OBJECT_BYTES = 8 * MIB
KEY = "objects/ab/cd/eightmib"


async def _store_an_object(root: Path) -> FilesystemStore:
    store = FilesystemStore(root, clock=SystemClock().now)
    hasher = StreamHasher()
    async for piece in _source(OBJECT_BYTES, MIB):
        hasher.update(piece)
    await store.put(
        KEY,
        _source(OBJECT_BYTES, MIB),
        size=OBJECT_BYTES,
        checksum=hasher.finalize().content_hash,
    )
    return store


@pytest.mark.asyncio
async def test_a_range_read_asks_the_file_only_for_the_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 64 KiB range of an 8 MiB object costs 64 KiB of reads, in one chunk's worth.

    This is the in-process half of the whole-object-read claim the perf suite
    measures in RSS: an implementation that read the object and then sliced it
    would answer the same bytes just as fast on a warm page cache, and only the
    size of what it asked the file for gives it away.
    """
    store = await _store_an_object(tmp_path / "store")
    wanted = 64 * 1024
    offset = OBJECT_BYTES // 3 + 12_345

    handles = _record_handles(monkeypatch)
    read = 0
    async for chunk in await store.get(KEY, range=(offset, offset + wanted - 1)):
        read += len(chunk)

    assert read == wanted
    served = [size for handle in handles for size in handle.reads]
    assert served, "the read never went through a file handle"
    assert max(served) <= DRIVER_CHUNK_BYTES, f"one read took {max(served)} bytes"
    assert sum(served) == wanted, f"the range cost {sum(served)} bytes of reads, not {wanted}"


@pytest.mark.asyncio
async def test_a_put_hands_the_file_one_chunk_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source yielding 4 MiB at a time still reaches the disk in chunks.

    The driver's granularity is its own, not its caller's: a body that arrives
    in large pieces — or in one — must not become one large write, because a
    writer that holds what it is about to write holds the body.
    """
    store = FilesystemStore(tmp_path / "store", clock=SystemClock().now)
    hasher = StreamHasher()
    async for piece in _source(OBJECT_BYTES, 4 * MIB):
        hasher.update(piece)

    handles = _record_handles(monkeypatch)
    await store.put(
        KEY,
        _source(OBJECT_BYTES, 4 * MIB),
        size=OBJECT_BYTES,
        checksum=hasher.finalize().content_hash,
    )

    written = [size for handle in handles for size in handle.writes]
    assert sum(written) == OBJECT_BYTES, "the object did not land through the recorded handle"
    assert max(written) <= DRIVER_CHUNK_BYTES, f"one write took {max(written)} bytes"
