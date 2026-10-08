"""Who is acting, and on whose behalf.

A request is never just "a user". It may be a user's own session, an agent
running inside a user's chat session, a CI or proxy token that represents no
human at all, a personal access token standing in for its owner, or a chat box
speaking on its own machine credential. Every one of those resolves to one
:class:`ActingContext`: the principal that performed the call, the user (if
any) whose permissions apply, and the full delegation chain between them.
Policies read the context; audit rows persist it (through
:class:`~alkera_core.authz.actor_chain.ActorChainRecord`).

The invariants are enforced at construction and raise ``ValueError`` — a
context that cannot be built cannot be authorized, which is the fail-closed
default this module exists to provide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from alkera_core.authz.actor_chain import ActorChainRecord
from alkera_core.authz.enums import CredentialKind, PrincipalKind


@dataclass(frozen=True, slots=True)
class Principal:
    """One link in a delegation chain.

    ``id`` is ``str(uuid)`` for users, tokens and personal access tokens, and
    the chat session id for agents. ``label`` is the human handle an audit
    reader wants next to the id: an email, a token label, a session id.
    ``credential`` names how this principal was authenticated — ``None`` when
    the principal presented nothing itself (the owner behind a personal access
    token, say). ``credential_id`` is the id of the very credential presented
    (a session token's ``jti``) when it has one: two sessions of one user are
    two credentials, and a box is verified against the one that registered it.
    """

    kind: PrincipalKind
    id: str
    org_id: UUID
    label: str = ""
    credential: CredentialKind | None = None
    credential_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PrincipalKind):
            raise ValueError(f"principal kind must be a PrincipalKind, got {self.kind!r}")
        if not self.id:
            raise ValueError("a principal needs a non-empty id")
        if self.kind is PrincipalKind.USER:
            try:
                UUID(self.id)
            except ValueError as exc:
                raise ValueError(f"a user principal's id must be a UUID, got {self.id!r}") from exc


@dataclass(frozen=True, slots=True)
class ActingContext:
    """The resolved actor of one request.

    * ``acting_principal`` — the link that made the call.
    * ``delegating_user`` — the user whose permissions apply when the acting
      principal is not itself a user (an agent in their session, their personal
      access token). ``None`` when a user acts directly or when no human is
      involved at all (a CI or proxy token).
    * ``delegation_chain`` — every link from the delegating user (when there is
      one) down to the acting principal. Defaults to ``(acting_principal,)``.
    * ``served_org_ids`` / ``serves_every_org`` — the orgs OTHER than its own a
      machine may act within: the one org a dedicated box is assigned to, or
      every org for a pool box. Empty and ``False`` for anything that is not a
      machine; a machine never delegates and never inherits a user's rights, so
      what it reaches inside a served org is decided by each policy's own
      "the machine holds this resource" branch, never by these sets alone.
    * ``personal_owner_id`` — for a person's own box, the one person whose
      chats it may hold. ``None`` for everything else.

    Invariants (violations raise ``ValueError``): the chain ends with the acting
    principal; every link is in the same org; a user acting principal has no
    delegating user and a single-link chain; a chain never contains a user it
    does not name as the delegating user; only a user can delegate, and the
    delegating user is the chain's first link; only a machine serves an org
    that is not its own, and a machine has no delegating user.
    """

    acting_principal: Principal
    delegating_user: Principal | None = None
    delegation_chain: tuple[Principal, ...] = ()
    served_org_ids: frozenset[UUID] = frozenset()
    serves_every_org: bool = False
    personal_owner_id: UUID | None = None

    def __post_init__(self) -> None:
        acting = self.acting_principal
        chain = tuple(self.delegation_chain) or (acting,)
        object.__setattr__(self, "delegation_chain", chain)
        object.__setattr__(self, "served_org_ids", frozenset(self.served_org_ids))
        if acting.kind is not PrincipalKind.MACHINE and (
            self.served_org_ids or self.serves_every_org
        ):
            raise ValueError("only a machine serves an org other than its own")
        if acting.kind is PrincipalKind.MACHINE and self.delegating_user is not None:
            raise ValueError("a machine acts on its own credential and never for a user")
        if self.personal_owner_id is not None and (
            acting.kind is not PrincipalKind.MACHINE or self.served_org_ids or self.serves_every_org
        ):
            raise ValueError("only a machine bound to its one org is a personal box")
        if acting.credential is CredentialKind.MACHINE_WORKER and (
            acting.kind is not PrincipalKind.MACHINE or self.served_org_ids or self.serves_every_org
        ):
            raise ValueError("a machine worker is a machine bound to its one org")
        if chain[-1] != acting:
            raise ValueError("the delegation chain must end with the acting principal")
        if any(link.org_id != acting.org_id for link in chain):
            raise ValueError("every link in a delegation chain must belong to the same org")
        users = [link for link in chain if link.kind is PrincipalKind.USER]
        delegating = self.delegating_user
        if acting.kind is PrincipalKind.USER:
            if delegating is not None:
                raise ValueError("a user acts for themselves and has no delegating user")
            if len(chain) != 1:
                raise ValueError("a user acting principal has a single-link chain")
            return
        if delegating is None:
            if users:
                raise ValueError(
                    "a chain that contains a user must name that user as the delegating user"
                )
            return
        if delegating.kind is not PrincipalKind.USER:
            raise ValueError("only a user can delegate")
        if chain[0] != delegating:
            raise ValueError("the delegation chain must start with the delegating user")
        if users != [delegating]:
            raise ValueError("a delegation chain carries exactly one user: the delegating user")

    # ------------------------------------------------------------------
    # Derived views
    # ------------------------------------------------------------------

    @property
    def org_id(self) -> UUID:
        return self.acting_principal.org_id

    @property
    def subject(self) -> Principal:
        """The principal whose roles apply: the delegating user when there is
        one, otherwise the acting principal itself."""
        return self.delegating_user or self.acting_principal

    @property
    def effective_user_id(self) -> UUID | None:
        """The user this request counts as, or ``None`` when no human is
        behind it."""
        subject = self.subject
        return UUID(subject.id) if subject.kind is PrincipalKind.USER else None

    @property
    def is_agent(self) -> bool:
        return self.acting_principal.kind is PrincipalKind.AGENT

    @property
    def is_machine(self) -> bool:
        """A box on its own machine credential — no user behind it."""
        return self.acting_principal.kind is PrincipalKind.MACHINE

    @property
    def is_machine_worker(self) -> bool:
        """A box's process for ONE org, on the org-bound credential its
        machine credential minted: a machine whose only org is that one."""
        return self.is_machine and self.acting_principal.credential is CredentialKind.MACHINE_WORKER

    def serves(self, org_id: UUID) -> bool:
        """Whether this context may act within ``org_id`` at all: its own org
        for everyone, plus the orgs a machine is assigned to or the whole
        platform for a pool box. The engine's tenancy floor asks this; it is
        the ceiling of a machine's reach, not a grant of anything inside it."""
        if org_id == self.org_id:
            return True
        return self.is_machine and (self.serves_every_org or org_id in self.served_org_ids)

    @property
    def credential_id(self) -> str | None:
        """The id of the session credential behind this request — the
        delegating user's token when an agent acts in their session, the
        user's own otherwise; ``None`` when the credential has no id."""
        return self.subject.credential_id

    def audit_dict(self) -> dict[str, Any]:
        """The persisted actor document (JSON-ready)."""
        return ActorChainRecord.from_context(self).model_dump(mode="json")

    # ------------------------------------------------------------------
    # Constructors — one per credential shape
    # ------------------------------------------------------------------

    @classmethod
    def for_user(
        cls,
        *,
        user_id: UUID,
        org_id: UUID,
        email: str,
        credential: CredentialKind = CredentialKind.JWT,
        credential_id: str | None = None,
    ) -> ActingContext:
        """A user acting directly on their own session."""
        user = Principal(
            PrincipalKind.USER,
            str(user_id),
            org_id,
            label=email,
            credential=credential,
            credential_id=credential_id,
        )
        return cls(acting_principal=user)

    @classmethod
    def for_agent(
        cls,
        *,
        user_id: UUID,
        org_id: UUID,
        email: str,
        session_id: str,
        credential_id: str | None = None,
    ) -> ActingContext:
        """An agent running inside a user's chat session: the user's JWT
        authenticated the request, the agent id came from request headers."""
        user = Principal(
            PrincipalKind.USER,
            str(user_id),
            org_id,
            label=email,
            credential=CredentialKind.JWT,
            credential_id=credential_id,
        )
        agent = Principal(
            PrincipalKind.AGENT,
            session_id,
            org_id,
            label=session_id,
            credential=CredentialKind.AGENT_HEADER,
        )
        return cls(acting_principal=agent, delegating_user=user, delegation_chain=(user, agent))

    @classmethod
    def for_service(
        cls,
        *,
        token_id: UUID,
        org_id: UUID,
        label: str,
        credential: CredentialKind,
    ) -> ActingContext:
        """A userless credential (CI token, proxy token) acting for its org."""
        service = Principal(
            PrincipalKind.SERVICE, str(token_id), org_id, label=label, credential=credential
        )
        return cls(acting_principal=service)

    @classmethod
    def for_pat(
        cls,
        *,
        token_id: UUID,
        org_id: UUID,
        label: str,
        user_id: UUID,
        email: str,
    ) -> ActingContext:
        """A personal access token standing in for its owner. The owner
        presented no credential of their own, so their link carries none."""
        user = Principal(PrincipalKind.USER, str(user_id), org_id, label=email, credential=None)
        pat = Principal(
            PrincipalKind.PAT, str(token_id), org_id, label=label, credential=CredentialKind.PAT
        )
        return cls(acting_principal=pat, delegating_user=user, delegation_chain=(user, pat))

    @classmethod
    def for_machine(
        cls,
        *,
        machine_id: UUID | None,
        credential_id: UUID,
        org_id: UUID,
        label: str,
        served_org_ids: frozenset[UUID] = frozenset(),
        serves_every_org: bool = False,
        personal_owner_id: UUID | None = None,
    ) -> ActingContext:
        """A chat box speaking on its own machine credential.

        The principal's id is the machine the credential holds — the id every
        chat it serves carries, so "the machine holds this chat" is one string
        comparison in a policy — and, before the box has claimed one, the
        credential's own id. ``credential_id`` is always the credential's, so
        the claim route can find the very row that authenticated the request.
        ``org_id`` is the operator org the credential was minted in; a served
        org is never the machine's own.
        """
        machine = Principal(
            PrincipalKind.MACHINE,
            str(machine_id or credential_id),
            org_id,
            label=label,
            credential=CredentialKind.MACHINE,
            credential_id=str(credential_id),
        )
        return cls(
            acting_principal=machine,
            served_org_ids=served_org_ids,
            serves_every_org=serves_every_org,
            personal_owner_id=personal_owner_id,
        )

    @classmethod
    def for_machine_worker(
        cls,
        *,
        machine_id: UUID,
        credential_id: UUID,
        org_id: UUID,
        label: str,
        personal_owner_id: UUID | None = None,
    ) -> ActingContext:
        """A box's process for one org, speaking on the org-bound credential
        its machine credential minted for ``org_id``.

        The principal is the machine (so "the machine holds this chat" reads
        exactly as it does for the box itself) and its org is the bound org,
        with no other org served: every tenancy floor that asks
        :meth:`serves` answers for that org alone, whatever the machine
        credential behind it may serve. ``credential_id`` is the machine
        credential's, so the credential's standing decides the worker's too.
        """
        machine = Principal(
            PrincipalKind.MACHINE,
            str(machine_id),
            org_id,
            label=label,
            credential=CredentialKind.MACHINE_WORKER,
            credential_id=str(credential_id),
        )
        return cls(acting_principal=machine, personal_owner_id=personal_owner_id)


__all__ = ["ActingContext", "Principal"]
