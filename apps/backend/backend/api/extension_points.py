"""What a private extension may add to the backend app.

The open factory (``backend.app_factory``) reads these points when it builds the
app; an extension registers into them from its ``install``. Where a router
mounts is the extension's choice of :class:`RouterSlot`, and the factory, not
the extension, applies the gate that slot stands for, so a registered router can
never land outside the trust model of the surfaces it sits beside.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum

from alkera_core.extensions import ExtensionPoint
from alkera_core.validation.storable_text import SelfValidated
from fastapi import APIRouter, FastAPI


class RouterSlot(StrEnum):
    """Where a registered router mounts, and the gate it gets there."""

    #: No gate: each route authenticates its caller itself (a webhook's
    #: signature, an OAuth callback's state, its own session dependency).
    #: Mounted after the core webhooks.
    PUBLIC = "public"
    #: Behind the email-verification gate every product router sits behind.
    #: Mounted with the org settings.
    GATED = "gated"
    #: The same gate, but a box on its own machine credential passes through to
    #: the route's own decision: a route a chat's machine calls.
    GATED_OR_MACHINE = "gated_or_machine"
    #: On ``/admin/v1``, behind the platform-staff floor and its cross-tenant
    #: window. Mounted with the per-org admin surfaces. Its routes must use
    #: ``AuditedRoute`` like every admin sub-router.
    ADMIN = "admin"
    #: No gate, under the webhook rate-limit class: a provider posting to an
    #: endpoint that trusts its signature alone (billing, the GitHub App).
    WEBHOOK = "webhook"
    #: A box's door on its own machine credential: the gate admits the machine
    #: principal (and holds a person to email verification), and each route
    #: decides on the chat or workspace the box holds. Under the chat
    #: rate-limit class.
    MACHINE = "machine"


class MountPoint(StrEnum):
    """Where in the route table a registered router is included.

    Starlette matches routes in the order they were included and the OpenAPI
    document lists paths in that order, so a router's place in the table is part
    of what the app serves. Each point is a fixed place beside the open routers
    it is named for; within a point, routers are included in registration order.
    """

    #: After the person's own account lifecycle, ahead of the core webhooks:
    #: ungated, like it.
    ACCOUNT_LIFECYCLE = "account_lifecycle"
    #: With the core webhooks, ahead of a provider's shutdown notice.
    WEBHOOKS = "webhooks"
    #: After the core webhooks, ahead of the org settings.
    PUBLIC = "public"
    #: After the org settings, ahead of the org's members.
    ORG = "org"
    #: After the org's audit surfaces, ahead of the users routes.
    KNOWLEDGE = "knowledge"
    #: After the teams routes, ahead of the memberships routes.
    CONNECTIONS = "connections"
    #: After the org's machines, ahead of a person's own boxes.
    ORG_MACHINES = "org_machines"
    #: After the realtime socket, ahead of the reader's preferences.
    ME = "me"
    #: After the reader's preferences, ahead of the admin surfaces.
    ACCOUNT = "account"
    #: The platform-admin router.
    ADMIN = "admin"
    #: After SCIM, ahead of Files.
    CI = "ci"
    #: After the workspace routes, ahead of the objects a box fills: the
    #: doors a box calls for what it holds.
    HELD = "held"


#: Where a router mounts when it names its slot and no point.
DEFAULT_POINT: dict[RouterSlot, MountPoint] = {
    RouterSlot.PUBLIC: MountPoint.PUBLIC,
    RouterSlot.GATED: MountPoint.ORG,
    RouterSlot.ADMIN: MountPoint.ADMIN,
    RouterSlot.WEBHOOK: MountPoint.WEBHOOKS,
    RouterSlot.MACHINE: MountPoint.HELD,
}


@dataclass(frozen=True, slots=True)
class SignedBody:
    """A path whose sender signs every body (a provider's webhook, Slack's
    events), and whose bodies the edge does not bound.

    The edge exempts ``path`` from its 8 KB body rule, and the app's pre-buffer
    guard bounds it instead: a body over ``max_bytes`` is refused from its
    declared length, and one that arrives without every header in
    ``signature_headers`` is refused with the bare 400 the route gives an
    unsigned call, before any of the body is read. Header names are lowercase.
    """

    path: str
    max_bytes: int
    signature_headers: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.signature_headers:
            raise ValueError(f"{self.path} names no signature header")
        if any(name != name.lower() for name in self.signature_headers):
            raise ValueError(f"{self.path}: signature headers are matched lowercase")
        if self.max_bytes <= 0:
            raise ValueError(f"{self.path} needs a positive body cap")


@dataclass(frozen=True, slots=True)
class RouterMount:
    """A router, the gate it gets (``slot``) and where it is included (``at``,
    the slot's own point when omitted). Only the admin slot mounts at the admin
    point, and nothing else does: the admin router applies its own floor.

    ``signed_bodies`` names the router's paths that take a signed body larger
    than the edge allows; each must be one of the router's own paths."""

    router: APIRouter
    slot: RouterSlot
    at: MountPoint | None = None
    signed_bodies: tuple[SignedBody, ...] = ()

    def __post_init__(self) -> None:
        if (self.slot is RouterSlot.ADMIN) != (self.point is MountPoint.ADMIN):
            raise ValueError(
                f"a {self.slot.value} router cannot mount at the {self.point.value} point"
            )
        served = {getattr(route, "path", "") for route in self.router.routes}
        for body in self.signed_bodies:
            if body.path not in served:
                raise ValueError(f"{body.path} is not a path this router serves")

    @property
    def point(self) -> MountPoint:
        return self.at if self.at is not None else DEFAULT_POINT[self.slot]


#: A context the server enters at startup and leaves at shutdown, given the app.
Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]

#: Routers an extension serves, in registration order within each slot.
HTTP_ROUTERS: ExtensionPoint[RouterMount] = ExtensionPoint("http_routers")

#: The parts of an extension's requests it validates itself, so the boundary
#: scan defers to it there (a public beacon that strips what it records).
SELF_VALIDATED: ExtensionPoint[SelfValidated] = ExtensionPoint("self_validated_surfaces")

#: What an extension keeps running for as long as the server does. Entered in
#: registration order after the realtime runtime starts, left in reverse before
#: it stops.
LIFESPANS: ExtensionPoint[Lifespan] = ExtensionPoint("lifespans")


def routers_for(slot: RouterSlot) -> tuple[APIRouter, ...]:
    """The registered routers that mount in ``slot``."""
    return tuple(mount.router for mount in HTTP_ROUTERS.items() if mount.slot is slot)


def mounts_at(point: MountPoint) -> tuple[RouterMount, ...]:
    """The registered routers included at ``point``, in registration order."""
    return tuple(mount for mount in HTTP_ROUTERS.items() if mount.point is point)


def signed_bodies() -> tuple[SignedBody, ...]:
    """Every signed body a registered router declares."""
    return tuple(body for mount in HTTP_ROUTERS.items() for body in mount.signed_bodies)
