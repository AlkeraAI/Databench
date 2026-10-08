"""The deployment-health runner: per-check classification, runner isolation
(a hang can't sink the run), the schedule-staleness rule across the boundary,
the Temporal probe, full-replace persistence, and the no-secrets invariant."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from alkera_core import deployment_health as dh
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.deployment_health import (
    SCHEDULED_STALE_AFTER,
    CheckContext,
    CheckResult,
    load_snapshot,
    run_all_checks,
    scrub_url,
    worker_beat_result,
)
from alkera_core.extensions import ExtensionPoint
from alkera_core.files.clock import SystemClock
from alkera_core.files.store.errors import (
    AccessDenied,
    ExpiredCredentials,
    InvalidKey,
    NoSuchBucket,
    NotFound,
    Throttled,
    Unavailable,
)
from alkera_core.files.store.protocol import ObjectInfo
from alkera_core.files.store.s3_compatible import (
    S3CompatibleStore,
    S3Config,
    normalize_client_error,
)
from alkera_core.models import DeploymentHealthCheck, DeploymentHealthRun
from botocore.exceptions import ClientError
from sqlalchemy import delete, select
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest


def _ctx(**over: Any) -> CheckContext:
    base: dict[str, Any] = {
        "trigger": "manual",
        "session_factory": AsyncSessionLocal,
        "http_client": httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ),
    }
    base.update(over)
    return CheckContext(**base)


# --- worker_beat_result (pure) ---------------------------------------------------


def test_worker_beat_across_the_stale_boundary() -> None:
    scheduled = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    assert worker_beat_result(scheduled, scheduled + timedelta(minutes=1)).status == "ok"
    assert worker_beat_result(scheduled, scheduled + SCHEDULED_STALE_AFTER).status == "ok"
    just_past = scheduled + SCHEDULED_STALE_AFTER + timedelta(seconds=1)
    assert worker_beat_result(scheduled, just_past).status == "fail"
    assert worker_beat_result(None, scheduled).status == "warn"


def test_a_stale_schedule_points_the_operator_at_the_worker_and_its_schedules() -> None:
    """The fail detail is the operator's next step: the worker container and the
    command that shows whether the catalog is on the server."""
    scheduled = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    detail = worker_beat_result(scheduled, scheduled + timedelta(hours=2)).detail
    assert "120m ago" in detail
    assert "worker container" in detail
    assert "python -m worker schedules list" in detail


def test_scrub_url_masks_the_password() -> None:
    assert scrub_url("redis://user:s3cret@host:6379/0") == "redis://user:***@host:6379/0"
    assert "s3cret" not in scrub_url("redis://user:s3cret@host:6379/0")


@pytest.mark.parametrize(
    "url",
    [
        # ElastiCache / Redis Cloud style: the password rides in the query string,
        # not userinfo — the form the userinfo-only scrubber leaked.
        "rediss://host:6380/0?password=t0pSecret",
        "redis://host:6379/0?ssl=true&password=t0pSecret",
        "amqp://host:5672//?authToken=t0pSecret",
        "redis://user:pw@host:6379/0?token=t0pSecret",
    ],
)
def test_scrub_url_masks_query_string_secrets(url: str) -> None:
    scrubbed = scrub_url(url)
    assert "t0pSecret" not in scrubbed
    assert "***" in scrubbed


def test_scrub_url_keeps_non_secret_query_params() -> None:
    # A non-secret param (ssl=true) survives; only the secret value is masked.
    scrubbed = scrub_url("redis://host:6379/0?ssl=true&password=t0pSecret")
    assert "ssl=true" in scrubbed
    assert "t0pSecret" not in scrubbed


def test_scrub_url_does_not_corrupt_host_when_password_is_a_substring() -> None:
    # The password "dev" also appears in the host — a naive full-string replace
    # would corrupt it to "redis-***.internal". Masking must be scoped to userinfo.
    scrubbed = scrub_url("redis://user:dev@redis-dev.internal:6379/0")
    assert scrubbed == "redis://user:***@redis-dev.internal:6379/0"
    assert "redis-dev.internal" in scrubbed  # host intact


# --- runner isolation ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hanging_check_fails_only_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dh, "PER_CHECK_TIMEOUT_S", 0.2)

    async def _check_hang(ctx: CheckContext) -> list[CheckResult]:
        await asyncio.sleep(10)
        return [CheckResult("hang", "Hang", "ok", "never")]

    async def _check_fine(ctx: CheckContext) -> list[CheckResult]:
        return [CheckResult("fine", "Fine", "ok", "ok")]

    async with httpx.AsyncClient() as client:
        results = await run_all_checks(_ctx(http_client=client), checks=[_check_hang, _check_fine])
    by_key = {r.key: r for r in results}
    assert by_key["hang"].status == "fail"  # the timed-out check names itself from __name__
    assert by_key["fine"].status == "ok"


@pytest.mark.asyncio
async def test_a_raising_check_fails_only_itself() -> None:
    async def _check_boom(ctx: CheckContext) -> list[CheckResult]:
        raise RuntimeError("kaboom")

    async def _check_fine(ctx: CheckContext) -> list[CheckResult]:
        return [CheckResult("fine", "Fine", "ok", "ok")]

    async with httpx.AsyncClient() as client:
        results = await run_all_checks(_ctx(http_client=client), checks=[_check_boom, _check_fine])
    by_key = {r.key: r for r in results}
    assert by_key["boom"].status == "fail"
    assert "kaboom" not in by_key["boom"].detail  # class name only, never the message
    assert by_key["fine"].status == "ok"


# --- individual check classification ---------------------------------------------


@pytest.mark.asyncio
async def test_postgres_and_migrations_ok_against_the_real_db() -> None:
    async with httpx.AsyncClient() as client:
        pg = await dh._check_postgres(_ctx(http_client=client))
        mig = await dh._check_migrations(_ctx(http_client=client))
    assert pg[0].status == "ok"
    assert mig[0].status == "ok"  # the per-worker DB is migrated to head


@pytest.mark.asyncio
async def test_entitlement_check_reports_its_own_latency_not_uptime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The valid-entitlement check must report its own (tiny) duration. Regression:
    it passed started=0 to _ok, so _ms computed (monotonic_now - 0) = the machine's
    monotonic clock — the entitlement row showed the host uptime (e.g. 159431795ms)."""

    class _Valid:
        def state(self, now: object = None) -> str:
            return "valid"

    monkeypatch.setattr(dh, "get_entitlements", lambda: _Valid())
    result = await dh._check_entitlement(_ctx())
    assert result[0].status == "ok"
    # A single indexed query — well under a second; NEVER the machine uptime.
    assert 0 <= result[0].latency_ms < 60_000


@pytest.mark.asyncio
async def test_migrations_fail_on_a_doctored_head(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dh, "EXPECTED_SCHEMA_HEAD", "9999")
    async with httpx.AsyncClient() as client:
        result = await dh._check_migrations(_ctx(http_client=client))
    assert result[0].status == "fail"
    assert "9999" in result[0].detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        pytest.param(200, {"status": "ok", "db": "ok"}, "ok", id="ready"),
        pytest.param(503, {"status": "degraded"}, "warn", id="refused"),
        pytest.param(200, None, "warn", id="no-body-says-nothing"),
        pytest.param(200, ["ok"], "warn", id="a-body-that-is-not-a-readiness-body"),
    ],
)
async def test_model_gateway_check(status_code: int, body: object, expected: str) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(status_code)
        return httpx.Response(status_code, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    async with client:
        result = await dh._check_model_gateway(_ctx(http_client=client))
    assert result[0].status == expected


@pytest.mark.asyncio
async def test_gateway_check_reads_body() -> None:
    """A gateway inside its grace window answers a load balancer 200 with
    ``status: "degraded"``: its database is unreachable. The check asks the
    strict probe and reads the body, so that gateway is not reported ready."""
    asked: list[httpx.URL] = []

    def graced(request: httpx.Request) -> httpx.Response:
        asked.append(request.url)
        return httpx.Response(
            200, json={"status": "degraded", "db": None, "detail": "database unreachable"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(graced))
    async with client:
        result = await dh._check_model_gateway(_ctx(http_client=client))

    assert result[0].status == "warn"
    assert "degraded" in result[0].detail
    assert [url.params.get("strict") for url in asked] == ["1"]


@pytest.mark.asyncio
async def test_entitlement_consistency_flags_a_gateway_backend_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails when the gateway and this process disagree on BYOK — one has
    ALKERA_ENTITLEMENTS and the other doesn't (the half-active-BYOK footgun where the
    gateway bills at provider cost while the backend still grants Alkera credit)."""
    monkeypatch.setattr(settings, "self_hosted", True)
    monkeypatch.setattr(settings, "gateway_upstream", "direct")
    monkeypatch.setattr(dh, "byok_active", lambda: True)  # this process: BYOK on

    def _gateway_reporting(byok: bool) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"status": "ok", "byok": byok})
            )
        )

    async with _gateway_reporting(True) as client:  # gateway agrees → ok
        agree = await dh._check_entitlement_consistency(_ctx(http_client=client))
    assert agree[0].status == "ok"

    async with _gateway_reporting(False) as client:  # gateway disagrees → fail
        split = await dh._check_entitlement_consistency(_ctx(http_client=client))
    assert split[0].status == "fail"
    assert "disagree" in split[0].detail


@pytest.mark.asyncio
async def test_entitlement_consistency_skipped_off_the_byok_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "self_hosted", True)
    monkeypatch.setattr(settings, "gateway_upstream", "proxy")  # bill-through-Alkera
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    async with client:
        result = await dh._check_entitlement_consistency(_ctx(http_client=client))
    assert result[0].status == "skipped"


@pytest.mark.asyncio
async def test_model_gateway_unreachable_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with client:
        result = await dh._check_model_gateway(_ctx(http_client=client))
    assert result[0].status == "fail"
    assert "GATEWAY_BASE_URL" in result[0].detail


# --- the Temporal probe ----------------------------------------------------------


class _FakeWorkflowService:
    def __init__(self, *, fail: Exception | None = None, hang: bool = False) -> None:
        self.fail = fail
        self.hang = hang
        self.requests: list[DescribeNamespaceRequest] = []

    async def describe_namespace(self, request: DescribeNamespaceRequest) -> object:
        self.requests.append(request)
        if self.hang:
            await asyncio.sleep(10)
        if self.fail is not None:
            raise self.fail
        return object()


class _FakeServiceClient:
    def __init__(self, workflow_service: _FakeWorkflowService) -> None:
        self.workflow_service = workflow_service


class _FakeClient:
    """The slice of ``temporalio.client.Client`` the probe touches."""

    def __init__(self, namespace: str, workflow_service: _FakeWorkflowService) -> None:
        self.namespace = namespace
        self.service_client = _FakeServiceClient(workflow_service)


def _factory(
    client: _FakeClient | None = None, *, fail: Exception | None = None, hang: bool = False
) -> Callable[[], Awaitable[Any]]:
    async def connect() -> Any:
        if hang:
            await asyncio.sleep(10)
        if fail is not None:
            raise fail
        return client

    return connect


@pytest.mark.asyncio
async def test_temporal_check_is_ok_when_the_namespace_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "temporal_namespace", "alkera.prod")
    service = _FakeWorkflowService()
    ctx = _ctx(temporal_client_factory=_factory(_FakeClient("alkera.prod", service)))
    result = await dh._check_temporal(ctx)
    assert result[0].key == "temporal"
    assert result[0].label == "Task orchestrator"
    assert result[0].status == "ok"
    assert "'alkera.prod'" in result[0].detail
    # The probe asks about the namespace the client is bound to, not a guess.
    assert [r.namespace for r in service.requests] == ["alkera.prod"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_factory",
    [
        pytest.param(
            lambda: _factory(fail=ConnectionRefusedError("connect: refused by sk-verysecret")),
            id="connect-raises",
        ),
        pytest.param(
            lambda: _factory(
                _FakeClient("default", _FakeWorkflowService(fail=RuntimeError("sk-verysecret")))
            ),
            id="describe-raises",
        ),
        pytest.param(lambda: _factory(hang=True), id="connect-hangs"),
        pytest.param(
            lambda: _factory(_FakeClient("default", _FakeWorkflowService(hang=True))),
            id="describe-hangs",
        ),
    ],
)
async def test_temporal_check_fails_closed_and_names_the_settings(
    make_factory: Callable[[], Callable[[], Awaitable[Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every failure shape — refused, erroring, or silent at either step — is one
    canned ``fail`` naming the address and the env vars to check, within the probe's
    own budget, and never carrying the transport's error text or the API key."""
    monkeypatch.setattr(settings, "temporal_address", "temporal.internal:7233")
    monkeypatch.setattr(settings, "temporal_namespace", "default")
    monkeypatch.setattr(settings, "temporal_api_key", "sk-verysecret")
    monkeypatch.setattr(dh, "TEMPORAL_PROBE_TIMEOUT_S", 0.2)
    started = time.monotonic()
    result = await dh._check_temporal(_ctx(temporal_client_factory=make_factory()))
    elapsed = time.monotonic() - started
    assert result[0].key == "temporal"
    assert result[0].status == "fail"
    assert "temporal.internal:7233" in result[0].detail
    assert "'default'" in result[0].detail
    for env_var in ("TEMPORAL_ADDRESS", "TEMPORAL_NAMESPACE", "TEMPORAL_API_KEY"):
        assert env_var in result[0].detail
    assert "sk-verysecret" not in result[0].detail
    assert "refused" not in result[0].detail
    assert elapsed < 2.0  # bounded by the probe's own timeout, not the runner's


@pytest.mark.asyncio
async def test_temporal_check_connects_through_the_shared_client_factory_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an injected factory the probe opens its connection with the same
    settings-driven ``connect_client`` the worker and the backend use — one
    address / TLS / identity resolution for every consumer — under its own name."""
    seen: list[dict[str, Any]] = []
    service = _FakeWorkflowService()

    async def fake_connect(**kwargs: Any) -> _FakeClient:
        seen.append(kwargs)
        return _FakeClient("default", service)

    monkeypatch.setattr(dh, "connect_client", fake_connect)
    result = await dh._check_temporal(_ctx())
    assert result[0].status == "ok"
    assert seen == [{"component": "deployment-health"}]
    assert len(service.requests) == 1


@pytest.mark.asyncio
@pytest.mark.temporal
async def test_temporal_check_is_ok_against_a_real_dev_server(
    temporal_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real handshake: a live ``Client`` against the session dev server answers
    ``describe_namespace`` for its namespace and the probe reports ok."""
    monkeypatch.setattr(settings, "temporal_namespace", temporal_client.namespace)

    async def connect() -> Any:
        return temporal_client

    result = await dh._check_temporal(_ctx(temporal_client_factory=connect))
    assert result[0].status == "ok", result[0].detail
    assert repr(temporal_client.namespace) in result[0].detail
    assert result[0].latency_ms < 5_000


def test_with_nothing_registered_only_the_open_checks_run() -> None:
    assert dh.registered_checks(ExtensionPoint("checks")) == dh.CHECK_REGISTRY


def test_a_registered_check_runs_after_the_open_ones() -> None:
    async def _check_extra(ctx: dh.CheckContext) -> list[dh.CheckResult]:
        return []

    point: ExtensionPoint[dh.Check] = ExtensionPoint("checks")
    point.register(_check_extra)
    assert dh.registered_checks(point) == (*dh.CHECK_REGISTRY, _check_extra)


def test_the_open_checks_read_no_billing_table() -> None:
    """The model catalog is billing's; its check registers from billing."""
    assert "catalog" not in [dh._name(check) for check in dh.CHECK_REGISTRY]


def test_the_open_checks_name_no_hosted_gateway() -> None:
    """The Alkera hosted gateway and its proxy token are billing's; an open
    install shows neither row."""
    names = {dh._name(check) for check in dh.CHECK_REGISTRY}
    assert not {"alkera_upstream", "alkera_proxy_token", "proxy_token"} & names


def test_the_registry_probes_temporal_and_no_broker() -> None:
    names = [dh._name(check) for check in dh.CHECK_REGISTRY]
    assert "temporal" in names
    assert names.index("temporal") < names.index("worker_beat")
    assert not {"redis", "broker", "celery_broker"} & set(names)


@pytest.mark.asyncio
async def test_secret_box_round_trips() -> None:
    async with httpx.AsyncClient() as client:
        result = await dh._check_secret_box(_ctx(http_client=client))
    assert result[0].status == "ok"


# --- persistence -----------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean() -> Any:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(DeploymentHealthCheck))
        await db.execute(delete(DeploymentHealthRun))
        await db.commit()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(delete(DeploymentHealthCheck))
        await db.execute(delete(DeploymentHealthRun))
        await db.commit()


@pytest.mark.asyncio
async def test_persist_full_replaces_and_bumps_scheduled_stamp() -> None:
    t0 = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    await dh.persist_snapshot(
        AsyncSessionLocal,
        [CheckResult("postgres", "Postgres", "ok", "a", 1)],
        trigger="scheduled",
        started_at=t0,
        duration_ms=10,
    )
    # A manual run replaces the rows but must NOT advance last_scheduled_at.
    t1 = t0 + timedelta(minutes=1)
    await dh.persist_snapshot(
        AsyncSessionLocal,
        [CheckResult("temporal", "Task orchestrator", "ok", "b", 2)],
        trigger="manual",
        started_at=t1,
        duration_ms=20,
    )
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(DeploymentHealthCheck))).scalars().all()
        run = await db.get(DeploymentHealthRun, 1)
    assert {r.check_key for r in rows} == {"temporal"}  # full-replace, old rows gone
    assert run is not None
    assert run.last_trigger == "manual"
    assert run.last_scheduled_at == t0  # scheduled stamp preserved across the manual run


@pytest.mark.asyncio
async def test_load_snapshot_scopes_org_rows() -> None:

    from alkera_core.models import Team

    async with AsyncSessionLocal() as db:
        mine = Team(name="mine", is_root=True)
        theirs = Team(name="theirs", is_root=True)
        db.add_all([mine, theirs])
        await db.flush()
        my_id, their_id = mine.id, theirs.id
        await db.commit()

    ran = datetime.now(UTC)
    await dh.persist_snapshot(
        AsyncSessionLocal,
        [
            CheckResult("postgres", "Postgres", "ok", "instance", 1),
            CheckResult("provider_anthropic", "Anthropic", "ok", "mine", 2, org_team_id=my_id),
            CheckResult("provider_openai", "OpenAI", "fail", "theirs", 3, org_team_id=their_id),
        ],
        trigger="scheduled",
        started_at=ran,
        duration_ms=5,
    )
    async with AsyncSessionLocal() as db:
        results, meta = await load_snapshot(db, org_team_id=my_id)
    keys = {r.key for r in results}
    assert "postgres" in keys  # instance-level always visible
    assert "provider_anthropic" in keys  # my org
    assert "provider_openai" not in keys  # foreign org excluded
    assert meta is not None


# --- no-secrets invariant --------------------------------------------------------


@pytest.mark.asyncio
async def test_no_check_detail_leaks_a_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "topsecret-passphrase-42"
    monkeypatch.setattr(settings, "temporal_api_key", secret)
    monkeypatch.setattr(dh, "TEMPORAL_PROBE_TIMEOUT_S", 0.2)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with client:
        results = await run_all_checks(
            _ctx(
                http_client=client,
                trigger="scheduled",
                temporal_client_factory=_factory(fail=PermissionError(f"bad api key {secret}")),
            )
        )
    keys = {r.key for r in results}
    assert "temporal" in keys  # the probe ran and its failure went through the sweep
    for r in results:
        assert secret not in r.detail, f"{r.key} leaked a secret"


# --- the Files object-store probe ------------------------------------------------


class _ProbeStore:
    """A store that answers the probe's HEAD however the case says.

    Only ``head`` is implemented: it is the one call ``probe_files_store``
    makes, and a stub that cannot serve anything else keeps the case honest.
    """

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[str] = []

    async def head(self, key: str) -> ObjectInfo | None:
        self.calls.append(key)
        if self.answer == "hang":
            await asyncio.sleep(3600)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@pytest.fixture
def gauge(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Every value the probe pushed onto ``files_store_available``."""
    written: list[bool] = []
    monkeypatch.setattr(dh, "record_files_store_available", written.append)
    return written


@pytest.mark.parametrize(
    ("answer", "available"),
    [
        pytest.param(
            ObjectInfo(size=0, checksum=None, etag=None, storage_class=None),
            True,
            id="200-the-object-is-there",
        ),
        # The S3 driver spells a 404 as None; the filesystem driver does too.
        pytest.param(None, True, id="404-no-such-key-is-an-answer"),
        pytest.param(NotFound("NoSuchKey"), True, id="404-raised-is-an-answer"),
        pytest.param(Throttled("SlowDown", retry_after=1.0), True, id="429-it-answered"),
        pytest.param(NoSuchBucket("NoSuchBucket"), False, id="the-bucket-does-not-exist"),
        pytest.param(Unavailable("500 (http 500)"), False, id="transport-or-5xx"),
        pytest.param(AccessDenied("AccessDenied"), False, id="auth-refused"),
        pytest.param(ExpiredCredentials("ExpiredToken"), False, id="credential-expired"),
        pytest.param(TimeoutError(), False, id="timed-out"),
        pytest.param(
            InvalidKey("relative key may not carry the domains/ prefix"),
            False,
            id="the-probe-never-reached-the-wire",
        ),
    ],
)
async def test_the_probe_calls_a_store_that_answers_available(
    answer: Any, available: bool, gauge: list[bool]
) -> None:
    store = _ProbeStore(answer)
    assert await dh.probe_files_store(store) is available
    assert store.calls == [dh.FILES_STORE_PROBE_KEY]
    assert gauge == [available]


async def test_a_store_that_never_answers_is_unavailable_within_the_budget(
    gauge: list[bool],
) -> None:
    store = _ProbeStore("hang")
    started = time.monotonic()
    assert await dh.probe_files_store(store) is False
    assert time.monotonic() - started < dh.FILES_STORE_PROBE_TIMEOUT_S + 1.0
    assert gauge == [False]


def test_the_driver_tells_a_missing_bucket_from_a_missing_key() -> None:
    """A 404 with no code at all (what ``HeadObject`` returns) stays a plain
    absence; only the endpoint that names ``NoSuchBucket`` gets the subclass."""

    def client_error(code: str, status: int) -> ClientError:
        return ClientError(
            {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "HeadObject"
        )

    bucket = normalize_client_error(client_error("NoSuchBucket", 404))
    assert isinstance(bucket, NoSuchBucket)
    key = normalize_client_error(client_error("NoSuchKey", 404))
    assert isinstance(key, NotFound) and not isinstance(key, NoSuchBucket)
    bare = normalize_client_error(client_error("", 404))
    assert isinstance(bare, NotFound) and not isinstance(bare, NoSuchBucket)


class _BucketedProbeStore(_ProbeStore):
    """What the S3 driver is: a store that can be asked about its bucket too."""

    def __init__(self, answer: Any, *, bucket: BaseException | None = None) -> None:
        super().__init__(answer)
        self.bucket = bucket
        self.bucket_calls = 0

    async def head_bucket(self) -> None:
        self.bucket_calls += 1
        if self.bucket is not None:
            raise self.bucket


async def test_a_missing_bucket_is_read_off_the_bucket_head_not_the_key_head(
    gauge: list[bool],
) -> None:
    """The key HEAD cannot see a wrong bucket at all: S3 answers a HEAD with no
    body, so the 404 under a bucket that does not exist names no error code and
    arrives as the same plain absence a missing key does — which the probe reads
    as proof of health. Ask the one call whose absence can only be the bucket."""
    store = _BucketedProbeStore(None, bucket=NoSuchBucket("nope"))

    assert await dh.probe_files_store(store) is False

    assert store.bucket_calls == 1
    # The bucket is gone; there is nothing left for a key HEAD to prove.
    assert store.calls == []
    assert gauge == [False]


async def test_a_present_bucket_with_no_probe_object_is_still_available(
    gauge: list[bool],
) -> None:
    """The probe key is never written, so a fresh bucket 404s on it forever;
    that is an answer, not an outage."""
    store = _BucketedProbeStore(None)

    assert await dh.probe_files_store(store) is True

    assert store.bucket_calls == 1
    assert store.calls == [dh.FILES_STORE_PROBE_KEY]
    assert gauge == [True]


async def test_a_wrong_bucket_answers_red_through_the_real_driver(gauge: list[bool]) -> None:
    """End to end on the driver, with the endpoint behaving as S3 does: every
    HEAD under a bucket that is not there is a bare, code-less 404. Before the
    bucket HEAD, that reached the probe as ``NotFound`` and a mistyped
    ``FILES_STORE_BUCKET`` reported the deployment healthy."""

    def bare_404() -> ClientError:
        return ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}},
            "HeadBucket",
        )

    class _Client:
        async def head_bucket(self, **_: Any) -> dict[str, Any]:
            raise bare_404()

        async def head_object(self, **_: Any) -> dict[str, Any]:
            raise bare_404()

    @asynccontextmanager
    async def factory() -> AsyncIterator[_Client]:
        yield _Client()

    store = S3CompatibleStore(
        S3Config(endpoint_url="http://127.0.0.1:1", region="us-east-1", bucket="typo"),
        clock=SystemClock(),
        layout="bucket",
        client_factory=factory,
    )

    assert await dh.probe_files_store(store) is False
    assert gauge == [False]


async def test_a_driver_with_no_bucket_to_miss_is_probed_on_the_key_alone(
    gauge: list[bool],
) -> None:
    """The filesystem driver has no container that can be absent, so it never
    grows a bucket HEAD — and the probe must not require one of it."""
    store = _ProbeStore(None)
    assert not hasattr(store, "head_bucket")

    assert await dh.probe_files_store(store) is True

    assert store.calls == [dh.FILES_STORE_PROBE_KEY]
    assert gauge == [True]


async def test_the_driver_head_hands_a_missing_bucket_to_the_probe() -> None:
    """``head`` swallows a missing key into ``None``; a missing bucket has to
    reach the caller, or the probe can never see it."""

    class _Client:
        async def head_object(self, **_: Any) -> dict[str, Any]:
            raise ClientError(
                {"Error": {"Code": "NoSuchBucket"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )

    @asynccontextmanager
    async def factory() -> AsyncIterator[_Client]:
        yield _Client()

    store = S3CompatibleStore(
        S3Config(endpoint_url="http://127.0.0.1:1", region="us-east-1", bucket="nope"),
        clock=SystemClock(),
        layout="bucket",
        client_factory=factory,
    )
    with pytest.raises(NoSuchBucket):
        await store.head(dh.FILES_STORE_PROBE_KEY)


@pytest.mark.live
async def test_the_probe_is_available_against_a_real_store_with_no_probe_object() -> None:
    """The whole point of F-332: a real bucket that has never been written to.

    ``FILES_LIVE_ENDPOINTS`` is the JSON list ``make files-live-local`` builds
    for this worktree's SeaweedFS; without it there is no store to ask.
    """
    raw = os.environ.get("FILES_LIVE_ENDPOINTS")
    if not raw:
        pytest.skip("FILES_LIVE_ENDPOINTS is unset - run `make files-live-local`")
    entry = json.loads(raw)[0]
    store = S3CompatibleStore(
        S3Config(
            endpoint_url=entry["endpoint"],
            region=entry.get("region", "us-east-1"),
            bucket=entry["bucket"],
            access_key=entry["access_key"],
            secret_key=entry["secret_key"],
            addressing="path",
        ),
        clock=SystemClock(),
        layout="bucket",
    )
    assert await store.head(dh.FILES_STORE_PROBE_KEY) is None
    assert await dh.probe_files_store(store) is True


# --- the two rows that cannot apply on SaaS -------------------------------------


@pytest.mark.asyncio
async def test_the_worker_beat_is_skipped_on_saas_and_warns_self_hosted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scheduled snapshot self-skips on SaaS (the worker never stamps the
    marker), so a manual read there must not warn forever; a self-hosted
    deployment with no stamp is the real "worker may be starting" case."""
    from alkera_core.deployment_health import _check_worker_beat

    monkeypatch.setattr(settings, "self_hosted", False)
    (saas,) = await _check_worker_beat(_ctx())
    assert saas.status == "skipped"
    assert "self-skips on SaaS" in saas.detail
    monkeypatch.setattr(settings, "self_hosted", True)
    (hosted,) = await _check_worker_beat(_ctx())
    assert hosted.status == "warn"
    assert "expected every 5 minutes" in hosted.detail


def test_provider_rows_without_org_keys_point_at_the_gateway_on_saas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no organization keys, the env fallback describes THIS process: on
    SaaS the provider keys live on the model gateway, so the rows are skipped
    with that reason; self-hosted, an empty env is a real warning; a key in
    this process's env is ok either way."""
    import alkera_core.deployment_health as health

    monkeypatch.setattr(health, "_env_provider_configured", lambda provider: False)
    monkeypatch.setattr(settings, "self_hosted", False)
    rows = health._env_fallback_rows()
    assert rows and {r.status for r in rows} == {"skipped"}
    assert all("model gateway" in r.detail for r in rows)
    monkeypatch.setattr(settings, "self_hosted", True)
    rows = health._env_fallback_rows()
    assert rows and {r.status for r in rows} == {"warn"}
    assert all("no credentials configured" in r.detail for r in rows)
    monkeypatch.setattr(health, "_env_provider_configured", lambda provider: True)
    monkeypatch.setattr(settings, "self_hosted", False)
    rows = health._env_fallback_rows()
    assert rows and {r.status for r in rows} == {"ok"}


@pytest.mark.asyncio
async def test_a_process_that_hands_over_no_store_reports_the_files_check_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """alkera_core imports no app: the store comes from the caller that holds
    one (the backend). Handed none, as in the worker, the check is skipped
    rather than reaching for the backend's factory itself."""
    monkeypatch.setattr(settings, "files_enabled", True)
    ctx = CheckContext(
        trigger="manual",
        session_factory=None,  # type: ignore[arg-type] — this check touches no session
        http_client=None,  # type: ignore[arg-type] — nor any HTTP client
    )

    (result,) = await dh._check_files_store(ctx)

    assert result.key == "files_store"
    assert result.status == "skipped"
