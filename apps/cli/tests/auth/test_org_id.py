"""One spelling of an org id, and a malformed one refused everywhere it is read.

Every place that keys on an org (a stored sign-in, its ``X-Alkera-Org`` header,
a box's slot table, a chat row's tenancy partition, the per-org preferences)
goes through :func:`canonical_org_id`. These pin its table, and that a value
that is not an org id never flows through any of those readers as if it were
one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml
from _profiles import API, USER, make_jwt
from alkera_cli.account import auth_file, memberships
from alkera_cli.account.auth_file import ORG_HEADER, load_profiles, profile_key
from alkera_cli.account.org_id import MalformedOrgIdError, canonical_org_id
from alkera_cli.cloud.mirror_factory import chat_org_id
from alkera_cli.supervisor.slots import SlotError, canonical_org

#: An org id with hex letters in it, so a change of case is a real respelling.
ORG = "abcdef12-3456-4789-8abc-def123456789"
BARE_HEX = ORG.replace("-", "")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(ORG, id="canonical"),
        pytest.param(ORG.upper(), id="upper-case"),
        pytest.param(BARE_HEX, id="bare-hex-as-tokens-carry-it"),
        pytest.param("{" + ORG + "}", id="braces"),
        pytest.param(f"  {ORG}\n", id="surrounding-space"),
        pytest.param(f"urn:uuid:{ORG}", id="urn-form"),
    ],
)
def test_every_spelling_of_one_org_reads_as_the_same_id(value: str) -> None:
    assert canonical_org_id(value) == ORG


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param("acme", id="a-name"),
        pytest.param(ORG[:-1], id="one-digit-short"),
        pytest.param(ORG + "0", id="one-digit-long"),
        pytest.param(ORG.replace("a", "g", 1), id="non-hex-digit"),
        pytest.param(None, id="none"),
        pytest.param(123, id="an-integer"),
        pytest.param(["x"], id="a-list"),
    ],
)
def test_anything_that_is_not_an_org_id_is_refused(value: Any) -> None:
    with pytest.raises(MalformedOrgIdError) as caught:
        canonical_org_id(value)
    assert caught.value.value == value
    assert isinstance(caught.value, ValueError)


def _write_auth_file(org_team_id: str) -> None:
    """``auth.yml`` as a hand edit or an older writer could leave it."""
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    key = f"{API}|{USER}|{org_team_id}"
    doc = {
        "schema_version": "2.0.0",
        "current": key,
        "profiles": [
            {
                "key": key,
                "api_url": API,
                "user_id": USER,
                "email": "a@x.com",
                "org_team_id": org_team_id,
                "org_name": "Acme",
                "token": make_jwt(org=None),
                "expires_at": expires,
            }
        ],
    }
    auth_file.AUTH_FILE_PATH.write_text(yaml.safe_dump(doc), encoding="utf-8")


def test_a_malformed_org_in_auth_yml_reads_as_no_org_rather_than_a_valid_looking_id() -> None:
    """The sign-in itself survives (its token still works; the server knows its
    org), but the bad value never becomes the profile's org: it keys nothing,
    asserts nothing in the org header, and finds no stored sign-in by it."""
    _write_auth_file("acme")

    loaded = load_profiles()
    assert loaded is not None and loaded.current_profile is not None
    profile = loaded.current_profile
    assert profile.org_team_id == ""
    assert profile.key == profile_key(API, USER, "")
    assert ORG_HEADER not in profile.headers()
    assert memberships.stored_profile_for(loaded, API, "acme") is None


def test_a_differently_spelled_org_in_auth_yml_reads_in_its_one_spelling() -> None:
    _write_auth_file(BARE_HEX.upper())

    loaded = load_profiles()
    assert loaded is not None and loaded.current_profile is not None
    assert loaded.current_profile.org_team_id == ORG
    assert loaded.current_profile.headers()[ORG_HEADER] == ORG
    assert memberships.stored_profile_for(loaded, API, ORG.upper()) is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(ORG.upper(), ORG, id="respelled"),
        pytest.param("acme", None, id="malformed-reads-as-no-org"),
        pytest.param("", None, id="empty"),
        pytest.param(42, None, id="not-a-string"),
    ],
)
def test_a_chat_rows_org_reads_through_the_one_rule(raw: object, expected: str | None) -> None:
    assert chat_org_id({"org_id": raw}) == expected


def test_the_slot_table_refuses_a_malformed_org_with_its_own_error() -> None:
    assert canonical_org(ORG.upper()) == ORG
    with pytest.raises(SlotError, match="not an org id"):
        canonical_org("acme")


def test_a_membership_is_found_by_any_spelling_of_its_id_and_never_by_a_bad_one() -> None:
    listed = memberships.Memberships(
        active_org_team_id=ORG,
        memberships=(memberships.Membership(org_team_id=ORG, org_name="Acme", role="admin"),),
    )
    found = memberships.find_membership(listed, ORG.upper())
    assert found is not None and found.org_team_id == ORG
    assert memberships.find_membership(listed, ORG[:-1]) is None
