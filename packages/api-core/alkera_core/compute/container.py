"""The platform container sandbox — declared in the registry, not yet built.

A web chat runs on the platform's container service and on nothing else. That
placement rule, the catalog row it names and the settings that select it are
useful before the container service itself exists, so the ``container``
provider kind is registered from here and every operation on it REFUSES.

Refusing is the whole point. A stub that returned a plausible pod id would let
the plane record an allocation for a machine nobody started, meter it, and
report a chat as placed on compute that does not exist; a refusal names the
deployment fact instead, at the one call that needed the thing. The refusal is
:class:`ComputeProviderUnavailableError` — permanent until the deployment changes,
never "try again" — so a route maps it the same way it maps any provider
failure and a sweep skips the row rather than terminating it.

When the real thing lands (ECS ``RunTask`` / ``DescribeTasks`` / ``StopTask``,
catalog price from the task size) it replaces this class behind the same
registration and nothing that calls :func:`provider_for` changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from alkera_core.compute.availability import Availability, SizeQuery, unknown
from alkera_core.compute.provider import (
    CONTAINER,
    ComputeProvider,
    ComputeProviderUnavailableError,
    NodeDescription,
    NodeLaunch,
    PodPhase,
    PodStatus,
    ProviderPod,
    register_provider,
)
from alkera_core.db.locking import io_boundary_class

if TYPE_CHECKING:
    from alkera_core.config import Settings
    from alkera_core.models.compute import ComputeMachineType

UNAVAILABLE = "the container service is not provisioned in this deployment yet"
"""The one sentence every refusal carries — stated once so a log, a 502 body
and a test all read the same words."""


@io_boundary_class("compute.container")
class ContainerProvider:
    """The container sandbox as a not-yet-built provider: implements the whole
    ``ComputeProvider`` surface, and fails CLOSED.

    Every operation that would act on a machine refuses with
    :class:`ComputeProviderUnavailableError` — permanent until the deployment
    changes, never "try again" — so a route maps it the same way it maps any
    provider failure and a sweep skips the row rather than terminating it. The
    two questions the plane asks BEFORE it would act answer without raising, so a
    listing is never a 500: :meth:`configured` is ``False`` (the size shows as
    unavailable, and provisioning refuses with a clean 409) and
    :meth:`availability` reports ``unknown`` with the reason.

    When the real thing lands (ECS ``RunTask`` / ``DescribeTasks`` / ``StopTask``)
    it replaces this class behind the same registration and nothing that calls
    :func:`~alkera_core.compute.provider.provider_for` changes."""

    kind = CONTAINER

    def _refuse(self) -> ComputeProviderUnavailableError:
        return ComputeProviderUnavailableError(UNAVAILABLE)

    def configured(self) -> bool:
        return False

    async def availability(self, sizes: list[SizeQuery]) -> dict[str, Availability]:
        return {s.code: unknown(UNAVAILABLE) for s in sizes}

    # -- provisioning + lifecycle (all refuse: nothing was ever started) ------

    async def create_pod(
        self, *, name: str, machine_type: ComputeMachineType, ssh_public_key: str
    ) -> str:
        raise self._refuse()

    async def run(self, launch: NodeLaunch) -> str:
        raise self._refuse()

    async def store_credential(self, allocation_id: UUID, secrets: dict[str, str]) -> None:
        raise self._refuse()

    async def delete_credential(self, allocation_id: UUID) -> None:
        raise self._refuse()

    async def bind_credential(self, allocation_id: UUID, machine_id: str) -> None:
        raise self._refuse()

    async def pod_status(self, pod_id: str) -> PodStatus:
        raise self._refuse()

    async def describe(self, machine_id: str) -> NodeDescription:
        raise self._refuse()

    async def find(self, allocation_id: UUID) -> NodeDescription | None:
        raise self._refuse()

    async def terminate_pod(self, pod_id: str) -> None:
        raise self._refuse()

    async def stop(self, machine_id: str) -> None:
        raise self._refuse()

    async def start(self, machine_id: str) -> None:
        raise self._refuse()

    async def grow_volume(self, machine_id: str, size_gb: int) -> None:
        raise self._refuse()

    async def terminate(self, machine_id: str) -> None:
        raise self._refuse()

    async def list_pods(self, *, name_prefix: str = "") -> list[ProviderPod]:
        raise self._refuse()

    def normalize_status(self, raw_status: str) -> PodPhase:
        raise self._refuse()

    async def catalog_prices(self, sizes: list[SizeQuery] | None = None) -> dict[str, int]:
        raise self._refuse()

    async def catalog_entries(
        self, sizes: list[SizeQuery] | None = None
    ) -> dict[str, dict[str, Any]]:
        raise self._refuse()


def make_container_provider(settings: Settings) -> ComputeProvider:
    """Registry factory. Annotated as the Protocol so the typechecker — not a
    test — is what proves ``ContainerProvider`` still implements the surface."""
    return ContainerProvider()


register_provider(CONTAINER, make_container_provider)

__all__ = ["UNAVAILABLE", "ContainerProvider", "make_container_provider"]
