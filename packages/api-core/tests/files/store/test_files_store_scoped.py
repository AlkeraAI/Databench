"""Domain-bound handles: a key can only ever name the domain that asked."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import UUID

import pytest
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import DomainId
from alkera_core.files.store import keys
from alkera_core.files.store.aws import AwsStore
from alkera_core.files.store.errors import InvalidKey, NotFound, StoreError, Unavailable
from alkera_core.files.store.protocol import (
    ListPage,
    ObjectInfo,
    PartResult,
    PutResult,
    ScopedCredentials,
    StoreCapabilities,
    UploadHandle,
)
from alkera_core.files.store.s3_compatible import S3Config
from alkera_core.files.store.scoped import (
    AccessDenied,
    AwsScoped,
    DomainStore,
    ExpiredCredentials,
    FilesystemScoped,
    PrefixedDomainStore,
    PrefixGuard,
    S3CompatConfig,
    S3CompatScoped,
    VendedClient,
)
from blake3 import blake3
from botocore.exceptions import ClientError

DOMAIN_A: Final = DomainId(UUID("11111111-1111-4111-8111-111111111111"))
DOMAIN_B: Final = DomainId(UUID("22222222-2222-4222-8222-222222222222"))

CAPABILITIES: Final = StoreCapabilities(
    conditional_write=True,
    presigned=True,
    range_signing=True,
    versioning=False,
    object_lock=False,
    lifecycle=False,
    scoped_credentials=True,
    storage_classes=False,
    strong_read_after_write=True,
    kms=False,
    max_object_bytes=1 << 40,
    min_part_bytes=1 << 20,
    max_part_bytes=1 << 30,
    max_parts=10_000,
)


async def _stream(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


class RecordingStore:
    """A store that records the keys it is asked for and never touches bytes.

    Its call log is the evidence for "the refusal happened before the request
    left the process": a guarded or validated key must leave the log empty.
    """

    def __init__(self, *, expire_first: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.capabilities = CAPABILITIES
        self._expire_first = expire_first

    def _record(self, op: str, key: str) -> None:
        if self._expire_first:
            self._expire_first = False
            raise ExpiredCredentials("the session token expired")
        self.calls.append((op, key))

    async def put(
        self,
        key: str,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
        if_absent: bool = True,
        storage_class: str | None = None,
    ) -> PutResult:
        self._record("put", key)
        return PutResult(key=key, size=size, checksum=checksum, etag=None)

    async def get(self, key: str, *, range: tuple[int, int] | None = None) -> AsyncIterator[bytes]:
        self._record("get", key)
        return _stream(b"")

    async def head(self, key: str) -> ObjectInfo | None:
        self._record("head", key)
        return ObjectInfo(size=0, checksum=None, etag=None, storage_class=None)

    async def delete(self, key: str) -> None:
        self._record("delete", key)

    async def move(self, src: str, dst: str) -> None:
        self._record("move", f"{src}->{dst}")

    async def list_prefix(
        self, prefix: str, *, after: str | None = None, limit: int = 1000
    ) -> ListPage:
        self._record("list_prefix", prefix)
        return ListPage(keys=(), next_after=None)

    async def multipart_create(self, key: str, *, size: int) -> UploadHandle:
        self._record("multipart_create", key)
        return UploadHandle(key=key, upload_id="upload-1", size=size)

    async def multipart_put_part(
        self,
        handle: UploadHandle,
        part_no: int,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
    ) -> PartResult:
        self._record("multipart_put_part", handle.key)
        return PartResult(part_no=part_no, size=size, checksum=checksum, etag=None)

    async def multipart_complete(
        self, handle: UploadHandle, parts: Sequence[PartResult]
    ) -> PutResult:
        self._record("multipart_complete", handle.key)
        return PutResult(key=handle.key, size=handle.size, checksum=b"", etag=None)

    async def multipart_abort(self, handle: UploadHandle) -> None:
        self._record("multipart_abort", handle.key)

    def presign_get(self, key: str, *, range: tuple[int, int] | None, ttl: timedelta) -> str:
        self._record("presign_get", key)
        return f"https://example.invalid/{key}"

    def presign_put_part(
        self, handle: UploadHandle, part_no: int, *, size: int, ttl: timedelta
    ) -> str:
        self._record("presign_put_part", handle.key)
        return f"https://example.invalid/{handle.key}/{part_no}"

    async def vend_scoped_credentials(
        self, prefix: str, *, ttl: timedelta, read_only: bool
    ) -> ScopedCredentials:
        raise NotImplementedError


def _tree(root: Path) -> set[str]:
    """Every file under ``root``, relative and sorted — the disk's own account."""
    return {
        os.path.relpath(os.path.join(dirpath, name), root)
        for dirpath, _dirs, names in os.walk(root)
        for name in names
    }


CRAFTED_KEYS: Final = [
    pytest.param("../x", id="parent-escape"),
    pytest.param("objects/../../x", id="parent-escape-mid-key"),
    pytest.param("/x", id="absolute"),
    pytest.param(f"{keys.DOMAIN_PREFIX}{DOMAIN_B}/objects/aa/bb/cc", id="other-domain-prefix"),
    pytest.param(f"{keys.DOMAIN_PREFIX}{DOMAIN_A}/objects/aa/bb/cc", id="own-domain-prefix"),
    pytest.param("objects/aa\x00bb", id="nul-byte"),
    pytest.param("objects//aa", id="empty-segment"),
    pytest.param("Objects/aa", id="uppercase"),
]


@pytest.mark.parametrize("crafted", CRAFTED_KEYS)
async def test_crafted_key_raises_before_the_filesystem_driver_is_reached(
    tmp_path: Path, clock: FakeClock, crafted: str
) -> None:
    factory = FilesystemScoped(tmp_path, clock=clock)
    handle = await factory.for_domain(DOMAIN_A)
    await handle.put(
        "objects/aa/bb/good", _stream(b"kept"), size=4, checksum=blake3(b"kept").digest()
    )
    before = _tree(tmp_path)

    for attempt in (
        lambda: handle.head(crafted),
        lambda: handle.delete(crafted),
        lambda: handle.get(crafted),
        lambda: handle.move("objects/aa/bb/good", crafted),
    ):
        with pytest.raises(InvalidKey):
            await attempt()

    assert _tree(tmp_path) == before


async def test_a_symlinked_segment_cannot_carry_a_write_out_of_the_domain_root(
    tmp_path: Path, clock: FakeClock
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"other tenant")
    factory = FilesystemScoped(tmp_path, clock=clock)
    handle = await factory.for_domain(DOMAIN_A)
    domain_root = tmp_path / "domains" / str(DOMAIN_A)
    domain_root.mkdir(parents=True, exist_ok=True)
    (domain_root / "objects").symlink_to(outside, target_is_directory=True)

    with pytest.raises(InvalidKey):
        await handle.put(
            "objects/secret",
            _stream(b"overwritten"),
            size=11,
            checksum=blake3(b"overwritten").digest(),
        )

    assert (outside / "secret").read_bytes() == b"other tenant"


async def test_one_domains_handle_cannot_reach_another_domains_bytes(
    tmp_path: Path, clock: FakeClock
) -> None:
    factory = FilesystemScoped(tmp_path, clock=clock)
    a = await factory.for_domain(DOMAIN_A)
    b = await factory.for_domain(DOMAIN_B)
    key = "objects/aa/bb/shared-name"
    await b.put(key, _stream(b"B bytes"), size=7, checksum=blake3(b"B bytes").digest())

    assert await a.head(key) is None
    with pytest.raises(NotFound):
        await a.get(key)
    await a.delete(key)
    with pytest.raises(NotFound):
        await a.move(key, "objects/aa/bb/stolen")
    absolute = f"{keys.DOMAIN_PREFIX}{DOMAIN_B}/{key}"
    with pytest.raises(InvalidKey):
        await a.head(absolute)

    stream = await b.get(key)
    assert b"".join([chunk async for chunk in stream]) == b"B bytes"


async def test_a_relative_key_round_trips_through_the_domain_prefix() -> None:
    inner = RecordingStore()
    handle = PrefixedDomainStore(inner, DOMAIN_A)

    result = await handle.put(
        "objects/aa/bb/cc", _stream(b"hi"), size=2, checksum=blake3(b"hi").digest()
    )
    upload = await handle.multipart_create("incoming/session/object", size=8)

    assert inner.calls[0] == ("put", f"{keys.DOMAIN_PREFIX}{DOMAIN_A}/objects/aa/bb/cc")
    assert result.key == "objects/aa/bb/cc"
    assert upload.key == "incoming/session/object"
    await handle.multipart_abort(upload)
    assert inner.calls[-1] == (
        "multipart_abort",
        f"{keys.DOMAIN_PREFIX}{DOMAIN_A}/incoming/session/object",
    )


async def test_prefix_guard_refuses_a_foreign_key_before_the_request_leaves_the_process() -> None:
    inner = RecordingStore()
    guard = PrefixGuard(inner, f"{keys.DOMAIN_PREFIX}{DOMAIN_A}/")

    with pytest.raises(InvalidKey):
        await guard.head(f"{keys.DOMAIN_PREFIX}{DOMAIN_B}/objects/aa/bb/cc")
    with pytest.raises(InvalidKey):
        await guard.list_prefix("domains/")
    with pytest.raises(InvalidKey):
        guard.presign_get(
            f"{keys.DOMAIN_PREFIX}{DOMAIN_A}/../{DOMAIN_B}/x", range=None, ttl=timedelta(minutes=1)
        )
    assert inner.calls == []

    await guard.head(f"{keys.DOMAIN_PREFIX}{DOMAIN_A}/objects/aa/bb/cc")
    assert inner.calls == [("head", f"{keys.DOMAIN_PREFIX}{DOMAIN_A}/objects/aa/bb/cc")]


async def test_a_guarded_s3_handle_reports_that_its_credential_is_not_scoped(
    clock: FakeClock,
) -> None:
    inner = RecordingStore()
    factory = S3CompatScoped(S3CompatConfig(store=inner), clock=clock)

    handle = await factory.for_domain(DOMAIN_A)

    assert handle.capabilities.scoped_credentials is False
    assert factory.admin().capabilities.scoped_credentials is True
    with pytest.raises(InvalidKey):
        await handle.head(f"{keys.DOMAIN_PREFIX}{DOMAIN_B}/objects/aa/bb/cc")
    assert inner.calls == []


class FakeAwsStore(RecordingStore):
    """An AWS driver whose ``AssumeRole`` is a counter and a scripted outcome."""

    def __init__(self, *, deny: bool = False, expire_first_client: bool = False) -> None:
        super().__init__()
        self.vended: list[str] = []
        self.clients: list[RecordingStore] = []
        self._deny = deny
        self._expire_first_client = expire_first_client
        self.expires_at = None

    async def vend_scoped_credentials(
        self, prefix: str, *, ttl: timedelta, read_only: bool
    ) -> ScopedCredentials:
        if self._deny:
            raise AccessDenied("the files-vend role was revoked")
        self.vended.append(prefix)
        assert self.expires_at is not None
        return ScopedCredentials(
            access_key_id=f"AKIA{len(self.vended)}",
            secret_access_key="secret",
            session_token="token",
            expires_at=self.expires_at,
            prefix=prefix,
            read_only=read_only,
        )

    def client_for(self, credentials: ScopedCredentials) -> RecordingStore:
        expire = self._expire_first_client and not self.clients
        client = RecordingStore(expire_first=expire)
        self.clients.append(client)
        return client


async def test_one_session_is_vended_per_domain_and_re_vended_past_the_skew(
    clock: FakeClock,
) -> None:
    store = FakeAwsStore()
    ttl = timedelta(minutes=55)
    skew = timedelta(minutes=5)
    store.expires_at = clock.now() + ttl
    factory = AwsScoped(store, clock=clock, ttl=ttl, skew=skew)

    await factory.for_domain(DOMAIN_A)
    await factory.for_domain(DOMAIN_A)
    await factory.for_domain(DOMAIN_B)
    assert store.vended == [
        f"{keys.DOMAIN_PREFIX}{DOMAIN_A}/",
        f"{keys.DOMAIN_PREFIX}{DOMAIN_B}/",
    ]

    clock.advance(ttl - skew - timedelta(seconds=1))
    await factory.for_domain(DOMAIN_A)
    assert len(store.vended) == 2

    clock.advance(timedelta(seconds=1))
    store.expires_at = clock.now() + ttl
    await factory.for_domain(DOMAIN_A)
    assert store.vended[2] == f"{keys.DOMAIN_PREFIX}{DOMAIN_A}/"
    assert len(store.vended) == 3


async def test_an_expired_session_is_refreshed_once_and_the_call_retried(
    clock: FakeClock,
) -> None:
    store = FakeAwsStore(expire_first_client=True)
    store.expires_at = clock.now() + timedelta(minutes=55)
    factory = AwsScoped(store, clock=clock)
    handle = await factory.for_domain(DOMAIN_A)

    info = await handle.head("objects/aa/bb/cc")

    assert info is not None
    assert len(store.vended) == 2
    assert store.clients[0].calls == []
    assert store.clients[1].calls == [("head", f"{keys.DOMAIN_PREFIX}{DOMAIN_A}/objects/aa/bb/cc")]

    await handle.head("objects/aa/bb/dd")
    assert len(store.vended) == 2


async def test_a_revoked_role_fails_closed_and_never_falls_back_to_the_admin_handle(
    clock: FakeClock,
) -> None:
    store = FakeAwsStore(deny=True)
    store.expires_at = clock.now() + timedelta(minutes=55)
    factory = AwsScoped(store, clock=clock)

    with pytest.raises(Unavailable):
        await factory.for_domain(DOMAIN_A)

    assert store.calls == []
    assert store.clients == []


async def test_a_scoping_failure_on_an_s3_compatible_store_also_fails_closed(
    clock: FakeClock,
) -> None:
    inner = RecordingStore()

    async def refusing_vendor(domain_id: DomainId, prefix: str) -> None:
        raise StoreError(f"the {domain_id} key for {prefix} could not be minted")

    factory = S3CompatScoped(
        S3CompatConfig(store=inner),
        clock=clock,
        credential_vendor=refusing_vendor,  # type: ignore[arg-type]
    )

    with pytest.raises(Unavailable):
        await factory.for_domain(DOMAIN_A)
    assert inner.calls == []


# -- the AWS driver really implements the surface the factory calls --------


class _NullAsync:
    """The one-client-per-call context manager the drivers open."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    async def __aenter__(self) -> object:
        return self._inner

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSts:
    """``AssumeRole`` as a counter and a scripted credential."""

    def __init__(self, clock: FakeClock, tags: list[str]) -> None:
        self._clock = clock
        self._tags = tags

    async def assume_role(self, **kwargs: object) -> dict[str, object]:
        tags = kwargs["Tags"]
        assert isinstance(tags, list)
        self._tags.append(str(tags[0]["Value"]))
        return {
            "Credentials": {
                "AccessKeyId": f"AKIA{len(self._tags)}",
                "SecretAccessKey": "secret",
                "SessionToken": f"token-{len(self._tags)}",
                "Expiration": self._clock.now() + timedelta(minutes=55),
            }
        }


class _FakeEndpoint:
    """A dict-backed S3 endpoint the real driver drives."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.faults: dict[str, list[BaseException]] = {}

    def __call__(self) -> _NullAsync:
        return _NullAsync(self)

    def _fail(self, name: str) -> None:
        queue = self.faults.get(name)
        if queue:
            raise queue.pop(0)

    async def put_object(self, **kwargs: object) -> dict[str, object]:
        self._fail("put_object")
        stored = kwargs["Body"]
        payload = stored.read() if hasattr(stored, "read") else stored
        assert isinstance(payload, bytes)
        self.objects[str(kwargs["Key"])] = payload
        return {"ETag": '"etag"'}

    async def head_object(self, **kwargs: object) -> dict[str, object]:
        self._fail("head_object")
        key = str(kwargs["Key"])
        if key not in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey", "Message": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404, "HTTPHeaders": {}},
                },
                "HeadObject",
            )
        return {"ContentLength": len(self.objects[key]), "ETag": '"etag"'}

    async def delete_object(self, **kwargs: object) -> dict[str, object]:
        self.objects.pop(str(kwargs["Key"]), None)
        return {}


def _aws(clock: FakeClock, endpoint: _FakeEndpoint, tags: list[str]) -> AwsStore:
    return AwsStore(
        S3Config(
            endpoint_url="https://s3.invalid",
            region="us-west-2",
            bucket="alkera-files",
            vend_role_arn="arn:aws:iam::1:role/files-vend",
        ),
        clock=clock,
        client_factory=endpoint,
        sts_factory=lambda: _NullAsync(_FakeSts(clock, tags)),
    )


async def test_the_aws_scoped_path_vends_a_client_and_writes_through_it(
    clock: FakeClock,
) -> None:
    """``AwsScoped`` calls ``store.client_for(credentials)``; ``AwsStore`` must have it.

    Without the method the whole AWS scoped path raised ``AttributeError`` on
    the first request, so it never existed end to end. This drives it: assume
    the role, build a client from the vended credential, write an object.
    """
    endpoint = _FakeEndpoint()
    tags: list[str] = []
    factory = AwsScoped(_aws(clock, endpoint, tags), clock=clock)

    handle = await factory.for_domain(DOMAIN_A)
    payload = b"written through the vended session"
    await handle.put(
        "objects/ab/cd/x", _stream(payload), size=len(payload), checksum=blake3(payload).digest()
    )

    assert tags == [str(DOMAIN_A)]
    assert endpoint.objects == {f"{keys.DOMAIN_PREFIX}{DOMAIN_A}/objects/ab/cd/x": payload}


async def test_a_vended_client_refuses_a_relative_key_before_the_wire(
    clock: FakeClock,
) -> None:
    """The vended handle is a bucket-layout driver: its namespace is the absolute one."""
    endpoint = _FakeEndpoint()
    store = _aws(clock, endpoint, [])
    vended = store.client_for(
        ScopedCredentials(
            access_key_id="ak",
            secret_access_key="sk",
            session_token="tok",
            expires_at=clock.now() + timedelta(minutes=55),
            prefix=f"{keys.DOMAIN_PREFIX}{DOMAIN_A}/",
            read_only=False,
        )
    )

    with pytest.raises(InvalidKey):
        await vended.head("objects/ab/cd/x")
    assert endpoint.objects == {}


@pytest.mark.parametrize(
    "code",
    [
        pytest.param("ExpiredToken", id="expired-token"),
        pytest.param("ExpiredTokenException", id="expired-token-exception"),
        pytest.param("InvalidToken", id="invalid-token"),
    ],
)
async def test_a_real_expired_token_from_the_driver_drives_exactly_one_refresh(
    clock: FakeClock, code: str
) -> None:
    """Not a stub raising the class: a botocore error the driver normalises onto it.

    Mapping ``ExpiredToken`` onto ``Unavailable`` meant no real driver ever
    raised what the refresh is keyed on, so the whole path was proven only
    against fakes.
    """
    endpoint = _FakeEndpoint()
    endpoint.faults["head_object"] = [
        ClientError(
            {
                "Error": {"Code": code, "Message": code},
                "ResponseMetadata": {"HTTPStatusCode": 400, "HTTPHeaders": {}},
            },
            "HeadObject",
        )
    ]
    tags: list[str] = []
    factory = AwsScoped(_aws(clock, endpoint, tags), clock=clock)
    handle = await factory.for_domain(DOMAIN_A)

    assert await handle.head("objects/ab/cd/x") is None

    assert endpoint.faults["head_object"] == []
    # One session at open, a second because the first had expired.
    assert tags == [str(DOMAIN_A), str(DOMAIN_A)]


# -- a consumed stream is never replayed -----------------------------------


class _ExpiringOnce(RecordingStore):
    """A store whose first call of each kind reports an expired session."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts: dict[str, int] = {}
        self.expires_at: datetime | None = None
        self.vended: list[str] = []

    def _count(self, op: str) -> None:
        self.attempts[op] = self.attempts.get(op, 0) + 1
        if self.attempts[op] == 1:
            raise ExpiredCredentials("the session token expired")

    async def put(
        self,
        key: str,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
        if_absent: bool = True,
        storage_class: str | None = None,
    ) -> PutResult:
        # Drain the body the way a driver does: that is what makes a retry
        # send nothing at all.
        [chunk async for chunk in data]
        self._count("put")
        self.calls.append(("put", key))
        return PutResult(key=key, size=size, checksum=checksum, etag=None)

    async def multipart_put_part(
        self,
        handle: UploadHandle,
        part_no: int,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
    ) -> PartResult:
        [chunk async for chunk in data]
        self._count("multipart_put_part")
        return PartResult(part_no=part_no, size=size, checksum=checksum, etag=None)

    async def head(self, key: str) -> ObjectInfo | None:
        self._count("head")
        self.calls.append(("head", key))
        return None

    async def vend_scoped_credentials(
        self, prefix: str, *, ttl: timedelta, read_only: bool
    ) -> ScopedCredentials:
        self.vended.append(prefix)
        assert self.expires_at is not None
        return ScopedCredentials(
            access_key_id="ak",
            secret_access_key="sk",
            session_token="tok",
            expires_at=self.expires_at,
            prefix=prefix,
            read_only=read_only,
        )

    def client_for(self, credentials: ScopedCredentials) -> _ExpiringOnce:
        return self


async def _expiring(clock: FakeClock) -> tuple[_ExpiringOnce, DomainStore]:
    store = _ExpiringOnce()
    store.expires_at = clock.now() + timedelta(minutes=55)
    factory = AwsScoped(store, clock=clock)
    return store, await factory.for_domain(DOMAIN_A)


async def test_an_expired_credential_on_a_put_reaches_the_caller_unretried(
    clock: FakeClock,
) -> None:
    """The body is an ``AsyncIterator``: by the time the store answers it is drained.

    Retrying would send the empty remainder of a consumed stream under the
    caller's checksum. The handle raises instead, and the content layer — which
    still holds the source — re-drives the transfer.
    """
    store, handle = await _expiring(clock)
    payload = b"the bytes that only exist once"

    with pytest.raises(ExpiredCredentials):
        await handle.put(
            "objects/ab/cd/x",
            _stream(payload),
            size=len(payload),
            checksum=blake3(payload).digest(),
        )

    assert store.attempts["put"] == 1
    assert store.calls == []


async def test_an_expired_credential_on_a_part_reaches_the_caller_unretried(
    clock: FakeClock,
) -> None:
    store, handle = await _expiring(clock)
    payload = b"a part that only exists once"
    upload = UploadHandle(key="objects/ab/cd/x", upload_id="u1", size=len(payload))

    with pytest.raises(ExpiredCredentials):
        await handle.multipart_put_part(
            upload, 1, _stream(payload), size=len(payload), checksum=blake3(payload).digest()
        )

    assert store.attempts["multipart_put_part"] == 1


async def test_a_call_that_carries_no_body_still_refreshes_once(clock: FakeClock) -> None:
    """The negative twin: only the body-carrying calls give up the retry."""
    store, handle = await _expiring(clock)

    assert await handle.head("objects/ab/cd/x") is None

    assert store.attempts["head"] == 2
    assert store.vended == [f"{keys.DOMAIN_PREFIX}{DOMAIN_A}/"] * 2


# -- the admin handle addresses the bucket ---------------------------------


async def test_the_filesystem_admin_handle_reads_and_moves_another_domains_object(
    tmp_path: Path, clock: FakeClock
) -> None:
    """The janitor holds ``admin()``; if it refuses ``domains/…`` it cannot work."""
    factory = FilesystemScoped(tmp_path / "root", clock=clock)
    handle = await factory.for_domain(DOMAIN_B)
    payload = b"another domain's object"
    await handle.put(
        "objects/ab/cd/x", _stream(payload), size=len(payload), checksum=blake3(payload).digest()
    )
    absolute = f"{keys.DOMAIN_PREFIX}{DOMAIN_B}/objects/ab/cd/x"

    admin = factory.admin()
    page = await admin.list_prefix(f"{keys.DOMAIN_PREFIX}{DOMAIN_B}/")
    info = await admin.head(absolute)
    await admin.move(absolute, f"{keys.DOMAIN_PREFIX}{DOMAIN_B}/deleted/objects/ab/cd/x")

    assert list(page.keys) == [absolute]
    assert info is not None and info.size == len(payload)
    assert await handle.head("objects/ab/cd/x") is None
    assert await handle.head("deleted/objects/ab/cd/x") is not None


# -- the vended lease is dropped at expires_at - skew, not at expires_at ---


class _CountingVendor:
    """Vends a lease that expires at a fixed instant, and counts the vends."""

    def __init__(self, clock: FakeClock, ttl: timedelta) -> None:
        self._clock = clock
        self._ttl = ttl
        self.vends = 0

    async def __call__(self, domain_id: DomainId, prefix: str) -> VendedClient:
        self.vends += 1
        return VendedClient(store=PrefixGuard(RecordingStore(), prefix), expires_at=self.expires_at)

    @property
    def expires_at(self) -> datetime:
        return self._clock.now() + self._ttl


@pytest.mark.parametrize(
    ("offset", "vends"),
    [
        pytest.param(timedelta(seconds=-1), 1, id="one_second_inside_the_skew_window_reuses"),
        pytest.param(timedelta(0), 2, id="exactly_at_expires_at_minus_skew_re_vends"),
        pytest.param(timedelta(seconds=1), 2, id="one_second_past_it_re_vends"),
    ],
)
async def test_a_vended_lease_is_dropped_a_skew_before_it_expires(
    clock: FakeClock, offset: timedelta, vends: int
) -> None:
    """The lease is only usable while it will still be valid when it lands.

    A credential handed out at ``expires_at - 1s`` is expired by the time the
    request reaches the store, so the window that matters is the one the skew
    carves out — and it is a boundary, which means both sides of it have to be
    driven across, not one fixed ``now``.
    """
    ttl = timedelta(minutes=55)
    skew = timedelta(minutes=5)
    vendor = _CountingVendor(clock, ttl)
    factory = S3CompatScoped(
        S3CompatConfig(store=RecordingStore(), ttl=ttl, skew=skew),
        clock=clock,
        credential_vendor=vendor,
    )
    first = await factory.for_domain(DOMAIN_A)

    clock.advance(ttl - skew + offset)
    second = await factory.for_domain(DOMAIN_A)

    assert vendor.vends == vends
    assert (second is first) is (vends == 1)


async def test_an_unvended_lease_never_expires_however_far_the_clock_runs(
    clock: FakeClock,
) -> None:
    """``PrefixGuard`` is in-process, so there is no credential to go stale."""
    factory = S3CompatScoped(S3CompatConfig(store=RecordingStore()), clock=clock)

    first = await factory.for_domain(DOMAIN_A)
    clock.advance(timedelta(days=400))
    second = await factory.for_domain(DOMAIN_A)

    assert second is first
