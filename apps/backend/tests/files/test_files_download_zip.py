"""The tree download: a real ZIP64, the truth about what was left out, and a
memory bound that a buffering implementation cannot meet.

The three properties are tested separately because they fail for different
reasons. Validity is about the bytes ``zipfile`` produced; the skipped list is
about what the walk decided and whether it leaked a name; the bound is about
whether the archive was ever resident, which only a second process can answer
honestly.
"""

from __future__ import annotations

import io
import struct
import subprocess
import sys
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from _peak_rss import PEAK_RSS_SOURCE
from backend.services.files.archive import (
    SKIP_NOT_BYTES,
    SKIP_UNREADABLE,
    SKIPPED_MEMBER,
    ArchiveEntry,
    ArchivePlan,
    ArchiveSkip,
    stream_zip64,
)

#: The ZIP64 extended-information extra field's header id.
ZIP64_EXTRA_ID = 0x0001


def _bytes_for(index: int) -> bytes:
    """Content that differs per member, so a mixed-up archive cannot pass."""
    return f"file {index} ".encode() * (index + 1)


def _entry(index: int, path: str) -> ArchiveEntry:
    payload = _bytes_for(index)

    async def open_stream() -> AsyncIterator[bytes]:
        async def gen() -> AsyncIterator[bytes]:
            for start in range(0, len(payload), 7):
                yield payload[start : start + 7]

        return gen()

    return ArchiveEntry(path=path, size=len(payload), open=open_stream)


async def _collect(plan: ArchivePlan) -> bytes:
    return b"".join([chunk async for chunk in stream_zip64(plan)])


def _local_header_extra_ids(raw: bytes) -> set[int]:
    """The extra-field ids on the archive's first local file header.

    Parsed by hand rather than through ``zipfile`` because ``zipfile`` reads the
    central directory, and the question here is what the *streaming* writer put
    in the local header — which is where a non-ZIP64 writer would differ.
    """
    name_len, extra_len = struct.unpack("<HH", raw[26:30])
    extra = raw[30 + name_len : 30 + name_len + extra_len]
    ids: set[int] = set()
    cursor = 0
    while cursor + 4 <= len(extra):
        header_id, size = struct.unpack("<HH", extra[cursor : cursor + 4])
        ids.add(header_id)
        cursor += 4 + size
    return ids


@pytest.fixture
def tree_plan() -> ArchivePlan:
    """A 50-file tree with an empty folder, one unreadable file and one object
    node — the fixture the acceptance criterion names."""
    entries: list[ArchiveEntry] = [ArchiveEntry(path="empty-folder")]
    entries += [_entry(index, f"papers/file-{index:02d}.txt") for index in range(50)]
    return ArchivePlan(
        entries=tuple(entries),
        skips=(
            ArchiveSkip(node_id="11111111-1111-1111-1111-111111111111", code=SKIP_UNREADABLE),
            ArchiveSkip(node_id="22222222-2222-2222-2222-222222222222", code=SKIP_NOT_BYTES),
        ),
    )


@pytest.mark.asyncio
async def test_members_are_byte_for_byte_the_readable_files(tree_plan: ArchivePlan) -> None:
    raw = await _collect(tree_plan)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.testzip() is None
        for index in range(50):
            assert archive.read(f"papers/file-{index:02d}.txt") == _bytes_for(index)


@pytest.mark.asyncio
async def test_an_empty_folder_survives_the_archive(tree_plan: ArchivePlan) -> None:
    raw = await _collect(tree_plan)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert "empty-folder/" in archive.namelist()
        assert archive.getinfo("empty-folder/").is_dir()


@pytest.mark.asyncio
async def test_every_member_carries_the_zip64_extra(tree_plan: ArchivePlan) -> None:
    """The archive is ZIP64 by construction, not by luck of being small: the
    first local header carries the extended-information field even though no
    member is anywhere near 4 GiB."""
    raw = await _collect(tree_plan)
    body = raw[raw.index(b"PK\x03\x04", 1) :]  # skip the directory member
    assert ZIP64_EXTRA_ID in _local_header_extra_ids(body)


@pytest.mark.asyncio
async def test_the_skipped_member_names_the_two_by_id_only(tree_plan: ArchivePlan) -> None:
    raw = await _collect(tree_plan)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        listed = archive.read(SKIPPED_MEMBER).decode()
    for skip in tree_plan.skips:
        assert skip.node_id in listed
        assert skip.code in listed
    # Nothing that could be a name, a path or a target is in there.
    assert "papers/" not in listed
    assert ".txt" not in listed


@pytest.mark.asyncio
async def test_no_skipped_member_when_nothing_was_skipped() -> None:
    """The negative twin: a clean archive must not gain a mystery file."""
    raw = await _collect(ArchivePlan(entries=(_entry(0, "a.txt"),)))
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.namelist() == ["a.txt"]


@pytest.mark.asyncio
async def test_the_skip_list_is_reported_as_operation_errors(tree_plan: ArchivePlan) -> None:
    """The archive and the operation say the same thing, in the same words."""
    assert [skip.as_error() for skip in tree_plan.skips] == [
        {
            "itemId": "11111111-1111-1111-1111-111111111111",
            "code": SKIP_UNREADABLE,
            "message": SKIP_UNREADABLE,
        },
        {
            "itemId": "22222222-2222-2222-2222-222222222222",
            "code": SKIP_NOT_BYTES,
            "message": SKIP_NOT_BYTES,
        },
    ]


#: Driven in a child process so the number is a real RSS and not an allocator
#: statistic, and measured as the GROWTH over the process's own baseline so the
#: interpreter and the import graph are not being weighed. 200 MiB of members
#: through a 64 MiB ceiling: an implementation that buffered the archive — or
#: even one member — could not pass.
_MEMORY_PROBE = (
    """
import asyncio, sys
from backend.services.files.archive import ArchiveEntry, ArchivePlan, stream_zip64

MEMBER = 200
CHUNK = b"x" * (1 << 16)
PER_MEMBER = 1 << 20

def entry(index):
    async def open_stream():
        async def gen():
            for _ in range(PER_MEMBER // len(CHUNK)):
                yield CHUNK
        return gen()
    return ArchiveEntry(path="m/%03d.bin" % index, size=PER_MEMBER, open=open_stream)

async def main():
    plan = ArchivePlan(entries=tuple(entry(i) for i in range(MEMBER)))
    total = 0
    async for chunk in stream_zip64(plan):
        total += len(chunk)
    return total

"""
    + PEAK_RSS_SOURCE
    + """

def rss():
    return peak_rss(sys.platform)

baseline = rss()
total = asyncio.run(main())
print(total, baseline, rss())
"""
)


@pytest.mark.parametrize("ceiling_mib", [64], ids=["64MiB"])
def test_a_200_mib_tree_streams_under_the_memory_ceiling(ceiling_mib: int) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    finished = subprocess.run(
        [sys.executable, "-c", _MEMORY_PROBE],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo_root,
    )
    produced, baseline, peak = (int(value) for value in finished.stdout.split())
    assert produced > 200 * (1 << 20), "the probe did not actually archive 200 MiB"
    growth = peak - baseline
    assert growth < ceiling_mib * (1 << 20), f"streaming grew RSS by {growth / (1 << 20):.0f} MiB"
