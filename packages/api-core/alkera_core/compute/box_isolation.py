"""How a box keeps the orgs it serves apart, probed at startup.

A box runs each org's chats in a worker process of that org's own. How far
apart those workers are depends on what the host lets the box do, and hosts
differ: an EC2 instance is a whole kernel the box is root on, while a RunPod
pod is a container whose root cgroup holds the container's own processes,
whose capabilities leave out ``CAP_SYS_ADMIN`` and whose ``unshare`` is too old
to map an id range. So the box never assumes a mechanism. At startup it tries
each one (:class:`IsolationMechanism`) and derives its profile from what worked
(:func:`profile_for`):

``org_namespaces``
    Every mechanism in :data:`ORG_NAMESPACES_NEEDS` works. Each org's worker
    runs in a cgroup of its own delegated to it, in a private mount namespace
    where the host is read-only but for the org's root (with private ``/tmp``
    and ``/dev/shm``, and a ``/proc`` that shows no other org's process), as
    uid 0 of a user namespace mapped onto the org's own range of host ids, in
    a network namespace whose one link the box firewall polices. Several orgs
    may share the box.

``single_org``
    Anything less. The box serves one org for its whole life and refuses a
    second (the supervisor holds the first org it served; the backend places
    no second org on a box that does not name ``BoxCapability.ORG_ISOLATION``).
    What it still guarantees: the org worker runs without the machine
    credential (it gets one bound to its org, on its socketpair), every chat's
    agent runs as that chat's own non-root uid with no new privileges (the
    chat sandbox's controls, which need only root, ``setpriv`` and
    ``setfacl``), each chat gets a cgroup with limits where the host delegates
    one and the provider's own limit on the container bounds them all where
    it does not, and the org's data lives under its own root on the box's
    volume. What it does not: a namespace boundary between the worker and the
    host. That is safe only because nothing of another org is ever on the box.

What a probe found is an :class:`IsolationReport`. The box reports its profile
as a capability on its heartbeat (``BoxCapability.ORG_ISOLATION``, which only
:meth:`IsolationReport.capabilities` names, and only under ``org_namespaces``),
so a box that says nothing (an older build, or one that could not tell) is read
as ``single_org``: fail closed. The mechanisms are reported beside it for the
console.

Standard library only: the box's root process imports this.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from alkera_core.compute.box_contract import BoxCapability


class IsolationMechanism(StrEnum):
    """One thing a box can do to keep an org's worker apart, proven by doing it."""

    #: A cgroup of the org's own under the box's, with the cpu, cpuset, io,
    #: memory and pids controllers enabled for it, or systemd delegating one.
    CGROUP_DELEGATION = "cgroup_delegation"
    #: A private mount namespace (``unshare --mount``: ``CAP_SYS_ADMIN``).
    MOUNT_NAMESPACE = "mount_namespace"
    #: A user namespace mapping a range of host ids (``unshare --map-users``,
    #: util-linux 2.38 or later, and the right to create one).
    USER_NAMESPACE = "user_namespace"
    #: A network namespace with a veth link to the host (``CAP_NET_ADMIN``).
    NETWORK_NAMESPACE = "network_namespace"
    #: Systemd as the host's init, to run each worker as a hardened unit.
    SYSTEMD = "systemd"


#: The environment variable the supervisor hands each worker its box's
#: profile in; the worker reads it to decide how much of its own setup to do.
ENV_ORG_ISOLATION: Final = "ALKERA_ORG_ISOLATION"


class IsolationProfile(StrEnum):
    """What the box's mechanisms add up to (see the module docstring)."""

    ORG_NAMESPACES = "org_namespaces"
    SINGLE_ORG = "single_org"


#: The mechanisms a box needs before it may hold more than one org.
ORG_NAMESPACES_NEEDS: Final = frozenset(
    {
        IsolationMechanism.CGROUP_DELEGATION,
        IsolationMechanism.MOUNT_NAMESPACE,
        IsolationMechanism.USER_NAMESPACE,
        IsolationMechanism.NETWORK_NAMESPACE,
    }
)


def profile_for(mechanisms: Iterable[IsolationMechanism]) -> IsolationProfile:
    """The profile a box with ``mechanisms`` runs under."""
    if ORG_NAMESPACES_NEEDS <= frozenset(mechanisms):
        return IsolationProfile.ORG_NAMESPACES
    return IsolationProfile.SINGLE_ORG


def profile_from_capabilities(capabilities: Iterable[str] | None) -> IsolationProfile:
    """The profile a box's last heartbeat stated. Saying nothing is
    ``single_org``."""
    if BoxCapability.ORG_ISOLATION in set(capabilities or ()):
        return IsolationProfile.ORG_NAMESPACES
    return IsolationProfile.SINGLE_ORG


@dataclass(frozen=True, slots=True)
class IsolationReport:
    """What a probe found: the mechanisms that worked, and for each one that
    did not, why (for the box's log)."""

    mechanisms: frozenset[IsolationMechanism]
    missing: Mapping[IsolationMechanism, str] = field(default_factory=dict)

    @property
    def profile(self) -> IsolationProfile:
        return profile_for(self.mechanisms)

    @property
    def units(self) -> bool:
        """Whether each worker runs as a systemd unit: namespaced, on systemd."""
        return (
            self.profile == IsolationProfile.ORG_NAMESPACES
            and IsolationMechanism.SYSTEMD in self.mechanisms
        )

    def admits(self, org_id: str, held: Iterable[str]) -> bool:
        """Whether a worker of ``org_id`` may start beside the orgs the box
        ``held``: always under ``org_namespaces``; under ``single_org`` only
        when the box holds no other org (its data and whatever its worker
        wrote stay on the box for the box's life)."""
        if self.profile == IsolationProfile.ORG_NAMESPACES:
            return True
        return not set(held) - {org_id}

    def capabilities(self) -> tuple[BoxCapability, ...]:
        """What the heartbeat adds for the profile: the one place a box names
        ``BoxCapability.ORG_ISOLATION``."""
        if self.profile == IsolationProfile.ORG_NAMESPACES:
            return (BoxCapability.ORG_ISOLATION,)
        return ()

    def heartbeat(self) -> dict[str, object]:
        """The report as the heartbeat carries it (``MachineIsolationReport``)."""
        return {
            "profile": self.profile.value,
            "mechanisms": sorted(m.value for m in self.mechanisms),
        }


#: The most a probe can find. What it would report is what a build can report.
EVERY_MECHANISM: Final = IsolationReport(frozenset(IsolationMechanism))


def known_mechanisms(raw: Iterable[str]) -> list[IsolationMechanism]:
    """The mechanisms in ``raw`` this side knows, sorted; a newer box's
    unknown names are left out."""
    known = {item.value for item in IsolationMechanism}
    return sorted(IsolationMechanism(name) for name in set(raw) if name in known)


__all__ = [
    "ENV_ORG_ISOLATION",
    "EVERY_MECHANISM",
    "ORG_NAMESPACES_NEEDS",
    "IsolationMechanism",
    "IsolationProfile",
    "IsolationReport",
    "known_mechanisms",
    "profile_for",
    "profile_from_capabilities",
]
