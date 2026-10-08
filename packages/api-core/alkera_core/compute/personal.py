"""A person's own box: a provider kind the plane names but never acts through.

A box registered through the device flow is the person's hardware (a laptop,
a workstation, a VM they run). The plane did not create it and cannot start,
stop, price or inspect it: its daemon's heartbeat is the only liveness it has,
which is how the meter already treats every machine a daemon registered. So
the ``personal`` kind exists so its one catalog row (the fixed ``personal``
machine type) satisfies the catalog's provider constraint, and every operation
REFUSES with :class:`ComputeProviderUnavailableError`, the permanent "not
here" a route and a sweep already read as "skip this row", never as "it is
gone, terminate it".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from alkera_core.compute.availability import Availability, SizeQuery, unknown
from alkera_core.compute.provider import (
    PERSONAL,
    ComputeProvider,
    ComputeProviderUnavailableError,
    NodeDescription,
    NodeLaunch,
    PodPhase,
    PodStatus,
    ProviderPod,
    ProviderTraits,
    register_provider,
)
from alkera_core.db.locking import io_boundary_class

if TYPE_CHECKING:
    from alkera_core.config import Settings
    from alkera_core.models.compute import ComputeMachineType

UNAVAILABLE = "a personal box is its owner's hardware; the plane does not run it"
"""The one sentence every refusal carries."""

#: The fixed catalog row every personal box registers as. Never offered for a
#: new allocation and never priced: nothing is billed for a person's own box.
PERSONAL_MACHINE_TYPE_ID = "personal"


@io_boundary_class("compute.personal")
class PersonalProvider:
    """Implements the whole ``ComputeProvider`` surface and refuses all of it:
    :meth:`configured` is ``False`` (nothing can be provisioned) and
    :meth:`availability` reports ``unknown`` with the reason."""

    kind = PERSONAL

    def _refuse(self) -> ComputeProviderUnavailableError:
        return ComputeProviderUnavailableError(UNAVAILABLE)

    def configured(self) -> bool:
        return False

    async def availability(self, sizes: list[SizeQuery]) -> dict[str, Availability]:
        return {s.code: unknown(UNAVAILABLE) for s in sizes}

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


def make_personal_provider(settings: Settings) -> ComputeProvider:
    """Registry factory, annotated as the Protocol so the typechecker proves the
    class still implements the surface."""
    return PersonalProvider()


register_provider(
    PERSONAL, make_personal_provider, traits=ProviderTraits(catalog_provisioned=False)
)

__all__ = ["PERSONAL_MACHINE_TYPE_ID", "UNAVAILABLE", "PersonalProvider", "make_personal_provider"]
