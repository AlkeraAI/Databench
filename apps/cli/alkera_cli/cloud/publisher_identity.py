"""Who the box speaks as, and for whom a turn runs.

The box signs in with ONE credential — the operator's device token — and
serves every member's chat. Two things keep that honest:

* **The machine identity.** The daemon registers the pod it runs on and from
  then on asserts the machine id it was given as its agent id. There are two
  ways in. A box the customer runs registers as its org's own machine
  (``POST /api/v1/machines/register``: the provider, the pod id and the catalog
  code — facts only provisioning knows, so they are passed in, never
  defaulted). A box the PLATFORM runs carries a machine credential instead and
  claims the machine that credential was minted for
  (``POST /api/v1/machines/claim``): the credential says what the box is — its
  kind, its size, and whether it serves the shared pool or one org — so the box
  says only which instance it is and how many chats it can hold, and never
  which orgs it serves. A box holding both prefers the credential: the
  platform's word on what a box is outranks the org grant its operator happens
  to have. Either way the chat row names the machine it is bound to, and the
  gateway grants the write to an agent whose id IS that machine's id
  (``alkera_core.authz.chat_writable``). A box that cannot say which pod it is
  cannot register, cannot publish, and must not pretend: :func:`require_machine_identity`
  refuses to start it with a message naming what is missing.

* **The turn's principal.** A prompt reaches the box as a relay that names the
  member who sent it. The turn runs for THAT member — the receipt of a result
  it produces names them as the delegating user and the machine as the agent
  — never the operator whose token the box happens to hold.

And the one verdict the box must never argue with: a durable op the gateway
refuses as ``forbidden`` (or ``not_found``, or a full org) is not a transient
condition. :func:`is_publishing_refusal` names the codes after which the turn
is stopped and nothing more is published.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from alkera_core.schemas.chat import Event, PartCreated, ToolCall

from alkera_cli.cloud.receipt import ReceiptPrincipal

ENV_MACHINE_PROVIDER = "ALKERA_MACHINE_PROVIDER"
ENV_MACHINE_PROVIDER_POD_ID = "ALKERA_MACHINE_PROVIDER_POD_ID"
ENV_MACHINE_TYPE_CODE = "ALKERA_MACHINE_TYPE_CODE"
#: The platform machine credential provisioning put in the box's environment
#: (the credential secret's ``MACHINE_CREDENTIAL`` key, written into the 0600
#: env file). Its presence is what makes this a platform box.
ENV_MACHINE_CREDENTIAL = "ALKERA_MACHINE_CREDENTIAL"
#: What a box whose environment names no provider registers as: a host its
#: operator runs. Every machine the platform starts names its own provider.
DEFAULT_PROVIDER = "self_hosted"

#: Refusals after which a durable op will never land from this socket: the
#: gateway has decided who this chat's publisher is, and it is not us (or the
#: chat is gone, or the org may hold no more documents). Anything else the
#: mirror retries, shrinks, or drops as a defect of the one entry.
PUBLISHING_REFUSALS = frozenset({"forbidden", "not_found", "quota_exceeded", "blocked"})
#: How many produced events' attribution the mirror remembers.
ATTRIBUTION_MEMORY = 4096


class MachineIdentityError(ValueError):
    """The box cannot say which machine it is, so it cannot register."""


@dataclass(frozen=True, slots=True)
class MachineIdentity:
    """What the box tells the backend about itself when it registers.

    Two shapes in one: without a credential it is a register body (the provider,
    the pod and the catalog code the org's grant admits); with one it is a claim
    body, and the catalog code is not asked for at all — the credential names the
    kind and the size, so a box cannot promote itself to a bigger machine by
    editing its own environment file.
    """

    name: str
    provider: str = DEFAULT_PROVIDER
    provider_pod_id: str = ""
    machine_type_code: str = ""
    #: The platform machine credential, when this is a platform box. Kept out
    #: of the repr: this dataclass is logged whole in more than one place, and
    #: a bearer secret in a log line is a leaked secret.
    credential: str = field(default="", repr=False)

    @classmethod
    def from_env(
        cls,
        *,
        name: str,
        provider: str | None = None,
        provider_pod_id: str | None = None,
        machine_type_code: str | None = None,
        credential: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> MachineIdentity:
        """An explicit value wins over the environment. Only the provider has a
        default (:data:`DEFAULT_PROVIDER`)."""
        source = os.environ if env is None else env
        return cls(
            name=name,
            provider=(provider or "").strip()
            or source.get(ENV_MACHINE_PROVIDER, "").strip()
            or DEFAULT_PROVIDER,
            provider_pod_id=(provider_pod_id or "").strip()
            or source.get(ENV_MACHINE_PROVIDER_POD_ID, "").strip(),
            machine_type_code=(machine_type_code or "").strip()
            or source.get(ENV_MACHINE_TYPE_CODE, "").strip(),
            credential=(credential or "").strip() or source.get(ENV_MACHINE_CREDENTIAL, "").strip(),
        )

    @property
    def is_platform(self) -> bool:
        """Whether this box registers by claiming a platform credential rather
        than by registering under its org's grant."""
        return bool(self.credential)

    def missing(self) -> list[str]:
        """The registration facts this identity lacks, each named the way the
        operator sets it: the env var and the flag."""
        gaps: list[str] = []
        if not self.provider_pod_id:
            gaps.append(f"{ENV_MACHINE_PROVIDER_POD_ID} (--provider-pod-id)")
        if not self.machine_type_code and not self.is_platform:
            gaps.append(f"{ENV_MACHINE_TYPE_CODE} (--machine-type-code)")
        return gaps


def require_machine_identity(identity: MachineIdentity) -> MachineIdentity:
    """``identity`` when it can register; :class:`MachineIdentityError`
    otherwise, with the message the operator needs."""
    gaps = identity.missing()
    if gaps:
        raise MachineIdentityError(
            "The cloud mirror cannot register this machine: "
            + ", ".join(gaps)
            + " not set. Set them in the box's environment; a box that "
            "cannot register must not serve chats it could never publish."
        )
    return identity


def is_publishing_refusal(code: str) -> bool:
    """Whether a durable op's refusal means this socket may not publish the
    chat at all — as opposed to a defect of the one entry, or a condition the
    mirror waits out."""
    return code in PUBLISHING_REFUSALS


@dataclass(frozen=True, slots=True)
class PublishingRefusal:
    """Why the mirror stopped publishing a chat, as the gateway put it."""

    code: str
    message: str

    @property
    def reason(self) -> str:
        return f"{self.code}: {self.message}" if self.message else self.code


def relaying_user_of(relay: Mapping[str, Any]) -> str | None:
    """The member a relay names as its sender: the gateway stamps every
    recorded prompt with the socket user's id, and a REST-posted message with
    the poster's."""
    user_id = relay.get("user_id")
    return user_id if isinstance(user_id, str) and user_id else None


class TurnAttribution:
    """For whom the current turn runs, and which produced event belongs to
    which member's turn.

    ``begin`` is called with the relaying user when a prompt starts a turn;
    every event the harness produces afterwards is ``observe``d and remembered
    under that user until the next prompt. ``user_for`` answers a promote
    later: the result was produced for the member who asked, whoever presses
    the button, and whoever's token the box holds. An event nobody's turn
    produced (one fed outside a turn, one older than the memory) falls back
    to the box's own user — the operator — which is what the receipt named
    before a relay carried a sender at all.
    """

    def __init__(self, *, fallback_user_id: str, memory: int = ATTRIBUTION_MEMORY) -> None:
        self._fallback = fallback_user_id
        self._current: str | None = None
        self._by_event: OrderedDict[str, str] = OrderedDict()
        self._memory = memory

    @property
    def current(self) -> str:
        """The user the running (or last) turn runs for."""
        return self._current or self._fallback

    def begin(self, user_id: str | None) -> None:
        self._current = user_id or None

    def attribute(self, event_id: str, user_id: str | None) -> None:
        """Remember that ``event_id`` was produced for ``user_id``."""
        self._by_event[event_id] = user_id or self._fallback
        while len(self._by_event) > self._memory:
            self._by_event.popitem(last=False)

    def observe(self, event: Event) -> None:
        """A harness event of the current turn: the ones a promote can name
        (a tool call and its result part) are remembered under the turn's user."""
        if isinstance(event, ToolCall | PartCreated):
            self.attribute(str(event.event_id), self._current)

    def user_for(self, event_id: str) -> str:
        return self._by_event.get(event_id) or self._fallback


def receipt_principal(*, user_id: str, agent_id: str) -> ReceiptPrincipal:
    """The chain a receipt carries: the member the query ran for, then the
    machine (or, before it registered, the chat) acting for them."""
    return ReceiptPrincipal(
        user_id=user_id,
        agent_id=agent_id,
        chain=[link for link in (user_id, agent_id) if link],
    )


__all__ = [
    "ATTRIBUTION_MEMORY",
    "DEFAULT_PROVIDER",
    "ENV_MACHINE_CREDENTIAL",
    "ENV_MACHINE_PROVIDER",
    "ENV_MACHINE_PROVIDER_POD_ID",
    "ENV_MACHINE_TYPE_CODE",
    "PUBLISHING_REFUSALS",
    "MachineIdentity",
    "MachineIdentityError",
    "PublishingRefusal",
    "TurnAttribution",
    "is_publishing_refusal",
    "receipt_principal",
    "relaying_user_of",
    "require_machine_identity",
]
