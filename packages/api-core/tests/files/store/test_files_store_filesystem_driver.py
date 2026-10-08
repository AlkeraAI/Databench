"""The filesystem driver: round trips, refusals, and containment under the root."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alkera_core.files.store.errors import (
    ChecksumMismatch,
    InvalidKey,
    NotFound,
    PreconditionFailed,
)
from alkera_core.files.store.filesystem import KEY_MARKER, PARTS_ROOT, FilesystemStore
from alkera_core.files.store.protocol import ObjectStore, PartResult
from blake3 import blake3

KEY = "objects/ab/cd/abcd"


def _clock() -> datetime:
    return datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> FilesystemStore:
    return FilesystemStore(tmp_path / "root", clock=_clock)


async def _stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def _digest(data: bytes) -> bytes:
    return blake3(data).digest()


async def _drain(chunks: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in chunks])


def _tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def test_the_driver_satisfies_the_object_store_protocol(tmp_path: Path) -> None:
    assert isinstance(_store(tmp_path), ObjectStore)


def test_the_capability_record_states_what_the_driver_cannot_do(tmp_path: Path) -> None:
    caps = _store(tmp_path).capabilities
    assert (caps.presigned, caps.range_signing, caps.scoped_credentials) == (False, False, False)
    assert (caps.conditional_write, caps.strong_read_after_write) == (True, True)


@pytest.mark.asyncio
async def test_put_then_get_and_head_round_trip_the_exact_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = b"the quick brown fox" * 1000
    result = await store.put(KEY, _stream(payload), size=len(payload), checksum=_digest(payload))

    assert result.size == len(payload)
    assert result.checksum == _digest(payload)
    assert await _drain(await store.get(KEY)) == payload
    info = await store.head(KEY)
    assert info is not None
    assert info.size == len(payload)


@pytest.mark.asyncio
async def test_a_multi_chunk_stream_is_reassembled_in_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chunks = [b"a" * (1 << 20), b"b" * 7, b"c" * (1 << 21)]
    payload = b"".join(chunks)
    await store.put(KEY, _stream(*chunks), size=len(payload), checksum=_digest(payload))
    assert await _drain(await store.get(KEY)) == payload


@pytest.mark.asyncio
async def test_put_if_absent_refuses_a_second_write_and_keeps_the_first_bytes(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = b"first"
    await store.put(KEY, _stream(first), size=len(first), checksum=_digest(first))

    second = b"second"
    with pytest.raises(PreconditionFailed):
        await store.put(KEY, _stream(second), size=len(second), checksum=_digest(second))

    assert await _drain(await store.get(KEY)) == first
    assert _tree(tmp_path / "root") == [KEY]


@pytest.mark.asyncio
async def test_put_without_if_absent_replaces_the_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.put(KEY, _stream(b"first"), size=5, checksum=_digest(b"first"))
    await store.put(KEY, _stream(b"second"), size=6, checksum=_digest(b"second"), if_absent=False)
    assert await _drain(await store.get(KEY)) == b"second"


@pytest.mark.asyncio
async def test_a_checksum_mismatch_is_refused_and_leaves_no_file_behind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = b"honest bytes"
    with pytest.raises(ChecksumMismatch):
        await store.put(KEY, _stream(payload), size=len(payload), checksum=_digest(b"other bytes"))

    assert _tree(tmp_path / "root") == []
    assert await store.head(KEY) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param(0, 0, id="first_byte"),
        pytest.param(0, 1, id="first_two_bytes"),
        pytest.param(3, 6, id="middle_range"),
        pytest.param(9, 9, id="last_byte"),
        pytest.param(0, 9, id="whole_object"),
        pytest.param(7, 99, id="end_past_eof_clamps"),
    ],
)
async def test_a_range_get_returns_exactly_the_bytes_asked_for(
    tmp_path: Path, start: int, end: int
) -> None:
    store = _store(tmp_path)
    payload = b"0123456789"
    await store.put(KEY, _stream(payload), size=len(payload), checksum=_digest(payload))
    got = await _drain(await store.get(KEY, range=(start, end)))
    assert got == payload[start : min(end, len(payload) - 1) + 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param(-1, 3, id="negative_start"),
        pytest.param(5, 4, id="end_before_start"),
        pytest.param(10, 12, id="start_at_eof"),
    ],
)
async def test_an_unsatisfiable_range_is_refused(tmp_path: Path, start: int, end: int) -> None:
    store = _store(tmp_path)
    payload = b"0123456789"
    await store.put(KEY, _stream(payload), size=len(payload), checksum=_digest(payload))
    with pytest.raises(PreconditionFailed):
        await store.get(KEY, range=(start, end))


@pytest.mark.asyncio
async def test_head_and_get_of_a_missing_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert await store.head(KEY) is None
    with pytest.raises(NotFound):
        await store.get(KEY)


@pytest.mark.asyncio
async def test_delete_removes_the_object_and_is_silent_when_it_is_already_gone(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    await store.put(KEY, _stream(b"x"), size=1, checksum=_digest(b"x"))
    await store.delete(KEY)
    assert await store.head(KEY) is None
    await store.delete(KEY)


@pytest.mark.asyncio
async def test_move_carries_the_bytes_to_a_new_prefix_and_the_source_is_gone(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    payload = b"moving day"
    await store.put(KEY, _stream(payload), size=len(payload), checksum=_digest(payload))
    await store.move(KEY, f"deleted/{KEY}")

    assert await store.head(KEY) is None
    assert await _drain(await store.get(f"deleted/{KEY}")) == payload

    with pytest.raises(NotFound):
        await store.move(KEY, "deleted/gone")


@pytest.mark.asyncio
async def test_a_concurrent_reader_sees_the_old_or_the_new_object_never_neither(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    old = b"old bytes"
    new = b"new bytes"
    await store.put(KEY, _stream(old), size=len(old), checksum=_digest(old))
    await store.put("staging/next", _stream(new), size=len(new), checksum=_digest(new))

    observations: list[int | None] = []
    stop = asyncio.Event()

    async def reader() -> None:
        while not stop.is_set():
            info = await store.head(KEY)
            observations.append(None if info is None else info.size)
            await asyncio.sleep(0)

    task = asyncio.create_task(reader())
    await asyncio.sleep(0)
    await store.move("staging/next", KEY)
    stop.set()
    await task

    assert observations, "the reader never ran"
    assert None not in observations
    assert set(observations) <= {len(old), len(new)}
    assert await _drain(await store.get(KEY)) == new


@pytest.mark.asyncio
async def test_list_prefix_pages_by_keyset_over_the_directory_tree(tmp_path: Path) -> None:
    store = _store(tmp_path)
    keys = [f"objects/ab/cd/{index:04d}" for index in range(5)]
    for key in keys:
        await store.put(key, _stream(b"x"), size=1, checksum=_digest(b"x"))
    await store.put("incoming/s/1", _stream(b"y"), size=1, checksum=_digest(b"y"))

    first = await store.list_prefix("objects/", limit=3)
    assert list(first.keys) == keys[:3]
    assert first.next_after == keys[2]

    second = await store.list_prefix("objects/", after=first.next_after, limit=3)
    assert list(second.keys) == keys[3:]
    assert second.next_after is None

    assert list((await store.list_prefix("incoming/")).keys) == ["incoming/s/1"]


@pytest.mark.asyncio
async def test_multipart_completes_byte_identical_to_the_concatenated_parts(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    bodies = [b"part-one-", b"part-two-", b"part-three"]
    whole = b"".join(bodies)
    handle = await store.multipart_create(KEY, size=len(whole))
    parts = [
        await store.multipart_put_part(
            handle, index, _stream(body), size=len(body), checksum=_digest(body)
        )
        for index, body in enumerate(bodies, start=1)
    ]

    # The caller hashed the whole object as it streamed the parts; no part's
    # checksum is the object's, so the driver has to be told.
    result = await store.multipart_complete(handle, parts, checksum=_digest(whole))
    assert result.checksum == _digest(whole)
    assert await _drain(await store.get(KEY)) == whole
    assert _tree(tmp_path / "root") == [KEY]


@pytest.mark.asyncio
async def test_a_part_whose_checksum_does_not_match_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    handle = await store.multipart_create(KEY, size=4)
    with pytest.raises(ChecksumMismatch):
        await store.multipart_put_part(
            handle, 1, _stream(b"real"), size=4, checksum=_digest(b"fake")
        )
    # Only the session's own key marker: no part landed, and no object.
    assert _tree(tmp_path / "root") == [f"{PARTS_ROOT}/{handle.upload_id}/{KEY_MARKER}"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda parts: parts[:-1], id="a_part_omitted"),
        pytest.param(
            lambda parts: [*parts, PartResult(part_no=9, size=1, checksum=b"\x00", etag=None)],
            id="a_part_never_uploaded",
        ),
        pytest.param(
            lambda parts: [
                PartResult(part_no=p.part_no, size=p.size + 1, checksum=p.checksum, etag=p.etag)
                if p.part_no == 1
                else p
                for p in parts
            ],
            id="a_part_size_rewritten",
        ),
        pytest.param(
            lambda parts: [
                PartResult(part_no=p.part_no, size=p.size, checksum=b"\x01" * 32, etag=p.etag)
                if p.part_no == 2
                else p
                for p in parts
            ],
            id="a_part_checksum_rewritten",
        ),
    ],
)
async def test_multipart_complete_refuses_a_part_list_that_does_not_match_the_store(
    tmp_path: Path,
    mutate: object,
) -> None:
    store = _store(tmp_path)
    bodies = [b"aaaa", b"bbbb", b"cccc"]
    handle = await store.multipart_create(KEY, size=12)
    parts = [
        await store.multipart_put_part(
            handle, index, _stream(body), size=len(body), checksum=_digest(body)
        )
        for index, body in enumerate(bodies, start=1)
    ]

    with pytest.raises(PreconditionFailed):
        await store.multipart_complete(handle, mutate(parts))  # type: ignore[operator]

    assert await store.head(KEY) is None


@pytest.mark.asyncio
async def test_multipart_abort_removes_every_staged_part(tmp_path: Path) -> None:
    store = _store(tmp_path)
    handle = await store.multipart_create(KEY, size=4)
    await store.multipart_put_part(handle, 1, _stream(b"aaaa"), size=4, checksum=_digest(b"aaaa"))
    # Parts stage under the reserved dot namespace, keyed by upload id, so two
    # sessions on one key never share a directory.
    assert _tree(tmp_path / "root") == [
        f"{PARTS_ROOT}/{handle.upload_id}/1",
        f"{PARTS_ROOT}/{handle.upload_id}/{KEY_MARKER}",
    ]

    await store.multipart_abort(handle)
    assert _tree(tmp_path / "root") == []
    await store.multipart_abort(handle)


@pytest.mark.asyncio
async def test_a_part_number_outside_the_capability_range_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    handle = await store.multipart_create(KEY, size=1)
    for part_no in (0, store.capabilities.max_parts + 1):
        with pytest.raises(PreconditionFailed):
            await store.multipart_put_part(
                handle, part_no, _stream(b"x"), size=1, checksum=_digest(b"x")
            )
    await store.multipart_put_part(
        handle, store.capabilities.max_parts, _stream(b"x"), size=1, checksum=_digest(b"x")
    )


@pytest.mark.asyncio
async def test_completing_or_aborting_without_a_session_is_a_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    handle = await store.multipart_create(KEY, size=1)
    await store.multipart_abort(handle)
    with pytest.raises(NotFound):
        await store.multipart_complete(handle, [])
    with pytest.raises(NotFound):
        await store.multipart_put_part(handle, 1, _stream(b"x"), size=1, checksum=_digest(b"x"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        pytest.param("../x", id="parent_escape"),
        pytest.param("objects/../../x", id="parent_escape_mid_path"),
        pytest.param("/etc/passwd", id="absolute"),
        pytest.param("a//b", id="empty_segment"),
        pytest.param("a/\x00/b", id="nul_byte"),
        pytest.param("domains/other/objects/ab", id="domains_prefix"),
        pytest.param("Objects/ab", id="uppercase"),
    ],
)
async def test_a_hostile_key_raises_before_any_io_happens(tmp_path: Path, key: str) -> None:
    store = _store(tmp_path)
    root = tmp_path / "root"
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")

    for call in (
        store.put(key, _stream(b"x"), size=1, checksum=_digest(b"x")),
        store.head(key),
        store.delete(key),
        store.get(key),
        store.move(key, "objects/ab/cd/dst"),
        store.multipart_create(key, size=1),
    ):
        with pytest.raises(InvalidKey):
            await call

    assert _tree(root) == []
    assert outside.read_text() == "untouched"


@pytest.mark.asyncio
async def test_a_symlinked_segment_planted_under_the_root_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "root"
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    (secret_dir / "cd").mkdir()
    (secret_dir / "cd" / "abcd").write_text("secret")

    (root / "objects").mkdir(parents=True)
    os.symlink(secret_dir, root / "objects" / "ab")

    with pytest.raises(InvalidKey):
        await store.head(KEY)
    with pytest.raises(InvalidKey):
        await store.put(KEY, _stream(b"x"), size=1, checksum=_digest(b"x"))
    with pytest.raises(InvalidKey):
        await store.delete(KEY)

    assert (secret_dir / "cd" / "abcd").read_text() == "secret"


@pytest.mark.asyncio
async def test_a_symlinked_leaf_planted_under_the_root_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "root"
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")
    (root / "objects" / "ab" / "cd").mkdir(parents=True)
    os.symlink(outside, root / "objects" / "ab" / "cd" / "abcd")

    with pytest.raises(InvalidKey):
        await store.put(KEY, _stream(b"x"), size=1, checksum=_digest(b"x"), if_absent=False)
    assert outside.read_text() == "untouched"


def test_the_unsupported_capabilities_raise_rather_than_pretend(tmp_path: Path) -> None:
    from datetime import timedelta

    store = _store(tmp_path)
    with pytest.raises(NotImplementedError):
        store.presign_get(KEY, range=None, ttl=timedelta(minutes=5))
    with pytest.raises(NotImplementedError):
        store.presign_put_part(
            asyncio.run(store.multipart_create(KEY, size=1)), 1, size=1, ttl=timedelta(minutes=5)
        )
    with pytest.raises(NotImplementedError):
        asyncio.run(
            store.vend_scoped_credentials("objects/", ttl=timedelta(minutes=5), read_only=True)
        )
