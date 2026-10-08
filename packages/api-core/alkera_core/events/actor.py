"""Builders for the ``actor`` document on an outbox row.

The document IS the dump of :class:`~alkera_core.authz.actor_chain.ActorChainRecord`
— the same shape :meth:`~alkera_core.authz.principal.ActingContext.audit_dict`
produces — so a producer holding a resolved acting context passes
``ctx.audit_dict()`` and one holding only a ``User``, a ``CiToken`` or a
component name uses these. There is no second shape.

Ids only. A user's link carries an empty label: an audit reader joins on the
id, and an email is the kind of PII the event stream must never carry. A
token's label is its operator-given name; a system actor's label is the
component (``"backend"``, ``"worker:connections.probe"``, ``"stripe:invoice.paid"``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from alkera_core.authz.actor_chain import ActorChainRecord, PrincipalRecord
from alkera_core.authz.enums import CredentialKind, PrincipalKind

if TYPE_CHECKING:
    from alkera_core.models.ci_token import CiToken
    from alkera_core.models.user import User


def _document(acting: PrincipalRecord) -> dict[str, Any]:
    # A principal acting alone is its own one-link chain, exactly as
    # ``ActingContext`` records a user acting directly.
    return ActorChainRecord(acting=acting, chain=[acting]).model_dump(mode="json")


def actor_for_user(user: User, *, org_id: UUID) -> dict[str, Any]:
    """A user acting for themselves, in ``org_id``: the org of the credential
    or object the row concerns, which is the org of the stream the row is
    filed in. Required, because a person may belong to several orgs and the
    row of one must never name another."""
    return _document(
        PrincipalRecord(
            kind=PrincipalKind.USER.value,
            id=str(user.id),
            org_id=str(org_id),
            label="",
            credential=None,
        )
    )


def actor_for_ci_token(token: CiToken) -> dict[str, Any]:
    """A CI token acting for its org (no human behind it)."""
    return _document(
        PrincipalRecord(
            kind=PrincipalKind.SERVICE.value,
            id=str(token.id),
            org_id=str(token.org_team_id),
            label=token.label or "",
            credential=CredentialKind.CI_TOKEN.value,
        )
    )


def actor_system(label: str) -> dict[str, Any]:
    """A component acting on its own (a worker task, a webhook drain, the
    backend's own housekeeping). ``label`` names the component and is the id."""
    if not label or not label.strip():
        raise ValueError("a system actor needs a non-empty label")
    return _document(
        PrincipalRecord(
            kind=PrincipalKind.SERVICE.value,
            id=label,
            org_id="",
            label=label,
            credential=None,
        )
    )


__all__ = ["actor_for_ci_token", "actor_for_user", "actor_system"]
