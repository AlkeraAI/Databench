"""The fencing context a test writes under, built the way a route builds it.

A route never hands the library the (epoch, instance) pair on its own:
``items._fence`` and ``content.fence`` both carry the identity the server
derived for the request (:func:`~alkera_core.files.leases.holder_identity`),
because the pair says WHICH lease a write claims and only that identity says
the claim is the caller's. A test that hand-built the pair was therefore refused for a reason it
never meant — every case came back ``files.lease_fenced``, the ones that were
supposed to land included — so the epoch it varied proved nothing either way.

One spelling here rather than in each suite, so a case cannot drift back to the
pair and pass for the wrong reason.
"""

from __future__ import annotations

from alkera_core.authz.principal import ActingContext
from alkera_core.files.leases import LeaseContext, holder_identity


def held_by(
    ctx: ActingContext,
    epoch: int | None,
    instance_id: str | None,
    *,
    verified_machine_id: str | None = None,
    final: bool = False,
    can_read_target: bool = True,
    can_read_holder: bool = True,
) -> LeaseContext:
    """``ctx``'s claim on ``epoch``/``instance_id``, with ``ctx`` behind it.

    ``epoch`` and ``instance_id`` are whatever the case is about — the holder's
    own, a superseded one, another instance's — and the holder is always the
    caller as the SERVER resolves them, which is what a request cannot lie
    about. ``verified_machine_id`` is the box case: a caller acting as an agent
    is somebody only once its assertion has been checked, so a test that means
    "a proven box" says which machine was proven.
    """
    return LeaseContext(
        epoch=epoch,
        instance_id=instance_id,
        holder=holder_identity(ctx, verified_machine_id=verified_machine_id),
        final=final,
        can_read_target=can_read_target,
        can_read_holder=can_read_holder,
    )


__all__ = ["held_by"]
