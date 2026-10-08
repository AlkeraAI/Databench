"""A self-hosted box: a host the deployment's operator runs, registered by itself.

The one-machine Docker Compose install runs its box beside the services: the
box signs in through the device flow and calls the register route, and the
registration makes it its org's machine (an org machine the org added, with
nothing charged). The plane did not create it and cannot start, stop, price or
inspect it; its daemon's heartbeat is the only liveness it has. So, like a
personal box, every provider operation REFUSES with
:class:`ComputeProviderUnavailableError`, the permanent "not here" a route, a
sweep and the org-machine reconcile read as "skip this row".

The kind's one catalog row (:data:`SELF_HOSTED_TYPE_CODE`) is seeded by every
deployment and never offered for a new allocation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alkera_core.compute.availability import Availability, SizeQuery, unknown
from alkera_core.compute.personal import PersonalProvider
from alkera_core.compute.provider import (
    SELF_HOSTED,
    ComputeProvider,
    ComputeProviderUnavailableError,
    ProviderTraits,
    register_provider,
)

if TYPE_CHECKING:
    from alkera_core.config import Settings

UNAVAILABLE = "a self-hosted box is run by its operator; the plane does not run it"
"""The one sentence every refusal carries."""

#: The catalog code every self-hosted box registers as.
SELF_HOSTED_TYPE_CODE = "self-hosted"


class SelfHostedProvider(PersonalProvider):
    """The personal box's refusals under this kind and reason: nothing can be
    provisioned (:meth:`configured` is ``False``) and every call refuses."""

    kind = SELF_HOSTED

    def _refuse(self) -> ComputeProviderUnavailableError:
        return ComputeProviderUnavailableError(UNAVAILABLE)

    async def availability(self, sizes: list[SizeQuery]) -> dict[str, Availability]:
        return {s.code: unknown(UNAVAILABLE) for s in sizes}


def make_self_hosted_provider(settings: Settings) -> ComputeProvider:
    """Registry factory, annotated as the Protocol so the typechecker proves the
    class still implements the surface."""
    return SelfHostedProvider()


register_provider(
    SELF_HOSTED,
    make_self_hosted_provider,
    traits=ProviderTraits(catalog_provisioned=False, self_registers=True),
)

__all__ = [
    "SELF_HOSTED_TYPE_CODE",
    "UNAVAILABLE",
    "SelfHostedProvider",
    "make_self_hosted_provider",
]
