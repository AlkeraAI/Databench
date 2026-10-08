"""What a box may do on its own machine credential: claim and heartbeat the
one machine the credential names, and speak as that machine at all.

A machine credential is a service principal with a very small reach. It is
not a member of any org (the pool box serves every org and belongs to none),
so nothing about roles or email verification applies; what decides is whether
the credential is live and whether the machine it is being used for is the
machine it was minted for. A credential that was claimed by one box and is
presented for another is refused as not-found: the id of a machine is on
every chat it serves, and a box that learned an id must not be able to
heartbeat on somebody else's behalf. Every fact is required on every branch.

A credential that is missing, revoked, rotated away or was never minted is
refused as a CREDENTIAL (a 401) rather than as the box's reach: the box must
stop presenting it, not retry. Revoked, rotated away and never minted read the
same (``credential_live`` is false for all three, and the answer is one code
and one message), so nothing in the response says which of them it was.

An org-bound worker credential is not the machine credential: it is what the
machine credential minted for one org's process, and it may not act on the
machine's own standing (claim, heartbeat, mint, read the routing feed) any
more than a chat may. It is refused as not-found whatever the facts say.

The converse holds once a box runs a worker per org (its last heartbeat said
``org_workers``): the machine credential then acts on the machine's own
standing and nothing else. Every chat and every byte of an org is reached
through that org's worker credential, so a box whose process was swapped for
one that serves every org from one home cannot take the machine credential
back to the reach it had before. Refused with its own code, so the box's log
says what to use instead.

A worker credential is minted only for an org the machine reaches: one with
a live chat bound to it. A pool box may be placed anywhere, but what it holds
is the chats bound to it, and a stolen box must not be able to mint its way
into an org it serves nothing for. Refused as not-found.
"""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource
from alkera_core.machine_refusals import (
    MACHINE_CREDENTIAL_REFUSED,
    MACHINE_CREDENTIAL_REQUIRED,
    MACHINE_WORKER_CREDENTIAL_REQUIRED,
)

POLICY = "compute.machine_credential"
AUDITED = frozenset(
    {
        "is_machine",
        "credential_live",
        "machine_matches",
        "own_standing",
        "runs_org_workers",
        "org_reached",
    }
)

MACHINE_MESSAGE = "A machine credential is required"
REFUSED_MESSAGE = "Machine credential refused"
NOT_FOUND_MESSAGE = "Machine not found"
WORKER_REQUIRED_MESSAGE = (
    "This machine runs a worker per organization; reach an organization's chats "
    "and files on that organization's worker credential"
)

#: WRITE is the box acting on its own standing: the claim, the heartbeat, a
#: worker credential minted for one org, and any request on which it asserts
#: it is the machine. READ is the box reading what that standing covers: the
#: routing feed of the chats bound to its machine.
SUPPORTED = frozenset({Action.READ, Action.WRITE})


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    is_machine = require_attr(attrs, "is_machine", bool)
    live = require_attr(attrs, "credential_live", bool)
    matches = require_attr(attrs, "machine_matches", bool)
    # The request is one the machine makes on its own standing: the claim,
    # the heartbeat, a worker credential's mint, the routing feed.
    own_standing = require_attr(attrs, "own_standing", bool)
    # The machine's last heartbeat said it runs a worker per org.
    runs_org_workers = require_attr(attrs, "runs_org_workers", bool)
    # The org the request concerns is one the machine reaches: for a worker
    # credential's mint, an org with a live chat bound to the machine.
    org_reached = require_attr(attrs, "org_reached", bool)
    if not is_machine:
        return deny(
            POLICY,
            "machine_credential_required",
            message=MACHINE_MESSAGE,
            error_code=MACHINE_CREDENTIAL_REQUIRED,
            unauthenticated=True,
        )
    if not live:
        return deny(
            POLICY,
            "credential_refused",
            message=REFUSED_MESSAGE,
            error_code=MACHINE_CREDENTIAL_REFUSED,
            unauthenticated=True,
        )
    if ctx.is_machine_worker:
        return deny(POLICY, "org_bound_credential", message=NOT_FOUND_MESSAGE, as_not_found=True)
    if not matches:
        return deny(POLICY, "not_this_machine", message=NOT_FOUND_MESSAGE, as_not_found=True)
    if runs_org_workers and not own_standing:
        return deny(
            POLICY,
            "worker_credential_required",
            message=WORKER_REQUIRED_MESSAGE,
            error_code=MACHINE_WORKER_CREDENTIAL_REQUIRED,
        )
    if not org_reached:
        return deny(POLICY, "org_not_reached", message=NOT_FOUND_MESSAGE, as_not_found=True)
    if not own_standing:
        return allow(POLICY, "machine_serves_in_one_process")
    return allow(POLICY, "machine_self")


register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.MACHINE_CREDENTIAL,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = [
    "AUDITED",
    "MACHINE_MESSAGE",
    "NOT_FOUND_MESSAGE",
    "POLICY",
    "REFUSED_MESSAGE",
    "SUPPORTED",
    "WORKER_REQUIRED_MESSAGE",
    "decide",
]
