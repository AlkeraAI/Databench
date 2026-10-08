"""Account lifecycle shapes: the deletion plan, the erasure certificate, and the
HTTP bodies of the export and deletion routes.

``DeletionPlan`` and ``ErasureCertificate`` are persisted (JSONB on
``account_deletion_requests``) and outlive the process that wrote them, so they
are :class:`~alkera_core.versioning.VersionedModel`. Neither carries a name, an
address or a title: ids, codes and counts only, so the completed request can be
kept as the record of the erasure without keeping anything personal.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from alkera_core.versioning import VersionedModel

#: What happens to one org the person belongs to.
#:
#: * ``leave``: the org keeps going; the person's membership ends, their private
#:   items are deleted and their shared items move to ``transfer_to_user_id``.
#: * ``close``: the person is its only member; its content is erased and the
#:   root row stays only to hold the billing records.
#: * ``blocked``: the person is its last active admin and others remain.
OrgFate = Literal["leave", "close", "blocked"]

#: Why a deletion cannot go ahead yet. Each names something the person can do.
BlockerCode = Literal[
    "last_admin",
    "live_compute",
    "org_machines",
    "paid_plan",
    "unpaid_balance",
    "legal_hold",
    "platform_staff",
]


class PlanBlocker(VersionedModel):
    """One thing that must be settled before the account can be erased."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    code: BlockerCode
    org_id: uuid.UUID | None = None


class PlannedOrg(VersionedModel):
    """The fate of one org and what moves or goes with it."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    org_id: uuid.UUID
    fate: OrgFate
    #: Who receives the person's shared items when the fate is ``leave``.
    transfer_to_user_id: uuid.UUID | None = None
    #: Chats, workspaces, saved queries and knowledge items visible to others.
    shared_items: int = 0
    #: Items only the person can see: deleted with the account.
    private_items: int = 0


class DeletionPlan(VersionedModel):
    """What deleting this account does, computed from live rows.

    Shown before the person confirms, stored with the request, and computed
    again when the grace window ends; the erasure acts on the second one."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    computed_at: datetime | None = None
    orgs: list[PlannedOrg] = Field(default_factory=list)
    blockers: list[PlanBlocker] = Field(default_factory=list)
    #: Prepaid credit left on the person's own seats, forfeited at erasure.
    forfeited_credit_nanos: int = 0

    @property
    def can_proceed(self) -> bool:
        return not self.blockers


class ResealedChain(VersionedModel):
    """One org audit chain the erasure rewrote and resealed."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    org_id: uuid.UUID
    rows_rewritten: int = 0
    old_head: str | None = None
    new_head: str | None = None


class ErasureCertificate(VersionedModel):
    """What the erasure did, by disposition, written in the erasure's own
    transaction so it can never claim a step that rolled back."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    erased_at: datetime | None = None
    #: ``"<table>.<column>:<disposition>"`` to the rows that disposition touched.
    rows: dict[str, int] = Field(default_factory=dict)
    orgs_left: list[uuid.UUID] = Field(default_factory=list)
    orgs_closed: list[uuid.UUID] = Field(default_factory=list)
    resealed: list[ResealedChain] = Field(default_factory=list)
    archives_deleted: int = 0


# --------------------------------------------------------------------------- #
# HTTP shapes (in flight only)
# --------------------------------------------------------------------------- #


class PlanBlockerRead(BaseModel):
    code: BlockerCode
    org_id: uuid.UUID | None = None
    org_name: str | None = None
    message: str


class PlannedOrgRead(BaseModel):
    org_id: uuid.UUID
    org_name: str
    fate: OrgFate
    transfer_to_name: str | None = None
    shared_items: int
    private_items: int


class DeletionPlanRead(BaseModel):
    orgs: list[PlannedOrgRead]
    blockers: list[PlanBlockerRead]
    forfeited_credit_nanos: int
    can_proceed: bool
    grace_days: int
    #: What the person proves before confirming: ``password`` (and
    #: ``mfa_code`` when ``mfa_required``) or a ``recent_sign_in``.
    reauth: Literal["password", "recent_sign_in"]
    mfa_required: bool


class DeletionRequestBody(BaseModel):
    #: The account's email, typed by the person.
    confirm_email: str = Field(min_length=1, max_length=320)
    current_password: str | None = Field(default=None, max_length=1024)
    mfa_code: str | None = Field(default=None, max_length=32)


class DeletionStatusRead(BaseModel):
    id: uuid.UUID
    status: Literal["scheduled", "cancelled", "completed"]
    source: Literal["self", "support", "restore"]
    requested_at: datetime
    purge_after: datetime
    cancelled_at: datetime | None = None
    completed_at: datetime | None = None
    blocked_reason: str | None = None


class DeletionStateRead(BaseModel):
    """The live request, or none."""

    request: DeletionStatusRead | None = None


class AdminAccountRead(BaseModel):
    """The support tool's view of one person's lifecycle requests."""

    user_id: uuid.UUID
    deleted_at: datetime | None = None
    deletion: DeletionStatusRead | None = None


class AdminDeletionBody(BaseModel):
    #: Run the erasure at once instead of after the grace window (a verified
    #: request the person made through support, already past its own grace).
    immediate: bool = False


__all__ = [
    "AdminAccountRead",
    "AdminDeletionBody",
    "BlockerCode",
    "DeletionPlan",
    "DeletionPlanRead",
    "DeletionRequestBody",
    "DeletionStateRead",
    "DeletionStatusRead",
    "ErasureCertificate",
    "OrgFate",
    "PlanBlocker",
    "PlanBlockerRead",
    "PlannedOrg",
    "PlannedOrgRead",
    "ResealedChain",
]
