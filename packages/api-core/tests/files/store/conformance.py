"""The one suite every object-store driver must pass.

A driver is only portable if the layers above it can be written against
:class:`~alkera_core.files.store.protocol.ObjectStore` alone, so the contract
lives here once instead of being re-litigated per driver. A binding supplies a
``store`` fixture and subclasses :class:`StoreConformance`; every case below is
named for the invariant it pins, and a capability a driver does not declare is
skipped with the capability named rather than silently passing.

Checksums the cases assert are computed here with
:mod:`alkera_core.files.hashing` — never read back from the store, which would
let a driver that stored the wrong bytes agree with itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta

import pytest
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.hashing import hash_bytes
from alkera_core.files.store.errors import InvalidKey, PreconditionFailed, StoreError
from alkera_core.files.store.filesystem import BEFORE_MOVE_PUBLISH
from alkera_core.files.store.protocol import ObjectStore, PartResult, UploadHandle

KEY = "objects/ab/cd/abcdef"
OTHER_KEY = "objects/ab/cd/fedcba"
MIB = 1 << 20

HOSTILE_KEYS = [
    pytest.param("../escape", id="parent-segment"),
    pytest.param("/absolute", id="absolute"),
    pytest.param("objects//empty", id="empty-segment"),
    pytest.param("Objects/Upper", id="uppercase"),
    pytest.param("domains/00000000-0000-0000-0000-000000000000/x", id="domain-prefix"),
    pytest.param("objects/nul\x00byte", id="nul-byte"),
    pytest.param("objects/./here", id="dot-segment"),
]


def body(size: int) -> bytes:
    """Deterministic bytes of an exact length whose content varies with position."""
    seed = b"alkera-files-conformance-"
    return (seed * (size // len(seed) + 1))[:size]


async def stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def drain(chunks: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in chunks])


async def refusing_stream() -> AsyncIterator[bytes]:
    """A body a driver must never read, because it has to refuse the write first."""
    raise AssertionError("the driver read bytes it was required to refuse before upload")
    yield b""  # pragma: no cover - unreachable; keeps this an async generator


class ChunkMeter:
    """Counts how many yielded chunks a driver is holding at one time."""

    def __init__(self) -> None:
        self.outstanding = 0
        self.peak = 0

    async def feed(self, chunk: bytes, count: int) -> AsyncIterator[bytes]:
        for _ in range(count):
            self.outstanding += 1
            self.peak = max(self.peak, self.outstanding)
            yield chunk
            self.outstanding -= 1


async def put_bytes(store: ObjectStore, key: str, data: bytes, *, if_absent: bool = True) -> bytes:
    """Write ``data`` and return the content hash the *test* computed for it."""
    checksum = hash_bytes(data).content_hash
    await store.put(key, stream(data), size=len(data), checksum=checksum, if_absent=if_absent)
    return checksum


async def read_all(store: ObjectStore, key: str) -> bytes:
    return await drain(await store.get(key))


async def every_key(store: ObjectStore, prefix: str = "") -> list[str]:
    """Every key under ``prefix``, walked page by page through ``after``."""
    keys: list[str] = []
    after: str | None = None
    for _ in range(64):
        page = await store.list_prefix(prefix, after=after, limit=100)
        keys.extend(page.keys)
        if page.next_after is None:
            return keys
        after = page.next_after
    raise AssertionError("list_prefix never reported the end of the listing")


async def upload_parts(
    store: ObjectStore, key: str, parts: Sequence[bytes]
) -> tuple[UploadHandle, list[PartResult]]:
    """Open one multipart session and stage every part in it."""
    handle = await store.multipart_create(key, size=sum(len(part) for part in parts))
    accepted: list[PartResult] = []
    for number, part in enumerate(parts, start=1):
        accepted.append(
            await store.multipart_put_part(
                handle,
                number,
                stream(part),
                size=len(part),
                checksum=hash_bytes(part).content_hash,
            )
        )
    return handle, accepted


class StoreConformance:
    """Subclass this and supply a ``store`` fixture; nothing else is required."""

    # -- put / get / head / delete ---------------------------------------

    @pytest.mark.parametrize(
        "size",
        [pytest.param(0, id="empty"), pytest.param(1, id="one-byte"), pytest.param(MIB, id="1MiB")],
    )
    async def test_put_then_get_returns_the_exact_bytes(
        self, store: ObjectStore, size: int
    ) -> None:
        data = body(size)
        checksum = await put_bytes(store, KEY, data)
        read_back = await read_all(store, KEY)
        assert read_back == data
        assert hash_bytes(read_back).content_hash == checksum

    @pytest.mark.parametrize(
        "size",
        [pytest.param(0, id="empty"), pytest.param(1, id="one-byte"), pytest.param(MIB, id="1MiB")],
    )
    async def test_put_reports_the_size_and_checksum_the_caller_computed(
        self, store: ObjectStore, size: int
    ) -> None:
        data = body(size)
        expected = hash_bytes(data).content_hash
        result = await store.put(KEY, stream(data), size=size, checksum=expected)
        assert (result.key, result.size, result.checksum) == (KEY, size, expected)

    @pytest.mark.parametrize(
        "size", [pytest.param(0, id="empty"), pytest.param(3 * 1024, id="3KiB")]
    )
    async def test_head_reports_the_size_and_any_checksum_the_store_keeps(
        self, store: ObjectStore, size: int
    ) -> None:
        """The empty object is a real object, and ``head`` says so.

        An upload session proves its staged parts are in the store by heading
        each one and comparing the size to the row — so a driver that answered
        ``None`` for a zero-byte key, or reported it as missing, would make a
        zero-byte file impossible to commit.
        """
        data = body(size)
        checksum = await put_bytes(store, KEY, data)
        info = await store.head(KEY)
        assert info is not None
        assert info.size == len(data)
        if info.checksum is not None:
            assert info.checksum == checksum

    async def test_delete_removes_the_object_and_a_missing_key_is_silent(
        self, store: ObjectStore
    ) -> None:
        await put_bytes(store, KEY, body(64))
        await store.delete(KEY)
        assert await store.head(KEY) is None
        await store.delete(KEY)
        await store.delete(OTHER_KEY)
        assert await store.head(KEY) is None
        assert await every_key(store, "objects/") == []

    # -- move -------------------------------------------------------------

    async def test_a_move_shows_a_concurrent_reader_the_old_or_the_new_key_never_neither(
        self, store: ObjectStore
    ) -> None:
        """The portable guarantee is "old or new, never neither".

        A driver that can publish the destination and drop the source in one
        step declares ``atomic_move`` and is held to the stronger "exactly
        one"; a copy-then-delete store (every S3 endpoint) shows both keys for
        a moment, which is legal, while showing neither never is.
        """
        data = body(2048)
        await put_bytes(store, KEY, data)
        observations = await self._observe_across_a_move(store)

        assert observations, "the observer never got to run"
        assert all(sum(seen) >= 1 for seen in observations), observations
        if store.capabilities.atomic_move:
            assert all(sum(seen) == 1 for seen in observations), observations
        assert await store.head(KEY) is None
        assert await read_all(store, OTHER_KEY) == data

    async def _observe_across_a_move(self, store: ObjectStore) -> list[tuple[bool, bool]]:
        """Read both keys while a move of KEY to OTHER_KEY is in flight.

        A driver that offers `with_checkpoints` gets the exact observation: a
        second handle over the same bytes parks inside its own `move` at
        `store.before_move_publish` and this coroutine reads the store there,
        strictly between the two effects. Nothing else can catch a move whose
        publish step is one synchronous syscall.

        A driver without the seam (every S3 endpoint, whose move is a copy and
        a delete with real awaits in between) is polled instead: its move
        suspends often enough for the reader to land in the window.
        """
        twin = getattr(store, "with_checkpoints", None)
        if twin is None:
            return await self._poll_across_a_move(store)

        pauses = PausingCheckpoints()
        pauses.pause(BEFORE_MOVE_PUBLISH)
        mover = asyncio.create_task(twin(pauses).move(KEY, OTHER_KEY))
        try:
            await pauses.wait_paused(BEFORE_MOVE_PUBLISH)
            seen = [(await store.head(KEY) is not None, await store.head(OTHER_KEY) is not None)]
            pauses.release(BEFORE_MOVE_PUBLISH)
        finally:
            await mover
        return seen

    async def _poll_across_a_move(self, store: ObjectStore) -> list[tuple[bool, bool]]:
        observations: list[tuple[bool, bool]] = []
        stop = asyncio.Event()

        async def observer() -> None:
            while not stop.is_set():
                src = await store.head(KEY)
                dst = await store.head(OTHER_KEY)
                observations.append((src is not None, dst is not None))
                await asyncio.sleep(0)

        watcher = asyncio.create_task(observer())
        await asyncio.sleep(0)
        await store.move(KEY, OTHER_KEY)
        stop.set()
        await watcher
        return observations

    # -- listing ----------------------------------------------------------

    async def test_list_prefix_pages_through_after_without_repeating_or_skipping(
        self, store: ObjectStore
    ) -> None:
        wanted = [f"objects/aa/bb/k{index:02d}" for index in range(7)]
        for key in wanted:
            await put_bytes(store, key, body(16))
        await put_bytes(store, "incoming/elsewhere", body(16))

        seen: list[str] = []
        after: str | None = None
        for _ in range(16):
            page = await store.list_prefix("objects/aa/bb/", after=after, limit=3)
            assert len(page.keys) <= 3
            seen.extend(page.keys)
            if page.next_after is None:
                break
            after = page.next_after
        else:  # pragma: no cover - a driver whose listing never terminates
            raise AssertionError("list_prefix never reported the end of the listing")

        assert len(seen) == len(set(seen)), seen
        assert sorted(seen) == sorted(wanted)

    # -- multipart --------------------------------------------------------

    async def test_multipart_of_three_parts_completes_byte_identical(
        self, store: ObjectStore
    ) -> None:
        floor = store.capabilities.min_part_bytes
        parts = [body(floor), body(floor)[::-1], body(97)]
        handle, accepted = await upload_parts(store, KEY, parts)
        joined = b"".join(parts)

        result = await store.multipart_complete(
            handle, accepted, checksum=hash_bytes(joined).content_hash
        )

        assert result.size == len(joined)
        # The hash the *caller* computed while streaming comes back: no part
        # carries it, and an ETag is not a content hash on any driver.
        assert result.checksum == hash_bytes(joined).content_hash
        assert await read_all(store, KEY) == joined

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param("missing", id="a-part-left-out"),
            pytest.param("checksum", id="a-part-with-the-wrong-checksum"),
            pytest.param("extra", id="a-part-that-was-never-staged"),
        ],
    )
    async def test_multipart_complete_refuses_a_part_list_that_does_not_match(
        self, store: ObjectStore, damage: str
    ) -> None:
        parts = [body(store.capabilities.min_part_bytes), body(64)]
        handle, accepted = await upload_parts(store, KEY, parts)
        if damage == "missing":
            declared = accepted[:1]
        elif damage == "checksum":
            wrong = hash_bytes(b"not these bytes at all").content_hash
            declared = [accepted[0], PartResult(2, accepted[1].size, wrong, "wrong-etag")]
        else:
            declared = [*accepted, PartResult(3, 64, hash_bytes(body(64)).content_hash, None)]

        with pytest.raises(PreconditionFailed):
            await store.multipart_complete(handle, declared)
        assert await store.head(KEY) is None

    async def test_multipart_abort_removes_every_part_and_leaves_no_object(
        self, store: ObjectStore
    ) -> None:
        parts = [body(store.capabilities.min_part_bytes), body(64)]
        handle, _accepted = await upload_parts(store, KEY, parts)

        await store.multipart_abort(handle)

        assert await store.head(KEY) is None
        assert await every_key(store) == []

    async def test_a_part_over_the_capability_is_refused_before_its_bytes_are_read(
        self, store: ObjectStore
    ) -> None:
        oversize = store.capabilities.max_part_bytes + 1
        handle = await store.multipart_create(KEY, size=oversize)
        with pytest.raises(StoreError):
            await store.multipart_put_part(
                handle,
                1,
                refusing_stream(),
                size=oversize,
                checksum=hash_bytes(b"").content_hash,
            )
        assert await store.head(KEY) is None
        # The driver closed the session itself, so a second abort is a no-op
        # rather than a NotFound; the live binding checks the endpoint's own
        # list of open uploads in its teardown to prove nothing was stranded.
        await store.multipart_abort(handle)

    # -- range reads ------------------------------------------------------

    @pytest.mark.parametrize(
        ("start", "end"),
        [
            pytest.param(0, 0, id="first-byte"),
            pytest.param(0, 1, id="first-two-bytes"),
            pytest.param(255, 255, id="last-byte"),
            pytest.param(0, 255, id="the-whole-object"),
            pytest.param(100, 149, id="a-middle-run"),
        ],
    )
    async def test_a_range_read_returns_exactly_the_bytes_asked_for(
        self, store: ObjectStore, start: int, end: int
    ) -> None:
        data = body(256)
        await put_bytes(store, KEY, data)
        got = await drain(await store.get(KEY, range=(start, end)))
        assert got == data[start : end + 1]

    async def test_a_range_that_starts_past_the_end_raises_a_store_error(
        self, store: ObjectStore
    ) -> None:
        await put_bytes(store, KEY, body(256))
        with pytest.raises(StoreError):
            await drain(await store.get(KEY, range=(256, 300)))

    # -- conditional write and checksums ---------------------------------

    async def test_put_if_absent_on_an_existing_key_honours_the_capability(
        self, store: ObjectStore
    ) -> None:
        data = body(512)
        await put_bytes(store, KEY, data)
        if store.capabilities.conditional_write:
            with pytest.raises(PreconditionFailed):
                await put_bytes(store, KEY, data)
        else:
            await put_bytes(store, KEY, data)
        info = await store.head(KEY)
        assert info is not None
        assert info.size == len(data)
        assert await read_all(store, KEY) == data

    async def test_put_if_absent_over_the_multipart_threshold_honours_the_capability(
        self, store: ObjectStore
    ) -> None:
        """A conditional put is conditional at every size, not only the small ones.

        Past ``2 x min_part_bytes`` the driver assembles the object out of
        parts, and the conditional has to ride the *assembly*: without it a
        large put silently overwrites the object a small one is refused.
        """
        data = body(2 * store.capabilities.min_part_bytes)
        await put_bytes(store, KEY, data)
        if store.capabilities.conditional_write:
            with pytest.raises(PreconditionFailed):
                await put_bytes(store, KEY, data)
        else:
            await put_bytes(store, KEY, data)
        assert await read_all(store, KEY) == data

    async def test_a_put_that_streams_fewer_bytes_than_it_declared_leaves_no_object(
        self, store: ObjectStore
    ) -> None:
        data = body(4096)
        with pytest.raises(StoreError):
            await store.put(
                KEY, stream(data), size=len(data) + 1, checksum=hash_bytes(data).content_hash
            )
        assert await store.head(KEY) is None

    async def test_a_put_that_streams_more_bytes_than_it_declared_leaves_no_object(
        self, store: ObjectStore
    ) -> None:
        data = body(4096)
        with pytest.raises(StoreError):
            await store.put(
                KEY, stream(data), size=len(data) - 1, checksum=hash_bytes(data).content_hash
            )
        assert await store.head(KEY) is None

    async def test_multipart_complete_without_a_whole_object_checksum_is_refused(
        self, store: ObjectStore
    ) -> None:
        """No part's hash is the object's, so the driver refuses to invent one."""
        floor = store.capabilities.min_part_bytes
        handle, accepted = await upload_parts(store, KEY, [body(floor), body(64)])
        with pytest.raises(StoreError):
            await store.multipart_complete(handle, accepted)

    async def test_a_single_part_upload_needs_no_declared_checksum(
        self, store: ObjectStore
    ) -> None:
        """One part IS the whole object, and its checksum was verified going in."""
        data = body(1024)
        handle, accepted = await upload_parts(store, KEY, [data])
        result = await store.multipart_complete(handle, accepted)
        assert result.checksum == hash_bytes(data).content_hash

    async def test_a_put_whose_checksum_does_not_match_leaves_no_object(
        self, store: ObjectStore
    ) -> None:
        data = body(4096)
        wrong = hash_bytes(b"a different body entirely").content_hash
        with pytest.raises(StoreError):
            await store.put(KEY, stream(data), size=len(data), checksum=wrong)
        assert await store.head(KEY) is None
        assert await every_key(store) == []

    async def test_five_concurrent_puts_of_one_key_leave_exactly_one_object(
        self, store: ObjectStore
    ) -> None:
        data = body(8192)
        results = await asyncio.gather(
            *(put_bytes(store, KEY, data) for _ in range(5)), return_exceptions=True
        )
        for outcome in results:
            if isinstance(outcome, BaseException):
                assert isinstance(outcome, PreconditionFailed), outcome
        assert any(not isinstance(outcome, BaseException) for outcome in results)
        assert await every_key(store) == [KEY]
        assert await read_all(store, KEY) == data

    # -- key validation ---------------------------------------------------

    @pytest.mark.parametrize("hostile", HOSTILE_KEYS)
    async def test_a_hostile_key_is_refused_and_stores_nothing(
        self, store: ObjectStore, hostile: str
    ) -> None:
        data = body(32)
        with pytest.raises(InvalidKey):
            await store.put(
                hostile, stream(data), size=len(data), checksum=hash_bytes(data).content_hash
            )
        assert await every_key(store) == []

    # -- capability-gated surfaces ----------------------------------------

    async def test_presign_get_returns_a_url_when_the_driver_signs(
        self, store: ObjectStore
    ) -> None:
        if not store.capabilities.presigned:
            pytest.skip("capabilities.presigned is False")
        await put_bytes(store, KEY, body(64))
        url = store.presign_get(KEY, range=None, ttl=timedelta(minutes=5))
        assert url.startswith("http")

    async def test_presign_put_part_returns_a_url_when_the_driver_signs(
        self, store: ObjectStore
    ) -> None:
        if not store.capabilities.presigned:
            pytest.skip("capabilities.presigned is False")
        handle = await store.multipart_create(KEY, size=MIB)
        url = store.presign_put_part(handle, 1, size=MIB, ttl=timedelta(minutes=5))
        assert url.startswith("http")

    async def test_vend_scoped_credentials_is_prefix_scoped_when_supported(
        self, store: ObjectStore
    ) -> None:
        if not store.capabilities.scoped_credentials:
            pytest.skip("capabilities.scoped_credentials is False")
        vended = await store.vend_scoped_credentials(
            "objects/", ttl=timedelta(minutes=5), read_only=True
        )
        assert vended.prefix == "objects/"
        assert vended.read_only is True
        assert vended.access_key_id

    # -- streaming --------------------------------------------------------

    async def test_a_64_mib_put_never_holds_more_than_two_chunks_at_once(
        self, store: ObjectStore
    ) -> None:
        chunk = body(MIB)
        count = 64
        meter = ChunkMeter()
        expected = hash_bytes(chunk * count).content_hash

        result = await store.put(KEY, meter.feed(chunk, count), size=MIB * count, checksum=expected)

        assert result.size == MIB * count
        assert result.checksum == expected
        assert meter.peak <= 2, f"the driver held {meter.peak} chunks at once"
