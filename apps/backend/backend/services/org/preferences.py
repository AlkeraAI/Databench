"""The server's copy of a user's preferences.

The write is a MERGE, never a replacement. That rule is the daemon's
(``alkera_cli.daemon.methods.preferences.preferences_set``) and it is copied
here deliberately: two clients of different ages write the same document, and a
replace would let the older one — which cannot even name the newer one's field —
delete a setting the person chose in a client it has never seen. Merging costs a
read; replacing costs a silent loss.

``Preferences`` allows unknown fields, so the merge is total: a key nobody here
understands round-trips through the model untouched.

One document, two rows. The keys in :data:`ORG_SCOPED_KEYS` name something in
an org's model catalog, so they live per (identity, org) in
``user_org_preferences``; everything else is the identity's own and lives in
``user_preferences``. Every read and write names the org the request acts in.
A read never falls back to the identity row's copy of an org-scoped key: a
model id from one org's catalog must not surface in another org.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import structlog
from alkera_core.db.locking import LockRank, lock_or_insert
from alkera_core.models import UserOrgPreference, UserPreference
from alkera_core.schemas.objects.specs import (
    CLOUD_PERMISSION_MODES,
    DEFAULT_CLOUD_PERMISSION_MODE,
    NEW_CHAT_PERMISSION_MODE,
    CloudPermissionMode,
)
from alkera_core.schemas.preferences import ORG_SCOPED_KEYS, Preferences
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


async def _row(db: AsyncSession, *, user_id: UUID) -> UserPreference | None:
    return (
        await db.execute(select(UserPreference).where(UserPreference.user_id == user_id))
    ).scalar_one_or_none()


async def _org_row(db: AsyncSession, *, user_id: UUID, org_id: UUID) -> UserOrgPreference | None:
    return (
        await db.execute(
            select(UserOrgPreference).where(
                UserOrgPreference.user_id == user_id, UserOrgPreference.org_team_id == org_id
            )
        )
    ).scalar_one_or_none()


async def _overlaid(
    db: AsyncSession, identity: UserPreference | None, *, user_id: UUID, org_id: UUID
) -> Preferences:
    """The identity document with its org-scoped keys replaced by ``org_id``'s.

    The identity row's own copy of those keys is dropped first, so an org with
    no row of its own reads them at their defaults rather than another org's.
    The overlay is validated as ONE document, so the version ladder and the
    field validators see exactly what a reader will be handed.
    """
    data = dict(identity.preferences or {}) if identity is not None else {}
    for key in ORG_SCOPED_KEYS:
        data.pop(key, None)
    scoped = await _org_row(db, user_id=user_id, org_id=org_id)
    if scoped is not None:
        data.update({k: v for k, v in (scoped.preferences or {}).items() if k in ORG_SCOPED_KEYS})
    return Preferences.model_validate(data)


async def load(db: AsyncSession, *, user_id: UUID, org_id: UUID) -> Preferences:
    """This user's stored preferences, as they read in ``org_id``.

    An absent row IS the default document — the same answer the CLI gives for a
    missing ``preferences.yml`` — so nothing has to be seeded at signup. A row
    written by an older schema is migrated on the way out by the model's own
    ladder, which is why the whole document is stored rather than parsed fields.
    """
    row = await _row(db, user_id=user_id)
    return await _overlaid(db, row, user_id=user_id, org_id=org_id)


async def stored(db: AsyncSession, *, user_id: UUID, org_id: UUID) -> Preferences | None:
    """This user's preferences ONLY IF they have saved some, else ``None``.

    :func:`load` cannot answer this: an absent row and a saved document that
    happens to hold the defaults both read as the default document, and the two
    are not the same question. A caller whose own floor differs from the
    schema's default has to be able to tell "this person chose the default"
    from "this person has never chosen" — the browser's new-chat stance is
    exactly that case. "Saved some" is the identity row: every save writes it.
    """
    row = await _row(db, user_id=user_id)
    if row is None:
        return None
    return await _overlaid(db, row, user_id=user_id, org_id=org_id)


async def merge(
    db: AsyncSession, *, user_id: UUID, org_id: UUID, incoming: dict[str, Any]
) -> Preferences:
    """Apply ``incoming`` ON TOP of what is stored and return the whole document.

    The same three lines the daemon runs: dump the current document, update it
    with the caller's fields, revalidate. Revalidating is what refuses a value
    the schema does not accept — a permission mode that is not one, a timeout
    that is not a number — rather than storing it for a reader to trip over.
    Flushed, not committed: the caller owns the transaction.

    Both rows are made if missing and locked before they are read, identity
    first: two saves at once are taken one after the other, each merged onto
    what the one before it stored, so neither is refused as a duplicate and
    neither drops the other's fields.

    The result is split on the way down. The identity row takes every key that
    is not org-scoped and keeps whatever copy of the org-scoped keys it already
    held untouched (an older task of a rolling deploy still reads them there);
    ``org_id``'s row takes the org-scoped keys, and is written only when the
    caller sent one of them.
    """
    row = (
        await lock_or_insert(
            db,
            LockRank.USER_PREFERENCES,
            select(UserPreference)
            .where(UserPreference.user_id == user_id)
            .execution_options(populate_existing=True),
            insert(UserPreference).values(
                user_id=user_id, preferences=Preferences().model_dump(mode="json")
            ),
        )
    )[0].scalar_one()
    scoped: UserOrgPreference | None = None
    if ORG_SCOPED_KEYS.intersection(incoming):
        scoped = (
            await lock_or_insert(
                db,
                LockRank.USER_PREFERENCES,
                select(UserOrgPreference)
                .where(
                    UserOrgPreference.user_id == user_id,
                    UserOrgPreference.org_team_id == org_id,
                )
                .execution_options(populate_existing=True),
                insert(UserOrgPreference).values(
                    user_id=user_id, org_team_id=org_id, preferences={}
                ),
            )
        )[0].scalar_one()
    current = await _overlaid(db, row, user_id=user_id, org_id=org_id)
    data = current.model_dump()
    data.update(incoming)
    merged = Preferences.model_validate(data)
    document = merged.model_dump(mode="json")
    identity_part = {k: v for k, v in document.items() if k not in ORG_SCOPED_KEYS}
    base = Preferences.model_validate(row.preferences or {}).model_dump(mode="json")
    row.preferences = {**base, **identity_part}
    if scoped is not None:
        scoped.preferences = {k: document[k] for k in sorted(ORG_SCOPED_KEYS) if k in document}
    await db.flush()
    return merged


async def starting_cloud_mode(
    db: AsyncSession, *, user_id: UUID, org_id: UUID
) -> CloudPermissionMode:
    """The stance a cloud chat opened on this user's behalf starts in.

    One resolver for every door that opens a cloud chat — the browser composer
    and a Slack mention both — because the spec asks a Slack chat to open "as if
    one was started in the web". A door that resolved its own stance would drift
    from the other the first time either changed, and the drift is invisible:
    both answers are a legal mode, and the wrong one is only noticed when a
    person's edit is refused without ever being offered.

    Two narrowings, both deliberate.

    A reader who has never saved preferences starts in ``default``: ask before
    each change, the same stance the desktop starts in. The product decided a
    new chat should be able to act once its person approves, rather than refuse
    every change until someone finds the mode picker.

    And a saved stance is honoured only where the doors could SET it. The
    preference is one value shared with the editor, and both now offer the same
    five stances — but a word from a retired vocabulary, or one a newer client
    invented, still starts a cloud chat ``read_only`` rather than being smuggled
    into a stance the doors would refuse to set directly. That floor is the
    legacy one, not the new-chat start, so a corrupt preference never widens.
    """
    saved = await stored(db, user_id=user_id, org_id=org_id)
    if saved is None:
        return NEW_CHAT_PERMISSION_MODE
    if saved.default_permission_mode in CLOUD_PERMISSION_MODES:
        return cast(CloudPermissionMode, saved.default_permission_mode)
    # The door refuses a word like this now, but a row written before it did
    # still holds one, and the floor it lands on looks identical to a deliberate
    # read_only. Say which value we could not honour so the person whose setting
    # stopped working is not the one who has to notice.
    logger.warning(
        "chat.starting_mode.unrecognized_preference",
        user_id=str(user_id),
        saved=saved.default_permission_mode,
        applied=DEFAULT_CLOUD_PERMISSION_MODE,
    )
    return DEFAULT_CLOUD_PERMISSION_MODE


__all__ = ["load", "merge", "starting_cloud_mode", "stored"]
