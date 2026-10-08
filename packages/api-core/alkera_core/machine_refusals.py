"""The codes a refused machine credential is answered with.

A leaf module on purpose: the ``compute.machine_credential`` policy answers
with these codes and the daemon stops on them, and the policy sits under the
authorization package, which the auth package's own imports reach — so the
codes cannot live anywhere that imports either.
"""

from __future__ import annotations

#: The code a refused machine credential is answered with: revoked, rotated
#: away, never minted, or presented for a machine it no longer holds. One code
#: for all of them, so the answer says nothing about which it was. The status
#: is 401 on every door — the claim and heartbeat that carry the credential,
#: and every route the box reaches on its session while asserting the machine.
MACHINE_CREDENTIAL_REFUSED = "machine_credential_refused"
#: The code for a platform box's claim or heartbeat that carried no
#: credential at all (also 401).
MACHINE_CREDENTIAL_REQUIRED = "machine_credential_required"
#: The code an org-bound worker credential past its expiry is answered with
#: (401). Not fatal: the box mints a fresh one on its machine credential and
#: retries, which a revoked machine credential would refuse.
MACHINE_WORKER_CREDENTIAL_EXPIRED = "machine_worker_credential_expired"
#: The code a box that runs a worker per org is answered with when it presents
#: its machine credential anywhere but on the machine's own standing (403):
#: an org's chats and files are reached on that org's worker credential.
MACHINE_WORKER_CREDENTIAL_REQUIRED = "machine_worker_credential_required"
#: The refusals no retry can change: the box stops serving and says why.
#: The daemon keys off these; the backend answers with them.
FATAL_MACHINE_REFUSALS: frozenset[str] = frozenset(
    {MACHINE_CREDENTIAL_REFUSED, MACHINE_CREDENTIAL_REQUIRED}
)


__all__ = [
    "FATAL_MACHINE_REFUSALS",
    "MACHINE_CREDENTIAL_REFUSED",
    "MACHINE_CREDENTIAL_REQUIRED",
    "MACHINE_WORKER_CREDENTIAL_EXPIRED",
    "MACHINE_WORKER_CREDENTIAL_REQUIRED",
]
