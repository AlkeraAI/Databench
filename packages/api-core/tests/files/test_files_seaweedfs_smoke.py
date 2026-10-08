"""The local SeaweedFS object store answers a real S3 object lifecycle.

`make files-live-local` points ``FILES_LIVE_ENDPOINTS`` at this worktree's
``seaweedfs`` compose service; without that variable there is no store to talk to
and the module skips. The point of the test is that the compose service, the
mounted dev identity file and the auto-created bucket actually work together over
the wire — a mock would prove none of that.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest

# One xdist worker for this module: the module-scoped fixtures below are built
# once per worker, so splitting the module per test would rebuild them per worker.
pytestmark = [
    pytest.mark.live,
    pytest.mark.xdist_group("files_seaweedfs_smoke"),
]

boto3 = pytest.importorskip("boto3", reason="boto3 is required for the S3 smoke test")

from botocore.client import Config  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

_ENDPOINTS_ENV = "FILES_LIVE_ENDPOINTS"


def _first_endpoint() -> dict[str, Any]:
    """The first entry of the FILES_LIVE_ENDPOINTS JSON list, or skip."""
    raw = os.environ.get(_ENDPOINTS_ENV)
    if not raw:
        pytest.skip(f"{_ENDPOINTS_ENV} is unset — run `make files-live-local`")
    parsed = json.loads(raw)
    if not isinstance(parsed, list) or not parsed:
        pytest.skip(f"{_ENDPOINTS_ENV} is not a non-empty JSON list")
    entry = parsed[0]
    if not isinstance(entry, dict):
        pytest.skip(f"{_ENDPOINTS_ENV}[0] is not a JSON object")
    return entry


@pytest.fixture(scope="module")
def endpoint() -> dict[str, Any]:
    return _first_endpoint()


@pytest.fixture(scope="module")
def s3(endpoint: dict[str, Any]) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint["endpoint"],
        region_name=endpoint.get("region", "us-east-1"),
        aws_access_key_id=endpoint["access_key"],
        aws_secret_access_key=endpoint["secret_key"],
        # SeaweedFS has no bucket-per-subdomain DNS locally, and it signs v4.
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


@pytest.mark.parametrize(
    ("case_id", "body"),
    [
        pytest.param("empty", b"", id="empty-object"),
        pytest.param("one-byte", b"\x00", id="single-nul-byte"),
        pytest.param("binary", bytes(range(256)), id="every-byte-value"),
    ],
)
def test_put_get_round_trips_the_exact_bytes(
    s3: Any, endpoint: dict[str, Any], case_id: str, body: bytes
) -> None:
    """What the store hands back is byte-identical to what was written."""
    bucket = endpoint["bucket"]
    key = f"smoke/{uuid.uuid4()}/{case_id}"

    s3.put_object(Bucket=bucket, Key=key, Body=body)
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        assert head["ContentLength"] == len(body)

        fetched = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        assert fetched == body
    finally:
        s3.delete_object(Bucket=bucket, Key=key)


def test_get_after_delete_raises_no_such_key(s3: Any, endpoint: dict[str, Any]) -> None:
    """A deleted key is gone: the negative twin of the round-trip above."""
    bucket = endpoint["bucket"]
    key = f"smoke/{uuid.uuid4()}/deleted"

    s3.put_object(Bucket=bucket, Key=key, Body=b"transient")
    assert s3.get_object(Bucket=bucket, Key=key)["Body"].read() == b"transient"

    s3.delete_object(Bucket=bucket, Key=key)

    with pytest.raises(ClientError) as excinfo:
        s3.get_object(Bucket=bucket, Key=key)
    assert excinfo.value.response["Error"]["Code"] == "NoSuchKey"


def test_head_of_a_never_written_key_is_404(s3: Any, endpoint: dict[str, Any]) -> None:
    """A key that was never written is absent, not an empty object."""
    with pytest.raises(ClientError) as excinfo:
        s3.head_object(Bucket=endpoint["bucket"], Key=f"smoke/{uuid.uuid4()}/absent")
    assert excinfo.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404


# --- The lifecycle class tag, over the wire ---------------------------------
#
# The bucket's three expiry safety nets filter on an object TAG, because every
# stored key is domain-absolute and an S3 lifecycle prefix is a wildcard-free
# literal anchored at byte 0. The tag is therefore only a safety net if the
# endpoint actually records what the driver stamps -- which no fake can prove.
#
# SeaweedFS divergence, measured here: it honours `Tagging` on PUT and on a copy
# that declares a non-empty tag set, but it IGNORES `TaggingDirective=REPLACE`
# when the copy declares no tags (or an empty `Tagging`), keeping the source's
# tag set instead. A driver that leaned on that empty REPLACE would publish
# every promoted object still stamped `incoming`, and an endpoint that does run
# the expire-incoming rule would then delete live content a fortnight after it
# was published. So the driver asks for the removal outright, and the second
# test below is what proves it over the wire on the endpoint that diverges.


async def test_a_staged_object_carries_the_lifecycle_tag_over_the_wire(
    s3: Any, endpoint: dict[str, Any]
) -> None:
    from alkera_core.files.clock import SystemClock
    from alkera_core.files.store.s3_compatible import (
        LIFECYCLE_TAG_KEY,
        S3CompatibleStore,
        S3Config,
    )
    from blake3 import blake3

    bucket = endpoint["bucket"]
    store = S3CompatibleStore(
        S3Config(
            endpoint_url=endpoint["endpoint"],
            region=endpoint.get("region", "us-east-1"),
            bucket=bucket,
            access_key=endpoint["access_key"],
            secret_key=endpoint["secret_key"],
            addressing="path",
        ),
        clock=SystemClock(),
    )
    session = uuid.uuid4()
    staged = f"incoming/{session}/object"
    parked = f"deleted/objects/{session}"
    payload = b"staged bytes"

    async def _body() -> Any:
        yield payload

    await store.put(staged, _body(), size=len(payload), checksum=blake3(payload).digest())
    try:
        tags = s3.get_object_tagging(Bucket=bucket, Key=staged)["TagSet"]
        assert {t["Key"]: t["Value"] for t in tags} == {LIFECYCLE_TAG_KEY: "incoming"}, (
            "the bucket's expire-incoming rule filters on this tag; unrecorded, it is a no-op"
        )

        # A move re-stamps the destination's class rather than carrying the
        # source's: the same object is now the deleted rule's to expire.
        await store.move(staged, parked)
        tags = s3.get_object_tagging(Bucket=bucket, Key=parked)["TagSet"]
        assert {t["Key"]: t["Value"] for t in tags} == {LIFECYCLE_TAG_KEY: "deleted"}
    finally:
        s3.delete_object(Bucket=bucket, Key=staged)
        s3.delete_object(Bucket=bucket, Key=parked)


async def test_a_promotion_out_of_incoming_leaves_no_lifecycle_tag(
    s3: Any, endpoint: dict[str, Any]
) -> None:
    """The promotion that publishes content must leave it unclassed.

    This endpoint keeps the source's tag set on a `TaggingDirective=REPLACE`
    that declares no tags, so it is the one that catches a driver relying on
    the directive alone: the content object would come back still tagged
    `incoming` and the expire-incoming rule would own it.
    """
    from alkera_core.files.clock import SystemClock
    from alkera_core.files.store.s3_compatible import S3CompatibleStore, S3Config
    from blake3 import blake3

    bucket = endpoint["bucket"]
    store = S3CompatibleStore(
        S3Config(
            endpoint_url=endpoint["endpoint"],
            region=endpoint.get("region", "us-east-1"),
            bucket=bucket,
            access_key=endpoint["access_key"],
            secret_key=endpoint["secret_key"],
            addressing="path",
        ),
        clock=SystemClock(),
    )
    session = uuid.uuid4()
    staged = f"incoming/{session}/object"
    published = f"objects/{session}"
    payload = b"published bytes"

    async def _body() -> Any:
        yield payload

    await store.put(staged, _body(), size=len(payload), checksum=blake3(payload).digest())
    try:
        await store.move(staged, published)

        tags = s3.get_object_tagging(Bucket=bucket, Key=published)["TagSet"]
        assert tags == [], (
            "published content is still classed for a lifecycle sweep; "
            "the bucket would expire it a fortnight after it went live"
        )
    finally:
        s3.delete_object(Bucket=bucket, Key=staged)
        s3.delete_object(Bucket=bucket, Key=published)


async def test_the_bucket_head_names_a_bucket_that_is_not_there(endpoint: dict[str, Any]) -> None:
    """A key HEAD under a missing bucket is a bare 404 here as it is on S3, so
    the health probe can only tell a typo'd bucket from a fresh one by asking
    the bucket itself."""
    from alkera_core.files.clock import SystemClock
    from alkera_core.files.store.errors import NoSuchBucket
    from alkera_core.files.store.s3_compatible import S3CompatibleStore, S3Config

    def _store(bucket: str) -> Any:
        return S3CompatibleStore(
            S3Config(
                endpoint_url=endpoint["endpoint"],
                region=endpoint.get("region", "us-east-1"),
                bucket=bucket,
                access_key=endpoint["access_key"],
                secret_key=endpoint["secret_key"],
                addressing="path",
            ),
            clock=SystemClock(),
        )

    assert await _store(endpoint["bucket"]).head_bucket() is None
    with pytest.raises(NoSuchBucket):
        await _store(f"absent-{uuid.uuid4()}").head_bucket()
