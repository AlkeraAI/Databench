"""The persisted form of an acting context.

An :class:`~alkera_core.authz.principal.ActingContext` lives for one request;
its record outlives the process — it is the ``actor`` document on every event
outbox row and the ``detail.actor`` of an org audit event. Anything persisted
is a ``VersionedModel``: unknown fields from a newer writer survive an older
reader, and a shape change ships a ``SCHEMA_VERSION`` bump plus a fixture under
``packages/api-core/tests/fixtures/authz/``.

Ids and org ids are plain strings here (JSON has no UUID), and kinds and
credentials are the enum *values* so a reader that predates a new kind still
loads the row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import Field

from alkera_core.versioning import VersionedModel

if TYPE_CHECKING:
    from alkera_core.authz.principal import ActingContext, Principal

#: Recorded on every agent-acted row: the agent id was asserted by the client in
#: request headers, not verified by the server. The delegating user always was.
AGENT_ID_ASSERTED_BY_CLIENT = "client"


class PrincipalRecord(VersionedModel):
    """One link of a persisted delegation chain."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: str
    id: str
    org_id: str
    label: str = ""
    credential: str | None = None

    @classmethod
    def from_principal(cls, principal: Principal) -> PrincipalRecord:
        return cls(
            kind=principal.kind.value,
            id=principal.id,
            org_id=str(principal.org_id),
            label=principal.label,
            credential=principal.credential.value if principal.credential else None,
        )


class ActorChainRecord(VersionedModel):
    """The actor document: who acted, for whom, and through which links."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    acting: PrincipalRecord
    delegating_user: PrincipalRecord | None = None
    chain: list[PrincipalRecord] = Field(default_factory=list)
    agent_id_asserted_by: str | None = None

    @classmethod
    def from_context(cls, ctx: ActingContext) -> ActorChainRecord:
        return cls(
            acting=PrincipalRecord.from_principal(ctx.acting_principal),
            delegating_user=(
                PrincipalRecord.from_principal(ctx.delegating_user)
                if ctx.delegating_user is not None
                else None
            ),
            chain=[PrincipalRecord.from_principal(link) for link in ctx.delegation_chain],
            agent_id_asserted_by=AGENT_ID_ASSERTED_BY_CLIENT if ctx.is_agent else None,
        )


__all__ = ["AGENT_ID_ASSERTED_BY_CLIENT", "ActorChainRecord", "PrincipalRecord"]
