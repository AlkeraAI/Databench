"""The listing's audience predicate agrees with the policy it stands in for.

``objects.access.may_read`` computes the READ answer for a listing without a
decision row per row. That is only sound while it is the same predicate the
policy decides through, so every combination of the facts the policy reads is
put to both, and the two answers must agree — including the cases where the
answer is "no": a foreign org, a caller who is not a member, an audience the
caller is not in, a private object of someone else, and a scope nobody can
parse. A branch added to the policy's READ path that this predicate does not
mirror fails here before it can over-share through the list.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from uuid import UUID, uuid4

import pytest
from alkera_core.authz import (
    ActingContext,
    Action,
    Resource,
    ResourceType,
    Role,
    authorize,
    expand_roles,
)
from backend.services.sharing import access

ORG = UUID("00000000-0000-4000-8000-00000000000a")
OTHER_ORG = UUID("00000000-0000-4000-8000-00000000000b")
TEAM = UUID("00000000-0000-4000-8000-00000000000c")
OTHER_TEAM = UUID("00000000-0000-4000-8000-00000000000d")
OWNER = UUID("00000000-0000-4000-8000-000000000001")
READER = UUID("00000000-0000-4000-8000-000000000002")


@dataclass(frozen=True, slots=True)
class FakeObject:
    """The columns the predicate and the resource read, and nothing else."""

    id: UUID
    org_team_id: UUID
    team_id: UUID | None
    owner_user_id: UUID
    visibility_scope: str
    #: A chat is NOT decided by its audience, so it is not this predicate's
    #: question; every row here is an object that has one.
    type: str = "result"


def _reader(
    *,
    who: UUID,
    org: UUID = ORG,
    in_org: bool = True,
    roles: frozenset[Role] = frozenset({Role.MEMBER}),
    is_org_admin: bool = False,
    team_ids: frozenset[UUID] = frozenset(),
    agent: bool = False,
) -> access.Reader:
    email = f"{who}@example.com"
    ctx = (
        ActingContext.for_agent(user_id=who, org_id=org, email=email, session_id="sess-1")
        if agent
        else ActingContext.for_user(user_id=who, org_id=org, email=email)
    )
    return access.Reader(
        ctx=ctx,
        in_org=in_org,
        roles=expand_roles(roles),
        is_org_admin=is_org_admin,
        team_ids=team_ids,
        email_verified=True,
    )


SCOPES = ["org", f"team:{TEAM}", f"team:{OTHER_TEAM}", "private", "team:not-a-uuid", "public"]
READERS = {
    "owner": _reader(who=OWNER),
    "owners_agent": _reader(who=OWNER, agent=True),
    "member_on_team": _reader(who=READER, team_ids=frozenset({TEAM})),
    "member_off_team": _reader(who=READER),
    "org_admin": _reader(who=READER, roles=frozenset({Role.ADMIN, Role.MEMBER}), is_org_admin=True),
    "viewer_only": _reader(who=READER, roles=frozenset({Role.VIEWER})),
    "no_roles": _reader(who=READER, roles=frozenset()),
    "not_in_org": _reader(who=READER, in_org=False, roles=frozenset()),
    "other_org": _reader(who=READER, org=OTHER_ORG),
}


@pytest.mark.parametrize(
    ("who", "scope"),
    [pytest.param(who, scope, id=f"{who}-{scope}") for who, scope in product(READERS, SCOPES)],
)
def test_may_read_agrees_with_the_policy(who: str, scope: str) -> None:
    reader = READERS[who]
    team_id = (
        TEAM if scope == f"team:{TEAM}" else OTHER_TEAM if scope == f"team:{OTHER_TEAM}" else None
    )
    obj = FakeObject(
        id=uuid4(), org_team_id=ORG, team_id=team_id, owner_user_id=OWNER, visibility_scope=scope
    )
    decision = authorize(
        reader.ctx,
        Action.READ,
        Resource(ResourceType.WORKSPACE_OBJECT, id=str(obj.id), org_id=ORG, team_id=team_id),
        access.object_attrs(obj, reader),  # type: ignore[arg-type]
    )
    assert access.may_read(obj, reader) is decision.allowed, (  # type: ignore[arg-type]
        who,
        scope,
        decision.reason,
    )


def test_the_agreement_table_covers_both_answers() -> None:
    """A table where every row agreed because every row was 'no' (or 'yes')
    would prove nothing; both answers appear."""
    answers = set()
    for reader in READERS.values():
        for scope in SCOPES:
            obj = FakeObject(
                id=uuid4(),
                org_team_id=ORG,
                team_id=TEAM if scope == f"team:{TEAM}" else None,
                owner_user_id=OWNER,
                visibility_scope=scope,
            )
            answers.add(access.may_read(obj, reader))  # type: ignore[arg-type]
    assert answers == {True, False}
