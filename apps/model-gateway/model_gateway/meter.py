"""What the gateway asks of whoever meters and settles its requests.

The pipeline relays provider streams; it does not decide what a request costs
or who pays. At fixed points in a request's life it asks the registered
:class:`GatewayMeter`: admit this request before any provider call, keep its
hold alive while it runs, note the route that opened, and settle exactly once
with the usage that streamed. The product registers the billing-backed meter at
composition.

The contract, and what each side guarantees on every failure path, is stated
on :class:`GatewayMeter` and its methods below.

With nothing registered, :data:`UNMETERED` answers: it admits every request and
records nothing, so a self-hosted install streams on its own provider keys with
no billing. The point admits one meter: two answering the same admission would
leave the money path to registration order.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from alkera_core.auth import SessionClaims
from alkera_core.extensions import ExtensionError, ExtensionPoint
from alkera_core.model_catalog import Usage
from alkera_core.models.model_catalog import ModelRoute


@dataclass(frozen=True, slots=True)
class MeterRequest:
    """The facts of one request, as the pipeline knows them before the provider
    call. ``routes`` is the failover order, never empty."""

    claims: SessionClaims
    request_id: str
    model_id: str
    routes: Sequence[ModelRoute]
    input_tokens: int
    max_output_tokens: int
    forwarded_meter: str | None = None


@dataclass(frozen=True, slots=True)
class MeterRefusal:
    """A request the meter will not admit. With ``body`` set the pipeline sends
    it verbatim; otherwise it wraps ``message`` in the gateway's error envelope."""

    status_code: int
    message: str
    body: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Settlement:
    """How an admitted request ended. ``route`` is one of the request's routes,
    the one whose price applies; ``failed`` is false for a client that left
    mid-stream, since what streamed is owed."""

    route: ModelRoute
    usage: Usage
    estimated: bool
    failed: bool


class MeterLease(Protocol):
    """One admitted request's hold, from admission to settlement."""

    @property
    def usage_meter(self) -> str | None:
        """The meter label forwarded upstream in proxy mode, or None."""
        ...

    async def keep_alive(self, progress: Callable[[], Settlement]) -> None:
        """Keep the hold alive until cancelled. ``progress`` returns the settlement
        the request would get if it ended now, so a meter can record what is owed
        while the request runs. Never raises for a missed stamp."""
        ...

    async def mark_streaming(self, progress: Settlement) -> None:
        """A route (``progress.route``) opened and is serving the request.
        ``progress`` is what is owed at this moment: the provider has accepted
        the prompt."""
        ...

    async def settle(self, settlement: Settlement) -> None:
        """Debit what the request used, or release the hold when it used nothing."""
        ...


class GatewayMeter(Protocol):
    """Prices, reserves, meters and settles the gateway's requests."""

    @property
    def name(self) -> str:
        """Names the meter in the startup log."""
        ...

    async def recover(self) -> None:
        """Reclaim holds a previous process left in flight. Runs before serving."""
        ...

    async def admit(self, request: MeterRequest) -> MeterLease | MeterRefusal:
        """Reserve for ``request`` and commit, or refuse it with nothing reserved."""
        ...


class _UnmeteredLease:
    """An admission that holds nothing."""

    @property
    def usage_meter(self) -> str | None:
        return None

    async def keep_alive(self, progress: Callable[[], Settlement]) -> None:
        return None

    async def mark_streaming(self, progress: Settlement) -> None:
        return None

    async def settle(self, settlement: Settlement) -> None:
        return None


class UnmeteredGateway:
    """The meter of a gateway with no billing: admits everything, records nothing."""

    name = "unmetered"

    async def recover(self) -> None:
        return None

    async def admit(self, request: MeterRequest) -> MeterLease | MeterRefusal:
        return _UnmeteredLease()


UNMETERED: Final[GatewayMeter] = UnmeteredGateway()

#: The meter the gateway asks. At most one registers.
GATEWAY_METER: ExtensionPoint[GatewayMeter] = ExtensionPoint("gateway_meter")


def gateway_meter(point: ExtensionPoint[GatewayMeter] = GATEWAY_METER) -> GatewayMeter:
    """The meter registered on ``point``, or :data:`UNMETERED`. Freezes the
    point. Callers pass nothing; a test passes a point of its own so it never
    freezes the process-wide one before a composition root installs into it."""
    registered = point.items()
    if len(registered) > 1:
        raise ExtensionError(
            f"{len(registered)} meters registered on {point.name!r}; the gateway takes exactly one"
        )
    return registered[0] if registered else UNMETERED


__all__ = [
    "GATEWAY_METER",
    "UNMETERED",
    "GatewayMeter",
    "MeterLease",
    "MeterRefusal",
    "MeterRequest",
    "Settlement",
    "UnmeteredGateway",
    "gateway_meter",
]
