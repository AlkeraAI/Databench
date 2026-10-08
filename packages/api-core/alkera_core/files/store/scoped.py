"""Domain-bound store handles: mechanical tenant isolation.

A request path never holds a credential that can address more than one org.
It holds a :class:`DomainStore`, which takes and returns *domain-relative*
keys and prepends its own ``domains/<domain uuid>/`` prefix; the bucket-wide
:class:`~alkera_core.files.store.protocol.ObjectStore` is reachable only
through :meth:`ScopedStoreFactory.admin`, which the janitor, scrub, fsck,
backup and exit-drill code alone may call.

Isolation is done by the store's own access control wherever the store has
some — an STS session-tag role on AWS, a prefix-scoped application key on B2
or SeaweedFS — and by the in-process :class:`PrefixGuard` where it has none.
The guard is the weakest of the three, so a store that needs it also reports
``scoped_credentials=False`` and the production validator can refuse it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal, Protocol, TypeVar, runtime_checkable

from alkera_core.db.locking import io_boundary_class
from alkera_core.files.clock import Clock
from alkera_core.files.ids import DomainId
from alkera_core.files.store import keys
from alkera_core.files.store.errors import (
    AccessDenied,
    ExpiredCredentials,
    InvalidKey,
    StoreError,
    Unavailable,
)
from alkera_core.files.store.filesystem import FilesystemStore
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

DEFAULT_TTL: Final = timedelta(minutes=55)
"""How long a vended credential is asked for."""

DEFAULT_SKEW: Final = timedelta(minutes=5)
"""How far ahead of ``expires_at`` a cached handle is considered stale."""

T = TypeVar("T")

#: Re-exported so a caller catching a scoped-handle failure imports one module.
#: They live in ``errors`` because the drivers raise them: ``normalize_client_error``
#: maps ``ExpiredToken``/``InvalidToken`` onto :class:`ExpiredCredentials` and the
#: denial codes onto :class:`AccessDenied`, and a driver may not import this module.
__all__ = [
    "AccessDenied",
    "AwsScoped",
    "AwsStore",
    "DomainStore",
    "ExpiredCredentials",
    "FilesystemScoped",
    "PrefixGuard",
    "PrefixedDomainStore",
    "S3CompatConfig",
    "S3CompatScoped",
    "ScopedStoreFactory",
    "StoreAdmin",
]


@runtime_checkable
class DomainStore(Protocol):
    """The only store type a request path may hold.

    Keys are domain-relative (``objects/…``, ``incoming/…``); the handle
    prepends its own domain prefix and strips it off everything it returns.
    There is deliberately no ``list_prefix``: listing is the admin handle's
    job, and a listing is how a tenant-scoped bug becomes a tenant-wide one.
    """

    domain_id: DomainId
    capabilities: StoreCapabilities

    async def put(
        self,
        key: str,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
        if_absent: bool = True,
    ) -> PutResult: ...

    async def get(
        self, key: str, *, range: tuple[int, int] | None = None
    ) -> AsyncIterator[bytes]: ...

    async def head(self, key: str) -> ObjectInfo | None: ...

    async def delete(self, key: str) -> None: ...

    async def move(self, src: str, dst: str) -> None: ...

    async def multipart_create(self, key: str, *, size: int) -> UploadHandle: ...

    async def multipart_put_part(
        self,
        handle: UploadHandle,
        part_no: int,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
    ) -> PartResult: ...

    async def multipart_complete(
        self,
        handle: UploadHandle,
        parts: Sequence[PartResult],
        *,
        checksum: bytes | None = None,
    ) -> PutResult: ...

    async def multipart_abort(self, handle: UploadHandle) -> None: ...

    def presign_get(self, key: str, *, range: tuple[int, int] | None, ttl: timedelta) -> str: ...

    def presign_put_part(
        self, handle: UploadHandle, part_no: int, *, size: int, ttl: timedelta
    ) -> str: ...


@runtime_checkable
class StoreAdmin(ObjectStore, Protocol):
    """The bucket-wide handle plus the surface the janitor's sweepers need.

    ``incoming_orphans`` and ``store_multipart_aborts`` reap what no row owns,
    which they can only do if the driver will name its staged objects and its
    unfinished multipart uploads. Those four methods live here rather than on
    :class:`~alkera_core.files.store.protocol.ObjectStore` because a request
    path must never be able to enumerate a domain — the sweeper wires them by
    passing the handle :meth:`ScopedStoreFactory.admin` returns straight into
    ``SweepDeps(incoming=…, multipart=…)`` and ``Janitor(age_source=…)``: one
    object satisfies ``IncomingAdmin``, ``MultipartAdmin`` and
    ``ObjectAgeSource`` at once.

    Keys are in the handle's own namespace — absolute ``domains/<uuid>/…`` for
    a bucket-rooted handle, relative for a domain-rooted one — and a page
    round-trips into ``written_at`` and ``delete`` without translation.
    """

    async def list_incoming(
        self, *, after: str | None = None, limit: int = 1000
    ) -> tuple[Sequence[str], str | None]:
        """One keyset page of staged upload objects, and the next page's token."""
        ...

    async def written_at(self, key: str) -> datetime | None:
        """When the object under ``key`` was written, or ``None`` when unknown."""
        ...

    async def list_incomplete(self) -> Sequence[tuple[str, str, datetime]]:
        """``(upload_id, key, initiated_at)`` for every unfinished multipart upload."""
        ...

    async def abort(self, upload_id: str, key: str) -> None:
        """Abort one multipart upload; a missing one is already the goal state."""
        ...


@runtime_checkable
class ScopedStoreFactory(Protocol):
    """Where a domain-bound handle comes from."""

    async def for_domain(self, domain_id: DomainId) -> DomainStore:
        """A handle bound to ``domain_id``, cached until its credential goes stale."""
        ...

    def admin(self) -> StoreAdmin:
        """The bucket-wide store, with the sweepers' listing surface.

        Importable only by the janitor, scrub, fsck, backup and exit-drill
        code — an import-hygiene test enforces it.
        """
        ...


@io_boundary_class("object store, domain")
class PrefixedDomainStore:
    """A :class:`DomainStore` over a bucket-wide store, by key translation.

    Every relative key is validated before it is used, so a crafted key (``..``,
    absolute, an empty segment, a NUL, an uppercase byte, or one that already
    carries a ``domains/`` prefix) raises :class:`InvalidKey` before any call
    reaches the inner store.
    """

    def __init__(self, inner: ObjectStore, domain_id: DomainId) -> None:
        self._inner = inner
        self.domain_id = domain_id
        self.capabilities = inner.capabilities

    # -- key translation -------------------------------------------------

    def _to_inner(self, key: str) -> str:
        return keys.absolute(self.domain_id, key)

    def _from_inner(self, key: str) -> str:
        prefix = f"{keys.DOMAIN_PREFIX}{self.domain_id}/"
        if not key.startswith(prefix):
            raise InvalidKey(f"key {key!r} is outside domain {self.domain_id}")
        return key[len(prefix) :]

    def _handle_in(self, handle: UploadHandle) -> UploadHandle:
        return replace(handle, key=self._to_inner(handle.key))

    def _handle_out(self, handle: UploadHandle) -> UploadHandle:
        return replace(handle, key=self._from_inner(handle.key))

    # -- the DomainStore surface ----------------------------------------

    async def put(
        self,
        key: str,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
        if_absent: bool = True,
    ) -> PutResult:
        result = await self._inner.put(
            self._to_inner(key), data, size=size, checksum=checksum, if_absent=if_absent
        )
        return replace(result, key=self._from_inner(result.key))

    async def get(self, key: str, *, range: tuple[int, int] | None = None) -> AsyncIterator[bytes]:
        return await self._inner.get(self._to_inner(key), range=range)

    async def head(self, key: str) -> ObjectInfo | None:
        return await self._inner.head(self._to_inner(key))

    async def delete(self, key: str) -> None:
        await self._inner.delete(self._to_inner(key))

    async def move(self, src: str, dst: str) -> None:
        await self._inner.move(self._to_inner(src), self._to_inner(dst))

    async def multipart_create(self, key: str, *, size: int) -> UploadHandle:
        return self._handle_out(await self._inner.multipart_create(self._to_inner(key), size=size))

    async def multipart_put_part(
        self,
        handle: UploadHandle,
        part_no: int,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
    ) -> PartResult:
        return await self._inner.multipart_put_part(
            self._handle_in(handle), part_no, data, size=size, checksum=checksum
        )

    async def multipart_complete(
        self,
        handle: UploadHandle,
        parts: Sequence[PartResult],
        *,
        checksum: bytes | None = None,
    ) -> PutResult:
        result = await self._inner.multipart_complete(
            self._handle_in(handle), parts, checksum=checksum
        )
        return replace(result, key=self._from_inner(result.key))

    async def multipart_abort(self, handle: UploadHandle) -> None:
        await self._inner.multipart_abort(self._handle_in(handle))

    def presign_get(self, key: str, *, range: tuple[int, int] | None, ttl: timedelta) -> str:
        return self._inner.presign_get(self._to_inner(key), range=range, ttl=ttl)

    def presign_put_part(
        self, handle: UploadHandle, part_no: int, *, size: int, ttl: timedelta
    ) -> str:
        return self._inner.presign_put_part(self._handle_in(handle), part_no, size=size, ttl=ttl)


class _RootedDomainStore(PrefixedDomainStore):
    """A handle whose inner store is already rooted at the domain's directory.

    The keys the driver sees are the relative ones; the domain boundary is the
    per-domain root the driver was opened on, not a string prefix. The relative
    key is still validated here, so a crafted key raises before the driver is
    called at all.
    """

    def _to_inner(self, key: str) -> str:
        keys.validate_relative_key(key)
        return key

    def _from_inner(self, key: str) -> str:
        return key


class PrefixGuard:
    """An :class:`ObjectStore` wrapper that refuses keys outside one prefix.

    The last-resort isolation for a store that vends no scoped credential: the
    refusal happens in this process, before the request is signed or sent, so a
    service bug cannot reach another domain's prefix even though the credential
    could.
    """

    def __init__(self, inner: ObjectStore, prefix: str) -> None:
        if not prefix.endswith("/"):
            raise ValueError(f"a guard prefix must end with '/' (got {prefix!r})")
        self._inner = inner
        self.prefix = prefix
        self.capabilities = replace(inner.capabilities, scoped_credentials=False)

    def _check(self, key: str) -> str:
        if not key.startswith(self.prefix) or "/../" in key or key.endswith("/.."):
            raise InvalidKey(f"key {key!r} is outside the guarded prefix {self.prefix!r}")
        return key

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
        return await self._inner.put(
            self._check(key),
            data,
            size=size,
            checksum=checksum,
            if_absent=if_absent,
            storage_class=storage_class,
        )

    async def get(self, key: str, *, range: tuple[int, int] | None = None) -> AsyncIterator[bytes]:
        return await self._inner.get(self._check(key), range=range)

    async def head(self, key: str) -> ObjectInfo | None:
        return await self._inner.head(self._check(key))

    async def delete(self, key: str) -> None:
        await self._inner.delete(self._check(key))

    async def move(self, src: str, dst: str) -> None:
        await self._inner.move(self._check(src), self._check(dst))

    async def list_prefix(
        self, prefix: str, *, after: str | None = None, limit: int = 1000
    ) -> ListPage:
        return await self._inner.list_prefix(self._check(prefix), after=after, limit=limit)

    async def multipart_create(self, key: str, *, size: int) -> UploadHandle:
        return await self._inner.multipart_create(self._check(key), size=size)

    async def multipart_put_part(
        self,
        handle: UploadHandle,
        part_no: int,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
    ) -> PartResult:
        self._check(handle.key)
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
        self._check(handle.key)
        return await self._inner.multipart_complete(handle, parts, checksum=checksum)

    async def multipart_abort(self, handle: UploadHandle) -> None:
        self._check(handle.key)
        await self._inner.multipart_abort(handle)

    def presign_get(self, key: str, *, range: tuple[int, int] | None, ttl: timedelta) -> str:
        return self._inner.presign_get(self._check(key), range=range, ttl=ttl)

    def presign_put_part(
        self, handle: UploadHandle, part_no: int, *, size: int, ttl: timedelta
    ) -> str:
        self._check(handle.key)
        return self._inner.presign_put_part(handle, part_no, size=size, ttl=ttl)

    async def vend_scoped_credentials(
        self, prefix: str, *, ttl: timedelta, read_only: bool
    ) -> ScopedCredentials:
        raise NotImplementedError("a guarded store vends no credentials")


class FilesystemScoped:
    """One :class:`FilesystemStore` per domain, rooted at ``root/domains/<uuid>``.

    The per-domain root *is* the boundary: the driver resolves every path
    beneath it, so a symlinked segment or a ``..`` cannot leave the domain
    directory even if the key survived validation.
    """

    def __init__(self, root: Path, *, clock: Clock) -> None:
        self._root = Path(root)
        self._clock = clock
        self._handles: dict[DomainId, DomainStore] = {}

    async def for_domain(self, domain_id: DomainId) -> DomainStore:
        cached = self._handles.get(domain_id)
        if cached is not None:
            return cached
        domain_root = self._root / keys.DOMAIN_PREFIX.rstrip("/") / str(domain_id)
        handle = _RootedDomainStore(FilesystemStore(domain_root, clock=self._clock.now), domain_id)
        self._handles[domain_id] = handle
        return handle

    def admin(self) -> StoreAdmin:
        """The bucket-wide handle the janitor, scrub and fsck run on.

        It is rooted above every domain directory, so the keys it takes are
        the absolute ``domains/<uuid>/…`` ones ``keys.absolute`` spells — the
        relative namespace belongs to a domain handle and refuses them.
        """
        return FilesystemStore(self._root, clock=self._clock.now, layout="bucket")


@dataclass(frozen=True)
class VendedClient:
    """A per-domain client built from a store-vended, prefix-scoped credential."""

    store: ObjectStore
    expires_at: datetime


ClientVendor = Callable[[DomainId, str], Awaitable[VendedClient]]
"""Vends a client for one domain, scoped to the prefix it is handed."""


@dataclass(frozen=True)
class S3CompatConfig:
    """What the S3-compatible factory needs: the bucket-wide client, and how to scope it."""

    store: StoreAdmin
    ttl: timedelta = DEFAULT_TTL
    skew: timedelta = DEFAULT_SKEW


class S3CompatScoped:
    """Per-domain application keys where the store has them; ``PrefixGuard`` otherwise.

    B2 and SeaweedFS IAM can mint a key whose name prefix is the domain's, and
    then isolation is the store's. Everything else gets the in-process guard,
    and the handle's capability record says ``scoped_credentials=False`` so the
    production validator can refuse the combination.
    """

    def __init__(
        self,
        config: S3CompatConfig,
        *,
        clock: Clock,
        credential_vendor: ClientVendor | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._vendor = credential_vendor
        self._leases: dict[DomainId, tuple[DomainStore, datetime | None]] = {}

    async def for_domain(self, domain_id: DomainId) -> DomainStore:
        lease = self._leases.get(domain_id)
        if lease is not None:
            cached, expires_at = lease
            if expires_at is None or self._clock.now() < expires_at - self._config.skew:
                return cached
        prefix = f"{keys.DOMAIN_PREFIX}{domain_id}/"
        if self._vendor is None:
            guarded = PrefixGuard(self._config.store, prefix)
            unscoped = PrefixedDomainStore(guarded, domain_id)
            self._leases[domain_id] = (unscoped, None)
            return unscoped
        try:
            vended = await self._vendor(domain_id, prefix)
        except StoreError as exc:
            self._leases.pop(domain_id, None)
            raise Unavailable(f"could not scope a client to domain {domain_id}: {exc}") from exc
        handle = PrefixedDomainStore(vended.store, domain_id)
        self._leases[domain_id] = (handle, vended.expires_at)
        return handle

    def admin(self) -> StoreAdmin:
        return self._config.store


class AwsStore(StoreAdmin, Protocol):
    """The AWS driver's extra surface: it can turn a credential into a client."""

    def client_for(self, credentials: ScopedCredentials) -> ObjectStore: ...


class AwsScoped:
    """STS ``AssumeRole`` with a ``domainId`` session tag, one session per domain.

    The assumed role's policy names
    ``arn:aws:s3:::<bucket>/domains/${aws:PrincipalTag/domainId}/*``, so the
    credential itself cannot address another domain. Sessions are cached until
    ``expires_at - skew``; an expired session is re-vended once and the call
    retried once; a revoked role fails closed with :class:`Unavailable` and
    never falls back to the admin handle.
    """

    def __init__(
        self,
        store: AwsStore,
        *,
        clock: Clock,
        ttl: timedelta = DEFAULT_TTL,
        skew: timedelta = DEFAULT_SKEW,
    ) -> None:
        self._store = store
        self._clock = clock
        self._ttl = ttl
        self._skew = skew
        self._sessions: dict[DomainId, tuple[PrefixedDomainStore, datetime]] = {}

    async def for_domain(self, domain_id: DomainId) -> DomainStore:
        return _RefreshingDomainStore(self, domain_id, await self.session(domain_id))

    def admin(self) -> StoreAdmin:
        return self._store

    async def session(self, domain_id: DomainId, *, force: bool = False) -> PrefixedDomainStore:
        """The cached per-domain session, re-vended when stale or when forced."""
        cached = self._sessions.get(domain_id)
        if cached is not None and not force:
            handle, expires_at = cached
            if self._clock.now() < expires_at - self._skew:
                return handle
        prefix = f"{keys.DOMAIN_PREFIX}{domain_id}/"
        try:
            credentials = await self._store.vend_scoped_credentials(
                prefix, ttl=self._ttl, read_only=False
            )
        except StoreError as exc:
            self._sessions.pop(domain_id, None)
            raise Unavailable(f"could not assume a role for domain {domain_id}: {exc}") from exc
        handle = PrefixedDomainStore(self._store.client_for(credentials), domain_id)
        self._sessions[domain_id] = (handle, credentials.expires_at)
        return handle


class _RefreshingDomainStore:
    """A handle that re-vends its credential once when the store says it expired."""

    def __init__(self, factory: AwsScoped, domain_id: DomainId, inner: PrefixedDomainStore) -> None:
        self._factory = factory
        self._inner = inner
        self.domain_id = domain_id
        self.capabilities = inner.capabilities

    async def _run(self, op: Callable[[PrefixedDomainStore], Awaitable[T]]) -> T:
        """Run ``op``, re-vending and re-driving it once if the session expired.

        Only for operations whose arguments can be sent twice. A body is an
        ``AsyncIterator``: by the time the store says the credential expired,
        the driver has already drained it, so a second call would upload the
        empty remainder of a consumed stream under the caller's checksum.
        """
        try:
            return await op(self._inner)
        except ExpiredCredentials:
            self._inner = await self._factory.session(self.domain_id, force=True)
            self.capabilities = self._inner.capabilities
            return await op(self._inner)

    async def _run_streaming(self, op: Callable[[PrefixedDomainStore], Awaitable[T]]) -> T:
        """Run a body-carrying operation exactly once, refreshing nothing.

        :class:`ExpiredCredentials` reaches the caller instead: the content
        layer holds the upload session and can re-drive the transfer from its
        own source, which is the only place the bytes still exist.
        """
        return await op(self._inner)

    async def put(
        self,
        key: str,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
        if_absent: bool = True,
    ) -> PutResult:
        return await self._run_streaming(
            lambda s: s.put(key, data, size=size, checksum=checksum, if_absent=if_absent)
        )

    async def get(self, key: str, *, range: tuple[int, int] | None = None) -> AsyncIterator[bytes]:
        return await self._run(lambda s: s.get(key, range=range))

    async def head(self, key: str) -> ObjectInfo | None:
        return await self._run(lambda s: s.head(key))

    async def delete(self, key: str) -> None:
        await self._run(lambda s: s.delete(key))

    async def move(self, src: str, dst: str) -> None:
        await self._run(lambda s: s.move(src, dst))

    async def multipart_create(self, key: str, *, size: int) -> UploadHandle:
        return await self._run(lambda s: s.multipart_create(key, size=size))

    async def multipart_put_part(
        self,
        handle: UploadHandle,
        part_no: int,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
    ) -> PartResult:
        return await self._run_streaming(
            lambda s: s.multipart_put_part(handle, part_no, data, size=size, checksum=checksum)
        )

    async def multipart_complete(
        self,
        handle: UploadHandle,
        parts: Sequence[PartResult],
        *,
        checksum: bytes | None = None,
    ) -> PutResult:
        return await self._run(lambda s: s.multipart_complete(handle, parts, checksum=checksum))

    async def multipart_abort(self, handle: UploadHandle) -> None:
        await self._run(lambda s: s.multipart_abort(handle))

    def presign_get(self, key: str, *, range: tuple[int, int] | None, ttl: timedelta) -> str:
        return self._inner.presign_get(key, range=range, ttl=ttl)

    def presign_put_part(
        self, handle: UploadHandle, part_no: int, *, size: int, ttl: timedelta
    ) -> str:
        return self._inner.presign_put_part(handle, part_no, size=size, ttl=ttl)
