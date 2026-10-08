"""The S3-compatible driver: one wire dialect, every endpoint that speaks it.

Nothing store-specific may leak past this module, so a botocore ``ClientError``
is normalised onto the typed error set here and boto is imported here (and in
``aws.py``) and nowhere else in the package. Two habits are load-bearing:

* **head-after-put, always.** A checksum the store computed is a checksum the
  store can be wrong about; we compare what we hashed while streaming against
  what the store says it stored, and delete the object rather than reference
  bytes we cannot vouch for.
* **retries live here.** The SDK is configured with ``max_attempts=1``; every
  call goes through :func:`~alkera_core.files.store._retry.with_retries` with a
  shared budget, so a browning-out store is not retried into the ground.
"""

from __future__ import annotations

import asyncio
import contextlib
import random as _random
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from tempfile import SpooledTemporaryFile
from typing import Any, Final, Literal, NoReturn, TypeVar
from uuid import uuid4

import aioboto3
import boto3
from blake3 import blake3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from alkera_core.db.locking import io_boundary
from alkera_core.files.clock import Clock
from alkera_core.files.ids import DomainId
from alkera_core.files.store._retry import RetryBudget, RetryPolicy, Sleep, with_retries
from alkera_core.files.store.errors import (
    AccessDenied,
    ChecksumMismatch,
    ExpiredCredentials,
    InvalidRequest,
    NoSuchBucket,
    NotFound,
    PreconditionFailed,
    StoreError,
    Throttled,
    Unavailable,
)
from alkera_core.files.store.keys import DOMAIN_PREFIX, KeyLayout, absolute, validate_key
from alkera_core.files.store.protocol import (
    ListPage,
    ObjectInfo,
    PartResult,
    PutResult,
    ScopedCredentials,
    StoreCapabilities,
    UploadHandle,
    resolve_whole_object_checksum,
)

T = TypeVar("T")

ClientFactory = Callable[[], AbstractAsyncContextManager[Any]]
PresignerFactory = Callable[[], Any]

MIN_PART_BYTES: Final = 5 * 1024 * 1024
"""S3's floor for every part but the last."""

MAX_SINGLE_COPY_BYTES: Final = 5 * (1 << 30)
"""S3's ceiling for a one-call ``CopyObject``.

A source over 5 GiB is refused outright -- ``InvalidRequest: The specified copy
source is larger than the maximum allowable size`` -- and has to be copied range
by range with ``UploadPartCopy``. Lenient endpoints (SeaweedFS among them) copy
it anyway, so the ceiling is a production-only fault and one the driver has to
hold itself."""

SPOOL_BYTES: Final = 1 << 20
"""How much of a single put stays in memory before the spool reaches disk."""

STORAGE_CLASSES: Final = {"hot": "STANDARD", "cold": "GLACIER_IR"}

_THROTTLE_CODES: Final = frozenset(
    {"SlowDown", "Throttling", "ThrottlingException", "RequestLimitExceeded", "TooManyRequests"}
)
_NOT_FOUND_CODES: Final = frozenset({"NoSuchKey", "NoSuchUpload", "NotFound", "404"})
_NO_SUCH_BUCKET_CODES: Final = frozenset({"NoSuchBucket"})
"""The bucket, not the key, is missing.

Mapped to its own subclass of :class:`NotFound` so a health probe can tell a
store that answered "no such object" (reachable) from one pointed at a bucket
that does not exist (a misconfiguration). A bare ``HeadObject`` 404 carries no
error body and so no code at all, which is exactly why the distinction has to
come from the code when the endpoint does spell it."""
_EXPIRED_CODES: Final = frozenset({"ExpiredToken", "ExpiredTokenException", "InvalidToken"})
"""What every S3 dialect calls a session credential that has run out.

They arrive as a 400, so :func:`_retryable` refuses them and the scoped
factory — not the retry loop — is what re-vends and re-drives the call."""

_DENIED_CODES: Final = frozenset({"AccessDenied", "AccessDeniedException", "InvalidAccessKeyId"})

_PRECONDITION_CODES: Final = frozenset(
    {
        "PreconditionFailed",
        "ConditionalRequestConflict",
        "412",
        "InvalidRange",
        "416",
    }
)

_INVALID_REQUEST_CODES: Final = frozenset(
    {
        "EntityTooSmall",
        "EntityTooLarge",
        "InvalidPart",
        "InvalidPartOrder",
        "InvalidDigest",
        "BadDigest",
        "MalformedXML",
        "KeyTooLongError",
        "InvalidArgument",
        "InvalidObjectState",
    }
)
"""Permanent faults in the *request*, never in the store.

Left unmapped these became :class:`Unavailable` — a 503-shaped answer to a
call that can never succeed, which is exactly what a caller retries forever.
An undersized middle part, a spliced part list, a key the endpoint refuses as
too long and a read of a cold-tier object before it is restored are all the
caller's problem and must surface as one."""

INCOMING_PREFIX: Final = "incoming/"
"""Where an upload session stages its bytes, relative to a domain."""

LIFECYCLE_TAG_KEY: Final = "alkera-lifecycle"
"""The object tag a bucket lifecycle rule filters on.

Every stored key is domain-absolute (``domains/<uuid>/incoming/...``), and an S3
lifecycle ``filter.prefix`` is a wildcard-free literal anchored at byte 0, so no
prefix can name a class that lives one variable segment deep. The driver knows
which class it is writing, so it stamps the class on the object and the bucket
rules filter on the tag instead. Drivers with no tagging (the filesystem one)
ignore this: their sweeper is the reconciliation worker, which never reads it.
"""

LIFECYCLE_CLASSES: Final = ("incoming", "deleted", "erased")
"""The first relative key segments a lifecycle safety net expires."""

__all__ = [
    "LIFECYCLE_CLASSES",
    "LIFECYCLE_TAG_KEY",
    "MIN_PART_BYTES",
    "S3CompatibleStore",
    "S3Config",
    "lifecycle_class",
    "normalize_client_error",
    "s3_capabilities",
]


def lifecycle_class(key: str, layout: KeyLayout) -> str | None:
    """The lifecycle class ``key`` belongs to, or ``None`` for a content key.

    Deliberately total rather than validating: this runs on the write path of a
    key the caller already validated, and a key shaped like nothing we recognise
    simply carries no tag.
    """
    relative = key
    if layout == "bucket":
        if not key.startswith(DOMAIN_PREFIX):
            return None
        # ``domains/<uuid>/<relative>`` -- drop the two prefix segments.
        parts = key.split("/", 2)
        if len(parts) < 3:
            return None
        relative = parts[2]
    head = relative.split("/", 1)[0]
    return head if head in LIFECYCLE_CLASSES else None


@dataclass(frozen=True)
class S3Config:
    """Everything the driver needs, read from settings — never a vendor guess."""

    endpoint_url: str | None
    region: str
    bucket: str
    access_key: str | None = None
    # The config travels in error context and settings dumps; the secret never does.
    secret_key: str | None = field(default=None, repr=False)
    addressing: Literal["path", "virtual"] = "path"
    conditional_write: bool = True
    presigned: bool = True
    connect_timeout: float = 5.0
    read_timeout: float = 60.0
    vend_role_arn: str | None = None
    kms_key_id: str | None = None
    storage_class_hints: bool = False
    tagging: bool = True


def s3_capabilities(config: S3Config) -> StoreCapabilities:
    """What a plain S3-compatible endpoint can be relied on to do."""
    return StoreCapabilities(
        conditional_write=config.conditional_write,
        presigned=config.presigned,
        range_signing=config.presigned,
        versioning=False,
        object_lock=False,
        lifecycle=False,
        scoped_credentials=False,
        storage_classes=config.storage_class_hints,
        tagging=config.tagging,
        strong_read_after_write=True,
        kms=config.kms_key_id is not None,
        max_object_bytes=48 * (1 << 40),
        min_part_bytes=MIN_PART_BYTES,
        max_part_bytes=5 * (1 << 30),
        max_parts=10_000,
        # copy-then-delete: both keys are readable in between.
        atomic_move=False,
    )


def normalize_client_error(exc: BaseException) -> BaseException:
    """Map a botocore failure onto the typed set, keeping the HTTP status.

    ``status_code`` rides along so :func:`_retryable` can hold its rule (never
    retry a 4xx other than 429) without a second look at the vendor's error
    code.
    """
    if isinstance(exc, StoreError):
        return exc
    if isinstance(exc, ClientError):
        response = exc.response or {}
        error = response.get("Error", {}) or {}
        code = str(error.get("Code", ""))
        status = int(response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        headers = response.get("ResponseMetadata", {}).get("HTTPHeaders", {}) or {}
        if code in _EXPIRED_CODES:
            return _stamp(ExpiredCredentials(code), status or 400)
        if code in _DENIED_CODES:
            return _stamp(AccessDenied(code), status or 403)
        if code in _THROTTLE_CODES or status == 429:
            return _stamp(Throttled(code or "throttled", retry_after=_retry_after(headers)), 429)
        if code in _NO_SUCH_BUCKET_CODES:
            return _stamp(NoSuchBucket(code), status or 404)
        if code in _NOT_FOUND_CODES or status == 404:
            return _stamp(NotFound(code or "not found"), 404)
        if code in _PRECONDITION_CODES or status in {412, 416}:
            return _stamp(PreconditionFailed(code or "precondition failed"), status or 412)
        if code in _INVALID_REQUEST_CODES:
            return _stamp(InvalidRequest(f"{code} (http {status})"), status or 400)
        return _stamp(Unavailable(f"{code or 'unmapped'} (http {status})"), status)
    if isinstance(exc, BotoCoreError | ConnectionError | OSError | TimeoutError):
        return _stamp(Unavailable(str(exc) or type(exc).__name__), 0)
    return exc


def _list_call(params: dict[str, Any]) -> Callable[[Any], Awaitable[dict[str, Any]]]:
    """Bind one page's parameters, so a retried page cannot pick up the next one's."""

    def call(client: Any) -> Awaitable[dict[str, Any]]:
        result: Awaitable[dict[str, Any]] = client.list_objects_v2(**params)
        return result

    return call


def _multipart_list_call(params: dict[str, Any]) -> Callable[[Any], Awaitable[dict[str, Any]]]:
    """Bind one ``ListMultipartUploads`` page, so a retry cannot advance the markers."""

    def call(client: Any) -> Awaitable[dict[str, Any]]:
        result: Awaitable[dict[str, Any]] = client.list_multipart_uploads(**params)
        return result

    return call


def _copy_part_call(params: dict[str, Any]) -> Callable[[Any], Awaitable[dict[str, Any]]]:
    """Bind one ``UploadPartCopy``, so a retried part cannot copy another's range."""

    def call(client: Any) -> Awaitable[dict[str, Any]]:
        result: Awaitable[dict[str, Any]] = client.upload_part_copy(**params)
        return result

    return call


def _copy_ranges(size: int, part: int) -> list[tuple[int, int]]:
    """``(first, last)`` byte offsets, inclusive, for a part-by-part copy."""
    return [(first, min(first + part, size) - 1) for first in range(0, size, part)]


def _incoming_tail(key: str) -> str | None:
    """The ``incoming/…`` part of ``key``, or ``None`` when it stages nothing.

    Both key namespaces answer here: a domain-relative key *is* its own tail, and
    a bucket-absolute one carries it after the ``domains/<uuid>/`` prefix.
    """
    if key.startswith(INCOMING_PREFIX):
        return key
    if key.startswith(DOMAIN_PREFIX):
        _domain, _, rest = key[len(DOMAIN_PREFIX) :].partition("/")
        if rest.startswith(INCOMING_PREFIX):
            return rest
    return None


def _stamp(exc: StoreError, status: int) -> StoreError:
    exc.status_code = status  # type: ignore[attr-defined]
    return exc


def _retry_after(headers: dict[str, Any]) -> float | None:
    raw = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _content_range_total(header: str | None) -> int | None:
    """The object's total size out of a ``bytes <first>-<last>/<total>`` header.

    ``None`` means the endpoint sent no ``Content-Range`` at all, which is a
    different thing from an unparsable one: the first says the range was
    ignored, the second says the answer cannot be trusted, and both send the
    driver to ``head`` rather than to a guess.
    """
    if not header:
        return None
    _, _, total = header.partition("/")
    try:
        return int(total)
    except ValueError:
        return None


def _retryable(exc: BaseException) -> bool:
    """Throttles, unreachability and connection faults — never another 4xx."""
    status = getattr(exc, "status_code", 0)
    if isinstance(status, int) and 400 <= status < 500 and status != 429:
        return False
    return isinstance(exc, Throttled | Unavailable | ConnectionError | TimeoutError)


class S3CompatibleStore:
    """An ``ObjectStore`` over any endpoint speaking the S3 API."""

    def __init__(
        self,
        config: S3Config,
        *,
        clock: Clock,
        layout: KeyLayout = "domain",
        client_factory: ClientFactory | None = None,
        presigner_factory: PresignerFactory | None = None,
        sleep: Sleep = asyncio.sleep,
        random: Callable[[], float] = _random.random,
        policy: RetryPolicy | None = None,
        budget: RetryBudget | None = None,
    ) -> None:
        self._config = config
        self.layout = layout
        self._clock = clock
        self._sleep = sleep
        self._injected_client_factory = client_factory
        self._client_factory = client_factory or self._default_client_factory
        # The protocol's presign methods are synchronous, so signing goes
        # through a plain boto3 client rather than the async one.
        self._presigner_factory = presigner_factory or self._default_presigner_factory
        self._policy = policy or RetryPolicy(jitter=random)
        self._budget = budget or RetryBudget(20.0, 2.0, clock=clock)
        # What this process actually staged, per open upload id. `complete`
        # is checked against it before the endpoint is asked, so a lenient
        # endpoint cannot let a spliced part list through.
        self._staged: dict[str, dict[int, PartResult]] = {}
        # The content hash of a multipart object is the hash of the whole
        # object, which no single part carries. Parts that arrive in order
        # feed one hasher; anything else drops it and `complete` re-reads.
        self._rolling: dict[str, blake3 | None] = {}
        self.capabilities = s3_capabilities(config)
        # Held per store rather than read off the module: endpoints do not all
        # draw the single-copy ceiling in the same place, and a test drives the
        # part-by-part path without staging five gigabytes.
        self.max_single_copy_bytes = MAX_SINGLE_COPY_BYTES
        self._probed_conditional_complete = False
        # ``LastModified`` off the last listing page, so the janitor's age
        # question about a key it was just handed costs no extra HEAD.
        self._listed_at: dict[str, datetime] = {}

    # -- capability probing ----------------------------------------------

    async def probe_conditional_complete(self) -> bool:
        """Ask the endpoint, once, whether it honours ``IfNoneMatch`` on complete.

        Several S3-compatible endpoints (SeaweedFS among them) accept the
        header on ``CompleteMultipartUpload`` and assemble anyway, so a
        capability record that simply repeated ``conditional_write`` would be
        a claim rather than an observation — and the driver would report the
        large conditional put as atomic on a store where it is a racy
        head-then-complete. The probe writes a one-byte sentinel, tries to
        overwrite it through a conditional complete, and keeps the answer: the
        endpoint either refuses (atomic, the header works) or assembles (the
        head fallback is the mechanism). Idempotent — the cached answer is
        returned on every later call, so this costs one round trip per store.
        """
        if self.capabilities.conditional_complete:
            return True
        if self._probed_conditional_complete or not self.capabilities.conditional_write:
            return self.capabilities.conditional_complete
        key = self._probe_key()
        honoured = await self._run_conditional_complete_probe(key)
        self._probed_conditional_complete = True
        self.capabilities = replace(self.capabilities, conditional_complete=honoured)
        return honoured

    def _probe_key(self) -> str:
        """A sentinel in the sweepable ``incoming/`` namespace, never a content key."""
        relative = f"incoming/{uuid4()}/probe"
        if self.layout == "bucket":
            return absolute(DomainId(uuid4()), relative)
        return relative

    async def _run_conditional_complete_probe(self, key: str) -> bool:
        bucket = self._config.bucket
        await self._call(lambda client: client.put_object(Bucket=bucket, Key=key, Body=b"\x00"))
        try:
            created = await self._call(
                lambda client: client.create_multipart_upload(Bucket=bucket, Key=key)
            )
            upload_id = str(created["UploadId"])
            part = await self._call(
                lambda client: client.upload_part(
                    Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=1, Body=b"\x00"
                )
            )
            multipart = {"Parts": [{"PartNumber": 1, "ETag": part.get("ETag")}]}
            try:
                await self._call(
                    lambda client: client.complete_multipart_upload(
                        Bucket=bucket,
                        Key=key,
                        UploadId=upload_id,
                        MultipartUpload=multipart,
                        IfNoneMatch="*",
                    )
                )
            except PreconditionFailed:
                await self._call(
                    lambda client: client.abort_multipart_upload(
                        Bucket=bucket, Key=key, UploadId=upload_id
                    )
                )
                return True
            return False
        finally:
            with contextlib.suppress(StoreError):
                await self._call(lambda client: client.delete_object(Bucket=bucket, Key=key))

    # -- plumbing --------------------------------------------------------

    def _key(self, key: str) -> str:
        """Validate ``key`` for this driver's namespace before any request.

        The domain handle above validates too, but a driver reached directly —
        the janitor's admin handle, a service holding the raw store — must not
        be the one place a hostile key gets onto the wire.
        """
        validate_key(key, self.layout)
        return key

    def _boto_config(self) -> BotoConfig:
        return BotoConfig(
            retries={"max_attempts": 1, "mode": "standard"},
            s3={"addressing_style": self._config.addressing},
            connect_timeout=self._config.connect_timeout,
            read_timeout=self._config.read_timeout,
        )

    def _client_kwargs(self) -> dict[str, Any]:
        return {
            "service_name": "s3",
            "region_name": self._config.region,
            "endpoint_url": self._config.endpoint_url,
            "aws_access_key_id": self._config.access_key,
            "aws_secret_access_key": self._config.secret_key,
            "config": self._boto_config(),
        }

    def _default_client_factory(self) -> AbstractAsyncContextManager[Any]:
        session = aioboto3.Session()
        client: AbstractAsyncContextManager[Any] = session.client(**self._client_kwargs())
        return client

    def _default_presigner_factory(self) -> Any:
        return boto3.client(**self._client_kwargs())

    @io_boundary("object store, s3")
    async def _open(
        self, op: Callable[[Any], Awaitable[dict[str, Any]]]
    ) -> tuple[AbstractAsyncContextManager[Any], dict[str, Any]]:
        """Run ``op`` under the same retry budget, but keep the client open."""

        async def attempt() -> tuple[AbstractAsyncContextManager[Any], dict[str, Any]]:
            manager = self._client_factory()
            client = await manager.__aenter__()
            try:
                return manager, await op(client)
            except BaseException as exc:
                await manager.__aexit__(type(exc), exc, exc.__traceback__)
                raise normalize_client_error(exc) from exc

        return await with_retries(
            attempt,
            policy=self._policy,
            budget=self._budget,
            clock=self._clock,
            sleep=self._sleep,
            retryable=_retryable,
        )

    @io_boundary("object store, s3")
    async def _call(self, op: Callable[[Any], Awaitable[T]]) -> T:
        async def attempt() -> T:
            try:
                async with self._client_factory() as client:
                    return await op(client)
            except Exception as exc:
                raise normalize_client_error(exc) from exc

        return await with_retries(
            attempt,
            policy=self._policy,
            budget=self._budget,
            clock=self._clock,
            sleep=self._sleep,
            retryable=_retryable,
        )

    # -- writes ----------------------------------------------------------

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
        self._key(key)
        if size < 0:
            raise InvalidRequest(f"{key}: a put cannot declare {size} bytes")
        part_size = self.capabilities.min_part_bytes
        if size < part_size * 2:
            return await self._put_single(
                key,
                data,
                size=size,
                checksum=checksum,
                if_absent=if_absent,
                storage_class=storage_class,
            )
        return await self._put_multipart(
            key,
            data,
            size=size,
            checksum=checksum,
            if_absent=if_absent,
            storage_class=storage_class,
        )

    async def _put_single(
        self,
        key: str,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
        if_absent: bool,
        storage_class: Literal["hot", "cold"] | None,
    ) -> PutResult:
        with SpooledTemporaryFile(max_size=SPOOL_BYTES) as spool:
            hasher = blake3()
            written = 0
            async for chunk in data:
                spool.write(chunk)
                hasher.update(chunk)
                written += len(chunk)
            digest = hasher.digest()
            if written != size:
                raise InvalidRequest(f"{key}: declared {size} bytes, streamed {written}")
            spool.seek(0)
            params: dict[str, Any] = {
                "Bucket": self._config.bucket,
                "Key": key,
                # A seekable file, not a buffer: botocore signs and sends it
                # without the driver ever holding the object twice, and the
                # spool itself only reaches disk past SPOOL_BYTES.
                "Body": spool,
                "ContentLength": written,
            }
            if if_absent and self.capabilities.conditional_write:
                params["IfNoneMatch"] = "*"
            params.update(self._write_extras(key, storage_class))
            response = await self._call(lambda client: client.put_object(**params))
        return await self._verify(
            key, size=written, digest=digest, declared=checksum, response=response
        )

    async def _put_multipart(
        self,
        key: str,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
        if_absent: bool,
        storage_class: Literal["hot", "cold"] | None,
    ) -> PutResult:
        handle = await self.multipart_create(key, size=size, storage_class=storage_class)
        hasher = blake3()
        parts: list[PartResult] = []
        written = 0
        try:
            part_no = 0
            async for buffer in _repack(data, self.capabilities.min_part_bytes):
                part_no += 1
                hasher.update(buffer)
                written += len(buffer)
                parts.append(await self._upload_part(handle, part_no, buffer))
            if written != size:
                raise InvalidRequest(f"{key}: declared {size} bytes, streamed {written}")
        except Exception:
            await self.multipart_abort(handle)
            raise
        try:
            await self._complete_raw(handle, parts, if_absent=if_absent)
        except Exception:
            await self.multipart_abort(handle)
            raise
        return await self._verify(key, size=written, digest=hasher.digest(), declared=checksum)

    def _write_extras(
        self, key: str, storage_class: Literal["hot", "cold"] | None
    ) -> dict[str, Any]:
        extras: dict[str, Any] = {}
        if storage_class is not None and self.capabilities.storage_classes:
            extras["StorageClass"] = STORAGE_CLASSES[storage_class]
        if self._config.kms_key_id is not None:
            extras["ServerSideEncryption"] = "aws:kms"
            extras["SSEKMSKeyId"] = self._config.kms_key_id
        extras.update(self._lifecycle_tagging(key))
        return extras

    def _lifecycle_tagging(self, key: str) -> dict[str, Any]:
        """``Tagging`` for the bucket's expiry rules, when the key has a class.

        An endpoint that does not speak object tagging refuses the header
        outright, which would turn every write under a classed prefix into a
        hard failure. Degrade to no tag there: the reconciliation worker is the
        truth on every driver and the tag is only ever the safety net.
        """
        if not self.capabilities.tagging:
            return {}
        found = lifecycle_class(key, self.layout)
        if found is None:
            return {}
        return {"Tagging": f"{LIFECYCLE_TAG_KEY}={found}"}

    async def _verify(
        self,
        key: str,
        *,
        size: int,
        digest: bytes,
        declared: bytes,
        response: dict[str, Any] | None = None,
    ) -> PutResult:
        """Head-after-put, always: the store's own account of what it stored."""
        info = await self.head(key)
        if digest != declared or info is None or info.size != size:
            await self.delete(key)
            stored = "absent" if info is None else str(info.size)
            raise ChecksumMismatch(
                f"{key}: declared {declared.hex()}/{size} bytes, "
                f"stored {digest.hex()}/{stored} bytes"
            )
        etag = info.etag or (response or {}).get("ETag")
        return PutResult(key=key, size=size, checksum=digest, etag=etag)

    # -- reads -----------------------------------------------------------

    async def get(self, key: str, *, range: tuple[int, int] | None = None) -> AsyncIterator[bytes]:
        """Open the object and hand back a stream that owns its client.

        The body is a live socket, so the client that produced it has to stay
        open until the last chunk has been read; closing it with the call — as
        every other operation here does — drops the connection under the
        caller and surfaces as "Connection closed." on the first object too
        large to have been buffered by the SDK.
        """
        params: dict[str, Any] = {"Bucket": self._config.bucket, "Key": self._key(key)}
        if range is not None:
            params["Range"] = f"bytes={range[0]}-{range[1]}"
        manager, response = await self._open(lambda client: client.get_object(**params))
        try:
            window = await self._window(key, range, response)
        except BaseException:
            await manager.__aexit__(None, None, None)
            raise
        return _stream_body(response["Body"], manager, window)

    async def _window(
        self, key: str, range: tuple[int, int] | None, response: dict[str, Any]
    ) -> tuple[int, int] | None:
        """The slice this driver still owes the caller after the endpoint answered.

        A ``Content-Range`` means the endpoint honoured the request and there
        is nothing left to do. Without one it answered 200 with the whole
        object — S3 never does, lenient endpoints do — and the range semantics
        are ours to enforce: a start past the end is 416 rather than a silent
        whole-object read, and any other window is cut from the stream here.
        """
        if range is None:
            return None
        total = _content_range_total(response.get("ContentRange"))
        if total is None:
            # No Content-Range: the endpoint answered 200 with the whole
            # object, so ask what the object is before deciding.
            info = await self.head(key)
            total = 0 if info is None else info.size
        elif range[0] < total:
            return None
        if range[0] >= total:
            raise _stamp(
                PreconditionFailed(
                    f"{key}: bytes {range[0]}-{range[1]} of {total} is not satisfiable"
                ),
                416,
            )
        return range

    async def _head_raw(self, key: str) -> dict[str, Any] | None:
        """The endpoint's whole ``HeadObject`` answer, or ``None`` if absent."""
        try:
            response: dict[str, Any] = await self._call(
                lambda client: client.head_object(Bucket=self._config.bucket, Key=self._key(key))
            )
        except NoSuchBucket:
            # A missing key is an absence; a missing bucket is a broken
            # deployment, and the health probe reads them differently.
            raise
        except NotFound:
            return None
        return response

    async def head(self, key: str) -> ObjectInfo | None:
        response = await self._head_raw(key)
        if response is None:
            return None
        return ObjectInfo(
            size=int(response["ContentLength"]),
            checksum=None,
            etag=response.get("ETag"),
            storage_class=response.get("StorageClass"),
        )

    async def head_bucket(self) -> None:
        """Prove the configured bucket exists, raising :class:`NoSuchBucket` if not.

        A key HEAD cannot answer this. S3 replies to a HEAD with no body, so the
        404 it returns under a bucket that does not exist carries no error code
        at all and is indistinguishable from a missing key -- which every caller
        reads as "the store is reachable". A bucket-level HEAD has no such
        ambiguity: the only thing that can be absent is the bucket, so its 404 is
        mapped onto the typed error rather than left as a plain absence.
        """
        try:
            await self._call(lambda client: client.head_bucket(Bucket=self._config.bucket))
        except NoSuchBucket:
            raise
        except NotFound as exc:
            raise _stamp(NoSuchBucket(self._config.bucket), 404) from exc

    async def delete(self, key: str) -> None:
        try:
            await self._call(
                lambda client: client.delete_object(Bucket=self._config.bucket, Key=self._key(key))
            )
        except NotFound:
            return

    async def move(self, src: str, dst: str) -> None:
        """Copy then delete. This is **not** atomic on S3.

        Nothing above relies on it being atomic: the content layer writes into
        ``incoming/`` and promotes with a head, so a crash between the two calls
        leaves a sweepable temp object, never a half-written content key.

        A source over :data:`MAX_SINGLE_COPY_BYTES` cannot be copied in one
        call, so it is headed first and copied part by part instead. A source
        that is not there at all takes the single-call path, so a move of a
        missing key still fails the way it always did.
        """
        self._key(src)
        self._key(dst)
        tagging = self._lifecycle_tagging(dst)
        source = await self._head_raw(src)
        if source is not None and int(source["ContentLength"]) > self.max_single_copy_bytes:
            await self._copy_multipart(src, dst, source, tagging=tagging)
        else:
            await self._copy_single(src, dst, tagging=tagging)
        await self.delete(src)

    async def _copy_single(self, src: str, dst: str, *, tagging: dict[str, Any]) -> None:
        params: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": dst,
            "CopySource": {"Bucket": self._config.bucket, "Key": src},
        }
        if self.capabilities.tagging:
            # REPLACE, always: the default directive copies the source's tags,
            # so a promotion out of ``incoming/`` would carry the 14-day expiry
            # tag onto the content object it just published. The destination's
            # own class -- or no tag at all -- is the truth.
            params["TaggingDirective"] = "REPLACE"
            params.update(tagging)
        await self._call(lambda client: client.copy_object(**params))
        if self.capabilities.tagging and not tagging:
            # A REPLACE that declares no tags is *documented* to leave the
            # destination untagged, but it is not honoured everywhere --
            # SeaweedFS keeps the source's tag set instead -- and where it is
            # not, a promotion out of ``incoming/`` publishes content still
            # classed for the 14-day expiry sweep, which then deletes it. Ask
            # for the removal outright rather than inferring it from a
            # directive the endpoint may be ignoring.
            await self._untag(dst)

    async def _copy_multipart(
        self, src: str, dst: str, source: dict[str, Any], *, tagging: dict[str, Any]
    ) -> None:
        """Copy a source past the single-call ceiling range by range.

        A multipart copy inherits nothing the way ``CopyObject`` does: the
        destination carries only what ``CreateMultipartUpload`` declared, so the
        content type and user metadata are carried across by hand, the
        destination's own lifecycle class is declared there rather than
        replaced afterwards, and no tag has to be stripped off a classless
        destination because none was ever copied onto it.
        """
        size = int(source["ContentLength"])
        bucket = self._config.bucket
        # Cut before the session is opened: a source no part list can cover
        # must leave nothing behind to abort.
        ranges = _copy_ranges(size, self._copy_part_bytes(dst, size))
        params: dict[str, Any] = {"Bucket": bucket, "Key": dst}
        content_type = source.get("ContentType")
        if content_type:
            params["ContentType"] = str(content_type)
        metadata = source.get("Metadata")
        if metadata:
            params["Metadata"] = dict(metadata)
        if self.capabilities.tagging:
            params.update(tagging)
        created = await self._call(lambda client: client.create_multipart_upload(**params))
        upload_id = str(created["UploadId"])
        try:
            parts: list[dict[str, Any]] = []
            for part_no, (first, last) in enumerate(ranges, start=1):
                response = await self._call(
                    _copy_part_call(
                        {
                            "Bucket": bucket,
                            "Key": dst,
                            "UploadId": upload_id,
                            "PartNumber": part_no,
                            "CopySource": {"Bucket": bucket, "Key": src},
                            "CopySourceRange": f"bytes={first}-{last}",
                        }
                    )
                )
                etag = (response.get("CopyPartResult") or {}).get("ETag")
                parts.append({"PartNumber": part_no, "ETag": etag})
            await self._call(
                lambda client: client.complete_multipart_upload(
                    Bucket=bucket,
                    Key=dst,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
            )
        except BaseException:
            # An abandoned session bills for its staged parts until a lifecycle
            # rule reaps it, so a copy that fails closes its own session.
            with contextlib.suppress(StoreError):
                await self._call(
                    lambda client: client.abort_multipart_upload(
                        Bucket=bucket, Key=dst, UploadId=upload_id
                    )
                )
            raise

    def _copy_part_bytes(self, key: str, size: int) -> int:
        """How big each copied range has to be for the endpoint to assemble it.

        The floor is the store's minimum part size, but a large enough source
        cut into minimum parts would need more than ``max_parts`` of them --
        1000 GB is 204,800 five-mebibyte parts -- so the range grows with the
        source instead, and a source no part list can cover is refused before
        a session is opened for it.
        """
        caps = self.capabilities
        part = max(caps.min_part_bytes, -(-size // caps.max_parts))
        if part > caps.max_part_bytes:
            raise InvalidRequest(
                f"{key}: {size} bytes is more than {caps.max_parts} parts of "
                f"{caps.max_part_bytes} bytes can copy"
            )
        return part

    async def _untag(self, key: str) -> None:
        """Drop every tag on ``key``; a key that vanished under us is done."""
        try:
            await self._call(
                lambda client: client.delete_object_tagging(Bucket=self._config.bucket, Key=key)
            )
        except NotFound:
            return
        except AccessDenied:
            # S3 charges the outright removal to its own action, so a deployment
            # whose policy still grants only the write half refuses it -- and a
            # refusal here would fail the promotion that publishes content.
            # Replacing the tag set with an empty one clears it just as
            # completely and is charged to the write action every writer already
            # holds, so a policy lagging the driver costs a round trip rather
            # than taking publishing down.
            try:
                await self._call(
                    lambda client: client.put_object_tagging(
                        Bucket=self._config.bucket, Key=key, Tagging={"TagSet": []}
                    )
                )
            except NotFound:
                return

    async def list_prefix(
        self, prefix: str, *, after: str | None = None, limit: int = 1000
    ) -> ListPage:
        params: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Prefix": prefix,
            "MaxKeys": limit,
        }
        if after is not None:
            params["StartAfter"] = after
        token: str | None = None
        while True:
            page_params = dict(params)
            if token is not None:
                # The continuation token already encodes where the scan left
                # off, and S3 refuses to combine it with StartAfter.
                page_params.pop("StartAfter", None)
                page_params["ContinuationToken"] = token
            response = await self._call(_list_call(page_params))
            keys = [str(item["Key"]) for item in response.get("Contents", [])]
            truncated = bool(response.get("IsTruncated"))
            token = str(response.get("NextContinuationToken") or "") or None
            # S3 scans a bounded window of the bucket per request, so a page
            # under a sparse prefix can legitimately come back truncated with
            # zero keys. Reporting that as the end of the listing is how a
            # reconciler silently stops seeing objects that are still there:
            # continue from the token instead, and answer only once this page
            # has keys or the listing is genuinely exhausted.
            if keys or not truncated or token is None:
                break
        return ListPage(keys=keys, next_after=keys[-1] if truncated and keys else None)

    # -- multipart -------------------------------------------------------

    async def multipart_create(
        self, key: str, *, size: int, storage_class: Literal["hot", "cold"] | None = None
    ) -> UploadHandle:
        params: dict[str, Any] = {"Bucket": self._config.bucket, "Key": self._key(key)}
        params.update(self._write_extras(key, storage_class))
        response = await self._call(lambda client: client.create_multipart_upload(**params))
        upload_id = str(response["UploadId"])
        self._rolling[upload_id] = blake3()
        return UploadHandle(key=key, upload_id=upload_id, size=size)

    async def multipart_put_part(
        self,
        handle: UploadHandle,
        part_no: int,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
    ) -> PartResult:
        if part_no < 1 or part_no > self.capabilities.max_parts:
            # A bad part number wastes one call; the session is still usable.
            raise PreconditionFailed(f"{handle.key}: part number {part_no} is out of range")
        if size > self.capabilities.max_part_bytes:
            # Refused on the declared size, before a single byte is read.
            await self._refuse(
                handle,
                f"part {part_no} declares {size} bytes, over {self.capabilities.max_part_bytes}",
            )
        body = bytearray()
        hasher = blake3()
        async for chunk in data:
            body += chunk
            hasher.update(chunk)
        digest = hasher.digest()
        if digest != checksum:
            # Not a session-ending refusal: this one part arrived corrupt and
            # the caller can re-send it under the same handle.
            raise ChecksumMismatch(
                f"{handle.key} part {part_no}: declared {checksum.hex()}, computed {digest.hex()}"
            )
        return await self._upload_part(handle, part_no, bytes(body))

    async def _refuse(self, handle: UploadHandle, why: str) -> NoReturn:
        """Refuse a part and close the session, so nothing is left half-open.

        A part the driver itself rejects can never be re-sent under this
        handle, so leaving the session open would only strand parts in the
        bucket for a sweeper to find. A *transient* wire failure is
        deliberately not routed here: that part is retryable and the session
        has to survive it.
        """
        await self.multipart_abort(handle)
        raise PreconditionFailed(f"{handle.key}: {why}")

    async def _upload_part(self, handle: UploadHandle, part_no: int, body: bytes) -> PartResult:
        response = await self._call(
            lambda client: client.upload_part(
                Bucket=self._config.bucket,
                Key=handle.key,
                UploadId=handle.upload_id,
                PartNumber=part_no,
                Body=body,
            )
        )
        part = PartResult(
            part_no=part_no,
            size=len(body),
            checksum=blake3(body).digest(),
            etag=response.get("ETag"),
        )
        staged = self._staged.setdefault(handle.upload_id, {})
        if part_no == len(staged) + 1 and handle.upload_id in self._rolling:
            hasher = self._rolling[handle.upload_id]
            if hasher is not None:
                hasher.update(body)
        else:
            self._rolling[handle.upload_id] = None
        staged[part_no] = part
        return part

    async def multipart_complete(
        self,
        handle: UploadHandle,
        parts: Sequence[PartResult],
        *,
        checksum: bytes | None = None,
    ) -> PutResult:
        self._check_part_list(handle, parts)
        rolling = self._rolling.get(handle.upload_id)
        await self._complete_raw(handle, parts)
        size = sum(part.size for part in parts)
        info = await self.head(handle.key)
        if info is None or info.size != size:
            await self.delete(handle.key)
            raise ChecksumMismatch(f"{handle.key}: completed upload is not {size} bytes")
        # The caller hashed the whole object while it streamed the parts, so
        # its checksum is the authority; without one the driver falls back to
        # what it hashed itself, and refuses to invent a hash it cannot stand
        # behind for a multi-part object it never saw assembled in order.
        assembled = rolling.digest() if rolling is not None else None
        digest = resolve_whole_object_checksum(
            handle.key, declared=checksum, assembled=assembled, parts=parts
        )
        return PutResult(key=handle.key, size=size, checksum=digest, etag=info.etag)

    def _check_part_list(self, handle: UploadHandle, parts: Sequence[PartResult]) -> None:
        """Refuse a part list that is not what this driver staged.

        The endpoint is asked only once the list has been matched against the
        ledger written at ``multipart_put_part`` — count, order, sizes,
        checksums and ETags — because an endpoint that does not verify the
        list itself would otherwise assemble whatever it was handed. Every
        refusal is raised before ``CompleteMultipartUpload`` goes out, so the
        key never comes into existence.
        """
        numbers = [part.part_no for part in parts]
        if not parts:
            raise PreconditionFailed(f"{handle.key}: an empty part list completes nothing")
        if numbers != sorted(set(numbers)):
            raise PreconditionFailed(f"{handle.key}: part list {numbers} is out of order")
        missing_etag = [part.part_no for part in parts if not part.etag]
        if missing_etag:
            raise PreconditionFailed(f"{handle.key}: parts {missing_etag} carry no ETag")
        staged = self._staged.get(handle.upload_id)
        if staged is None:
            # Nothing was staged through this instance (a resumed session), so
            # the ledger has nothing to say and the endpoint remains the judge.
            return
        if numbers != sorted(staged):
            raise PreconditionFailed(
                f"{handle.key}: part list {numbers} does not match the staged {sorted(staged)}"
            )
        for part in parts:
            accepted = staged[part.part_no]
            if (part.size, part.checksum, part.etag) != (
                accepted.size,
                accepted.checksum,
                accepted.etag,
            ):
                raise PreconditionFailed(
                    f"{handle.key}: part {part.part_no} is not the part that was staged"
                )

    async def _complete_raw(
        self, handle: UploadHandle, parts: Sequence[PartResult], *, if_absent: bool = False
    ) -> None:
        """Assemble the staged parts, conditionally where the endpoint can.

        ``IfNoneMatch: "*"`` on ``CompleteMultipartUpload`` is what makes a
        large conditional put as safe as a small one: the endpoint refuses the
        assembly rather than overwriting, and the loser sees the same
        :class:`PreconditionFailed` a single put raises. Endpoints that do not
        implement the header on *complete* — several S3-compatible stores,
        SeaweedFS among them — answer as if it were absent, so the driver
        heads the key first and refuses there. That check is not atomic: the
        window between the head and the assembly is closed only where the
        conditional complete is honoured, which is why the head is a fallback
        and not the mechanism.
        """
        ordered = sorted(parts, key=lambda part: part.part_no)
        params: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": handle.key,
            "UploadId": handle.upload_id,
            "MultipartUpload": {
                "Parts": [{"PartNumber": part.part_no, "ETag": part.etag} for part in ordered]
            },
        }
        if if_absent and self.capabilities.conditional_write:
            params["IfNoneMatch"] = "*"
            if not self.capabilities.conditional_complete:
                # Only where the endpoint has been *observed* to honour the
                # header is the assembly itself the refusal; everywhere else
                # the head is the mechanism, and its race is the price.
                if await self.head(handle.key) is not None:
                    raise PreconditionFailed(f"{handle.key}: key already exists")
        await self._call(lambda client: client.complete_multipart_upload(**params))
        self._staged.pop(handle.upload_id, None)
        self._rolling.pop(handle.upload_id, None)

    async def multipart_abort(self, handle: UploadHandle) -> None:
        self._staged.pop(handle.upload_id, None)
        self._rolling.pop(handle.upload_id, None)
        try:
            await self._call(
                lambda client: client.abort_multipart_upload(
                    Bucket=self._config.bucket, Key=handle.key, UploadId=handle.upload_id
                )
            )
        except NotFound:
            return

    # -- the janitor's admin surface -------------------------------------

    async def list_incoming(
        self, *, after: str | None = None, limit: int = 1000
    ) -> tuple[Sequence[str], str | None]:
        """One keyset page of staged upload objects, in this driver's namespace.

        The listing pages ``ListObjectsV2`` under whichever prefix this driver's
        layout roots ``incoming/`` at, keeping each page's ``LastModified`` so
        the age question that follows costs no HEAD.
        """
        prefix = INCOMING_PREFIX if self.layout == "domain" else DOMAIN_PREFIX
        collected: list[str] = []
        token: str | None = None
        self._listed_at.clear()
        while True:
            params: dict[str, Any] = {
                "Bucket": self._config.bucket,
                "Prefix": prefix,
                "MaxKeys": max(limit, 1),
            }
            if token is not None:
                params["ContinuationToken"] = token
            elif after is not None:
                params["StartAfter"] = after
            response = await self._call(_list_call(params))
            for item in response.get("Contents", []):
                key = str(item["Key"])
                if _incoming_tail(key) is None or (after is not None and key <= after):
                    continue
                when = item.get("LastModified")
                if isinstance(when, datetime):
                    self._listed_at[key] = when.astimezone(UTC)
                collected.append(key)
            truncated = bool(response.get("IsTruncated"))
            token = str(response.get("NextContinuationToken") or "") or None
            if len(collected) > limit:
                break
            # A truncated page with no ``Contents`` is the endpoint saying it
            # scanned its window and found nothing yet, not that the listing is
            # over; stopping there is how a sweep silently stops seeing the
            # orphans it exists to collect.
            if not truncated or token is None:
                break
        page = collected[:limit]
        return page, (page[-1] if len(collected) > limit else None)

    async def written_at(self, key: str) -> datetime | None:
        """``LastModified`` for ``key``: from the last listing page, else a HEAD."""
        cached = self._listed_at.get(key)
        if cached is not None:
            return cached
        try:
            response = await self._call(
                lambda client: client.head_object(Bucket=self._config.bucket, Key=self._key(key))
            )
        except NotFound:
            return None
        when = response.get("LastModified")
        return when.astimezone(UTC) if isinstance(when, datetime) else None

    async def list_incomplete(self) -> Sequence[tuple[str, str, datetime]]:
        """``(upload_id, key, initiated_at)`` for every unfinished multipart upload."""
        pending: list[tuple[str, str, datetime]] = []
        key_marker: str | None = None
        id_marker: str | None = None
        while True:
            params: dict[str, Any] = {"Bucket": self._config.bucket}
            if key_marker is not None:
                params["KeyMarker"] = key_marker
                if id_marker is not None:
                    params["UploadIdMarker"] = id_marker
            response = await self._call(_multipart_list_call(params))
            for item in response.get("Uploads", []):
                initiated = item.get("Initiated")
                # SeaweedFS answers the call but omits ``Initiated``. Reporting
                # such an upload as started *now* is the safe direction: the
                # sweeper's age gate then never fires on that endpoint, so a
                # transfer still in flight is never aborted underneath its
                # client. Dropping the row instead would hide the upload from
                # the sweeper entirely, and calling it ancient would abort a
                # live one.
                when = (
                    initiated.astimezone(UTC)
                    if isinstance(initiated, datetime)
                    else self._clock.now()
                )
                pending.append((str(item["UploadId"]), str(item["Key"]), when))
            if not bool(response.get("IsTruncated")):
                return pending
            key_marker = str(response.get("NextKeyMarker") or "") or None
            id_marker = str(response.get("NextUploadIdMarker") or "") or None
            if key_marker is None:
                return pending

    async def abort(self, upload_id: str, key: str) -> None:
        """Abort one multipart upload. Missing is success — the goal is gone."""
        await self.multipart_abort(UploadHandle(key=key, upload_id=upload_id, size=0))

    # -- signing ---------------------------------------------------------

    def presign_get(self, key: str, *, range: tuple[int, int] | None, ttl: timedelta) -> str:
        params: dict[str, Any] = {"Bucket": self._config.bucket, "Key": self._key(key)}
        if range is not None:
            # The range is signed, so a holder of the URL cannot widen it.
            params["Range"] = f"bytes={range[0]}-{range[1]}"
        return str(
            self._presigner_factory().generate_presigned_url(
                "get_object", Params=params, ExpiresIn=int(ttl.total_seconds())
            )
        )

    def presign_put_part(
        self, handle: UploadHandle, part_no: int, *, size: int, ttl: timedelta
    ) -> str:
        params: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": handle.key,
            "UploadId": handle.upload_id,
            "PartNumber": part_no,
            "ContentLength": size,
        }
        return str(
            self._presigner_factory().generate_presigned_url(
                "upload_part", Params=params, ExpiresIn=int(ttl.total_seconds())
            )
        )

    async def vend_scoped_credentials(
        self, prefix: str, *, ttl: timedelta, read_only: bool
    ) -> ScopedCredentials:
        msg = "a plain S3-compatible endpoint vends no scoped credentials"
        raise NotImplementedError(msg)


async def _repack(data: AsyncIterator[bytes], part_size: int) -> AsyncIterator[bytes]:
    """Re-chunk an arbitrarily chunked stream into exact part buffers."""
    buffer = bytearray()
    async for chunk in data:
        buffer += chunk
        while len(buffer) >= part_size:
            yield bytes(buffer[:part_size])
            del buffer[:part_size]
    if buffer:
        yield bytes(buffer)


async def _stream_body(
    body: Any,
    manager: AbstractAsyncContextManager[Any],
    window: tuple[int, int] | None,
) -> AsyncIterator[bytes]:
    """Yield the object's bytes, then close the client that produced them."""
    try:
        if window is None:
            async for chunk in body.iter_chunks():
                yield chunk
            return
        start, end = window
        offset = 0
        async for chunk in body.iter_chunks():
            low = max(start - offset, 0)
            high = min(end + 1 - offset, len(chunk))
            offset += len(chunk)
            if high > low:
                yield chunk[low:high]
            if offset > end:
                return
    finally:
        await manager.__aexit__(None, None, None)
