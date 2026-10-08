"""The janitor's admin surface on the drivers: staged bytes and open multiparts.

``IncomingOrphans`` and ``StoreMultipartAborts`` reap what no row owns, so a
driver that cannot name its ``incoming/`` objects or its unfinished multipart
uploads makes both sweepers unrunnable outside a hand-written test double. What
is proven here is the driver's own answer: an object staged under ``incoming/``
comes back with the age its bytes actually carry, an object that is not staged
never does, and a multipart session appears while it is open and is gone once it
is completed or aborted.

The filesystem cases run against a real tree; the S3 cases run against the
dict-backed fake endpoint used by the driver tests (real wire shapes, no
network), and the live twin runs the same assertions against whatever
``FILES_LIVE_ENDPOINTS`` names — the local SeaweedFS under
``make files-live-local``.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alkera_core.files.clock import FakeClock, SystemClock
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.s3_compatible import S3CompatibleStore, S3Config
from alkera_core.files.store.scoped import FilesystemScoped, StoreAdmin
from blake3 import blake3

EPOCH = datetime(2026, 3, 1, tzinfo=UTC)
BUCKET = "alkera-files"
DOMAIN = uuid.UUID("11111111-2222-3333-4444-555555555555")
SESSION = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


async def _bytes(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


def _digest(payload: bytes) -> bytes:
    return bytes(blake3(payload).digest())


# --------------------------------------------------------------------------
# filesystem
# --------------------------------------------------------------------------


def _fs(tmp_path: Path) -> FilesystemStore:
    return FilesystemStore(tmp_path, clock=FakeClock(EPOCH).now)


async def _stage(store: FilesystemStore, key: str, payload: bytes = b"staged") -> None:
    await store.put(key, _bytes(payload), size=len(payload), checksum=_digest(payload))


@pytest.mark.anyio
async def test_filesystem_lists_a_staged_object_with_its_age(tmp_path: Path) -> None:
    """An orphan under ``incoming/`` is listed, and its age is the file's own mtime."""
    store = _fs(tmp_path)
    key = f"incoming/{SESSION}/object"
    await _stage(store, key)
    aged = EPOCH - timedelta(hours=9)
    os.utime(tmp_path / key, (aged.timestamp(), aged.timestamp()))

    keys, next_after = await store.list_incoming(after=None, limit=10)

    assert list(keys) == [key]
    assert next_after is None
    assert await store.written_at(key) == aged


@pytest.mark.anyio
async def test_filesystem_lists_no_object_outside_incoming(tmp_path: Path) -> None:
    """A content object is not a staged one: the sweeper must never see it."""
    store = _fs(tmp_path)
    await _stage(store, "objects/ab/cd/abcd")
    await _stage(store, f"incoming/{SESSION}/object")

    keys, _ = await store.list_incoming(after=None, limit=10)

    assert list(keys) == [f"incoming/{SESSION}/object"]


@pytest.mark.anyio
async def test_filesystem_incoming_pages_from_a_cursor(tmp_path: Path) -> None:
    """A budgeted page reports a cursor, and the next call resumes strictly after it."""
    store = _fs(tmp_path)
    staged = [f"incoming/{SESSION}/{name}" for name in ("a", "b", "c")]
    for key in staged:
        await _stage(store, key)

    first, cursor = await store.list_incoming(after=None, limit=2)
    assert list(first) == staged[:2]
    assert cursor == staged[1]

    second, done = await store.list_incoming(after=cursor, limit=2)
    assert list(second) == staged[2:]
    assert done is None


@pytest.mark.anyio
async def test_filesystem_written_at_is_none_for_a_missing_key(tmp_path: Path) -> None:
    """An object deleted between the listing and the age question keeps its bytes safe."""
    assert await _fs(tmp_path).written_at(f"incoming/{SESSION}/gone") is None


@pytest.mark.anyio
async def test_filesystem_lists_an_open_multipart_and_forgets_a_finished_one(
    tmp_path: Path,
) -> None:
    """Open sessions are listed with their key; a completed one is not."""
    store = _fs(tmp_path)
    open_handle = await store.multipart_create(f"incoming/{SESSION}/big", size=8)
    done_handle = await store.multipart_create(f"incoming/{SESSION}/small", size=4)
    payload = b"done"
    part = await store.multipart_put_part(
        done_handle, 1, _bytes(payload), size=len(payload), checksum=_digest(payload)
    )
    await store.multipart_complete(done_handle, [part])

    pending = await store.list_incomplete()

    assert [(upload, key) for upload, key, _ in pending] == [
        (open_handle.upload_id, open_handle.key)
    ]
    assert pending[0][2].tzinfo is not None


@pytest.mark.anyio
async def test_filesystem_abort_drops_the_staged_parts(tmp_path: Path) -> None:
    """After an abort the session is neither listed nor holding bytes on disk."""
    store = _fs(tmp_path)
    handle = await store.multipart_create(f"incoming/{SESSION}/big", size=4)
    payload = b"part"
    await store.multipart_put_part(
        handle, 1, _bytes(payload), size=len(payload), checksum=_digest(payload)
    )
    assert [upload for upload, _, _ in await store.list_incomplete()] == [handle.upload_id]

    await store.abort(handle.upload_id, handle.key)

    assert await store.list_incomplete() == []
    assert not (tmp_path / ".parts" / handle.upload_id).exists()
    # An abort of what is already gone is the goal state, not a failure.
    await store.abort(handle.upload_id, handle.key)


@pytest.mark.anyio
async def test_admin_handle_sees_a_domain_handles_work(tmp_path: Path) -> None:
    """The bucket-rooted admin handle reaches what a per-domain handle staged.

    The request path opens its own driver rooted at ``domains/<uuid>/``, so an
    admin handle that only looked at its own root would report an empty bucket
    however many orphans were sitting under it.
    """
    factory = FilesystemScoped(tmp_path, clock=FakeClock(EPOCH))
    domain = await factory.for_domain(DOMAIN)
    payload = b"staged"
    await domain.put(
        f"incoming/{SESSION}/object",
        _bytes(payload),
        size=len(payload),
        checksum=_digest(payload),
    )
    handle = await domain.multipart_create(f"incoming/{SESSION}/big", size=4)

    admin: StoreAdmin = factory.admin()
    keys, _ = await admin.list_incoming(after=None, limit=10)
    pending = await admin.list_incomplete()

    absolute = f"domains/{DOMAIN}/incoming/{SESSION}/object"
    assert list(keys) == [absolute]
    assert await admin.written_at(absolute) is not None
    assert [(upload, key) for upload, key, _ in pending] == [
        (handle.upload_id, f"incoming/{SESSION}/big")
    ]

    await admin.abort(handle.upload_id, f"incoming/{SESSION}/big")
    assert await admin.list_incomplete() == []


# --------------------------------------------------------------------------
# s3-compatible, against a fake endpoint
# --------------------------------------------------------------------------


class FakeListing:
    """A ListObjectsV2 / ListMultipartUploads endpoint with real paging shapes.

    ``pages`` is the exact sequence of responses the endpoint hands back, so a
    truncated page carrying no ``Contents`` — the shape F-104 was about — is
    something the test can hand the driver rather than something it hopes for.
    """

    def __init__(
        self,
        pages: Sequence[dict[str, Any]] | None = None,
        uploads: Sequence[dict[str, Any]] | None = None,
        heads: dict[str, datetime] | None = None,
    ) -> None:
        self._pages = list(pages or [])
        self._uploads = list(uploads or [])
        self.list_params: list[dict[str, Any]] = []
        self.aborted: list[tuple[str, str]] = []
        self._heads = heads or {}

    async def __aenter__(self) -> FakeListing:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def list_objects_v2(self, **params: Any) -> dict[str, Any]:
        self.list_params.append(params)
        if not self._pages:
            return {"Contents": [], "IsTruncated": False}
        return self._pages.pop(0)

    async def list_multipart_uploads(self, **params: Any) -> dict[str, Any]:
        if not self._uploads:
            return {"Uploads": [], "IsTruncated": False}
        return self._uploads.pop(0)

    async def head_object(self, **params: Any) -> dict[str, Any]:
        return {"ContentLength": 1, "LastModified": self._heads[params["Key"]]}

    async def abort_multipart_upload(self, **params: Any) -> dict[str, Any]:
        self.aborted.append((params["UploadId"], params["Key"]))
        return {}


def _s3(endpoint: FakeListing) -> S3CompatibleStore:
    return S3CompatibleStore(
        S3Config(endpoint_url="http://fake", region="us-east-1", bucket=BUCKET),
        clock=FakeClock(EPOCH),
        client_factory=lambda: endpoint,
    )


@pytest.mark.anyio
async def test_s3_lists_a_staged_object_with_its_last_modified() -> None:
    """The staged key comes back with the endpoint's own ``LastModified``, no HEAD."""
    aged = EPOCH - timedelta(hours=9)
    key = f"incoming/{SESSION}/object"
    endpoint = FakeListing(
        pages=[
            {
                "Contents": [{"Key": key, "LastModified": aged}],
                "IsTruncated": False,
            }
        ]
    )
    store = _s3(endpoint)

    keys, next_after = await store.list_incoming(after=None, limit=10)

    assert list(keys) == [key]
    assert next_after is None
    # No ``heads`` were configured, so an answer here can only have come off
    # the listing page — a HEAD would raise KeyError.
    assert await store.written_at(key) == aged


@pytest.mark.anyio
async def test_s3_listing_continues_past_a_truncated_empty_page() -> None:
    """F-104: a truncated page with zero Contents is not the end of the listing."""
    key = f"incoming/{SESSION}/object"
    endpoint = FakeListing(
        pages=[
            {"Contents": [], "IsTruncated": True, "NextContinuationToken": "t1"},
            {"Contents": [], "IsTruncated": True, "NextContinuationToken": "t2"},
            {"Contents": [{"Key": key, "LastModified": EPOCH}], "IsTruncated": False},
        ]
    )
    store = _s3(endpoint)

    keys, _ = await store.list_incoming(after=None, limit=10)

    assert list(keys) == [key]
    assert [params.get("ContinuationToken") for params in endpoint.list_params] == [
        None,
        "t1",
        "t2",
    ]


@pytest.mark.anyio
async def test_s3_listing_skips_keys_outside_incoming() -> None:
    """A bucket-wide prefix still yields only what an upload session staged."""
    staged = f"incoming/{SESSION}/object"
    endpoint = FakeListing(
        pages=[
            {
                "Contents": [
                    {"Key": "objects/ab/cd/abcd", "LastModified": EPOCH},
                    {"Key": staged, "LastModified": EPOCH},
                ],
                "IsTruncated": False,
            }
        ]
    )

    keys, _ = await _s3(endpoint).list_incoming(after=None, limit=10)

    assert list(keys) == [staged]


@pytest.mark.anyio
async def test_s3_written_at_falls_back_to_head_for_an_unlisted_key() -> None:
    """A key the janitor carried over from an earlier page still has an age."""
    key = f"incoming/{SESSION}/object"
    aged = EPOCH - timedelta(days=2)
    store = _s3(FakeListing(heads={key: aged}))

    assert await store.written_at(key) == aged


@pytest.mark.anyio
async def test_s3_lists_incomplete_multipart_uploads_across_pages() -> None:
    """Every unfinished upload is reported, including past a truncated page."""
    endpoint = FakeListing(
        uploads=[
            {
                "Uploads": [{"UploadId": "u1", "Key": "incoming/a/big", "Initiated": EPOCH}],
                "IsTruncated": True,
                "NextKeyMarker": "incoming/a/big",
                "NextUploadIdMarker": "u1",
            },
            {
                "Uploads": [{"UploadId": "u2", "Key": "incoming/b/big", "Initiated": EPOCH}],
                "IsTruncated": False,
            },
        ]
    )

    pending = await _s3(endpoint).list_incomplete()

    assert [(upload, key) for upload, key, _ in pending] == [
        ("u1", "incoming/a/big"),
        ("u2", "incoming/b/big"),
    ]


@pytest.mark.anyio
async def test_s3_reports_an_upload_an_endpoint_will_not_date_as_fresh() -> None:
    """SeaweedFS omits ``Initiated``; such an upload is reported, dated now.

    Dropping the row would hide it from the sweeper; dating it ancient would
    abort a transfer still in flight. Reporting the clock keeps it listed and
    keeps the age gate from firing.
    """
    endpoint = FakeListing(
        uploads=[{"Uploads": [{"UploadId": "u1", "Key": "incoming/a/big"}], "IsTruncated": False}]
    )

    pending = await _s3(endpoint).list_incomplete()

    assert [(upload, key) for upload, key, _ in pending] == [("u1", "incoming/a/big")]
    assert pending[0][2] == EPOCH


@pytest.mark.anyio
async def test_s3_abort_reaches_the_endpoint() -> None:
    """``abort`` aborts exactly the named upload on the named key."""
    endpoint = FakeListing()

    await _s3(endpoint).abort("u1", "incoming/a/big")

    assert endpoint.aborted == [("u1", "incoming/a/big")]


# --------------------------------------------------------------------------
# the live twin
# --------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.anyio
async def test_live_endpoint_lists_staged_bytes_and_an_open_multipart() -> None:
    """The same two questions against a real endpoint (local SeaweedFS).

    A mock cannot say whether the endpoint implements ``ListMultipartUploads``
    at all, which is the one thing ``store_multipart_aborts`` depends on.
    """
    raw = os.environ.get("FILES_LIVE_ENDPOINTS")
    if not raw:
        pytest.skip("FILES_LIVE_ENDPOINTS is unset — run `make files-live-local`")
    row = json.loads(raw)[0]
    domain = uuid.uuid4()
    store = S3CompatibleStore(
        S3Config(
            endpoint_url=row["endpoint"],
            region=row.get("region", "us-east-1"),
            bucket=row["bucket"],
            access_key=row.get("access_key"),
            secret_key=row.get("secret_key"),
        ),
        clock=SystemClock(),
        layout="bucket",
    )
    session = uuid.uuid4()
    key = f"domains/{domain}/incoming/{session}/object"
    payload = b"live-staged"
    await store.put(key, _bytes(payload), size=len(payload), checksum=_digest(payload))
    handle = await store.multipart_create(
        f"domains/{domain}/incoming/{session}/big", size=len(payload)
    )
    try:
        keys, _ = await store.list_incoming(after=f"domains/{domain}/incoming/", limit=100)
        assert key in list(keys)
        written = await store.written_at(key)
        assert written is not None
        assert abs((datetime.now(tz=UTC) - written).total_seconds()) < 600

        pending = await store.list_incomplete()
        assert handle.upload_id in {upload for upload, _, _ in pending}
    finally:
        await store.abort(handle.upload_id, handle.key)
        await store.delete(key)

    assert handle.upload_id not in {upload for upload, _, _ in await store.list_incomplete()}
