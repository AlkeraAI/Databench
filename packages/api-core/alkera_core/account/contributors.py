"""How a private domain takes part in exporting and erasing an account.

The account lifecycle is open platform; some of what it must answer belongs to
private domains (billing: a seat's renewing plan blocks a deletion, its ledger
goes in the export, its Stripe customer is deleted with the account). Each such
domain registers an :class:`AccountContributor` into
:data:`ACCOUNT_CONTRIBUTORS` when its extension installs, and the open code
reads the point. With nothing installed the lifecycle runs on its own: no
extra blockers, no extra export documents, nothing outside the database to
erase.

A domain that owns tables also says what erasure does to the columns in them
that name a person: it registers those dispositions
(:func:`alkera_core.account.dispositions.register`) where its models are
defined, so a table and its disposition always load together. The hooks here
cover what one statement per column cannot: the domain's rows in the export,
the content a person owns (counted in the plan, handed over or erased as their
place in an org ends), the rows that follow content handed to another member,
and the rows that go when an org closes with the account.

Every hook is optional. ``stored_objects`` names what the domain keeps for the
person in the account archive store (an export's archive); the erasure reads
it before it erases any row and deletes those objects once it has committed.
``external_erasure`` runs inside the erasure's own
transaction, after the database half is written and before it commits: a hook
that cannot finish raises, the transaction rolls back, and the next lifecycle
pass tries the whole erasure again. Hooks must therefore be idempotent (a
customer already deleted is a success).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.extensions import ExtensionPoint
from alkera_core.schemas.account import PlanBlocker


@dataclass(frozen=True, slots=True)
class ExternalErasure:
    """What an erasure hands an ``external_erasure`` hook."""

    db: AsyncSession
    user_id: uuid.UUID
    #: Orgs the person was alone in, closing with the account.
    closed_org_ids: Sequence[uuid.UUID]


@dataclass(frozen=True, slots=True)
class ContentHandover:
    """What a ``content_handover`` hook is handed: the person leaves an org
    that keeps going, and their shared objects now belong to ``recipient``."""

    db: AsyncSession
    org_id: uuid.UUID
    user_id: uuid.UUID
    recipient: uuid.UUID
    #: The objects whose owner just became ``recipient``. Never empty.
    object_ids: Sequence[uuid.UUID]


@dataclass(frozen=True, slots=True)
class OrgClosing:
    """What an ``org_closing`` hook is handed: an org the person was alone in
    closes with their account."""

    db: AsyncSession
    org_id: uuid.UUID
    user_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class OwnedContent:
    """What an ``owned_content`` hook is handed: the person's place in an org
    ends. What they shared follows ``recipient`` when there is one; everything
    they still own in the org is then erased."""

    db: AsyncSession
    org_id: uuid.UUID
    user_id: uuid.UUID
    recipient: uuid.UUID | None


Blockers = Callable[[AsyncSession, uuid.UUID], Awaitable[list[PlanBlocker]]]
Forfeited = Callable[[AsyncSession, uuid.UUID], Awaitable[int]]
ExportDocuments = Callable[[AsyncSession, uuid.UUID], Awaitable[Mapping[str, object]]]
ExternalHook = Callable[[ExternalErasure], Awaitable[Mapping[str, int]]]
ProfileSections = Callable[[AsyncSession, uuid.UUID], Awaitable[Mapping[str, object]]]
OrgDocuments = Callable[[AsyncSession, uuid.UUID, uuid.UUID], Awaitable[Mapping[str, object]]]
#: Each returns counts for the erasure certificate, keyed like a disposition
#: (``table.column:kind``).
HandoverHook = Callable[[ContentHandover], Awaitable[Mapping[str, int]]]
ClosingHook = Callable[[OrgClosing], Awaitable[Mapping[str, int]]]
OwnedContentHook = Callable[[OwnedContent], Awaitable[Mapping[str, int]]]
#: ``(shared, private)`` live items the person owns, given ``(db, org_id, user_id)``.
OwnedCounts = Callable[[AsyncSession, uuid.UUID, uuid.UUID], Awaitable[tuple[int, int]]]
RemainingContent = Callable[[AsyncSession, uuid.UUID], Awaitable[None]]
#: Keys in the account archive store (:mod:`alkera_core.account.archive_store`)
#: that this domain holds for the person, given ``(db, user_id)``.
StoredObjects = Callable[[AsyncSession, uuid.UUID], Awaitable[Sequence[str]]]


@dataclass(frozen=True, slots=True, eq=False)
class AccountContributor:
    """One domain's part in the account lifecycle. Every hook is optional."""

    name: str
    #: What this domain says must be settled before the account can go.
    blockers: Blockers | None = None
    #: Value the person loses with the account (prepaid credit), in nano-USD.
    forfeited_nanos: Forfeited | None = None
    #: Extra documents for the export, by archive file name.
    export_documents: ExportDocuments | None = None
    #: Extra sections of the export's ``profile.json``, by key.
    profile_sections: ProfileSections | None = None
    #: Extra documents in each org's folder of the export, by file name, given
    #: ``(db, org_id, user_id)``.
    org_documents: OrgDocuments | None = None
    #: The items this domain holds that the person owns in an org, which the
    #: deletion plan counts as shared or private.
    owned_counts: OwnedCounts | None = None
    #: Hand over and erase the person's own items in an org they leave.
    owned_content: OwnedContentHook | None = None
    #: Erase whatever the person still owns in this domain, in any org, once
    #: every org has been handled.
    remaining_content: RemainingContent | None = None
    #: Move this domain's rows that follow the handed-over objects.
    content_handover: HandoverHook | None = None
    #: Erase or detach what this domain holds for an org that closes.
    org_closing: ClosingHook | None = None
    #: Erase what this domain holds outside the database (a Stripe customer).
    #: Returns counts for the erasure certificate, keyed by what was erased.
    external_erasure: ExternalHook | None = None
    #: The objects this domain keeps for the person in the account archive
    #: store. Read inside the erasure before any row is erased; the objects are
    #: deleted from the store once the erasure commits.
    stored_objects: StoredObjects | None = None
    #: Free-form notes for operators reading the registry.
    notes: tuple[str, ...] = field(default=())


ACCOUNT_CONTRIBUTORS: ExtensionPoint[AccountContributor] = ExtensionPoint("account.contributors")


def contributors() -> tuple[AccountContributor, ...]:
    return ACCOUNT_CONTRIBUTORS.items()


__all__ = [
    "ACCOUNT_CONTRIBUTORS",
    "AccountContributor",
    "ContentHandover",
    "ExternalErasure",
    "OrgClosing",
    "OwnedContent",
    "contributors",
]
