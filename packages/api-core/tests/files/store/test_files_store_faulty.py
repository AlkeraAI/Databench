"""The fault injector's own self-test: every fault fires exactly as scheduled.

A fault injector that quietly does nothing turns every degradation test above
it into a tautology, so this module drives ``FaultyStore`` over a real
``FilesystemStore`` and asserts the *observable* effect of each kind — the
bytes a reader got, the exception a writer saw, what the directory holds —
never that a wrapper was called. The one deliberate exception is ``slow_read``,
where "the injected sleep seam was spent instead of real time" IS the
behaviour under test.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alkera_core.files.store.errors import Throttled, Unavailable
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.protocol import ObjectStore, UploadHandle
from alkera_test_support.files.faulty_store import Call, Fault, FaultSchedule, FaultyStore
from blake3 import blake3

KEY = "objects/ab/cd/target"
OTHER = "objects/ef/01/bystander"
DATA = b"the quick brown fox jumps over the lazy dog\n" * 4

Runner = Callable[[FaultyStore, str], Awaitable[str]]


def _clock() -> datetime:
    return datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def _inner(tmp_path: Path, name: str = "root") -> FilesystemStore:
    return FilesystemStore(tmp_path / name, clock=_clock)


async def _stream(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


async def _drain(chunks: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in chunks])


def _digest(payload: bytes) -> bytes:
    return blake3(payload).digest()


async def _seed(store: FilesystemStore, *keys: str) -> None:
    for key in keys:
        await store.put(key, _stream(DATA), size=len(DATA), checksum=_digest(DATA))


def _objects(root: Path) -> list[str]:
    """Every real object under the driver root; a temp spool file is not an object."""
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.name.startswith(".tmp-")
    )


# -- one runner per fault kind: makes the call under test, tags what happened --


async def _run_put(store: FaultyStore, key: str) -> str:
    try:
        await store.put(key, _stream(DATA), size=len(DATA), checksum=_digest(DATA))
    except Unavailable:
        return "unavailable"
    return "put"


async def _run_put_then_head(store: FaultyStore, key: str) -> str:
    await store.put(key, _stream(DATA), size=len(DATA), checksum=_digest(DATA))
    return "found" if await store.head(key) is not None else "missing"


async def _run_read(store: FaultyStore, key: str) -> str:
    try:
        body = await _drain(await store.get(key))
    except Throttled as exc:
        return f"throttled:{exc.retry_after}"
    except Unavailable:
        return "unavailable"
    if body == DATA:
        return "clean"
    if len(body) < len(DATA):
        return f"truncated:{len(body)}"
    return "corrupt"


async def _run_delete(store: FaultyStore, key: str) -> str:
    try:
        await store.delete(key)
    except Unavailable:
        return "unavailable"
    return "deleted"


#: kind → (fault, runner, tag when it fires, tag when it does not, method it lands on,
#: whether the object must already exist).
CASES = [
    pytest.param(
        Fault("partial_write", key=KEY, bytes_before_failure=8),
        _run_put,
        "unavailable",
        "put",
        "put",
        False,
        id="partial_write",
    ),
    pytest.param(
        Fault("delayed_visibility", key=KEY),
        _run_put_then_head,
        "missing",
        "found",
        "head",
        False,
        id="delayed_visibility",
    ),
    pytest.param(
        Fault("bit_flip", key=KEY, offset=5),
        _run_read,
        "corrupt",
        "clean",
        "get",
        True,
        id="bit_flip",
    ),
    pytest.param(
        Fault("truncated_read", key=KEY, bytes_before_failure=10),
        _run_read,
        "truncated:10",
        "clean",
        "get",
        True,
        id="truncated_read",
    ),
    pytest.param(
        Fault("server_error", key=KEY),
        _run_read,
        "unavailable",
        "clean",
        "get",
        True,
        id="server_error",
    ),
    pytest.param(
        Fault("unavailable", key=KEY),
        _run_read,
        "unavailable",
        "clean",
        "get",
        True,
        id="unavailable",
    ),
    pytest.param(
        Fault("throttled", key=KEY, retry_after=1.5),
        _run_read,
        "throttled:1.5",
        "clean",
        "get",
        True,
        id="throttled",
    ),
    pytest.param(
        Fault("slow_read", key=KEY, retry_after=0.25),
        _run_read,
        "clean",
        "clean",
        "get",
        True,
        id="slow_read",
    ),
    pytest.param(
        Fault("read_only", key=KEY),
        _run_delete,
        "unavailable",
        "deleted",
        "delete",
        True,
        id="read_only",
    ),
]


def test_the_injector_satisfies_the_object_store_protocol(tmp_path: Path) -> None:
    inner = _inner(tmp_path)
    store = FaultyStore(inner, FaultSchedule([]))
    assert isinstance(store, ObjectStore)
    assert store.capabilities == inner.capabilities


@pytest.mark.asyncio
@pytest.mark.parametrize(("fault", "runner", "faulted", "clean", "method", "seeded"), CASES)
async def test_a_scheduled_fault_fires_on_its_key_with_its_effect(
    tmp_path: Path,
    fault: Fault,
    runner: Runner,
    faulted: str,
    clean: str,
    method: str,
    seeded: bool,
) -> None:
    inner = _inner(tmp_path)
    if seeded:
        await _seed(inner, KEY, OTHER)
    store = FaultyStore(inner, FaultSchedule([fault]))

    assert await runner(store, KEY) == faulted

    assert [fired.kind for fired, _ in store.fired] == [fault.kind]
    fired_call = store.fired[0][1]
    assert (fired_call.method, fired_call.key) == (method, KEY)
    assert store.unfired() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("fault", "runner", "faulted", "clean", "method", "seeded"), CASES)
async def test_the_same_schedule_does_not_fire_on_a_different_key(
    tmp_path: Path,
    fault: Fault,
    runner: Runner,
    faulted: str,
    clean: str,
    method: str,
    seeded: bool,
) -> None:
    inner = _inner(tmp_path)
    if seeded:
        await _seed(inner, KEY, OTHER)
    store = FaultyStore(inner, FaultSchedule([fault]))

    assert await runner(store, OTHER) == clean

    assert store.fired == []
    assert store.unfired() == [fault]


@pytest.mark.asyncio
async def test_a_fault_waits_for_its_call_index_then_fires(tmp_path: Path) -> None:
    inner = _inner(tmp_path)
    await _seed(inner, KEY)
    fault = Fault("truncated_read", key=KEY, call_index=2, bytes_before_failure=4)
    store = FaultyStore(inner, FaultSchedule([fault]))

    reads = [await _run_read(store, KEY) for _ in range(4)]

    assert reads == ["clean", "clean", "truncated:4", "clean"]


@pytest.mark.asyncio
async def test_a_call_index_counts_only_the_method_the_fault_lands_on(tmp_path: Path) -> None:
    """A HEAD in between must not consume the GET the fault is aimed at."""
    inner = _inner(tmp_path)
    await _seed(inner, KEY)
    store = FaultyStore(inner, FaultSchedule([Fault("server_error", key=KEY, call_index=1)]))

    assert await _run_read(store, KEY) == "clean"
    assert await store.head(KEY) is not None
    assert await _run_read(store, KEY) == "unavailable"


@pytest.mark.asyncio
async def test_a_count_of_two_fires_twice_then_stops(tmp_path: Path) -> None:
    inner = _inner(tmp_path)
    await _seed(inner, KEY)
    store = FaultyStore(inner, FaultSchedule([Fault("server_error", key=KEY, count=2)]))

    reads = [await _run_read(store, KEY) for _ in range(3)]

    assert reads == ["unavailable", "unavailable", "clean"]
    assert len(store.fired) == 2
    assert store.unfired() == []


@pytest.mark.asyncio
async def test_a_prefix_addresses_every_key_beneath_it_and_nothing_else(tmp_path: Path) -> None:
    inner = _inner(tmp_path)
    await _seed(inner, KEY, OTHER)
    store = FaultyStore(
        inner, FaultSchedule([Fault("server_error", key_prefix="objects/ab/", count=5)])
    )

    assert await _run_read(store, KEY) == "unavailable"
    assert await _run_read(store, OTHER) == "clean"


@pytest.mark.asyncio
async def test_a_fault_free_schedule_is_byte_identical_to_the_wrapped_driver(
    tmp_path: Path,
) -> None:
    plain = _inner(tmp_path, "plain")
    faulty = FaultyStore(_inner(tmp_path, "wrapped"), FaultSchedule([]))

    async def exercise(store: ObjectStore) -> tuple[object, ...]:
        put = await store.put(KEY, _stream(DATA), size=len(DATA), checksum=_digest(DATA))
        body = await _drain(await store.get(KEY))
        window = await _drain(await store.get(KEY, range=(3, 11)))
        info = await store.head(KEY)
        missing = await store.head("objects/00/00/absent")
        handle = await store.multipart_create(OTHER, size=len(DATA))
        part = await store.multipart_put_part(
            handle, 1, _stream(DATA), size=len(DATA), checksum=_digest(DATA)
        )
        completed = await store.multipart_complete(handle, [part])
        joined = await _drain(await store.get(OTHER))
        listing = await store.list_prefix("objects/")
        return (
            put,
            body,
            window,
            info,
            missing,
            part.size,
            part.checksum,
            completed,
            joined,
            tuple(listing.keys),
        )

    assert await exercise(faulty) == await exercise(plain)
    assert _objects(tmp_path / "wrapped") == _objects(tmp_path / "plain")


@pytest.mark.asyncio
async def test_the_call_log_records_every_call_with_its_method_and_key_in_order(
    tmp_path: Path,
) -> None:
    store = FaultyStore(_inner(tmp_path), FaultSchedule([]))

    await store.put(KEY, _stream(DATA), size=len(DATA), checksum=_digest(DATA))
    await store.head(KEY)
    await _drain(await store.get(KEY))
    await store.move(KEY, OTHER)
    await store.list_prefix("objects/")
    handle = await store.multipart_create(KEY, size=len(DATA))
    part = await store.multipart_put_part(
        handle, 1, _stream(DATA), size=len(DATA), checksum=_digest(DATA)
    )
    await store.multipart_complete(handle, [part])
    await store.delete(KEY)

    assert [(call.method, call.key) for call in store.calls] == [
        ("put", KEY),
        ("head", KEY),
        ("get", KEY),
        ("move", KEY),
        ("list_prefix", "objects/"),
        ("multipart_create", KEY),
        ("multipart_put_part", KEY),
        ("multipart_complete", KEY),
        ("delete", KEY),
    ]
    assert all(isinstance(call, Call) for call in store.calls)
    assert store.calls[3].args["dst"] == OTHER
    assert store.calls[6].args["part_no"] == 1


@pytest.mark.asyncio
async def test_a_partial_write_leaves_no_object_behind_on_the_driver(tmp_path: Path) -> None:
    inner = _inner(tmp_path)
    store = FaultyStore(
        inner, FaultSchedule([Fault("partial_write", key=KEY, bytes_before_failure=8)])
    )

    with pytest.raises(Unavailable):
        await store.put(KEY, _stream(DATA), size=len(DATA), checksum=_digest(DATA))

    assert _objects(tmp_path / "root") == []
    assert await inner.head(KEY) is None


@pytest.mark.asyncio
async def test_the_same_put_without_the_fault_does_leave_an_object(tmp_path: Path) -> None:
    """The negative twin: the listing above is empty because nothing was written."""
    store = FaultyStore(_inner(tmp_path), FaultSchedule([]))

    await store.put(KEY, _stream(DATA), size=len(DATA), checksum=_digest(DATA))

    assert _objects(tmp_path / "root") == [KEY]


@pytest.mark.asyncio
async def test_unfired_reports_a_fault_whose_key_was_never_touched(tmp_path: Path) -> None:
    inner = _inner(tmp_path)
    await _seed(inner, KEY)
    never = Fault("server_error", key="objects/99/99/untouched")
    store = FaultyStore(inner, FaultSchedule([Fault("server_error", key=KEY), never]))

    assert await _run_read(store, KEY) == "unavailable"

    assert store.unfired() == [never]


@pytest.mark.asyncio
async def test_unfired_reports_a_fault_that_has_not_spent_its_full_count(tmp_path: Path) -> None:
    inner = _inner(tmp_path)
    await _seed(inner, KEY)
    fault = Fault("server_error", key=KEY, count=3)
    store = FaultyStore(inner, FaultSchedule([fault]))

    await _run_read(store, KEY)

    assert store.unfired() == [fault]


@pytest.mark.asyncio
async def test_a_slow_read_spends_the_injected_seam_and_not_real_time(tmp_path: Path) -> None:
    inner = _inner(tmp_path)
    await _seed(inner, KEY)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    store = FaultyStore(
        inner,
        FaultSchedule([Fault("slow_read", key=KEY, retry_after=30.0)]),
        sleep=sleep,
    )

    started = time.monotonic()
    body = await _drain(await store.get(KEY))
    elapsed = time.monotonic() - started

    assert body == DATA
    assert slept == [30.0]
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_read_only_refuses_every_mutating_method_and_still_serves_reads(
    tmp_path: Path,
) -> None:
    inner = _inner(tmp_path)
    await _seed(inner, KEY)
    handle = UploadHandle(key=KEY, upload_id="upload", size=len(DATA))
    store = FaultyStore(inner, FaultSchedule([Fault("read_only", key_prefix="objects/", count=9)]))

    for call in (
        store.put(KEY, _stream(DATA), size=len(DATA), checksum=_digest(DATA)),
        store.delete(KEY),
        store.move(KEY, OTHER),
        store.multipart_create(KEY, size=1),
        store.multipart_put_part(handle, 1, _stream(DATA), size=1, checksum=_digest(DATA)),
        store.multipart_complete(handle, []),
        store.multipart_abort(handle),
    ):
        with pytest.raises(Unavailable):
            await call

    assert await _drain(await store.get(KEY)) == DATA
    assert await inner.head(KEY) is not None


@pytest.mark.asyncio
async def test_a_bit_flip_without_an_offset_is_reproducible_from_the_seed(
    tmp_path: Path,
) -> None:
    async def read_with_seed(seed: int, name: str) -> bytes:
        inner = _inner(tmp_path, name)
        await _seed(inner, KEY)
        store = FaultyStore(inner, FaultSchedule([Fault("bit_flip", key=KEY)]), seed=seed)
        return await _drain(await store.get(KEY))

    first = await read_with_seed(7, "a")
    again = await read_with_seed(7, "b")

    assert first != DATA
    assert first == again
    assert sum(one != two for one, two in zip(first, DATA, strict=True)) == 1
