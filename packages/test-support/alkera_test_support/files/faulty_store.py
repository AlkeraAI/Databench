"""A deterministic fault injector that wraps any object-store driver.

Every degradation the layers above must survive — a write that dies half way,
an object that is not visible yet, a bit that rotted on the wire, a 5xx, a 429,
a store that has gone read-only — is reproduced here as a *scheduled* fault
rather than a mock that returns an error. A fault names the method it lands on,
the key (or key prefix) it addresses, which matching call it waits for, and how
many times it fires; the schedule is spent in order, and :meth:`FaultyStore.unfired`
reports whatever the test never reached, so "the schedule was consumed" is
itself assertable.

The wrapper satisfies the same :class:`~alkera_core.files.store.protocol.ObjectStore`
protocol as the driver it wraps, so nothing above Layer 1 knows it is there, and
an empty schedule is byte-for-byte the wrapped driver. Two seams keep runs
deterministic: ``sleep`` (a ``slow_read`` never spends real time) and ``seed``
(the bit a ``bit_flip`` picks when no offset is given).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final, Literal

from alkera_core.files.store.errors import Throttled, Unavailable
from alkera_core.files.store.protocol import (
    ListPage,
    ObjectInfo,
    ObjectStore,
    PartResult,
    PutResult,
    ScopedCredentials,
    StoreCapabilities,
    UploadHandle,
)

FaultKind = Literal[
    "partial_write",
    "delayed_visibility",
    "bit_flip",
    "truncated_read",
    "server_error",
    "throttled",
    "slow_read",
    "unavailable",
    "read_only",
]

_READS: Final = ("get",)
_WRITES: Final = (
    "put",
    "delete",
    "move",
    "multipart_create",
    "multipart_put_part",
    "multipart_complete",
    "multipart_abort",
)
_EVERY_METHOD: Final = (*_WRITES, "get", "head", "list_prefix")

#: Which methods a kind can land on. A kind that models a whole-store condition
#: (a 5xx, a 429, an outage) addresses every method; the rest are specific.
KIND_METHODS: Final[Mapping[FaultKind, tuple[str, ...]]] = {
    "partial_write": ("put",),
    "delayed_visibility": ("head",),
    "bit_flip": _READS,
    "truncated_read": _READS,
    "slow_read": _READS,
    "server_error": _EVERY_METHOD,
    "unavailable": _EVERY_METHOD,
    "throttled": _EVERY_METHOD,
    "read_only": _WRITES,
}


@dataclass(frozen=True)
class Fault:
    """One scheduled failure, addressed to a method, a key and a call number.

    ``key`` matches exactly, ``key_prefix`` matches every key beneath it, and
    neither matches every key. ``call_index`` is counted per method, so a HEAD
    in between two GETs cannot consume the GET a read fault is aimed at.

    ``bytes_before_failure`` is how much of a stream survives (``partial_write``,
    ``truncated_read``); ``offset`` is the byte a ``bit_flip`` rots; and
    ``retry_after`` carries the seconds a ``Throttled`` asks for, or — for
    ``slow_read`` — the delay handed to the injected sleep before each chunk.
    """

    kind: FaultKind
    key: str | None = None
    key_prefix: str | None = None
    call_index: int = 0
    count: int = 1
    offset: int | None = None
    bytes_before_failure: int | None = None
    retry_after: float | None = None

    def __post_init__(self) -> None:
        if self.count < 1:
            msg = f"a fault must fire at least once (count={self.count})"
            raise ValueError(msg)
        if self.call_index < 0:
            msg = f"a call index cannot be negative (call_index={self.call_index})"
            raise ValueError(msg)
        if self.key is not None and self.key_prefix is not None:
            msg = "a fault addresses a key or a key prefix, never both"
            raise ValueError(msg)

    def addresses(self, key: str) -> bool:
        """Whether this fault is aimed at ``key``."""
        if self.key is not None:
            return key == self.key
        if self.key_prefix is not None:
            return key.startswith(self.key_prefix)
        return True


@dataclass(frozen=True)
class FaultSchedule:
    """The faults a store will inject, spent in order."""

    faults: Sequence[Fault]


@dataclass(frozen=True)
class Call:
    """One call the driver saw, in the order it saw it."""

    method: str
    key: str
    args: Mapping[str, object] = field(default_factory=dict)


Sleep = Callable[[float], Awaitable[None]]


class FaultyStore:
    """An :class:`ObjectStore` that wraps another and injects a fault schedule."""

    def __init__(
        self,
        inner: ObjectStore,
        schedule: FaultSchedule,
        *,
        sleep: Sleep | None = None,
        seed: int = 0,
    ) -> None:
        self._inner = inner
        self._faults = list(schedule.faults)
        self._sleep: Sleep = sleep if sleep is not None else asyncio.sleep
        self._random = random.Random(seed)  # noqa: S311 — reproducibility, not secrecy
        self._spent = [0] * len(self._faults)
        self._seen: dict[tuple[int, str], int] = {}
        self._armed: dict[int, bool] = {}
        self.calls: list[Call] = []
        self.fired: list[tuple[Fault, Call]] = []
        self.capabilities: StoreCapabilities = inner.capabilities

    # -- schedule ---------------------------------------------------------

    def unfired(self) -> list[Fault]:
        """Every fault the run never spent in full — an unconsumed schedule."""
        return [
            fault for index, fault in enumerate(self._faults) if self._spent[index] < fault.count
        ]

    def _record(self, method: str, key: str, **args: object) -> Call:
        call = Call(method=method, key=key, args=args)
        self.calls.append(call)
        return call

    def _select(self, call: Call) -> Fault | None:
        """The first unspent fault this call trips, if any."""
        for index, fault in enumerate(self._faults):
            if self._spent[index] >= fault.count:
                continue
            if call.method not in KIND_METHODS[fault.kind]:
                continue
            if not fault.addresses(call.key):
                continue
            if fault.kind == "delayed_visibility" and not self._armed.get(index, False):
                continue
            seen = self._seen.get((index, call.method), 0)
            self._seen[(index, call.method)] = seen + 1
            if seen < fault.call_index:
                continue
            self._spent[index] += 1
            self.fired.append((fault, call))
            return fault
        return None

    def _arm(self, key: str) -> None:
        """A completed write makes a delayed-visibility fault on that key eligible."""
        for index, fault in enumerate(self._faults):
            if fault.kind == "delayed_visibility" and fault.addresses(key):
                self._armed[index] = True

    def _raise(self, fault: Fault) -> None:
        if fault.kind == "throttled":
            raise Throttled("the store asked us to slow down", retry_after=fault.retry_after)
        raise Unavailable(f"injected {fault.kind}")

    # -- writes -----------------------------------------------------------

    async def put(
        self,
        key: str,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
        if_absent: bool = True,
        storage_class: Literal["hot", "cold"] | None = None,
    ) -> PutResult:
        call = self._record("put", key, size=size, if_absent=if_absent, storage_class=storage_class)
        fault = self._select(call)
        if fault is not None:
            if fault.kind != "partial_write":
                self._raise(fault)
            data = _cut_short(data, fault.bytes_before_failure or 0)
        result = await self._inner.put(
            key,
            data,
            size=size,
            checksum=checksum,
            if_absent=if_absent,
            storage_class=storage_class,
        )
        self._arm(key)
        return result

    async def delete(self, key: str) -> None:
        fault = self._select(self._record("delete", key))
        if fault is not None:
            self._raise(fault)
        await self._inner.delete(key)

    async def move(self, src: str, dst: str) -> None:
        fault = self._select(self._record("move", src, dst=dst))
        if fault is not None:
            self._raise(fault)
        await self._inner.move(src, dst)
        self._arm(dst)

    # -- reads ------------------------------------------------------------

    async def get(self, key: str, *, range: tuple[int, int] | None = None) -> AsyncIterator[bytes]:
        fault = self._select(self._record("get", key, range=range))
        if fault is not None and fault.kind not in ("bit_flip", "truncated_read", "slow_read"):
            self._raise(fault)
        chunks = await self._inner.get(key, range=range)
        if fault is None:
            return chunks
        if fault.kind == "bit_flip":
            return self._flip_a_bit(chunks, fault)
        if fault.kind == "truncated_read":
            return _cut_short(chunks, fault.bytes_before_failure or 0, then_raise=False)
        return self._read_slowly(chunks, fault)

    async def head(self, key: str) -> ObjectInfo | None:
        fault = self._select(self._record("head", key))
        if fault is not None:
            if fault.kind == "delayed_visibility":
                return None
            self._raise(fault)
        return await self._inner.head(key)

    async def list_prefix(
        self, prefix: str, *, after: str | None = None, limit: int = 1000
    ) -> ListPage:
        fault = self._select(self._record("list_prefix", prefix, after=after, limit=limit))
        if fault is not None:
            self._raise(fault)
        return await self._inner.list_prefix(prefix, after=after, limit=limit)

    # -- multipart --------------------------------------------------------

    async def multipart_create(self, key: str, *, size: int) -> UploadHandle:
        fault = self._select(self._record("multipart_create", key, size=size))
        if fault is not None:
            self._raise(fault)
        return await self._inner.multipart_create(key, size=size)

    async def multipart_put_part(
        self,
        handle: UploadHandle,
        part_no: int,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
    ) -> PartResult:
        call = self._record("multipart_put_part", handle.key, part_no=part_no, size=size)
        fault = self._select(call)
        if fault is not None:
            if fault.kind != "partial_write":
                self._raise(fault)
            data = _cut_short(data, fault.bytes_before_failure or 0)
        return await self._inner.multipart_put_part(
            handle, part_no, data, size=size, checksum=checksum
        )

    async def multipart_complete(
        self,
        handle: UploadHandle,
        parts: Sequence[PartResult],
        *,
        checksum: bytes | None = None,
    ) -> PutResult:
        call = self._record("multipart_complete", handle.key, parts=len(parts))
        fault = self._select(call)
        if fault is not None:
            self._raise(fault)
        result = await self._inner.multipart_complete(handle, parts, checksum=checksum)
        self._arm(handle.key)
        return result

    async def multipart_abort(self, handle: UploadHandle) -> None:
        fault = self._select(self._record("multipart_abort", handle.key))
        if fault is not None:
            self._raise(fault)
        await self._inner.multipart_abort(handle)

    # -- passed straight through -----------------------------------------

    def presign_get(self, key: str, *, range: tuple[int, int] | None, ttl: timedelta) -> str:
        return self._inner.presign_get(key, range=range, ttl=ttl)

    def presign_put_part(
        self, handle: UploadHandle, part_no: int, *, size: int, ttl: timedelta
    ) -> str:
        return self._inner.presign_put_part(handle, part_no, size=size, ttl=ttl)

    async def vend_scoped_credentials(
        self, prefix: str, *, ttl: timedelta, read_only: bool
    ) -> ScopedCredentials:
        return await self._inner.vend_scoped_credentials(prefix, ttl=ttl, read_only=read_only)

    # -- stream corruption ------------------------------------------------

    async def _flip_a_bit(self, source: AsyncIterator[bytes], fault: Fault) -> AsyncIterator[bytes]:
        bit = self._random.randrange(8)
        target = fault.offset
        position = 0
        flipped = False
        async for chunk in source:
            if not flipped and chunk:
                if target is None:
                    target = position + self._random.randrange(len(chunk))
                if position <= target < position + len(chunk):
                    rotted = bytearray(chunk)
                    rotted[target - position] ^= 1 << bit
                    chunk = bytes(rotted)
                    flipped = True
            position += len(chunk)
            yield chunk

    async def _read_slowly(
        self, source: AsyncIterator[bytes], fault: Fault
    ) -> AsyncIterator[bytes]:
        delay = fault.retry_after if fault.retry_after is not None else 0.0
        async for chunk in source:
            await self._sleep(delay)
            yield chunk


async def _cut_short(
    source: AsyncIterator[bytes], limit: int, *, then_raise: bool = True
) -> AsyncIterator[bytes]:
    """Forward at most ``limit`` bytes, then die (a write) or simply stop (a read)."""
    sent = 0
    async for chunk in source:
        if sent + len(chunk) >= limit:
            head = chunk[: limit - sent]
            if head:
                yield head
            if then_raise:
                raise Unavailable(f"the connection died after {limit} bytes")
            return
        sent += len(chunk)
        yield chunk
    if then_raise:
        raise Unavailable(f"the connection died after {sent} bytes")
