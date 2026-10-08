"""A scripted, in-memory node provider for tests.

It keeps the machines it was asked to run and the credentials it was asked to
store, and answers ``describe`` from a phase a test sets with :meth:`script`.
Nothing here reaches a network. Register it with
``register_node_provider(kind, lambda _s: fake)`` to put it behind a kind.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from alkera_core.compute.availability import Availability, AvailabilityStatus, SizeQuery
from alkera_core.compute.nodes import NodeDescription, NodeLaunch
from alkera_core.compute.provider import GONE, STARTING, ComputeProviderError, PodPhase
from alkera_core.db.locking import io_boundary_class


@dataclass
class FakeNode:
    launch: NodeLaunch
    phase: PodPhase = STARTING
    stopped: bool = False
    #: What the provider has on record about the node (its last logged error).
    note: str = ""


@dataclass
@io_boundary_class("compute.fake")
class FakeNodeProvider:
    kind: str = "fake"
    is_configured: bool = True
    nodes: dict[str, FakeNode] = field(default_factory=dict)
    secrets: dict[UUID, dict[str, str]] = field(default_factory=dict)
    #: When set, the next ``run`` raises this instead of starting a node.
    fail_run: str = ""
    #: When set, ``bind_credential`` raises this.
    fail_bind: str = ""
    #: allocation id -> the machine its credential was bound to.
    bound: dict[UUID, str] = field(default_factory=dict)
    #: Machine ids whose terminate raises (a provider outage).
    fail_terminate: set[str] = field(default_factory=set)
    #: What ``availability`` answers per size code (absent = not mentioned).
    stock: dict[str, AvailabilityStatus] = field(default_factory=dict)
    #: Seconds ``availability`` takes to answer (drives the time-out path).
    probe_delay: float = 0.0
    probes: int = 0
    #: machine id -> the volume size it was last grown to.
    grown: dict[str, int] = field(default_factory=dict)
    _counter: int = 0

    def configured(self) -> bool:
        return self.is_configured

    async def availability(self, sizes: list[SizeQuery]) -> dict[str, Availability]:
        self.probes += 1
        if self.probe_delay:
            await asyncio.sleep(self.probe_delay)
        moment = datetime.now(UTC)
        return {
            s.code: Availability(status=self.stock[s.code], detail="scripted", checked_at=moment)
            for s in sizes
            if s.code in self.stock
        }

    def script(self, machine_id: str, phase: PodPhase, *, note: str = "") -> None:
        self.nodes[machine_id].phase = phase
        self.nodes[machine_id].note = note

    async def store_credential(self, allocation_id: UUID, secrets: dict[str, str]) -> None:
        self.secrets[allocation_id] = dict(secrets)

    async def delete_credential(self, allocation_id: UUID) -> None:
        self.secrets.pop(allocation_id, None)

    async def bind_credential(self, allocation_id: UUID, machine_id: str) -> None:
        if self.fail_bind:
            raise ComputeProviderError(self.fail_bind)
        self.bound[allocation_id] = machine_id

    async def run(self, launch: NodeLaunch) -> str:
        if self.fail_run:
            message, self.fail_run = self.fail_run, ""
            raise ComputeProviderError(message)
        self._counter += 1
        machine_id = f"{self.kind}-{self._counter:04d}"
        self.nodes[machine_id] = FakeNode(launch=launch)
        return machine_id

    async def describe(self, machine_id: str) -> NodeDescription:
        node = self.nodes.get(machine_id)
        if node is None:
            return NodeDescription(machine_id=machine_id, phase=GONE, raw_status="not-found")
        return NodeDescription(
            machine_id=machine_id, phase=node.phase, raw_status=node.phase, note=node.note
        )

    async def find(self, allocation_id: UUID) -> NodeDescription | None:
        for machine_id, node in self.nodes.items():
            if node.launch.allocation_id == allocation_id and node.phase != GONE:
                return NodeDescription(
                    machine_id=machine_id, phase=node.phase, raw_status=node.phase
                )
        return None

    async def stop(self, machine_id: str) -> None:
        self.nodes[machine_id].stopped = True

    async def start(self, machine_id: str) -> None:
        self.nodes[machine_id].stopped = False

    async def grow_volume(self, machine_id: str, size_gb: int) -> None:
        self.grown[machine_id] = size_gb

    async def terminate(self, machine_id: str) -> None:
        if machine_id in self.fail_terminate:
            raise ComputeProviderError(f"terminate {machine_id} refused")
        node = self.nodes.get(machine_id)
        if node is not None:
            node.phase = GONE


__all__ = ["FakeNode", "FakeNodeProvider"]
