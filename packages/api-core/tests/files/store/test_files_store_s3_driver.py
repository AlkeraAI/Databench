"""The S3 driver against a fake endpoint that actually stores bytes.

The fake keeps a dict of objects, so every assertion here is an observable
outcome — the object is there with those bytes, or it is gone — rather than a
record of which method was called. The wire itself (real S3 and SeaweedFS) is
proven by the conformance suite in the next stage; what is proven here is the
driver's own decisions: single vs multipart, the conditional header, the
head-after-put, the error table and the signed parameters.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.deployment_health import FILES_STORE_PROBE_KEY, probe_files_store
from alkera_core.files.clock import FakeClock
from alkera_core.files.store._retry import RetryBudget, RetryPolicy
from alkera_core.files.store.aws import AwsStore, domain_of
from alkera_core.files.store.errors import (
    AccessDenied,
    ChecksumMismatch,
    ExpiredCredentials,
    InvalidKey,
    InvalidRequest,
    NoSuchBucket,
    NotFound,
    PreconditionFailed,
    Throttled,
    Unavailable,
)
from alkera_core.files.store.protocol import UploadHandle
from alkera_core.files.store.s3_compatible import (
    LIFECYCLE_TAG_KEY,
    S3CompatibleStore,
    S3Config,
    lifecycle_class,
    normalize_client_error,
)
from blake3 import blake3
from botocore.exceptions import ClientError, EndpointConnectionError

EPOCH = datetime(2026, 3, 1, tzinfo=UTC)
BUCKET = "alkera-files"


def client_error(code: str, status: int, headers: dict[str, str] | None = None) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status, "HTTPHeaders": headers or {}},
        },
        "PutObject",
    )


class Body:
    """A botocore streaming body, chunked the way the real one is.

    It reads off a live connection, so it fails the way aiohttp does when the
    client that opened it has already been closed.
    """

    def __init__(self, data: bytes, endpoint: FakeS3 | None = None) -> None:
        self._data = data
        self._endpoint = endpoint

    async def iter_chunks(self, chunk_size: int = 8) -> AsyncIterator[bytes]:
        for offset in range(0, len(self._data), chunk_size):
            if self._endpoint is not None and self._endpoint.open_bodies == 0:
                raise RuntimeError("Connection closed.")
            yield self._data[offset : offset + chunk_size]


class FakeS3:
    """A dict-backed S3 endpoint: enough semantics to be worth asserting on."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        # The object tag set, as a real bucket keeps it: what the lifecycle
        # rules filter on. Absent means untagged.
        self.tagging: dict[str, str] = {}
        self.upload_tagging: dict[str, str] = {}
        self.uploads: dict[str, dict[int, bytes]] = {}
        # Every key the driver asked the endpoint to strip the tag set from,
        # by either verb.
        self.untagged: list[str] = []
        # Every `PutObjectTagging` call, verbatim.
        self.tag_puts: list[dict[str, Any]] = []
        self.copy_calls: list[dict[str, Any]] = []
        # Every `UploadPartCopy`, verbatim: which range of which source.
        self.copy_parts: list[dict[str, Any]] = []
        # What the endpoint refuses to copy in one call. AWS draws this at
        # 5 GiB; SeaweedFS draws it nowhere, which is why the ceiling is a
        # production-only fault.
        self.max_single_copy_bytes: int | None = None
        # A real object carries a content type; nothing this driver writes
        # declares one, so a moved object is where it can be observed.
        self.content_types: dict[str, str] = {}
        self.upload_content_types: dict[str, str] = {}
        self.bucket_heads: list[str] = []
        self.bucket_exists = True
        # SeaweedFS's measured divergence: it honours `TaggingDirective=REPLACE`
        # only when the copy declares a non-empty tag set, and keeps the
        # source's tag set when the copy declares none.
        self.ignores_empty_replace = False
        self.faults: dict[str, list[BaseException]] = {}
        self.presigned: list[dict[str, Any]] = []
        self.conditional_headers: list[str | None] = []
        self.complete_conditional_headers: list[str | None] = []
        self.honours_conditional_complete = True
        self.put_content_lengths: list[int | None] = []
        self.truncate_next_head: int | None = None
        self.ignores_range = False
        self.open_bodies = 0
        self.heads: list[str] = []
        self.multipart_creates: list[dict[str, Any]] = []
        self.uploaded_parts: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        # How many truncated-but-empty pages the endpoint answers before it
        # starts returning keys: a real S3 scans a bounded window per request,
        # so a sparse prefix legitimately produces them.
        self.empty_truncated_pages = 0

    # the store opens a client per call; the fake is its own client
    def __call__(self) -> Any:
        return _NullContext(self)

    def _maybe_fail(self, name: str) -> None:
        queue = self.faults.get(name)
        if queue:
            raise queue.pop(0)

    async def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail("put_object")
        key = kwargs["Key"]
        self.conditional_headers.append(kwargs.get("IfNoneMatch"))
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise client_error("PreconditionFailed", 412)
        # A real endpoint takes bytes or a seekable file; the driver hands it a
        # spool so a single put never holds the object twice, so read it the
        # way botocore does rather than assuming a buffer.
        body = kwargs["Body"]
        payload = body.read() if hasattr(body, "read") else body
        self.put_content_lengths.append(kwargs.get("ContentLength"))
        self.objects[key] = payload
        self._retag(key, kwargs.get("Tagging"))
        return {"ETag": f'"{blake3(payload).hexdigest()}"'}

    def _retag(self, key: str, tagging: str | None) -> None:
        if tagging is None:
            self.tagging.pop(key, None)
        else:
            self.tagging[key] = tagging

    async def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail("head_object")
        key = kwargs["Key"]
        self.heads.append(key)
        if key not in self.objects:
            raise client_error("NoSuchKey", 404)
        size = len(self.objects[key])
        if self.truncate_next_head is not None:
            size = self.truncate_next_head
            self.truncate_next_head = None
        info: dict[str, Any] = {
            "ContentLength": size,
            "ETag": '"etag"',
            "StorageClass": "STANDARD",
        }
        content_type = self.content_types.get(key)
        if content_type is not None:
            info["ContentType"] = content_type
        return info

    async def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail("get_object")
        key = kwargs["Key"]
        if key not in self.objects:
            raise client_error("NoSuchKey", 404)
        data = self.objects[key]
        if "Range" not in kwargs:
            return {"Body": Body(data, self)}
        if self.ignores_range:
            # What SeaweedFS does: 200 with the whole object and no
            # Content-Range, whatever was asked for.
            return {"Body": Body(data, self)}
        start, end = (int(part) for part in kwargs["Range"].removeprefix("bytes=").split("-"))
        if start >= len(data):
            raise client_error("InvalidRange", 416)
        end = min(end, len(data) - 1)
        return {
            "Body": Body(data[start : end + 1], self),
            "ContentRange": f"bytes {start}-{end}/{len(data)}",
        }

    async def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail("delete_object")
        self.objects.pop(kwargs["Key"], None)
        self.tagging.pop(kwargs["Key"], None)
        self.content_types.pop(kwargs["Key"], None)
        return {}

    async def head_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail("head_bucket")
        self.bucket_heads.append(kwargs["Bucket"])
        if not self.bucket_exists:
            # What a real endpoint answers: a HEAD carries no body, so the 404
            # names no error code at all.
            raise client_error("", 404)
        return {}

    async def copy_object(self, **kwargs: Any) -> dict[str, Any]:
        self.copy_calls.append(dict(kwargs))
        source = kwargs["CopySource"]["Key"]
        if source not in self.objects:
            raise client_error("NoSuchKey", 404)
        if (
            self.max_single_copy_bytes is not None
            and len(self.objects[source]) > self.max_single_copy_bytes
        ):
            # AWS: "The specified copy source is larger than the maximum
            # allowable size" -- a one-call copy of it is simply refused.
            raise client_error("InvalidRequest", 400)
        self.objects[kwargs["Key"]] = self.objects[source]
        # The default metadata directive is COPY, so the destination inherits
        # the source's content type as well as its bytes.
        source_type = self.content_types.get(source)
        if source_type is not None:
            self.content_types[kwargs["Key"]] = source_type
        # S3's default directive is COPY: the destination inherits the source's
        # tag set unless the caller says REPLACE.
        honours_replace = kwargs.get("TaggingDirective") == "REPLACE" and not (
            self.ignores_empty_replace and kwargs.get("Tagging") is None
        )
        if honours_replace:
            self._retag(kwargs["Key"], kwargs.get("Tagging"))
        else:
            self._retag(kwargs["Key"], self.tagging.get(source))
        return {}

    async def delete_object_tagging(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail("delete_object_tagging")
        key = kwargs["Key"]
        self.untagged.append(key)
        if key not in self.objects:
            raise client_error("NoSuchKey", 404)
        self.tagging.pop(key, None)
        return {}

    async def put_object_tagging(self, **kwargs: Any) -> dict[str, Any]:
        """S3's other way to clear a tag set: a replacement that names none.
        Charged to ``s3:PutObjectTagging``, not to the delete action."""
        self._maybe_fail("put_object_tagging")
        key = kwargs["Key"]
        self.tag_puts.append(dict(kwargs))
        if key not in self.objects:
            raise client_error("NoSuchKey", 404)
        tag_set = kwargs["Tagging"]["TagSet"]
        if tag_set:
            self.tagging[key] = "&".join(f"{tag['Key']}={tag['Value']}" for tag in tag_set)
        else:
            self.untagged.append(key)
            self.tagging.pop(key, None)
        return {}

    async def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.list_calls.append(dict(kwargs))
        if self.empty_truncated_pages > 0:
            self.empty_truncated_pages -= 1
            # What a real bucket does when the 1000-key scan window matched
            # nothing under the prefix: truncated, no Contents key at all.
            return {
                "IsTruncated": True,
                "NextContinuationToken": f"scan-{self.empty_truncated_pages}",
            }
        keys = sorted(k for k in self.objects if k.startswith(kwargs["Prefix"]))
        after = kwargs.get("StartAfter")
        if after is not None:
            keys = [k for k in keys if k > after]
        limit = kwargs["MaxKeys"]
        page = keys[:limit]
        return {
            "Contents": [{"Key": key} for key in page],
            "IsTruncated": len(keys) > limit,
        }

    async def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.multipart_creates.append(dict(kwargs))
        upload_id = f"upload-{len(self.uploads)}"
        self.uploads[upload_id] = {}
        tagging = kwargs.get("Tagging")
        if tagging is not None:
            self.upload_tagging[upload_id] = str(tagging)
        content_type = kwargs.get("ContentType")
        if content_type is not None:
            self.upload_content_types[upload_id] = str(content_type)
        return {"UploadId": upload_id}

    async def upload_part_copy(self, **kwargs: Any) -> dict[str, Any]:
        """Stage one part cut straight out of another object, as S3 does.

        The range is inclusive on both ends and is what the endpoint reads --
        so a driver that miscounted its ranges assembles the wrong bytes here
        rather than passing on a call it merely made.
        """
        self._maybe_fail("upload_part_copy")
        self.copy_parts.append(dict(kwargs))
        source = kwargs["CopySource"]["Key"]
        if source not in self.objects:
            raise client_error("NoSuchKey", 404)
        first, last = (
            int(edge) for edge in kwargs["CopySourceRange"].removeprefix("bytes=").split("-")
        )
        data = self.objects[source][first : last + 1]
        if not data:
            raise client_error("InvalidRange", 416)
        self.uploads[kwargs["UploadId"]][kwargs["PartNumber"]] = data
        return {"CopyPartResult": {"ETag": f'"copy-{kwargs["PartNumber"]}"'}}

    async def upload_part(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail("upload_part")
        self.uploaded_parts.append(dict(kwargs))
        self.uploads[kwargs["UploadId"]][kwargs["PartNumber"]] = kwargs["Body"]
        return {"ETag": f'"part-{kwargs["PartNumber"]}"'}

    async def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail("complete_multipart_upload")
        parts = self.uploads.pop(kwargs["UploadId"])
        declared = [entry["PartNumber"] for entry in kwargs["MultipartUpload"]["Parts"]]
        if declared != sorted(parts):
            raise client_error("InvalidPart", 400)
        conditional = kwargs.get("IfNoneMatch")
        self.complete_conditional_headers.append(conditional)
        # An endpoint that implements the header on *complete* refuses the
        # assembly; one that does not (SeaweedFS today) ignores it silently,
        # which is exactly the case the driver's head fallback exists for.
        if (
            self.honours_conditional_complete
            and conditional == "*"
            and kwargs["Key"] in self.objects
        ):
            raise client_error("PreconditionFailed", 412)
        self.objects[kwargs["Key"]] = b"".join(parts[n] for n in sorted(parts))
        # A real endpoint applies the tag set declared at create time.
        self._retag(kwargs["Key"], self.upload_tagging.pop(kwargs["UploadId"], None))
        content_type = self.upload_content_types.pop(kwargs["UploadId"], None)
        if content_type is not None:
            self.content_types[kwargs["Key"]] = content_type
        return {"ETag": '"multipart"'}

    async def abort_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.uploads.pop(kwargs["UploadId"], None)
        return {}

    def generate_presigned_url(self, operation: str, **kwargs: Any) -> str:
        self.presigned.append({"operation": operation, **kwargs})
        return f"https://fake.invalid/{operation}?expires={kwargs['ExpiresIn']}"


class _NullContext:
    """Counts open clients, so a body read after its client closed is visible."""

    def __init__(self, client: FakeS3) -> None:
        self._client = client

    async def __aenter__(self) -> FakeS3:
        self._client.open_bodies += 1
        return self._client

    async def __aexit__(self, *exc: object) -> None:
        self._client.open_bodies -= 1


@pytest.fixture
def fake() -> FakeS3:
    return FakeS3()


def build(
    fake: FakeS3, *, part_bytes: int = 8, layout: str = "domain", **overrides: Any
) -> S3CompatibleStore:
    clock = FakeClock(EPOCH)
    config = replace(
        S3Config(endpoint_url="https://fake.invalid", region="us-west-2", bucket=BUCKET),
        **overrides,
    )
    store = S3CompatibleStore(
        config,
        clock=clock,
        layout=layout,  # type: ignore[arg-type]
        client_factory=fake,
        presigner_factory=lambda: fake,
        sleep=_no_sleep,
        policy=RetryPolicy(jitter=lambda: 0.0),
        budget=RetryBudget(10.0, 0.0, clock=clock),
    )
    store.capabilities = replace(store.capabilities, min_part_bytes=part_bytes)
    return store


async def _no_sleep(seconds: float) -> None:
    return None


async def stream(data: bytes, chunk: int = 3) -> AsyncIterator[bytes]:
    for offset in range(0, len(data), chunk):
        yield data[offset : offset + chunk]


async def drain(chunks: AsyncIterator[bytes]) -> bytes:
    out = bytearray()
    async for chunk in chunks:
        out += chunk
    return bytes(out)


# -- writes ---------------------------------------------------------------


async def test_a_small_object_goes_up_in_one_put(fake: FakeS3) -> None:
    payload = b"a small object"
    store = build(fake, part_bytes=64)

    result = await store.put(
        "objects/o/small", stream(payload), size=len(payload), checksum=blake3(payload).digest()
    )

    assert fake.objects["objects/o/small"] == payload
    assert fake.uploads == {}  # no multipart session was ever opened
    assert result.size == len(payload)
    assert result.checksum == blake3(payload).digest()


async def test_an_object_at_twice_the_part_size_goes_up_multipart(fake: FakeS3) -> None:
    payload = bytes(range(32))
    store = build(fake, part_bytes=8)

    result = await store.put(
        "objects/o/big", stream(payload), size=len(payload), checksum=blake3(payload).digest()
    )

    # reassembled from four exact parts, whatever the source chunking was
    assert fake.objects["objects/o/big"] == payload
    assert result.size == 32
    assert fake.uploads == {}  # the session was completed, not left open


@pytest.mark.parametrize(
    ("size", "multipart"),
    [
        pytest.param(15, False, id="one-under-the-threshold"),
        pytest.param(16, True, id="exactly-the-threshold"),
        pytest.param(17, True, id="one-over-the-threshold"),
    ],
)
async def test_the_multipart_threshold_is_twice_the_part_size(
    fake: FakeS3, size: int, multipart: bool
) -> None:
    payload = bytes(range(size))
    store = build(fake, part_bytes=8)

    await store.put(
        f"objects/o/{size}", stream(payload), size=size, checksum=blake3(payload).digest()
    )

    assert fake.objects[f"objects/o/{size}"] == payload
    # A single put carries the conditional on PutObject and a multipart one on
    # CompleteMultipartUpload; whichever call assembles the object is the one
    # that has to be conditional, so exactly one of the two lists records it.
    assert (fake.conditional_headers == []) is multipart
    assert (fake.complete_conditional_headers == ["*"]) is multipart


async def test_if_absent_is_the_only_thing_that_sends_the_conditional_header(
    fake: FakeS3,
) -> None:
    payload = b"conditional"
    store = build(fake, part_bytes=64)

    await store.put(
        "objects/o/a", stream(payload), size=len(payload), checksum=blake3(payload).digest()
    )
    await store.put(
        "objects/o/b",
        stream(payload),
        size=len(payload),
        checksum=blake3(payload).digest(),
        if_absent=False,
    )

    assert fake.conditional_headers == ["*", None]


async def test_a_conditional_write_that_loses_raises_precondition_failed(fake: FakeS3) -> None:
    payload = b"first writer wins"
    store = build(fake, part_bytes=64)
    await store.put(
        "objects/o/a", stream(payload), size=len(payload), checksum=blake3(payload).digest()
    )

    with pytest.raises(PreconditionFailed):
        await store.put(
            "objects/o/a",
            stream(b"second"),
            size=6,
            checksum=blake3(b"second").digest(),
        )

    assert fake.objects["objects/o/a"] == payload  # the first write survives


async def test_a_disabled_conditional_write_never_sends_the_header(fake: FakeS3) -> None:
    payload = b"no conditional support"
    store = build(fake, part_bytes=64, conditional_write=False)

    await store.put(
        "objects/o/a", stream(payload), size=len(payload), checksum=blake3(payload).digest()
    )

    assert fake.conditional_headers == [None]
    assert store.capabilities.conditional_write is False


async def test_a_declared_checksum_that_does_not_match_deletes_the_object(fake: FakeS3) -> None:
    payload = b"the bytes we actually sent"
    store = build(fake, part_bytes=64)

    with pytest.raises(ChecksumMismatch):
        await store.put(
            "objects/o/a", stream(payload), size=len(payload), checksum=blake3(b"other").digest()
        )

    assert "objects/o/a" not in fake.objects


async def test_a_head_that_reports_a_short_object_deletes_it(fake: FakeS3) -> None:
    payload = b"truncated on the way in"
    store = build(fake, part_bytes=64)
    fake.truncate_next_head = 3

    with pytest.raises(ChecksumMismatch):
        await store.put(
            "objects/o/a", stream(payload), size=len(payload), checksum=blake3(payload).digest()
        )

    assert "objects/o/a" not in fake.objects


async def test_a_failed_multipart_upload_aborts_the_session(fake: FakeS3) -> None:
    store = build(fake, part_bytes=8)
    fake.faults["upload_part"] = [client_error("AccessDenied", 403)]
    payload = bytes(32)

    with pytest.raises(AccessDenied):
        await store.put(
            "objects/o/big", stream(payload), size=32, checksum=blake3(payload).digest()
        )

    assert fake.uploads == {}  # nothing left dangling to be billed for


# -- reads ----------------------------------------------------------------


async def test_get_streams_the_object_and_a_range_of_it(fake: FakeS3) -> None:
    payload = bytes(range(32))
    store = build(fake, part_bytes=64)
    await store.put("objects/o/a", stream(payload), size=32, checksum=blake3(payload).digest())

    assert await drain(await store.get("objects/o/a")) == payload
    assert await drain(await store.get("objects/o/a", range=(4, 7))) == payload[4:8]


async def test_head_reports_none_for_a_missing_key(fake: FakeS3) -> None:
    store = build(fake)

    assert await store.head("objects/o/missing") is None


async def test_deleting_a_missing_key_is_silent(fake: FakeS3) -> None:
    store = build(fake)

    await store.delete("objects/o/missing")  # no raise

    assert fake.objects == {}


async def test_move_copies_then_removes_the_source(fake: FakeS3) -> None:
    payload = b"moved"
    store = build(fake, part_bytes=64)
    await store.put(
        "objects/incoming/x", stream(payload), size=5, checksum=blake3(payload).digest()
    )

    await store.move("objects/incoming/x", "objects/o/x")

    assert fake.objects == {"objects/o/x": payload}


async def test_list_prefix_pages_with_start_after(fake: FakeS3) -> None:
    store = build(fake, part_bytes=64)
    for name in ("a", "b", "c", "d"):
        await store.put(f"objects/o/{name}", stream(b"x"), size=1, checksum=blake3(b"x").digest())
    await store.put("erased/o/z", stream(b"x"), size=1, checksum=blake3(b"x").digest())

    first = await store.list_prefix("objects/", limit=2)
    second = await store.list_prefix("objects/", after=first.next_after, limit=2)

    assert list(first.keys) == ["objects/o/a", "objects/o/b"]
    assert first.next_after == "objects/o/b"
    assert list(second.keys) == ["objects/o/c", "objects/o/d"]
    assert second.next_after is None  # the last page is not truncated, so paging stops


# -- error normalisation --------------------------------------------------


@pytest.mark.parametrize(
    ("code", "status", "expected", "normalised_status"),
    [
        # a throttle is always recorded as 429 whatever status the endpoint chose,
        # because that status is what makes it the one retryable 4xx
        pytest.param("SlowDown", 503, Throttled, 429, id="slowdown"),
        pytest.param("ThrottlingException", 400, Throttled, 429, id="throttling-exception"),
        pytest.param("", 429, Throttled, 429, id="bare-429"),
        pytest.param("NoSuchKey", 404, NotFound, 404, id="no-such-key"),
        pytest.param("NoSuchUpload", 404, NotFound, 404, id="no-such-upload"),
        pytest.param("", 404, NotFound, 404, id="bare-404"),
        pytest.param("PreconditionFailed", 412, PreconditionFailed, 412, id="precondition"),
        pytest.param("InternalError", 500, Unavailable, 500, id="internal-error"),
        pytest.param("ServiceUnavailable", 503, Unavailable, 503, id="service-unavailable"),
        pytest.param("AccessDenied", 403, AccessDenied, 403, id="access-denied"),
        pytest.param("ExpiredToken", 400, ExpiredCredentials, 400, id="expired-token"),
        pytest.param("InvalidToken", 400, ExpiredCredentials, 400, id="invalid-token"),
        pytest.param("", 0, Unavailable, 0, id="non-xml-body"),
        # Permanent faults in the request: a caller retrying any of these
        # retries forever, so none of them may look like an outage.
        pytest.param("EntityTooSmall", 400, InvalidRequest, 400, id="entity-too-small"),
        pytest.param("EntityTooLarge", 400, InvalidRequest, 400, id="entity-too-large"),
        pytest.param("InvalidPart", 400, InvalidRequest, 400, id="invalid-part"),
        pytest.param("InvalidPartOrder", 400, InvalidRequest, 400, id="invalid-part-order"),
        pytest.param("BadDigest", 400, InvalidRequest, 400, id="bad-digest"),
        pytest.param("InvalidDigest", 400, InvalidRequest, 400, id="invalid-digest"),
        pytest.param("MalformedXML", 400, InvalidRequest, 400, id="malformed-xml"),
        pytest.param("KeyTooLongError", 400, InvalidRequest, 400, id="key-too-long"),
        pytest.param("InvalidArgument", 400, InvalidRequest, 400, id="invalid-argument"),
        # A cold-tier object read before it is restored: permanent until the
        # caller restores it, and AWS answers 403.
        pytest.param("InvalidObjectState", 403, InvalidRequest, 403, id="invalid-object-state"),
    ],
)
async def test_every_client_error_normalises_onto_the_typed_set(
    code: str, status: int, expected: type[Exception], normalised_status: int
) -> None:
    mapped = normalize_client_error(client_error(code, status))

    assert type(mapped) is expected
    assert mapped.status_code == normalised_status  # type: ignore[attr-defined]


async def test_a_throttle_carries_the_retry_after_header_as_a_float() -> None:
    mapped = normalize_client_error(client_error("SlowDown", 503, {"retry-after": "2.5"}))

    assert isinstance(mapped, Throttled)
    assert mapped.retry_after == pytest.approx(2.5)


async def test_a_connection_failure_normalises_to_unavailable() -> None:
    mapped = normalize_client_error(EndpointConnectionError(endpoint_url="https://fake.invalid"))

    assert isinstance(mapped, Unavailable)


async def test_a_throttle_on_the_wire_is_retried_and_the_object_still_lands(
    fake: FakeS3,
) -> None:
    payload = b"eventually consistent enough"
    store = build(fake, part_bytes=64)
    fake.faults["put_object"] = [client_error("SlowDown", 503)]

    await store.put(
        "objects/o/a", stream(payload), size=len(payload), checksum=blake3(payload).digest()
    )

    assert fake.objects["objects/o/a"] == payload


# -- signing --------------------------------------------------------------


async def test_presign_get_puts_the_range_in_the_signed_parameters(fake: FakeS3) -> None:
    store = build(fake)

    url = store.presign_get("objects/o/a", range=(0, 1023), ttl=timedelta(minutes=5))

    assert url == "https://fake.invalid/get_object?expires=300"
    assert fake.presigned[0]["Params"]["Range"] == "bytes=0-1023"


async def test_presign_get_without_a_range_signs_no_range(fake: FakeS3) -> None:
    store = build(fake)

    store.presign_get("objects/o/a", range=None, ttl=timedelta(minutes=5))

    assert "Range" not in fake.presigned[0]["Params"]


async def test_presign_put_part_signs_the_part_and_its_length(fake: FakeS3) -> None:
    store = build(fake)
    handle = UploadHandle(key="objects/o/a", upload_id="u1", size=100)

    url = store.presign_put_part(handle, 3, size=64, ttl=timedelta(minutes=15))

    assert url == "https://fake.invalid/upload_part?expires=900"
    assert fake.presigned[0]["Params"]["PartNumber"] == 3
    assert fake.presigned[0]["Params"]["ContentLength"] == 64


async def test_a_plain_endpoint_vends_no_scoped_credentials(fake: FakeS3) -> None:
    store = build(fake)

    assert store.capabilities.scoped_credentials is False
    with pytest.raises(NotImplementedError):
        await store.vend_scoped_credentials("objects/", ttl=timedelta(minutes=15), read_only=True)


# -- multipart contract ---------------------------------------------------


async def test_a_part_whose_bytes_do_not_match_its_checksum_is_refused(fake: FakeS3) -> None:
    store = build(fake, part_bytes=8)
    handle = await store.multipart_create("objects/o/a", size=32)

    with pytest.raises(ChecksumMismatch):
        await store.multipart_put_part(
            handle, 1, stream(b"12345678"), size=8, checksum=blake3(b"other").digest()
        )

    assert fake.uploads[handle.upload_id] == {}


async def test_a_completed_multipart_upload_carries_the_store_etags(fake: FakeS3) -> None:
    store = build(fake, part_bytes=8)
    handle = await store.multipart_create("objects/o/a", size=16)
    parts = [
        await store.multipart_put_part(
            handle, n, stream(bytes([n]) * 8), size=8, checksum=blake3(bytes([n]) * 8).digest()
        )
        for n in (1, 2)
    ]

    whole = b"".join(bytes([n]) * 8 for n in (1, 2))
    result = await store.multipart_complete(handle, parts, checksum=blake3(whole).digest())

    assert [part.etag for part in parts] == ['"part-1"', '"part-2"']
    assert result.size == 16
    assert fake.objects["objects/o/a"] == bytes([1]) * 8 + bytes([2]) * 8


async def test_aborting_an_upload_leaves_no_session(fake: FakeS3) -> None:
    store = build(fake, part_bytes=8)
    handle = await store.multipart_create("objects/o/a", size=16)

    await store.multipart_abort(handle)

    assert fake.uploads == {}


# -- the AWS variant ------------------------------------------------------


class FakeSts:
    open_bodies = 0

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self) -> Any:
        return _NullContext(self)  # type: ignore[arg-type]

    async def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "AKIAFAKE",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": EPOCH + timedelta(minutes=15),
            }
        }


async def test_the_aws_variant_scopes_a_credential_with_a_domain_session_tag(
    fake: FakeS3,
) -> None:
    sts = FakeSts()
    store = AwsStore(
        S3Config(
            endpoint_url=None,
            region="us-west-2",
            bucket=BUCKET,
            vend_role_arn="arn:aws:iam::1:role/files-vend",
        ),
        clock=FakeClock(EPOCH),
        sts_factory=sts,
        client_factory=fake,
        presigner_factory=lambda: fake,
    )

    credentials = await store.vend_scoped_credentials(
        "domains/11111111-2222-3333-4444-555555555555/o/",
        ttl=timedelta(minutes=15),
        read_only=True,
    )

    assert sts.calls[0]["Tags"] == [
        {"Key": "domainId", "Value": "11111111-2222-3333-4444-555555555555"}
    ]
    assert sts.calls[0]["DurationSeconds"] == 900
    assert credentials.access_key_id == "AKIAFAKE"
    assert credentials.expires_at == EPOCH + timedelta(minutes=15)
    assert credentials.read_only is True
    assert store.capabilities.scoped_credentials is True


async def test_the_aws_admin_handle_answers_the_readiness_probe_key(fake: FakeS3) -> None:
    """The readiness probe HEADs a bucket-absolute key under the zero domain
    through the provider's admin handle. The AWS driver must take absolute
    keys like the vended handles it mints; under the base class's domain
    layout the key was refused as "relative key may not carry the domains/
    prefix", the probe failed closed, and no instance with Files on ever
    became ready on AWS."""
    store = AwsStore(
        S3Config(endpoint_url=None, region="us-west-2", bucket=BUCKET),
        clock=FakeClock(EPOCH),
        sts_factory=FakeSts(),
        client_factory=fake,
        presigner_factory=lambda: fake,
    )
    assert store.layout == "bucket"
    # A fresh bucket has no probe object: the honest answer is "no such key",
    # which proves the store answered.
    assert await store.head(FILES_STORE_PROBE_KEY) is None
    assert fake.heads[-1] == FILES_STORE_PROBE_KEY
    assert await probe_files_store(store) is True


async def test_the_aws_variant_refuses_to_vend_without_a_role(fake: FakeS3) -> None:
    store = AwsStore(
        S3Config(endpoint_url=None, region="us-west-2", bucket=BUCKET),
        clock=FakeClock(EPOCH),
        sts_factory=FakeSts(),
        client_factory=fake,
        presigner_factory=lambda: fake,
    )

    with pytest.raises(NotImplementedError):
        await store.vend_scoped_credentials("objects/", ttl=timedelta(minutes=15), read_only=False)


@pytest.mark.parametrize(
    "prefix",
    [
        pytest.param("o/abcd", id="no-domain-prefix"),
        pytest.param("domains/", id="empty-domain"),
    ],
)
def test_a_prefix_that_names_no_domain_cannot_be_scoped(prefix: str) -> None:
    with pytest.raises(InvalidKey):
        domain_of(prefix)


def test_the_kms_capability_follows_the_configured_key(fake: FakeS3) -> None:
    without = build(fake)
    with_key = build(fake, kms_key_id="arn:aws:kms:us-west-2:1:key/abcd")

    assert without.capabilities.kms is False
    assert with_key.capabilities.kms is True


async def test_a_kms_key_is_passed_through_on_every_write(fake: FakeS3) -> None:
    store = build(fake, part_bytes=64, kms_key_id="arn:aws:kms:us-west-2:1:key/abcd")
    seen: list[dict[str, Any]] = []
    original = fake.put_object

    async def capture(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return await original(**kwargs)

    fake.put_object = capture  # type: ignore[method-assign]
    with contextlib.suppress(ChecksumMismatch):
        await store.put("objects/o/a", stream(b"x"), size=1, checksum=blake3(b"x").digest())

    assert seen[0]["SSEKMSKeyId"] == "arn:aws:kms:us-west-2:1:key/abcd"


def test_a_store_error_passes_through_normalisation_unchanged() -> None:
    original = NotFound("already typed")

    assert normalize_client_error(original) is original


# -- the driver gaps the live SeaweedFS conformance run found -------------


async def test_a_streamed_body_outlives_the_client_that_opened_it(fake: FakeS3) -> None:
    payload = b"a body big enough that the SDK has not buffered it" * 40
    store = build(fake, part_bytes=1 << 20)
    await store.put(
        "objects/o/big", stream(payload), size=len(payload), checksum=blake3(payload).digest()
    )

    assert await drain(await store.get("objects/o/big")) == payload


async def test_a_range_past_the_end_is_refused_even_by_a_lenient_endpoint(fake: FakeS3) -> None:
    payload = b"0123456789"
    store = build(fake, part_bytes=64)
    await store.put(
        "objects/o/r", stream(payload), size=len(payload), checksum=blake3(payload).digest()
    )
    fake.ignores_range = True

    with pytest.raises(PreconditionFailed):
        await drain(await store.get("objects/o/r", range=(10, 20)))


async def test_a_lenient_endpoints_whole_object_is_cut_to_the_window(fake: FakeS3) -> None:
    payload = b"0123456789"
    store = build(fake, part_bytes=64)
    await store.put(
        "objects/o/r", stream(payload), size=len(payload), checksum=blake3(payload).digest()
    )
    fake.ignores_range = True

    assert await drain(await store.get("objects/o/r", range=(3, 6))) == b"3456"


async def test_a_part_over_the_capability_is_refused_and_the_session_closed(fake: FakeS3) -> None:
    store = build(fake, part_bytes=8)
    handle = await store.multipart_create("objects/o/m", size=1)
    oversize = store.capabilities.max_part_bytes + 1

    async def unread() -> AsyncIterator[bytes]:
        raise AssertionError("the driver read bytes it had to refuse first")
        yield b""

    with pytest.raises(PreconditionFailed):
        await store.multipart_put_part(
            handle, 1, unread(), size=oversize, checksum=blake3(b"").digest()
        )

    assert fake.uploads == {}, "the refused session was left open"


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param("missing", id="a-part-left-out"),
        pytest.param("resized", id="a-part-with-a-different-size"),
        pytest.param("checksum", id="a-part-with-the-wrong-checksum"),
        pytest.param("extra", id="a-part-that-was-never-staged"),
        pytest.param("no-etag", id="a-part-with-no-etag"),
        pytest.param("reordered", id="a-part-list-out-of-order"),
    ],
)
async def test_complete_refuses_a_spliced_part_list_without_asking_the_endpoint(
    fake: FakeS3, damage: str
) -> None:
    store = build(fake, part_bytes=8)
    handle = await store.multipart_create("objects/o/m", size=16)
    staged = [
        await store.multipart_put_part(
            handle, number, stream(part), size=len(part), checksum=blake3(part).digest()
        )
        for number, part in enumerate([b"aaaaaaaa", b"bbbb"], start=1)
    ]
    if damage == "missing":
        declared = staged[:1]
    elif damage == "resized":
        declared = [staged[0], replace(staged[1], size=99)]
    elif damage == "checksum":
        declared = [staged[0], replace(staged[1], checksum=blake3(b"not it").digest())]
    elif damage == "extra":
        declared = [*staged, replace(staged[1], part_no=3)]
    elif damage == "no-etag":
        declared = [staged[0], replace(staged[1], etag=None)]
    else:
        declared = [staged[1], staged[0]]

    with pytest.raises(PreconditionFailed):
        await store.multipart_complete(handle, declared)

    # The endpoint was never asked: the session is still open and no object
    # was assembled, so a lenient endpoint had no chance to accept the splice.
    assert handle.upload_id in fake.uploads
    assert "objects/o/m" not in fake.objects


async def test_complete_accepts_the_list_it_staged(fake: FakeS3) -> None:
    store = build(fake, part_bytes=8)
    handle = await store.multipart_create("objects/o/m", size=12)
    staged = [
        await store.multipart_put_part(
            handle, number, stream(part), size=len(part), checksum=blake3(part).digest()
        )
        for number, part in enumerate([b"aaaaaaaa", b"bbbb"], start=1)
    ]

    result = await store.multipart_complete(
        handle, staged, checksum=blake3(b"aaaaaaaabbbb").digest()
    )

    assert result.size == 12
    assert fake.objects["objects/o/m"] == b"aaaaaaaabbbb"


# -- the conditional survives the multipart threshold ----------------------


async def test_a_conditional_put_above_the_threshold_sends_the_header_on_complete(
    fake: FakeS3,
) -> None:
    """The bug this pins: a large conditional put that silently overwrote."""
    store = build(fake, part_bytes=8)
    payload = bytes(32)

    await store.put("objects/o/big", stream(payload), size=32, checksum=blake3(payload).digest())

    assert fake.complete_conditional_headers == ["*"]


async def test_a_conditional_put_above_the_threshold_loses_to_the_object_already_there(
    fake: FakeS3,
) -> None:
    store = build(fake, part_bytes=8)
    first = b"a" * 32
    second = b"b" * 32
    await store.put("objects/o/big", stream(first), size=32, checksum=blake3(first).digest())

    with pytest.raises(PreconditionFailed):
        await store.put("objects/o/big", stream(second), size=32, checksum=blake3(second).digest())

    # The loser overwrote nothing and left no upload behind to be billed for.
    assert fake.objects["objects/o/big"] == first
    assert fake.uploads == {}


async def test_an_endpoint_that_ignores_the_conditional_on_complete_is_refused_by_the_head(
    fake: FakeS3,
) -> None:
    """SeaweedFS's shape: the header is accepted and ignored, so the head refuses.

    The window between the head and the assembly is not closed here — only a
    conditional complete closes it — but the overwrite is.
    """
    fake.honours_conditional_complete = False
    store = build(fake, part_bytes=8)
    first = b"a" * 32
    await store.put("objects/o/big", stream(first), size=32, checksum=blake3(first).digest())

    with pytest.raises(PreconditionFailed):
        await store.put(
            "objects/o/big", stream(b"b" * 32), size=32, checksum=blake3(b"b" * 32).digest()
        )

    assert fake.objects["objects/o/big"] == first
    assert fake.uploads == {}


async def test_a_non_conditional_put_above_the_threshold_replaces_the_object(
    fake: FakeS3,
) -> None:
    """The negative twin: without ``if_absent`` the large put still overwrites."""
    store = build(fake, part_bytes=8)
    await store.put(
        "objects/o/big", stream(b"a" * 32), size=32, checksum=blake3(b"a" * 32).digest()
    )

    second = b"b" * 32
    await store.put(
        "objects/o/big",
        stream(second),
        size=32,
        checksum=blake3(second).digest(),
        if_absent=False,
    )

    assert fake.objects["objects/o/big"] == second
    assert fake.complete_conditional_headers == ["*", None]


# -- the driver validates its own keys -------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param("../escape", id="parent-segment"),
        pytest.param("/absolute", id="absolute"),
        pytest.param("objects//empty", id="empty-segment"),
        pytest.param("Objects/Upper", id="uppercase"),
        pytest.param("domains/11111111-2222-3333-4444-555555555555/x", id="domain-prefix"),
        pytest.param("objects/nul\x00byte", id="nul-byte"),
    ],
)
async def test_a_hostile_key_never_reaches_the_endpoint(fake: FakeS3, hostile: str) -> None:
    """The raw driver is reachable without a domain handle, so it validates too."""
    store = build(fake)

    with pytest.raises(InvalidKey):
        await store.put(hostile, stream(b"x"), size=1, checksum=blake3(b"x").digest())
    with pytest.raises(InvalidKey):
        await store.head(hostile)
    with pytest.raises(InvalidKey):
        await store.delete(hostile)
    with pytest.raises(InvalidKey):
        await store.multipart_create(hostile, size=1)

    assert fake.objects == {}
    assert fake.uploads == {}


async def test_the_bucket_layout_takes_absolute_keys_and_refuses_relative_ones(
    fake: FakeS3,
) -> None:
    """The admin handle addresses the bucket, so its namespace is the absolute one."""
    admin = build(fake, layout="bucket")
    key = "domains/11111111-2222-3333-4444-555555555555/objects/ab/cd/x"

    await admin.put(key, stream(b"x"), size=1, checksum=blake3(b"x").digest())

    assert fake.objects[key] == b"x"
    with pytest.raises(InvalidKey):
        await admin.head("objects/ab/cd/x")


# -- the declared size is enforced -----------------------------------------


@pytest.mark.parametrize(
    ("declared", "sent"),
    [
        pytest.param(33, 32, id="one-byte-short"),
        pytest.param(31, 32, id="one-byte-long"),
    ],
)
async def test_a_multipart_put_whose_stream_disagrees_with_its_size_leaves_nothing(
    fake: FakeS3, declared: int, sent: int
) -> None:
    store = build(fake, part_bytes=8)
    payload = bytes(sent)

    with pytest.raises(InvalidRequest):
        await store.put(
            "objects/o/big", stream(payload), size=declared, checksum=blake3(payload).digest()
        )

    assert "objects/o/big" not in fake.objects
    assert fake.uploads == {}


async def test_a_single_put_streams_through_a_spool_and_declares_its_length(
    fake: FakeS3,
) -> None:
    """The bytes reach the endpoint as a seekable body with a length, not a buffer."""
    store = build(fake, part_bytes=1 << 20)
    payload = b"x" * 4096

    async def dribble() -> AsyncIterator[bytes]:
        for offset in range(0, len(payload), 7):
            yield payload[offset : offset + 7]

    await store.put("objects/o/s", dribble(), size=len(payload), checksum=blake3(payload).digest())

    assert fake.objects["objects/o/s"] == payload
    assert fake.put_content_lengths == [len(payload)]


async def test_an_empty_object_is_one_put_object_and_never_a_partless_multipart(
    fake: FakeS3,
) -> None:
    """A zero-byte file is a single empty ``PutObject``.

    S3 refuses ``CompleteMultipartUpload`` with no parts, so routing the empty
    case down the multipart path would make an empty file uncreatable against
    a real endpoint — and an upload session sends exactly one zero-byte part
    for a ``declaredSize: 0`` file. ``head`` has to see it afterwards, because
    that is how the session proves its staged part is really in the store.
    """
    store = build(fake, part_bytes=8)

    async def nothing() -> AsyncIterator[bytes]:
        return
        yield b""  # pragma: no cover - keeps this an async generator

    result = await store.put("objects/o/e", nothing(), size=0, checksum=blake3(b"").digest())

    assert result.size == 0
    assert fake.objects["objects/o/e"] == b""
    assert fake.put_content_lengths == [0]
    assert fake.multipart_creates == [], "an empty object must not open a multipart upload"
    info = await store.head("objects/o/e")
    assert info is not None and info.size == 0


# -- listing: a truncated page is not the end of the listing ---------------


async def test_a_truncated_page_with_no_keys_continues_from_the_continuation_token(
    fake: FakeS3,
) -> None:
    """A sparse prefix scan must not read as "there is nothing left".

    S3 scans a bounded window of the bucket per request, so a page can come
    back ``IsTruncated`` with zero ``Contents``. Answering that as the end of
    the listing is how a reconciler stops seeing objects that are still there
    — and never sweeps them.
    """
    store = build(fake)
    for name in ("a", "b", "c"):
        fake.objects[f"objects/o/{name}"] = b"x"
    fake.empty_truncated_pages = 2

    page = await store.list_prefix("objects/o/")

    assert list(page.keys) == ["objects/o/a", "objects/o/b", "objects/o/c"]
    # the continuation token, not a re-derived StartAfter, is what carried the scan
    assert [call.get("ContinuationToken") for call in fake.list_calls] == [
        None,
        "scan-1",
        "scan-0",
    ]
    assert all("StartAfter" not in call for call in fake.list_calls[1:])


async def test_a_truncated_page_that_offers_no_token_still_terminates(fake: FakeS3) -> None:
    """An endpoint that truncates without a token cannot spin the driver forever."""
    calls: list[dict[str, Any]] = []

    async def truncated_forever(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"IsTruncated": True}

    fake.list_objects_v2 = truncated_forever  # type: ignore[method-assign]
    store = build(fake)

    page = await store.list_prefix("objects/o/")

    assert list(page.keys) == []
    assert len(calls) == 1


# -- the conditional complete: probed, not assumed -------------------------


@pytest.mark.parametrize("honours", [True, False], ids=["endpoint-honours", "endpoint-ignores"])
async def test_the_conditional_complete_probe_reports_what_the_endpoint_does(
    fake: FakeS3, *, honours: bool
) -> None:
    """The capability is an observation, never a restatement of conditional_write."""
    fake.honours_conditional_complete = honours
    store = build(fake)
    assert store.capabilities.conditional_complete is False

    probed = await store.probe_conditional_complete()

    assert probed is honours
    assert store.capabilities.conditional_complete is honours
    # the sentinel is cleaned up whichever way the endpoint answered
    assert fake.objects == {}
    assert fake.uploads == {}


async def test_the_conditional_complete_probe_is_asked_once_per_store(fake: FakeS3) -> None:
    store = build(fake)

    first = await store.probe_conditional_complete()
    creates = len(fake.multipart_creates)
    second = await store.probe_conditional_complete()

    assert (first, second) == (True, True)
    assert len(fake.multipart_creates) == creates  # cached, not re-probed


async def test_a_probed_endpoint_refuses_a_conditional_put_at_the_assembly(
    fake: FakeS3,
) -> None:
    """Where the header works, the *complete* is the refusal — no head, no window."""
    payload = bytes(range(32))
    store = build(fake, part_bytes=8)
    fake.objects["objects/o/big"] = b"already here"
    await store.probe_conditional_complete()
    fake.heads.clear()

    with pytest.raises(PreconditionFailed):
        await store.put(
            "objects/o/big",
            stream(payload),
            size=len(payload),
            checksum=blake3(payload).digest(),
            if_absent=True,
        )

    assert fake.objects["objects/o/big"] == b"already here"
    assert fake.complete_conditional_headers[-1] == "*"
    # the atomic branch never consults the key first
    assert "objects/o/big" not in fake.heads


async def test_an_endpoint_that_ignores_the_header_falls_back_to_the_head(
    fake: FakeS3,
) -> None:
    payload = bytes(range(32))
    fake.honours_conditional_complete = False
    store = build(fake, part_bytes=8)
    fake.objects["objects/o/big"] = b"already here"
    await store.probe_conditional_complete()
    fake.heads.clear()

    with pytest.raises(PreconditionFailed):
        await store.put(
            "objects/o/big",
            stream(payload),
            size=len(payload),
            checksum=blake3(payload).digest(),
            if_absent=True,
        )

    assert store.capabilities.conditional_complete is False
    assert fake.objects["objects/o/big"] == b"already here"
    # unprobed or unsupported, the head is what refuses — and it is asked
    assert "objects/o/big" in fake.heads


# -- KMS rides every write, including the multipart session ----------------


async def test_a_kms_key_is_passed_through_on_the_multipart_session(fake: FakeS3) -> None:
    """The session carries the encryption, not just the single-put path.

    ``CreateMultipartUpload`` is where SSE-KMS is declared for every part that
    follows; a driver that only stamped ``PutObject`` would write every object
    over the single-put threshold in plaintext.
    """
    payload = bytes(range(32))
    store = build(fake, part_bytes=8, kms_key_id="arn:aws:kms:us-west-2:1:key/abcd")

    await store.put(
        "objects/o/big", stream(payload), size=len(payload), checksum=blake3(payload).digest()
    )

    assert len(fake.multipart_creates) == 1
    created = fake.multipart_creates[0]
    assert created["ServerSideEncryption"] == "aws:kms"
    assert created["SSEKMSKeyId"] == "arn:aws:kms:us-west-2:1:key/abcd"
    # UploadPart inherits the session's encryption; it must not re-declare a
    # key (S3 answers InvalidArgument when it does).
    assert fake.uploaded_parts, "the object went up multipart"
    assert all("SSEKMSKeyId" not in part for part in fake.uploaded_parts)


# -- permanent client errors are never an outage --------------------------


@pytest.mark.parametrize(
    "code",
    [
        "EntityTooSmall",
        "InvalidPart",
        "InvalidPartOrder",
        "KeyTooLongError",
        "InvalidObjectState",
    ],
)
async def test_a_permanent_client_error_is_never_retried(fake: FakeS3, code: str) -> None:
    """A call that can never succeed must not be driven at the store again."""
    store = build(fake, part_bytes=64)
    fake.faults["put_object"] = [client_error(code, 400)] * 4

    with pytest.raises(InvalidRequest):
        await store.put("objects/o/a", stream(b"x"), size=1, checksum=blake3(b"x").digest())

    assert len(fake.faults["put_object"]) == 3  # exactly one attempt was spent


# --- The lifecycle class tag ------------------------------------------------
#
# Every stored key is domain-absolute (`domains/<uuid>/incoming/...`) and an S3
# lifecycle `filter.prefix` is a wildcard-free literal anchored at byte 0, so
# the bucket's three expiry safety nets cannot name a class that lives one
# variable segment deep. The driver knows which class it is writing, so it
# stamps the class as an object tag and the rules filter on that instead. These
# assert the tag set a real bucket would end up holding, not the call.


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        pytest.param("incoming/session/1", "incoming", id="incoming"),
        pytest.param("deleted/objects/ab/cd/ef", "deleted", id="deleted"),
        pytest.param("erased/objects/ab/cd/ef", "erased", id="erased"),
        pytest.param("objects/ab/cd/ef", None, id="content-object-is-untagged"),
        pytest.param("incomingish/x", None, id="a-longer-first-segment-is-not-the-class"),
        pytest.param("x/incoming/y", None, id="the-class-is-the-first-segment-only"),
    ],
)
def test_the_lifecycle_class_is_read_off_a_relative_key(key: str, expected: str | None) -> None:
    assert lifecycle_class(key, "domain") == expected


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        pytest.param(
            "domains/2f2a2b6e-0000-4000-8000-000000000000/incoming/s/1",
            "incoming",
            id="past-the-domain-prefix",
        ),
        pytest.param(
            "domains/2f2a2b6e-0000-4000-8000-000000000000/objects/ab/cd/ef",
            None,
            id="content-object-is-untagged",
        ),
        pytest.param("incoming/s/1", None, id="a-bucket-key-without-the-domain-prefix"),
        pytest.param("domains/2f2a2b6e-0000-4000-8000-000000000000", None, id="no-object-at-all"),
    ],
)
def test_the_lifecycle_class_skips_the_domain_prefix_on_a_bucket_handle(
    key: str, expected: str | None
) -> None:
    assert lifecycle_class(key, "bucket") == expected


async def test_a_staged_upload_carries_the_tag_the_bucket_rule_filters_on(fake: FakeS3) -> None:
    payload = b"staged"
    store = build(fake, part_bytes=64)

    await store.put("incoming/s/1", stream(payload), size=6, checksum=blake3(payload).digest())

    assert fake.tagging == {"incoming/s/1": f"{LIFECYCLE_TAG_KEY}=incoming"}


async def test_a_content_object_is_written_untagged(fake: FakeS3) -> None:
    """A tag on a content key would hand it to whichever expiry rule matched."""
    payload = b"content"
    store = build(fake, part_bytes=64)

    await store.put("objects/ab/cd/ef", stream(payload), size=7, checksum=blake3(payload).digest())

    assert fake.tagging == {}


async def test_a_multipart_staged_upload_carries_the_tag(fake: FakeS3) -> None:
    payload = b"a" * 32
    store = build(fake, part_bytes=8)

    await store.put("incoming/s/big", stream(payload), size=32, checksum=blake3(payload).digest())

    assert fake.tagging == {"incoming/s/big": f"{LIFECYCLE_TAG_KEY}=incoming"}


async def test_promoting_out_of_incoming_drops_the_staging_tag(fake: FakeS3) -> None:
    """The copy half of a move must REPLACE the tag set, not inherit it: an
    object promoted into `objects/` while still tagged `incoming` would be
    deleted by the 14-day safety net a fortnight after it was published."""
    payload = b"promoted"
    store = build(fake, part_bytes=64)
    await store.put("incoming/s/1", stream(payload), size=8, checksum=blake3(payload).digest())

    await store.move("incoming/s/1", "objects/ab/cd/ef")

    assert fake.objects == {"objects/ab/cd/ef": payload}
    assert fake.tagging == {}


async def test_a_soft_delete_move_stamps_the_deleted_class(fake: FakeS3) -> None:
    payload = b"parked"
    store = build(fake, part_bytes=64)
    await store.put("objects/ab/cd/ef", stream(payload), size=6, checksum=blake3(payload).digest())

    await store.move("objects/ab/cd/ef", "deleted/objects/ab/cd/ef")

    assert fake.tagging == {"deleted/objects/ab/cd/ef": f"{LIFECYCLE_TAG_KEY}=deleted"}


async def test_a_bucket_handle_tags_past_the_domain_prefix(fake: FakeS3) -> None:
    payload = b"erased"
    store = build(fake, part_bytes=64, layout="bucket")
    key = "domains/2f2a2b6e-0000-4000-8000-000000000000/erased/objects/ab/cd/ef"

    await store.put(key, stream(payload), size=6, checksum=blake3(payload).digest())

    assert fake.tagging == {key: f"{LIFECYCLE_TAG_KEY}=erased"}


async def test_a_promotion_untags_even_where_an_empty_replace_is_ignored(fake: FakeS3) -> None:
    """The empty REPLACE is not enough on its own.

    SeaweedFS keeps the source's tag set on a `TaggingDirective=REPLACE` that
    declares no tags, so a driver leaning on the directive alone publishes
    content still stamped `incoming` — and on an endpoint that runs the
    expire-incoming rule the bucket deletes that live object a fortnight later.
    The removal has to be asked for outright.
    """
    fake.ignores_empty_replace = True
    payload = b"promoted"
    store = build(fake, part_bytes=64)
    await store.put("incoming/s/1", stream(payload), size=8, checksum=blake3(payload).digest())
    assert fake.tagging == {"incoming/s/1": f"{LIFECYCLE_TAG_KEY}=incoming"}

    await store.move("incoming/s/1", "objects/ab/cd/ef")

    assert fake.objects == {"objects/ab/cd/ef": payload}
    assert fake.tagging == {}
    assert fake.untagged == ["objects/ab/cd/ef"]


async def test_a_promotion_untags_by_replacement_when_the_strip_is_denied(fake: FakeS3) -> None:
    """A policy that lags the driver must not take publishing down.

    S3 charges the outright removal to `s3:DeleteObjectTagging`, an action a
    deployment still carrying the older grant does not hold — and the denial is
    not a `NotFound`, so it would escape `move()` and turn every content publish
    into a permanent 503. Replacing the tag set with an empty one clears it just
    as completely and is charged to `s3:PutObjectTagging`, which every writer
    already holds.
    """
    fake.ignores_empty_replace = True
    fake.faults["delete_object_tagging"] = [client_error("AccessDenied", 403)]
    payload = b"promoted"
    store = build(fake, part_bytes=64)
    await store.put("incoming/s/1", stream(payload), size=8, checksum=blake3(payload).digest())
    assert fake.tagging == {"incoming/s/1": f"{LIFECYCLE_TAG_KEY}=incoming"}

    await store.move("incoming/s/1", "objects/ab/cd/ef")

    assert fake.objects == {"objects/ab/cd/ef": payload}
    assert fake.tagging == {}
    assert [call["Tagging"] for call in fake.tag_puts] == [{"TagSet": []}]
    assert fake.untagged == ["objects/ab/cd/ef"]


async def test_a_denied_strip_of_a_vanished_object_is_still_swallowed(fake: FakeS3) -> None:
    """Both halves of the removal treat a key that went away under us as done:
    the fallback may not resurrect the 404 the direct call already forgives."""
    fake.ignores_empty_replace = True
    fake.faults["delete_object_tagging"] = [client_error("AccessDenied", 403)]
    fake.faults["put_object_tagging"] = [client_error("NoSuchKey", 404)]
    payload = b"promoted"
    store = build(fake, part_bytes=64)
    await store.put("incoming/s/1", stream(payload), size=8, checksum=blake3(payload).digest())

    await store.move("incoming/s/1", "objects/ab/cd/ef")

    assert fake.objects == {"objects/ab/cd/ef": payload}
    assert fake.faults["put_object_tagging"] == []


async def test_a_promotion_strip_that_is_refused_outright_still_fails(fake: FakeS3) -> None:
    """Only the denial has a second verb to fall back on: an endpoint that is
    simply broken must still surface, or a publish silently keeps the
    `incoming` class and the bucket deletes the live object a fortnight on."""
    fake.ignores_empty_replace = True
    fake.faults["delete_object_tagging"] = [client_error("InvalidArgument", 400)]
    payload = b"promoted"
    store = build(fake, part_bytes=64)
    await store.put("incoming/s/1", stream(payload), size=8, checksum=blake3(payload).digest())

    with pytest.raises(InvalidRequest):
        await store.move("incoming/s/1", "objects/ab/cd/ef")

    assert fake.tag_puts == []


async def test_a_classed_destination_is_stamped_rather_than_untagged(fake: FakeS3) -> None:
    """Only a classless destination is untagged: a move into a class names the
    class on the copy, so the removal can never race the stamp."""
    fake.ignores_empty_replace = True
    payload = b"parked"
    store = build(fake, part_bytes=64)
    await store.put("objects/ab/cd/ef", stream(payload), size=6, checksum=blake3(payload).digest())

    await store.move("objects/ab/cd/ef", "deleted/objects/ab/cd/ef")

    assert fake.tagging == {"deleted/objects/ab/cd/ef": f"{LIFECYCLE_TAG_KEY}=deleted"}
    assert fake.untagged == []


async def test_an_endpoint_without_tagging_never_sends_the_header(fake: FakeS3) -> None:
    """`tagging=False` degrades to no tag rather than failing every classed
    write: an endpoint that refuses the header would otherwise reject every
    upload into `incoming/` and every move that stamps a class."""
    payload = b"untagged"
    store = build(fake, part_bytes=64, tagging=False)
    assert store.capabilities.tagging is False

    await store.put("incoming/s/1", stream(payload), size=8, checksum=blake3(payload).digest())
    await store.move("incoming/s/1", "objects/ab/cd/ef")

    assert fake.objects == {"objects/ab/cd/ef": payload}
    assert fake.tagging == {}
    # No Tagging header, no directive, and no untag call to be refused either.
    assert fake.copy_calls[0].get("TaggingDirective") is None
    assert fake.copy_calls[0].get("Tagging") is None
    assert fake.untagged == []


async def test_a_multipart_write_carries_no_tag_without_tagging(fake: FakeS3) -> None:
    payload = b"a" * 32
    store = build(fake, part_bytes=8, tagging=False)

    await store.put("incoming/s/big", stream(payload), size=32, checksum=blake3(payload).digest())

    assert fake.upload_tagging == {}
    assert fake.tagging == {}


async def test_head_bucket_names_a_missing_bucket_out_of_a_bare_404(fake: FakeS3) -> None:
    """A real `HeadBucket` 404 carries no error code — a HEAD has no body — so
    the driver, not the endpoint, is what tells a wrong bucket from a missing
    key. Left as a plain absence it reads as "the store answered", which is
    exactly what the health probe treats as green."""
    fake.bucket_exists = False
    store = build(fake, part_bytes=64)

    with pytest.raises(NoSuchBucket):
        await store.head_bucket()

    assert fake.bucket_heads == [BUCKET]


async def test_head_bucket_is_silent_when_the_bucket_is_there(fake: FakeS3) -> None:
    store = build(fake, part_bytes=64)

    assert await store.head_bucket() is None
    assert fake.bucket_heads == [BUCKET]


# --- Copying past the single-call ceiling -----------------------------------
#
# Every promotion out of `incoming/` and every soft delete is a `move`, and AWS
# refuses a one-call `CopyObject` whose source is over 5 GiB -- which, with a
# 1000 GB maximum file, is the ordinary case rather than the exotic one. The
# fake refuses it the same way, so a driver that went back to a bare copy fails
# here and not only in production.


@pytest.mark.parametrize(
    ("max_parts", "expected"),
    [
        pytest.param(
            10_000,
            ["bytes=0-7", "bytes=8-15", "bytes=16-23", "bytes=24-31", "bytes=32-39"],
            id="at_the_minimum_part_size",
        ),
        pytest.param(
            3,
            ["bytes=0-13", "bytes=14-27", "bytes=28-39"],
            id="grown_so_the_part_list_fits",
        ),
    ],
)
async def test_a_source_over_the_ceiling_is_copied_range_by_range(
    fake: FakeS3, max_parts: int, expected: list[str]
) -> None:
    store = build(fake)
    store.capabilities = replace(store.capabilities, max_parts=max_parts)
    store.max_single_copy_bytes = 16
    fake.max_single_copy_bytes = 16
    payload = bytes(range(40))
    fake.objects["incoming/s/1"] = payload

    await store.move("incoming/s/1", "objects/ab/cd/ef")

    assert fake.objects["objects/ab/cd/ef"] == payload
    assert "incoming/s/1" not in fake.objects
    # Never the one-call copy the endpoint would have refused.
    assert fake.copy_calls == []
    assert [call["CopySourceRange"] for call in fake.copy_parts] == expected
    assert fake.uploads == {}


async def test_a_source_at_the_ceiling_is_still_copied_in_one_call(fake: FakeS3) -> None:
    """The limit is on a *larger* source: at the ceiling the single copy stands."""
    store = build(fake)
    store.max_single_copy_bytes = 16
    fake.max_single_copy_bytes = 16
    fake.objects["incoming/s/1"] = bytes(range(16))

    await store.move("incoming/s/1", "objects/ab/cd/ef")

    assert fake.objects["objects/ab/cd/ef"] == bytes(range(16))
    assert len(fake.copy_calls) == 1
    assert fake.copy_parts == []


@pytest.mark.parametrize("failing", ["upload_part_copy", "complete_multipart_upload"])
async def test_a_range_copy_that_fails_leaves_no_session_open(fake: FakeS3, failing: str) -> None:
    """An abandoned session bills for its staged parts until a rule reaps it."""
    store = build(fake)
    store.max_single_copy_bytes = 16
    fake.max_single_copy_bytes = 16
    fake.objects["incoming/s/1"] = bytes(range(40))
    fake.faults[failing] = [client_error("AccessDenied", 403)]

    with pytest.raises(AccessDenied):
        await store.move("incoming/s/1", "objects/ab/cd/ef")

    assert fake.uploads == {}
    assert "objects/ab/cd/ef" not in fake.objects
    # A move that could not copy has not deleted anything either.
    assert fake.objects["incoming/s/1"] == bytes(range(40))


async def test_a_range_copy_carries_the_content_type_and_the_destination_class(
    fake: FakeS3,
) -> None:
    """A multipart copy inherits nothing: both have to be declared at create."""
    store = build(fake)
    store.max_single_copy_bytes = 16
    fake.max_single_copy_bytes = 16
    fake.objects["objects/ab/cd/ef"] = bytes(range(40))
    fake.content_types["objects/ab/cd/ef"] = "text/csv"

    await store.move("objects/ab/cd/ef", "deleted/objects/ab/cd/ef")

    assert fake.content_types["deleted/objects/ab/cd/ef"] == "text/csv"
    assert fake.tagging["deleted/objects/ab/cd/ef"] == f"{LIFECYCLE_TAG_KEY}=deleted"


async def test_a_range_copy_out_of_incoming_publishes_untagged_and_unstripped(
    fake: FakeS3,
) -> None:
    """The staging tag cannot ride across, so nothing has to be stripped off.

    A published content object still carrying `incoming` is deleted by the
    bucket's 14-day rule a fortnight later, so the tag set the destination ends
    up with is the whole point of the case.
    """
    store = build(fake)
    store.max_single_copy_bytes = 16
    fake.max_single_copy_bytes = 16
    fake.objects["incoming/s/1"] = bytes(range(40))
    fake.tagging["incoming/s/1"] = f"{LIFECYCLE_TAG_KEY}=incoming"

    await store.move("incoming/s/1", "objects/ab/cd/ef")

    assert fake.tagging.get("objects/ab/cd/ef") is None
    assert fake.untagged == []


async def test_a_source_no_part_list_can_cover_is_refused_before_a_session_opens(
    fake: FakeS3,
) -> None:
    store = build(fake)
    store.capabilities = replace(store.capabilities, max_parts=2, max_part_bytes=8)
    store.max_single_copy_bytes = 16
    fake.max_single_copy_bytes = 16
    fake.objects["incoming/s/1"] = bytes(range(40))

    with pytest.raises(InvalidRequest):
        await store.move("incoming/s/1", "objects/ab/cd/ef")

    assert fake.multipart_creates == []
    assert fake.objects["incoming/s/1"] == bytes(range(40))


async def test_moving_a_key_that_is_not_there_still_raises(fake: FakeS3) -> None:
    """The size question is asked with a head, which a missing key answers too."""
    store = build(fake)

    with pytest.raises(NotFound):
        await store.move("incoming/s/1", "objects/ab/cd/ef")
