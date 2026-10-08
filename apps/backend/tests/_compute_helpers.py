"""Shared helpers for the compute test modules: a machine-type factory, a grant
factory, the funding account of an allocation (billing's record, read and
written through the compute funding port), and an in-memory ``ComputeProvider``
double that records what the plane asked of it. The provider double speaks
phases (the normalized vocabulary), never a provider word."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from alkera_core.compute.billing_port import compute_funding
from alkera_core.compute.provider import (
    GONE,
    RUNNING,
    STARTING,
    UNKNOWN,
    ComputeProviderError,
    PodPhase,
    PodStatus,
    ProviderPod,
)
from alkera_core.db.locking import io_boundary_class
from alkera_core.models.compute import ComputeAllocation, ComputeGrant, ComputeMachineType
from sqlalchemy.ext.asyncio import AsyncSession

#: The provider's per-minute price of the test machine type (our cost).
TEST_COST = 11_500_000


async def make_machine_type(
    session: AsyncSession,
    *,
    provider: str = "runpod",
    compute_class: str = "cpu",
    provider_price_per_minute_nanos: int = TEST_COST,
    active: bool = True,
    available_for_new: bool = True,
    provider_config: dict[str, Any] | None = None,
) -> ComputeMachineType:
    """A uniquely-named machine type (parallel runs never collide)."""
    mt = ComputeMachineType(
        provider=provider,
        provider_type_id=f"test-{compute_class}-{uuid.uuid4().hex[:10]}",
        display_name=f"Test {compute_class} type",
        compute_class=compute_class,
        gpu_count=1 if compute_class == "gpu" else 0,
        vcpu=8,
        memory_gb=32,
        provider_price_per_minute_nanos=provider_price_per_minute_nanos,
        active=active,
        available_for_new=available_for_new,
        provider_config=provider_config or {},
    )
    session.add(mt)
    await session.commit()
    return mt


async def make_grant(
    session: AsyncSession,
    *,
    org_team_id: UUID,
    machine_type_id: UUID | None = None,
    ceiling: int = 1,
    rate_per_minute_nanos: int = 0,
    per_user_max: int | None = None,
    funding_account_id: UUID | None = None,
    expires_in: timedelta = timedelta(days=14),
    created_by: UUID | None = None,
    note: str = "test grant",
) -> ComputeGrant:
    grant = ComputeGrant(
        org_team_id=org_team_id,
        machine_type_id=machine_type_id,
        ceiling=ceiling,
        rate_per_minute_nanos=rate_per_minute_nanos,
        per_user_max=per_user_max,
        expires_at=datetime.now(UTC) + expires_in,
        created_by=created_by,
        note=note,
    )
    session.add(grant)
    await session.flush()
    await compute_funding().set_grant_funding(session, grant.id, funding_account_id)
    await session.commit()
    return grant


async def fund(session: AsyncSession, alloc: ComputeAllocation, account_id: UUID | None) -> None:
    """Debit the (flushed) allocation's minutes from ``account_id``."""
    await compute_funding().fund_allocation(session, alloc.id, account_id)


async def funding_of(session: AsyncSession, alloc: ComputeAllocation) -> UUID | None:
    """The account the allocation's minutes debit, or ``None``."""
    return await compute_funding().allocation_funding(session, alloc.id)


def running(pod_id: str, *, ip: str = "203.0.113.7", port: int = 10022) -> PodStatus:
    return PodStatus(pod_id=pod_id, phase=RUNNING, public_ip=ip, ssh_port=port, raw_status="UP")


@dataclass
@io_boundary_class("compute.fake")
class FakeProvider:
    """In-memory ComputeProvider double.

    ``status_map`` answers ``pod_status`` per pod (unknown pods read as a benign
    ``starting`` — neither ready nor gone — so a shared database's residue is
    never billed or terminated by a sweep); ``status_errors`` raises instead;
    ``terminate_error`` makes every terminate fail. ``catalog`` / ``catalog_error``
    drive the catalog feed.
    """

    create_calls: list[dict[str, object]] = field(default_factory=list)
    terminate_calls: list[str] = field(default_factory=list)
    status_calls: list[str] = field(default_factory=list)
    status_map: dict[str, PodStatus] = field(default_factory=dict)
    status_errors: dict[str, ComputeProviderError] = field(default_factory=dict)
    create_error: str | None = None
    #: The status the refused create answers with. A status means the provider
    #: ANSWERED and refused (no machine was made); ``None`` means we never heard
    #: the end of the call, which is a different fact and a different outcome.
    create_status_code: int | None = 400
    #: Pods the provider holds, by id — what ``list_pods`` answers with. A real
    #: create adds to it, and a test seeds it to stage an orphan.
    pods: dict[str, ProviderPod] = field(default_factory=dict)
    list_error: ComputeProviderError | None = None
    terminate_error: ComputeProviderError | None = None
    catalog: dict[str, dict[str, Any]] = field(default_factory=dict)
    catalog_error: ComputeProviderError | None = None
    #: Pods the provider runs whose box cannot reach the API: :func:`meter_live`
    #: gives them no beat, which is how a test stages a silent box.
    silent: set[str] = field(default_factory=set)
    list_calls: int = 0
    #: The creation stamp every pod this double makes is born with.
    created_at: datetime | None = None
    _counter: int = 0

    async def create_pod(
        self, *, name: str, machine_type: ComputeMachineType, ssh_public_key: str
    ) -> str:
        if self.create_error is not None:
            raise ComputeProviderError(self.create_error, status_code=self.create_status_code)
        self._counter += 1
        pod_id = f"fakepod-{self._counter}"
        self.pods[pod_id] = ProviderPod(
            pod_id=pod_id, name=name, created_at=self.created_at, phase=STARTING
        )
        self.create_calls.append(
            {
                "name": name,
                "provider_type_id": machine_type.provider_type_id,
                "provider_config": dict(machine_type.provider_config or {}),
                "ssh_public_key": ssh_public_key,
                "pod_id": pod_id,
            }
        )
        return pod_id

    async def list_pods(self, *, name_prefix: str = "") -> list[ProviderPod]:
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return [p for p in self.pods.values() if p.name.startswith(name_prefix)]

    async def pod_status(self, pod_id: str) -> PodStatus:
        self.status_calls.append(pod_id)
        if pod_id in self.status_errors:
            raise self.status_errors[pod_id]
        return self.status_map.get(pod_id, PodStatus(pod_id=pod_id, phase=STARTING))

    async def terminate_pod(self, pod_id: str) -> None:
        self.terminate_calls.append(pod_id)
        if self.terminate_error is not None:
            raise self.terminate_error
        self.pods.pop(pod_id, None)

    def normalize_status(self, raw_status: str) -> PodPhase:
        return {"UP": RUNNING, "DOWN": GONE, "BOOT": STARTING}.get(raw_status, UNKNOWN)

    async def catalog_prices(self, sizes: Any = None) -> dict[str, int]:
        if self.catalog_error is not None:
            raise self.catalog_error
        return {k: int(v.get("price_nanos") or 0) for k, v in self.catalog.items()}

    async def catalog_entries(self, sizes: Any = None) -> dict[str, dict[str, Any]]:
        if self.catalog_error is not None:
            raise self.catalog_error
        return dict(self.catalog)

    def mark_ready(self, pod_id: str, *, ip: str = "203.0.113.7", port: int = 10022) -> None:
        self.status_map[pod_id] = running(pod_id, ip=ip, port=port)

    def mark_gone(self, pod_id: str) -> None:
        self.status_map[pod_id] = PodStatus(pod_id=pod_id, phase=GONE, raw_status="DOWN")


__all__ = ["TEST_COST", "FakeProvider", "make_grant", "make_machine_type", "running"]


async def hold_with_credential(session: AsyncSession, alloc: Any, *, revoked: bool = False) -> str:
    """Bind a machine credential to a platform box written straight into the
    database, the way ``/machines/claim`` leaves one: a pool or dedicated box
    stands only while a live credential holds it. ``revoked`` leaves it held by
    a revoked one — the box the platform took away. Commits, and returns the
    raw credential so a test can speak as the box (its heartbeat)."""
    from alkera_core.auth.machine_token import mint_machine_token
    from alkera_core.models import MachineCredential

    raw, digest = mint_machine_token()
    session.add(
        MachineCredential(
            token_hash=digest,
            label=alloc.name or "box",
            org_team_id=alloc.org_team_id,
            machine_type_id=alloc.machine_type_id,
            tenancy=alloc.tenancy,
            machine_id=alloc.id,
            revoked_at=datetime.now(UTC) if revoked else None,
        )
    )
    await session.commit()
    return raw
