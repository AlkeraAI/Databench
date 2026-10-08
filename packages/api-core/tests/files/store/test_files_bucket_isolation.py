"""The suite writes to a bucket of its own, never the deployment's.

``make dev-all`` and the test suite read the same ``.env.workspace``, so before
the root conftest took this over, a test that went through the real store
factory wrote into the bucket the running dev stack serves — and left its
objects there under a dedup domain whose database is dropped when the run ends,
where the per-org janitor can never see them again.

These pin the one property that stops it: what the code reads for a bucket name
is not what the environment configured. Every store builder in the codebase —
the backend's factory, the CLI's admin handle, the worker's boot — reads
``settings.files_store_bucket``, so that setting is the whole seam.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from alkera_core.config import settings
from freezegun import freeze_time

BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


def test_the_suite_is_never_pointed_at_the_deployment_bucket(
    files_deployment_bucket: str | None,
) -> None:
    """The invariant the isolation exists for. It holds in both shapes: a
    configured bucket is replaced, and no configured bucket means no bucket to
    write into in the first place."""
    if files_deployment_bucket is None:
        assert settings.files_store_bucket is None
        return
    assert settings.files_store_bucket is not None
    assert settings.files_store_bucket != files_deployment_bucket


def test_the_buckets_the_suite_uses_announce_themselves_as_a_test_run(
    files_deployment_bucket: str | None,
) -> None:
    """An operator looking at a real endpoint has to be able to tell a run's
    leftovers from a deployment's data by the name alone."""
    if files_deployment_bucket is None:
        assert settings.files_store_bucket is None
        return
    assert settings.files_store_bucket is not None
    assert settings.files_store_bucket.startswith("alkera-test-")


def test_the_run_bucket_is_a_legal_bucket_name(files_test_bucket: str | None) -> None:
    """S3 and SeaweedFS both refuse a name that is not DNS-shaped and inside 63
    bytes, and the failure would land on the first Files test rather than here."""
    if files_test_bucket is None:
        assert settings.files_store_bucket is None
        return
    assert BUCKET_NAME.match(files_test_bucket), files_test_bucket
    assert 3 <= len(files_test_bucket) <= 63
    assert files_test_bucket == files_test_bucket.lower()


def test_each_xdist_worker_owns_a_different_bucket(files_test_bucket: str | None) -> None:
    """Workers are separate processes against one endpoint, and each drops its
    bucket at the end of its session: a shared name would have one worker
    deleting another's objects mid-test."""
    if files_test_bucket is None:
        assert settings.files_store_bucket is None
        return
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    assert worker in files_test_bucket


def test_the_deployment_bucket_is_the_real_one_on_every_xdist_worker(
    files_deployment_bucket: str | None,
) -> None:
    """A worker inherits the controller's environment — redirect included — so
    the name it treats as the deployment's has to survive that boundary as
    itself. When it does not, every worker names its bucket after the
    controller's test bucket and the invariant above compares two test buckets
    and passes while proving nothing."""
    if files_deployment_bucket is None:
        assert settings.files_store_bucket is None
        return
    assert not files_deployment_bucket.startswith("alkera-test-")


def test_what_the_code_reads_is_the_bucket_the_session_owns(
    files_test_bucket: str | None,
) -> None:
    """The fixture that creates and drops the bucket and the setting every
    store builder reads must name the same bucket, or the suite would create
    one bucket and write into another."""
    assert settings.files_store_bucket == files_test_bucket


# --- what a run that never finished leaves behind ---------------------------

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
DEPLOYMENT = "alkera-files-somewhere"


class _StubEndpoint:
    """A store endpoint with SeaweedFS's two planes: buckets, and the collections
    the volume files actually live in.

    They are separate on purpose, because that separation IS the bug: an S3
    ``DeleteBucket`` takes the bucket and leaves the collection — and its
    preallocated volume files — standing, which is how a dev store fills up with
    gigabytes nothing will ever read. Only ``col/delete`` on the master takes the
    collection here, so a helper that skips that call cannot pass.
    """

    def __init__(self, buckets: dict[str, datetime]) -> None:
        self.buckets = dict(buckets)
        self.collections = set(buckets)
        self.objects: dict[str, list[str]] = {}
        self.deleted: list[str] = []
        self.admin_urls: list[str] = []
        #: What a store whose disk is full does: the S3 plane refuses, the
        #: master's still answers.
        self.refuse_bucket_deletes = False

    # --- the boto3 surface -------------------------------------------------

    def create_bucket(self, Bucket: str) -> None:  # noqa: N803 - the boto3 spelling
        self.buckets[Bucket] = datetime.now(UTC)
        self.collections.add(Bucket)

    def list_buckets(self) -> dict[str, Any]:
        return {
            "Buckets": [{"Name": n, "CreationDate": c} for n, c in sorted(self.buckets.items())]
        }

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        keys = self.objects.get(kwargs["Bucket"], [])
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def delete_object(self, Bucket: str, Key: str) -> None:  # noqa: N803 - the boto3 spelling
        self.objects[Bucket].remove(Key)

    def delete_bucket(self, Bucket: str) -> None:  # noqa: N803 - the boto3 spelling
        if self.refuse_bucket_deletes:
            raise RuntimeError("InternalError: We encountered an internal error")
        if self.objects.get(Bucket):
            raise AssertionError(f"{Bucket} still holds objects")
        self.buckets.pop(Bucket)
        self.deleted.append(Bucket)

    # --- the master's admin API -------------------------------------------

    def open_admin_url(self, url: str) -> None:
        self.admin_urls.append(url)
        query = urlparse(url).query
        for name in parse_qs(query).get("collection", []):
            self.collections.discard(name)


def _store(endpoint: _StubEndpoint, cls: Any) -> Any:
    return cls(endpoint, admin_endpoint="http://master:9333/", opener=endpoint.open_admin_url)


def test_a_bucket_a_killed_run_left_behind_is_reaped(
    stale_test_bucket_reaper: Callable[..., list[str]],
    files_test_store_class: Any,
) -> None:
    """A session that is killed never runs its teardown — a crashed worker, a
    host that reboots mid-suite. That is precisely how a store fills up with
    objects nothing will ever reach again, so the next run clears it."""
    endpoint = _StubEndpoint(
        {
            DEPLOYMENT: NOW - timedelta(days=30),
            "alkera-test-deadbeef01-gw0": NOW - timedelta(days=2),
            "alkera-test-mine000000-gw1": NOW,
        }
    )

    reaped = stale_test_bucket_reaper(
        _store(endpoint, files_test_store_class), keep="alkera-test-mine000000-gw1", now=NOW
    )

    assert reaped == ["alkera-test-deadbeef01-gw0"]
    assert endpoint.deleted == ["alkera-test-deadbeef01-gw0"]
    assert "alkera-test-deadbeef01-gw0" not in endpoint.collections


def test_the_reaper_leaves_the_deployment_this_session_and_a_live_one_alone(
    stale_test_bucket_reaper: Callable[..., list[str]],
    files_test_store_class: Any,
) -> None:
    """The three it must never take: the deployment's bucket (it is not ours),
    this session's own (still being written to), and another session's that is
    younger than the window — two suites can share one endpoint, and reaping a
    live run's bucket would fail that run's tests, not clean up after it."""
    endpoint = _StubEndpoint(
        {
            DEPLOYMENT: NOW - timedelta(days=30),
            "alkera-test-mine000000-gw1": NOW - timedelta(days=30),
            "alkera-test-running001-gw0": NOW - timedelta(minutes=20),
        }
    )

    reaped = stale_test_bucket_reaper(
        _store(endpoint, files_test_store_class), keep="alkera-test-mine000000-gw1", now=NOW
    )

    assert reaped == []
    assert endpoint.deleted == []
    assert endpoint.collections == set(endpoint.buckets)


def test_the_window_is_what_decides_and_it_can_be_narrowed(
    stale_test_bucket_reaper: Callable[..., list[str]],
    files_test_store_class: Any,
) -> None:
    """The age is a dial, not a constant baked into the walk: the same bucket
    is kept under the default window and taken under a shorter one."""
    aged = {DEPLOYMENT: NOW, "alkera-test-oldrun0001-gw0": NOW - timedelta(minutes=30)}

    kept = _store(_StubEndpoint(aged), files_test_store_class)
    assert stale_test_bucket_reaper(kept, keep="x", now=NOW) == []
    taken = _store(_StubEndpoint(aged), files_test_store_class)
    assert stale_test_bucket_reaper(taken, keep="x", now=NOW, older_than=timedelta(minutes=10)) == [
        "alkera-test-oldrun0001-gw0"
    ]


def test_the_sweep_takes_what_crossed_the_window_while_the_clock_moved(
    stale_test_bucket_reaper: Callable[..., list[str]],
    files_test_store_class: Any,
) -> None:
    """The age is measured against the wall clock a starting run reads, not a
    constant a caller passes: the same two buckets are both spared at the hour
    the run began and one is taken once the clock has crossed the window —
    which is the only way the buckets a crashed run left are ever collected."""
    endpoint = _StubEndpoint({})
    store = _store(endpoint, files_test_store_class)
    with freeze_time(NOW) as frozen:
        endpoint.create_bucket("alkera-test-crashed001-gw0")
        frozen.move_to(NOW + timedelta(hours=5))
        endpoint.create_bucket("alkera-test-running002-gw1")

        assert stale_test_bucket_reaper(store, keep="x", now=datetime.now(UTC)) == []

        frozen.move_to(NOW + timedelta(hours=7))
        reaped = stale_test_bucket_reaper(store, keep="x", now=datetime.now(UTC))

    assert reaped == ["alkera-test-crashed001-gw0"]
    assert endpoint.collections == {"alkera-test-running002-gw1"}


# --- what the helper is allowed to remove -----------------------------------


def test_dropping_a_run_bucket_takes_its_collection_too(files_test_store_class: Any) -> None:
    """The whole fix. S3 has no word for a collection, so a teardown that only
    calls DeleteBucket leaves SeaweedFS holding the collection's volume files —
    gigabytes per run per worker, on a store a dev machine shares with its
    running stack. The collection has to go with the bucket."""
    endpoint = _StubEndpoint({"alkera-test-mine000000-gw1": NOW})
    endpoint.objects["alkera-test-mine000000-gw1"] = ["a/blob", "b/blob"]

    _store(endpoint, files_test_store_class).delete_bucket_and_collection(
        "alkera-test-mine000000-gw1"
    )

    assert endpoint.buckets == {}
    assert endpoint.collections == set()
    assert endpoint.objects["alkera-test-mine000000-gw1"] == []
    assert endpoint.admin_urls == [
        "http://master:9333/col/delete?collection=alkera-test-mine000000-gw1"
    ]


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("alkera-files", id="a deployment's bucket"),
        pytest.param("alkera-files-jdoe-chat-workspace-19115", id="a worktree's dev bucket"),
        pytest.param("", id="nothing at all"),
        pytest.param("ALKERA-TEST-MINE000000-GW1", id="the prefix in the wrong case"),
        pytest.param("mine-alkera-test-000000-gw1", id="the prefix somewhere in the middle"),
    ],
)
def test_the_helper_refuses_every_name_that_is_not_a_run_of_this_suite(
    files_test_store_class: Any, name: str
) -> None:
    """The guard that makes the teardown safe to point at the dev store at all:
    the endpoint it deletes on is the one `make dev-all` serves, so a name that
    did not come from this suite's own generator is a deployment's data."""
    endpoint = _StubEndpoint({name: NOW} if name else {})

    with pytest.raises(AssertionError, match="refusing to delete"):
        _store(endpoint, files_test_store_class).delete_bucket_and_collection(name)

    assert endpoint.deleted == []
    assert endpoint.admin_urls == []
    assert endpoint.collections == set(endpoint.buckets)


def test_a_bucket_the_store_refuses_to_drop_still_loses_its_collection(
    files_test_store_class: Any,
) -> None:
    """A store under the pressure this exists to prevent answers DeleteBucket
    with an InternalError — a filer whose disk is full cannot remove a directory
    either — and that is the moment the volume files most need to go. Observed:
    36 buckets a full dev store would not drop, each still holding its
    collection. The empty bucket costs a directory entry; the collection costs
    gigabytes."""
    endpoint = _StubEndpoint({"alkera-test-mine000000-gw1": NOW})
    endpoint.refuse_bucket_deletes = True

    _store(endpoint, files_test_store_class).delete_bucket_and_collection(
        "alkera-test-mine000000-gw1"
    )

    assert endpoint.buckets == {"alkera-test-mine000000-gw1": NOW}
    assert endpoint.collections == set()


def test_a_store_with_no_master_still_drops_its_bucket(files_test_store_class: Any) -> None:
    """Not every endpoint publishes a master — an S3-compatible store that is
    not SeaweedFS has no collections at all. The bucket still goes, and a
    missing admin endpoint never takes a session's teardown down with it."""
    endpoint = _StubEndpoint({"alkera-test-mine000000-gw1": NOW})
    store = files_test_store_class(endpoint, admin_endpoint=None, opener=endpoint.open_admin_url)

    store.delete_bucket_and_collection("alkera-test-mine000000-gw1")

    assert endpoint.deleted == ["alkera-test-mine000000-gw1"]
    assert endpoint.admin_urls == []
