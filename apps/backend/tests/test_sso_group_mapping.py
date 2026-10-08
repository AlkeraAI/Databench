"""IdP group → org-role mapping: claim/attribute extraction + the login-time
role sync (promote/demote, highest-wins, no-match leaves alone, last-admin guard)."""

from __future__ import annotations

import uuid

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import SsoConnection, TeamRole
from backend.auth.oauth.oidc import normalize_groups
from backend.services.identity import sso as sso_service
from backend.services.org import memberships as membership_service
from tests.conftest import OrgWithAdmin, make_member

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Claim/attribute normalization + the pure mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(["a", "b"], ("a", "b"), id="list"),
        pytest.param(("x",), ("x",), id="tuple"),
        pytest.param("solo", ("solo",), id="single-string"),
        pytest.param("a, b  c", ("a", "b", "c"), id="csv-and-space"),
        pytest.param(["", "  ", "ok"], ("ok",), id="drops-blanks"),
        pytest.param(None, (), id="none"),
        pytest.param(123, (), id="non-iterable"),
    ],
)
def test_normalize_groups(value: object, expected: tuple[str, ...]) -> None:
    assert normalize_groups(value) == expected


def test_oidc_mapper_extracts_the_groups_claim() -> None:
    from backend.auth.oauth.oidc import _default_claim_mapper

    profile = _default_claim_mapper(
        "test", {"sub": "u1", "email": "A@x.com", "groups": ["admins", "eng"]}
    )
    assert profile.idp_groups == ("admins", "eng")


def test_oidc_mapper_falls_back_to_the_roles_claim() -> None:
    from backend.auth.oauth.oidc import _default_claim_mapper

    profile = _default_claim_mapper("test", {"sub": "u1", "email": "a@x.com", "roles": "admins"})
    assert profile.idp_groups == ("admins",)


def test_saml_extracts_multi_valued_group_attribute() -> None:
    from backend.auth.oauth.saml import _group_values
    from lxml import etree

    xml = (
        b'<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">'
        b"<saml:AttributeStatement>"
        b'<saml:Attribute FriendlyName="groups" Name="urn:custom:group">'
        b"<saml:AttributeValue>admins</saml:AttributeValue>"
        b"<saml:AttributeValue>eng</saml:AttributeValue>"
        b"</saml:Attribute></saml:AttributeStatement></saml:Assertion>"
    )
    assert _group_values(etree.fromstring(xml)) == ("admins", "eng")


def _conn(org_id: uuid.UUID, mapping: dict[str, str] | None) -> SsoConnection:
    return SsoConnection(org_team_id=org_id, groups_mapping=mapping)


@pytest.mark.parametrize(
    ("mapping", "groups", "expected"),
    [
        pytest.param({"admins": "admin"}, ("admins",), TeamRole.ADMIN, id="admin-match"),
        pytest.param({"staff": "member"}, ("staff",), TeamRole.MEMBER, id="member-match"),
        pytest.param(
            {"admins": "admin", "staff": "member"},
            ("staff", "admins"),
            TeamRole.ADMIN,
            id="highest-wins",
        ),
        pytest.param({"Admins": "admin"}, ("admins",), TeamRole.ADMIN, id="case-insensitive"),
        pytest.param({"admins": "admin"}, ("other",), None, id="no-match-none"),
        pytest.param({}, ("admins",), None, id="empty-mapping-none"),
        pytest.param({"admins": "bogus"}, ("admins",), None, id="bad-role-ignored"),
    ],
)
def test_role_for_groups(
    mapping: dict[str, str], groups: tuple[str, ...], expected: TeamRole | None
) -> None:
    assert sso_service.role_for_groups(_conn(uuid.uuid4(), mapping), groups) == expected


# --------------------------------------------------------------------------- #
# The login-time sync against a real org
# --------------------------------------------------------------------------- #


async def test_sync_promotes_a_member_to_admin(org_admin: OrgWithAdmin) -> None:
    async with AsyncSessionLocal() as s:
        member, _ = await make_member(s, org_id=org_admin.org_id)
        await s.commit()
        member_id = member.id
    conn = _conn(org_admin.org_id, {"alkera-admins": "admin"})
    async with AsyncSessionLocal() as s:
        member = await _reload(s, member_id)
        applied = await sso_service.sync_org_role(
            s, user=member, connection=conn, idp_groups=("alkera-admins",)
        )
        await s.commit()
    assert applied is TeamRole.ADMIN
    async with AsyncSessionLocal() as s:
        m = await membership_service.get(s, team_id=org_admin.org_id, user_id=member_id)
        assert m is not None and m.role is TeamRole.ADMIN


async def test_sync_no_match_leaves_role_unchanged(org_admin: OrgWithAdmin) -> None:
    async with AsyncSessionLocal() as s:
        member, _ = await make_member(s, org_id=org_admin.org_id)
        await s.commit()
        member_id = member.id
    conn = _conn(org_admin.org_id, {"alkera-admins": "admin"})
    async with AsyncSessionLocal() as s:
        member = await _reload(s, member_id)
        # The user is in NO mapped group → role must be left exactly as-is.
        applied = await sso_service.sync_org_role(
            s, user=member, connection=conn, idp_groups=("unrelated-group",)
        )
        await s.commit()
    assert applied is None
    async with AsyncSessionLocal() as s:
        m = await membership_service.get(s, team_id=org_admin.org_id, user_id=member_id)
        assert m is not None and m.role is TeamRole.MEMBER


async def test_sync_demotes_admin_when_another_admin_exists(org_admin: OrgWithAdmin) -> None:
    async with AsyncSessionLocal() as s:
        member, _ = await make_member(s, org_id=org_admin.org_id, role=TeamRole.ADMIN)
        await s.commit()
        member_id = member.id  # now there are TWO admins (fixture admin + this one)
    conn = _conn(org_admin.org_id, {"contractors": "member"})
    async with AsyncSessionLocal() as s:
        member = await _reload(s, member_id)
        applied = await sso_service.sync_org_role(
            s, user=member, connection=conn, idp_groups=("contractors",)
        )
        await s.commit()
    assert applied is TeamRole.MEMBER
    async with AsyncSessionLocal() as s:
        m = await membership_service.get(s, team_id=org_admin.org_id, user_id=member_id)
        assert m is not None and m.role is TeamRole.MEMBER


async def test_sync_never_demotes_the_last_org_admin(org_admin: OrgWithAdmin) -> None:
    # The fixture admin is the ONLY org admin; an IdP-driven demotion must be refused.
    conn = _conn(org_admin.org_id, {"contractors": "member"})
    async with AsyncSessionLocal() as s:
        admin = await _reload(s, org_admin.admin_id)
        applied = await sso_service.sync_org_role(
            s, user=admin, connection=conn, idp_groups=("contractors",)
        )
        await s.commit()
    assert applied is None  # guard kept them admin
    async with AsyncSessionLocal() as s:
        m = await membership_service.get(s, team_id=org_admin.org_id, user_id=org_admin.admin_id)
        assert m is not None and m.role is TeamRole.ADMIN


async def _reload(s: object, user_id: uuid.UUID) -> object:
    from backend.services.identity import users as user_service

    user = await user_service.get_by_id(s, user_id)  # type: ignore[arg-type]
    assert user is not None
    return user
