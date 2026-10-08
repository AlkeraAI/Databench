"""The one provider contract the compute plane speaks, and its registry.

``ComputeProvider`` is the single interface every provider implements in full:
RunPod (``runpod.py``), EC2 (``ec2.py``), the local developer box
(``localdev.py``) and the container placeholder (``container.py``). The meter,
the reconcile, the admin console, placement and provisioning all reach a
provider through :func:`provider_for` / :func:`provider_for_kind` and never
branch on the kind. (``nodes.py`` keeps the ``node_provider`` names as aliases
of this protocol and registry.)

The contract has two faces, both implemented by every configured provider:

- the **node lifecycle** the admin console drives: ``run`` a box from a
  rendered bootstrap, keep its credential (``store``/``bind``/``delete``),
  ``describe`` / ``find`` / ``stop`` / ``start`` / ``terminate`` it, and answer
  live ``availability`` for a size;
- the **pod view** the meter and the reconcile read: ``create_pod``,
  ``pod_status`` and ``list_pods`` to see what we are paying for,
  ``terminate_pod``, ``normalize_status``, and the priced ``catalog_*`` feed.

No provider word leaks past this module: each provider reads its own
parameters from the machine type's ``provider_config`` bag and maps its status
vocabulary onto :data:`PodPhase`.

A provider module calls :func:`register_provider` at import with a factory
that builds it from settings, so the plane learns a new provider by one
registration and one catalog row. :data:`COMPUTE_PROVIDER_KINDS` is the
vocabulary the ``compute_machine_types.provider`` CHECK constraint is written
over, so a registration without the matching migration is caught by a test.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from alkera_core.compute.availability import Availability, SizeQuery

if TYPE_CHECKING:
    from alkera_core.config import Settings
    from alkera_core.models.compute import ComputeMachineType

PodPhase = Literal["starting", "running", "stopped", "gone", "unknown"]
"""The provider-agnostic liveness of a pod.

``starting`` — created, not yet serving; ``running`` — up (SSH-connectable
when the status also carries an address); ``stopped`` — stopped on purpose
with its disk kept, so a ``start`` brings it back (a slept machine);
``gone`` — the provider no longer has it (terminated, failed, or unknown to
it), so it will never come back; ``unknown`` — a status the provider adapter
could not classify (treated as neither ready nor gone: the meter waits for
the next tick).

``stopped`` and ``gone`` are kept apart on purpose: a stopped machine is one
an org still holds, and reading it as gone would release it and its disk."""

GONE: PodPhase = "gone"
RUNNING: PodPhase = "running"
STARTING: PodPhase = "starting"
STOPPED: PodPhase = "stopped"
UNKNOWN: PodPhase = "unknown"

FailureKind = Literal["capacity", "quota", "invalid", "auth", "transient", "unknown"]
"""Why a provider refused or failed a call, in words every caller shares.

``capacity`` — no hardware of that shape free right now (try elsewhere, or
later); ``quota`` — the account's own limit; ``invalid`` — the request itself
is wrong and asking again changes nothing; ``auth`` — the provider does not
accept our credential; ``transient`` — throttled, timed out or a server error
that a retry can get past; ``unknown`` — nothing above could be read off the
answer. Each provider maps its own answers onto these once, in its module; a
caller (the retry policy, the org-machine reconcile, a route) reasons only
about the kind."""

CAPACITY_FAILURE: FailureKind = "capacity"
QUOTA_FAILURE: FailureKind = "quota"
INVALID_FAILURE: FailureKind = "invalid"
AUTH_FAILURE: FailureKind = "auth"
TRANSIENT_FAILURE: FailureKind = "transient"
UNKNOWN_FAILURE: FailureKind = "unknown"

FAILURE_KINDS: tuple[FailureKind, ...] = (
    CAPACITY_FAILURE,
    QUOTA_FAILURE,
    INVALID_FAILURE,
    AUTH_FAILURE,
    TRANSIENT_FAILURE,
    UNKNOWN_FAILURE,
)
"""Every failure kind, the vocabulary ``compute_allocations.failure_kind`` holds."""


RUNPOD = "runpod"
"""The GPU/CPU pod provider that provisions today's machines."""

CONTAINER = "container"
"""The platform container sandbox a web chat is placed on (see ``container.py``)."""

EC2 = "ec2"
"""An EC2 instance a platform admin provisioned by hand and registered with a
machine credential (see ``ec2.py``): the plane never creates one."""

LOCALDEV = "localdev"
"""A box run as a privileged Docker container on the developer's own machine,
with every chat under gVisor (see ``localdev.py``). Refuses outside a local
deployment."""

PERSONAL = "personal"
"""A person's own box, registered through the device flow (see
``personal.py``): the plane never creates, powers or prices one."""

SELF_HOSTED = "self_hosted"
"""A host a deployment's operator runs that registers itself as its org's
machine through the device flow (the one-machine Docker Compose box; see
``self_hosted.py``): the plane never creates, powers or prices one.
"""

SSH = "ssh"
"""A Linux host an org admin attaches by its SSH details (see ``compute/ssh``):
the plane installs and runs the node daemon on it, never buys or powers it."""

COMPUTE_PROVIDER_KINDS: tuple[str, ...] = (
    CONTAINER,
    EC2,
    LOCALDEV,
    PERSONAL,
    RUNPOD,
    SELF_HOSTED,
    SSH,
)
"""Every provider kind the catalog may name, sorted.

The ``ck_compute_machine_types_provider`` CHECK constraint is written over
exactly this tuple, so it is the one place the vocabulary is spelled: a new
provider extends it AND ships the migration that widens the constraint."""


class ComputeProviderError(Exception):
    """The provider refused or failed a call (routes map this to 502).

    ``kind`` says why, in the shared :data:`FailureKind` words; the provider
    that raised it classified its own answer. ``unknown`` when it could not."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        kind: FailureKind = UNKNOWN_FAILURE,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.kind: FailureKind = kind if kind in FAILURE_KINDS else UNKNOWN_FAILURE


class ComputeProviderUnavailableError(ComputeProviderError):
    """The provider is registered but this deployment cannot act through it.

    Distinct from a transient failure: a provider that answers this will answer
    it every time until the deployment changes, so a caller that catches it is
    telling a reader "not here", not "try again"."""


class UnknownComputeProviderError(ComputeProviderError):
    """A machine type names a provider kind nothing registered."""

    def __init__(self, kind: str, *, known: tuple[str, ...]) -> None:
        #: The provider kind nothing registered (``kind`` is the failure kind).
        self.provider_kind = kind
        self.known = known
        super().__init__(
            f"no compute provider is registered for {kind!r} "
            f"(registered: {', '.join(known) or 'none'})",
            kind=INVALID_FAILURE,
        )


@dataclass
class PodStatus:
    """A provider pod's liveness snapshot, already normalized by its provider."""

    pod_id: str
    phase: PodPhase = UNKNOWN
    public_ip: str = ""
    ssh_port: int = 0
    #: The provider's own status word, for logs and audits only.
    raw_status: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ssh_ready(self) -> bool:
        """Running with a public IP and a mapped SSH port — connectable over SSH."""
        return self.phase == RUNNING and bool(self.public_ip) and self.ssh_port > 0

    @property
    def stopped(self) -> bool:
        """Stopped with its disk kept; a ``start`` brings it back."""
        return self.phase == STOPPED

    @property
    def gone(self) -> bool:
        """Not serving and not coming back on its own: terminated, or stopped.

        This is the meter's question about a pod it bills, and a session pod
        has no wake: one that stopped (its main process exited) is as finished
        as one terminated. The node reconcile, which can tell a slept machine
        from a released one, reads :attr:`phase` instead."""
        return self.phase in (GONE, STOPPED)


@dataclass(frozen=True)
class ProviderPod:
    """One pod as the PROVIDER lists it — the other side of the reconciliation.

    ``name`` is what the plane asked the provider to call it, which is how a pod
    is matched back to the allocation that paid for it when the id never made it
    into the database. ``created_at`` is the provider's own creation stamp and
    is what a grace period is measured against; ``None`` means the provider did
    not say, and a pod whose age is unknown is never reaped.
    """

    pod_id: str
    name: str = ""
    created_at: datetime | None = None
    phase: PodPhase = UNKNOWN
    raw_status: str = ""


@dataclass(frozen=True)
class NodeLaunch:
    """Everything a provider needs to start one long-lived workspace node."""

    allocation_id: UUID
    #: What the provider is asked to call the machine (the pod name / Name tag).
    name: str
    #: The provider's own type id (``m6i.xlarge``, a RunPod CPU flavor).
    type_code: str
    storage_gb: int
    #: The rendered bootstrap script (``bootstrap.render_bootstrap``).
    script: str
    #: The node's secrets (``ALKERA_MACHINE_CREDENTIAL``, ``ALKERA_BOX_TOKEN``,
    #: ``ALKERA_BOX_TOKEN_EXPIRES``). RunPod passes them as pod env; EC2 ignores
    #: them here — they went to Secrets Manager first.
    secrets: dict[str, str] = field(default_factory=dict)
    #: ``alkera:*`` tags / labels the machine carries.
    tags: dict[str, str] = field(default_factory=dict)
    vcpu: int = 0
    compute_class: str = "cpu"
    #: How many GPUs the node is started with (the machine type's
    #: ``gpu_count``); a GPU node never asks for fewer than one.
    gpu_count: int = 0
    #: Provider-specific overrides for THIS attempt, from the machine type's
    #: ``provider_config["fallbacks"]`` (another data center, an alternative GPU
    #: id of the same class). Empty for the first choice. Each provider admits
    #: only the keys it knows how to place, and refuses any other key as
    #: ``invalid`` rather than letting a catalog row reach the node's secrets.
    overrides: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NodeDescription:
    """A node as its provider sees it now."""

    machine_id: str
    phase: PodPhase = UNKNOWN
    raw_status: str = ""
    public_ip: str = ""
    #: What the provider wants on record about how the machine ended, for a
    #: person to read (a release it gave up on, and what is left to do by
    #: hand). Empty for an ordinary answer.
    note: str = ""


@runtime_checkable
class OrphanStorage(Protocol):
    """A provider whose machines' disks are objects of their own (a RunPod CPU
    pod's network volume) and can outlive the machine they were made for."""

    async def release_orphan_storage(
        self, *, ours: Callable[[str], bool], owned: Collection[str]
    ) -> list[str]:
        """Delete every disk this deployment named (``ours`` says which names
        are its) that no name in ``owned`` claims; return the ids deleted."""
        ...


class ComputeProvider(Protocol):
    """The one contract every compute provider implements in full.

    Two faces on one interface (see the module docstring): the node lifecycle
    the admin console + provisioning + reconcile drive, and the pod view the
    meter + reconcile + catalog refresh read. A configured real provider (RunPod,
    EC2) implements every method for real; the unconfigured container placeholder
    fails closed with an actionable :class:`ComputeProviderUnavailableError`. No
    method raises "unsupported" by design on a provider that CAN act."""

    #: The provider kind this instance registers under (``runpod`` / ``ec2`` / …).
    kind: str

    def configured(self) -> bool:
        """Whether THIS deployment holds what the provider needs to act — the
        machine-type listing reports it as ``available``."""
        ...

    # -- provisioning ---------------------------------------------------------

    async def create_pod(
        self, *, name: str, machine_type: ComputeMachineType, ssh_public_key: str
    ) -> str:
        """Provision a SESSION pod of ``machine_type`` (reading the provider's own
        parameters from ``machine_type.provider_config``); returns its id.

        ``name`` is the plane's idempotency handle: it is derived from the
        allocation id and committed BEFORE this call, so a create whose answer
        never came back is matched to its row by name rather than being bought
        twice."""
        ...

    async def run(self, launch: NodeLaunch) -> str:
        """Start a long-lived WORKSPACE node from a rendered bootstrap; returns
        the provider's machine id."""
        ...

    # -- the node's credential ------------------------------------------------

    async def store_credential(self, allocation_id: UUID, secrets: dict[str, str]) -> None:
        """Keep the node's secrets where the node can read them at boot. Called
        BEFORE :meth:`run`, so a node never boots without them. A provider whose
        secrets travel with the machine itself keeps them there instead."""
        ...

    async def delete_credential(self, allocation_id: UUID) -> None:
        """Forget the node's credential; one already gone counts as success."""
        ...

    async def bind_credential(self, allocation_id: UUID, machine_id: str) -> None:
        """Tie the stored credential to the machine that may read it, once the
        machine exists. Idempotent. A provider whose secrets travel with the
        machine has nothing to do."""
        ...

    # -- liveness -------------------------------------------------------------

    async def pod_status(self, pod_id: str) -> PodStatus:
        """The pod's current status, its ``phase`` already normalized (the
        meter's view — carries the SSH address when the pod is reachable)."""
        ...

    async def describe(self, machine_id: str) -> NodeDescription:
        """The node's phase (the reconcile's view); a machine the provider no
        longer knows is ``gone``."""
        ...

    async def find(self, allocation_id: UUID) -> NodeDescription | None:
        """The live node started for ``allocation_id``, found by what the
        provider was told to call or tag it — for a row that never learned its
        machine id (a create whose answer was lost). ``None`` when there is
        none, or when this provider is reconciled by name (its pods are listed
        and adopted through :meth:`list_pods` instead)."""
        ...

    async def list_pods(self, *, name_prefix: str = "") -> list[ProviderPod]:
        """Every pod/instance this account holds whose name starts with
        ``name_prefix``.

        The reconciler's only way to see what we are actually paying for. A
        provider that cannot enumerate raises
        :class:`ComputeProviderUnavailableError`, which the reconciler reads as
        "not here" and skips — never as "there are no pods", which would be a
        licence to terminate."""
        ...

    def normalize_status(self, raw_status: str) -> PodPhase:
        """This provider's status vocabulary onto :data:`PodPhase`."""
        ...

    # -- power / lifecycle ----------------------------------------------------

    async def stop(self, machine_id: str) -> None:
        """Stop the machine without destroying it — its disk is kept and a
        later :meth:`start` brings it back (an ``asleep`` box)."""
        ...

    async def start(self, machine_id: str) -> None:
        """Bring a stopped machine back up."""
        ...

    async def grow_volume(self, machine_id: str, size_gb: int) -> None:
        """Grow the machine's data volume to ``size_gb``. Asked only of a
        stopped machine, and only where the provider's disk rules say its
        volume grows (``alkera_core.compute.disk``); never to shrink."""
        ...

    async def terminate(self, machine_id: str) -> None:
        """Terminate; a machine that no longer exists counts as success."""
        ...

    async def terminate_pod(self, pod_id: str) -> None:
        """Terminate by pod id — the meter/reconcile spelling of
        :meth:`terminate`; a pod that no longer exists counts as success."""
        ...

    # -- catalog + availability -----------------------------------------------

    async def catalog_prices(self, sizes: list[SizeQuery] | None = None) -> dict[str, int]:
        """``{provider_type_id: nano-USD per minute per pricing unit}`` at the
        provider's on-demand price (our cost). A provider that enumerates its own
        catalog (RunPod) ignores ``sizes``; one with no fixed catalog (EC2) prices
        exactly the ``sizes`` it is asked about."""
        ...

    async def catalog_entries(
        self, sizes: list[SizeQuery] | None = None
    ) -> dict[str, dict[str, Any]]:
        """``{provider_type_id: entry}`` — the priced catalog with stock, for the
        refresh job. Every entry carries the :data:`CATALOG_ENTRY_KEYS`: the
        price, the stock word and count, and the hardware a buyer reads (GPU
        name and memory, disk; zero or empty where the provider does not say).
        ``sizes`` scopes it the same way as :meth:`catalog_prices`."""
        ...

    async def availability(self, sizes: list[SizeQuery]) -> dict[str, Availability]:
        """Whether each size can be started right now — offered, and within the
        account's quota (EC2) or the provider's stock (RunPod). Answered for
        every size in one call; a provider that cannot ask answers ``unknown``."""
        ...


CATALOG_ENTRY_KEYS: tuple[str, ...] = (
    "price_nanos",
    "availability",
    "max_count",
    "gpu_name",
    "gpu_memory_gb",
    "disk_gb",
    "memory_gb_per_vcpu",
)
"""The keys every :meth:`ComputeProvider.catalog_entries` entry carries:
``price_nanos`` (int, per minute per pricing unit), ``availability`` (the stock
word), ``max_count`` (int), ``gpu_name`` (str, ``""`` for a CPU size),
``gpu_memory_gb`` (int, per GPU), ``disk_gb`` (int, the size's own disk; ``0``
when the provider sizes disk separately), ``memory_gb_per_vcpu`` (int, for a
size sold by the vCPU, whose memory is its vCPU count times this; ``0``
otherwise)."""


ProviderFactory = Callable[["Settings"], ComputeProvider]
"""Builds a provider from settings."""


@dataclass(frozen=True, slots=True)
class NodePlacement:
    """Where a provider's node keeps its regional secrets and ships its logs.
    Empty for a provider whose secrets travel in the node's own environment."""

    region: str = ""
    log_group: str = ""


@dataclass(frozen=True, slots=True)
class ProviderTraits:
    """What a provider kind is, declared once at registration, so a caller asks
    the trait instead of comparing the kind's name.

    ``catalog_provisioned``: the platform starts one of its machines from a
    catalog size (the admin console's provision list, an org's purchase).
    ``attaches_hosts``: an org admin attaches a host they already run by its
    connection details, and the plane installs the node on it.
    ``self_registers``: the host registers itself (its daemon signs in and
    calls the register route) and becomes its org's machine there; the plane
    never launches, starts or replaces one."""

    catalog_provisioned: bool = True
    attaches_hosts: bool = False
    self_registers: bool = False
    #: How the admin console names the provider in a sentence.
    console_name: str = ""
    #: What a deployment that cannot start its machines is missing.
    unconfigured_reason: str = ""
    #: The data disk its machines can attach: the largest size in GB, and what it is.
    data_disk: tuple[int, str] = (2048, "")
    #: Where its nodes are placed, read from the provider's own settings.
    placement: Callable[[], NodePlacement] = NodePlacement


_FACTORIES: dict[str, ProviderFactory] = {}
_TRAITS: dict[str, ProviderTraits] = {}

#: Provider modules that register themselves at import. Imported lazily on the
#: first lookup rather than from this module's body, because each one imports
#: this module for the Protocol and the error types.
_BUILTIN_PROVIDER_MODULES: tuple[str, ...] = (
    "alkera_core.compute.container",
    "alkera_core.compute.localdev",
    "alkera_core.compute.personal",
    "alkera_core.compute.self_hosted",
    "alkera_core.compute.ssh.provider",
)
_builtins_loaded = False


def register_provider_module(name: str) -> None:
    """Add a provider module a distribution ships (the product's cloud
    providers) to the ones imported on the first lookup, so it registers its
    factory, traits and disk rules the way a built-in one does. Idempotent."""
    global _BUILTIN_PROVIDER_MODULES, _builtins_loaded
    if name in _BUILTIN_PROVIDER_MODULES:
        return
    _BUILTIN_PROVIDER_MODULES = (*_BUILTIN_PROVIDER_MODULES, name)
    _builtins_loaded = False


_builtins_loading = False


def _load_builtin_providers() -> None:
    """Import the built-in provider modules, so their registrations ran.

    Marked done only once every import has finished: an import that raised
    (a test that broke one on purpose, a missing optional dependency) leaves
    the rest to be imported on the next lookup, never skipped for the life of
    the process. A lookup made while a provider module is still executing (it
    imports this one) does not re-enter."""
    global _builtins_loaded, _builtins_loading
    if _builtins_loaded or _builtins_loading:
        return
    _builtins_loading = True
    try:
        for name in _BUILTIN_PROVIDER_MODULES:
            importlib.import_module(name)
    finally:
        _builtins_loading = False
    _builtins_loaded = True


def builtin_provider_modules() -> tuple[ModuleType, ...]:
    """Every built-in provider module, imported. A module already imported is
    returned as it is; one a lookup is importing right now may not have run
    its body yet, so read what it declares with a default."""
    return tuple(importlib.import_module(name) for name in _BUILTIN_PROVIDER_MODULES)


def ensure_builtin_providers() -> None:
    """Make sure every built-in provider module has registered what it
    registers at import: its factory, and anything it adds to another
    registry (the catalog refresh's adopters)."""
    _load_builtin_providers()


def register_provider(
    kind: str, factory: ProviderFactory, *, traits: ProviderTraits | None = None
) -> None:
    """Register ``factory`` as the way to build the ``kind`` provider, with
    what the kind is (``traits``; the defaults when omitted).

    Called at import by each provider module; a test registers a double the
    same way. Re-registering a kind replaces it, which is what a test needs and
    what a deployment overriding a built-in would do. A re-registration that
    names no traits keeps the ones the kind was registered with."""
    if not kind:
        raise ValueError("a provider kind cannot be empty")
    _FACTORIES[kind] = factory
    if traits is not None or kind not in _TRAITS:
        _TRAITS[kind] = traits or ProviderTraits()


def provider_traits(kind: str) -> ProviderTraits:
    """What the registered ``kind`` is. Refuses a kind nothing registered."""
    _load_builtin_providers()
    traits = _TRAITS.get(kind)
    if traits is None:
        raise UnknownComputeProviderError(kind, known=tuple(sorted(_FACTORIES)))
    return traits


def node_placement(kind: str) -> NodePlacement:
    """Where the registered ``kind``'s nodes keep their secrets and ship their logs."""
    return provider_traits(kind).placement()


def kinds_with(predicate: Callable[[ProviderTraits], bool]) -> tuple[str, ...]:
    """Every registered kind whose traits satisfy ``predicate``, sorted."""
    _load_builtin_providers()
    return tuple(sorted(kind for kind, traits in _TRAITS.items() if predicate(traits)))


def registered_kinds() -> tuple[str, ...]:
    """Every registered provider kind, sorted."""
    _load_builtin_providers()
    return tuple(sorted(_FACTORIES))


def provider_for_kind(kind: str, settings: Settings) -> ComputeProvider:
    """The provider registered for ``kind``.

    Raises :class:`UnknownComputeProviderError` — naming the kind — rather than
    falling back to a default: a machine type pointing at a provider this
    deployment does not have is a misconfiguration, and silently metering or
    terminating it through somebody else's API is worse than refusing."""
    _load_builtin_providers()
    factory = _FACTORIES.get(kind)
    if factory is None:
        raise UnknownComputeProviderError(kind, known=tuple(sorted(_FACTORIES)))
    return factory(settings)


def provider_for(machine_type: ComputeMachineType, settings: Settings) -> ComputeProvider:
    """The provider that owns ``machine_type`` — the one call sites use."""
    return provider_for_kind(machine_type.provider, settings)


__all__ = [
    "AUTH_FAILURE",
    "CAPACITY_FAILURE",
    "CATALOG_ENTRY_KEYS",
    "COMPUTE_PROVIDER_KINDS",
    "CONTAINER",
    "EC2",
    "FAILURE_KINDS",
    "GONE",
    "INVALID_FAILURE",
    "LOCALDEV",
    "PERSONAL",
    "QUOTA_FAILURE",
    "RUNNING",
    "RUNPOD",
    "SELF_HOSTED",
    "SSH",
    "STARTING",
    "STOPPED",
    "TRANSIENT_FAILURE",
    "UNKNOWN",
    "UNKNOWN_FAILURE",
    "ComputeProvider",
    "ComputeProviderError",
    "ComputeProviderUnavailableError",
    "FailureKind",
    "NodeDescription",
    "NodeLaunch",
    "NodePlacement",
    "OrphanStorage",
    "PodPhase",
    "PodStatus",
    "ProviderFactory",
    "ProviderPod",
    "ProviderTraits",
    "UnknownComputeProviderError",
    "builtin_provider_modules",
    "ensure_builtin_providers",
    "kinds_with",
    "node_placement",
    "provider_for",
    "provider_for_kind",
    "provider_traits",
    "register_provider",
    "register_provider_module",
    "registered_kinds",
]
