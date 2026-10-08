"""What the team and personal connection routes share before a write: the row
or the 404, and the one build over a complete answer.

Both the open routes (``routes.connections.team_connections``) and a
distribution's routes over the same rows (a check before a save) read these, so
neither route module imports the other.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.connections.models import TeamConnection
from alkera_core.connections.schemas import TeamConnectionUpsertRequest
from alkera_core.models import Team, User
from cryptography.fernet import InvalidToken
from fastapi import HTTPException, status
from sqlalchemy import select

from backend.auth.dependencies import DbSession
from backend.services.connections import (
    CREDENTIAL_UNREADABLE_REASON,
    BuiltUpsert,
    SaveRefusedError,
    decrypt_named_secrets,
    decrypt_shared_secret,
    get_by_id,
    get_by_identity,
    mark_credential_unreadable,
    validate_and_build,
)


async def team_or_404(db: DbSession, team_id: UUID) -> Team:
    team = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found.")
    return team


def unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


async def credential_unreadable(conn: TeamConnection) -> HTTPException:
    """Record that Alkera cannot open this credential, and say so (409)."""
    await mark_credential_unreadable(conn)
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "credential_unreadable", "message": CREDENTIAL_UNREADABLE_REASON},
    )


async def validated_build(
    db: DbSession,
    team_id: UUID,
    payload: TeamConnectionUpsertRequest,
    *,
    owner_user_id: UUID | None = None,
) -> BuiltUpsert:
    """The save rule over the stored row's keep-blank-keeps secret. A refusal
    becomes the 422 whose sentence the admin reads.

    ``owner_user_id`` names which row a blank secret keeps: a person editing
    their own connection must not inherit the team's stored credential from the
    row that happens to share its names.
    """
    existing = await get_by_identity(
        db,
        team_id=team_id,
        plugin=payload.plugin,
        handle=payload.handle,
        owner_user_id=owner_user_id,
    )
    try:
        stored_secret = decrypt_shared_secret(existing) if existing is not None else None
        stored_named_secrets = decrypt_named_secrets(existing) if existing is not None else {}
    except InvalidToken:
        # A stored ciphertext nobody can open is Alkera's problem, and an edit
        # that leaves the secret blank would otherwise 500 on the way to reading
        # what it was going to keep.
        raise await credential_unreadable(existing) from None  # type: ignore[arg-type]
    try:
        built = validate_and_build(
            payload,
            stored_secret=stored_secret,
            stored_named_secrets=stored_named_secrets,
            stored_oauth_secret=bool(existing and existing.oauth_client_secret_encrypted),
            # Who owns the row decides which rules it answers to: a personal
            # connection has no members, so nothing in it is "left to" anyone.
            personal=owner_user_id is not None,
        )
    except SaveRefusedError as exc:
        raise unprocessable(str(exc)) from None
    return built


def personal_payload(payload: TeamConnectionUpsertRequest) -> TeamConnectionUpsertRequest:
    """A personal save, normalized.

    Nobody else holds a slot in a connection that belongs to one person: every
    value is theirs, so the distribute switches are all on and there is no
    member half to leave blank. Normalized here rather than trusted from the
    client so a crafted request cannot store a personal row that holds back
    fields no member will ever answer. ``auto_add`` is likewise the owner's own
    workspace decision, and stays as sent.
    """
    return payload.model_copy(
        update={"shared_fields": sorted(set(payload.shared_fields) | set(payload.fields))}
    )


async def team_connection_or_404(
    db: DbSession, team_id: UUID, connection_id: UUID
) -> TeamConnection:
    """The TEAM's row at this id — never a member's own.

    A personal row hangs off the org root team so the org's tenancy checks reach
    it, which means matching ``team_id`` is not on its own proof that the row is
    the team's; and an org admin administers that root by descent, so the admin
    gate passes there too. A row with an owner answers to that owner alone,
    through ``/me/connections``, so it is the same 404 here as a row that does
    not exist.
    """
    conn = await get_by_id(db, connection_id)
    if conn is None or conn.team_id != team_id or conn.owner_user_id is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found.")
    return conn


async def own_connection_or_404(
    db: DbSession, user: User, connection_id: UUID, *, org_id: UUID
) -> TeamConnection:
    """The caller's own personal row in ``org_id`` (its root team). Anything else
    — a team row, another member's row, their own row in another org — is the
    same 404, so these routes never confirm what they will not act on."""
    conn = await get_by_id(db, connection_id)
    if conn is None or conn.owner_user_id != user.id or conn.team_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found.")
    return conn


__all__ = [
    "credential_unreadable",
    "own_connection_or_404",
    "personal_payload",
    "team_connection_or_404",
    "team_or_404",
    "unprocessable",
    "validated_build",
]
