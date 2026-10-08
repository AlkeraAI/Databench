"""Who a share can name: the org's people and teams, searched by a fragment.

The share dialog needs to turn "Dana" or "dana@acme.com" into a principal id,
and the only listing that used to answer that was the org admin's member
table — so every member who was not an admin searched, was refused, and read
"nobody matches". This is the read the dialog was missing. It is asked only
through the permissions family, after the caller has been allowed to share
the node in question, so it says nothing to anyone who could not already name
these principals on a grant — the grant route accepts exactly this set.

The set is the one :func:`alkera_core.files.acl._assert_in_org` admits: users
with an active membership in the drive's org, and the teams in that org's tree,
the root included. A deactivated account is left out — it can no longer sign in, so a
share to it would reach nobody — and nothing outside the org is ever read.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from alkera_core.files.membership import active_member_of
from alkera_core.models import MembershipStatus, OrgMembership
from alkera_core.models.user import User
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.org import teams as team_service

#: The most suggestions one search returns. The dialog shows a short list and
#: narrows as the reader types; it never needs the whole directory at once.
CANDIDATE_LIMIT = 20

#: The longest fragment worth matching. Longer than any name or email the
#: product stores; a longer query is cut rather than refused, so the answer
#: never depends on anything but who matches.
QUERY_MAX = 320


@dataclass(frozen=True, slots=True)
class Candidate:
    """One principal a share may name."""

    kind: str
    id: uuid.UUID
    name: str
    #: A user's email; ``None`` for a team.
    email: str | None = None
    #: Whether this team is the org itself ("everyone in the organization").
    is_org: bool = False


def _like(fragment: str) -> str:
    """``fragment`` as a case-insensitive substring pattern, its wildcards
    taken literally: a search for ``50%`` means the characters, not a glob."""
    escaped = fragment.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def share_candidates(
    session: AsyncSession,
    *,
    org_team_id: uuid.UUID,
    query: str,
    limit: int = CANDIDATE_LIMIT,
) -> list[Candidate]:
    """The people, then the teams, of this org whose name or email contains
    ``query`` — at most ``limit`` in all. An empty query matches nobody: the
    read is a search, not a way to page through the directory."""
    text = query.strip()[:QUERY_MAX]
    if not text or limit <= 0:
        return []
    pattern = _like(text)
    full_name = func.trim(func.concat(User.first_name, " ", User.last_name))
    rows = await session.execute(
        select(User.id, User.first_name, User.last_name, User.email)
        .where(
            active_member_of(org_team_id),
            User.is_active.is_(True),
            or_(
                full_name.ilike(pattern, escape="\\"),
                User.email.ilike(pattern, escape="\\"),
            ),
        )
        .order_by(func.lower(full_name), func.lower(User.email))
        .limit(limit)
    )
    people = [
        Candidate(
            kind="user",
            id=user_id,
            name=f"{first} {last}".strip() or email,
            email=email,
        )
        for user_id, first, last, email in rows.all()
    ]
    room = limit - len(people)
    if room <= 0:
        return people
    needle = text.casefold()
    teams = [
        Candidate(kind="team", id=team.id, name=team.name, is_org=team.id == org_team_id)
        for team in await team_service.list_in_org(session, org_team_id)
        if needle in team.name.casefold()
    ]
    return people + teams[:room]


async def member_names(
    db: AsyncSession, org_id: uuid.UUID, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """The display names of those of ``ids`` who belong to ``org_id``, leaving
    out anyone without a name: no other org's people, and never an address."""
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(User.id, User.first_name, User.last_name)
            .join(OrgMembership, OrgMembership.user_id == User.id)
            .where(
                User.id.in_(ids),
                OrgMembership.org_team_id == org_id,
                OrgMembership.status == MembershipStatus.ACTIVE,
            )
        )
    ).all()
    return {
        row.id: name
        for row in rows
        if (name := f"{row.first_name or ''} {row.last_name or ''}".strip())
    }


__all__ = ["CANDIDATE_LIMIT", "QUERY_MAX", "Candidate", "member_names", "share_candidates"]
