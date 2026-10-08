"""The org library the CLI and the daemon share: which stored sign-in an org
names, switching to it, listing the person's orgs, and signing out.

The rule under test (``alkera_cli.account.orgs``): only sign-ins at the API the
machine acts against count; an org id in any spelling names its sign-in; a name
names one only when exactly one sign-in at that API carries it.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from _profiles import ORG_A, ORG_B, make_jwt, store
from alkera_cli.account import auth_file, memberships, orgs, session
from alkera_cli.account.orgs import AmbiguousOrgNameError, NoStoredSignInError

STAGING = "https://staging.example.test"
#: An org whose id carries hex letters, so a change of case is a respelling.
ORG_X = "abcdef12-3456-4789-8abc-def123456789"


def _loaded() -> auth_file.AuthFileV2:
    auth = auth_file.load_profiles()
    assert auth is not None
    return auth


# --- which stored sign-in an org names ---------------------------------------------


@pytest.mark.parametrize(
    ("asked", "expected"),
    [
        pytest.param(ORG_X, "x", id="an-id"),
        pytest.param(ORG_X.upper(), "x", id="an-id-in-another-spelling"),
        pytest.param(ORG_X.replace("-", ""), "x", id="an-id-as-tokens-spell-it"),
        pytest.param("Xenon", "x", id="a-name"),
        pytest.param("  xENON ", "x", id="a-name-in-any-case"),
        pytest.param("Acme", "a", id="the-current-orgs-name"),
        pytest.param("Zeta", None, id="an-unknown-name"),
        pytest.param(ORG_B, None, id="an-id-stored-only-at-another-api"),
        pytest.param("Bravo", None, id="a-name-stored-only-at-another-api"),
    ],
)
def test_an_org_names_a_stored_sign_in_only_at_the_acting_api(
    asked: str, expected: str | None
) -> None:
    by_label = {
        "a": store(ORG_A, org_name="Acme", current=True),
        "x": store(ORG_X, org_name="Xenon"),
    }
    store(ORG_B, org_name="Bravo", api_url=STAGING)

    found = orgs.stored_sign_in_for(_loaded(), asked)

    assert (found.key if found else None) == (by_label[expected].key if expected else None)


def test_the_acting_api_is_the_first_stored_when_none_is_current() -> None:
    staged = store(ORG_B, org_name="Bravo", api_url=STAGING)
    store(ORG_A, org_name="Acme")
    auth = _loaded()
    auth.current = None

    assert orgs.acting_api_url(auth) == STAGING
    found = orgs.stored_sign_in_for(auth, "Bravo")
    assert found is not None and found.key == staged.key


def test_a_name_two_sign_ins_share_at_the_acting_api_is_refused() -> None:
    store(ORG_A, org_name="Acme", current=True)
    store(ORG_B, org_name="ACME")

    with pytest.raises(AmbiguousOrgNameError, match=r"named Acme\. Use its id"):
        orgs.stored_sign_in_for(_loaded(), "Acme")
    found = orgs.stored_sign_in_for(_loaded(), ORG_B)
    assert found is not None and found.org_team_id == ORG_B, "the id still names one"


def test_a_name_shared_only_with_another_api_is_not_ambiguous() -> None:
    here = store(ORG_A, org_name="Acme", current=True)
    store(ORG_B, org_name="Acme", api_url=STAGING)

    found = orgs.stored_sign_in_for(_loaded(), "acme")

    assert found is not None and found.key == here.key


# --- switching ------------------------------------------------------------------------


def test_switch_makes_the_named_sign_in_current() -> None:
    store(ORG_A, org_name="Acme", current=True)
    b = store(ORG_B, org_name="Bravo")

    switched = orgs.switch_org("bravo")

    assert switched is not None and switched.key == b.key
    assert _loaded().current == b.key


@pytest.mark.parametrize(
    "asked",
    [
        pytest.param("Zeta", id="nothing-stored-for-it"),
        pytest.param(ORG_B, id="stored-only-at-another-api"),
    ],
)
def test_switch_to_an_org_with_no_sign_in_here_changes_nothing(asked: str) -> None:
    a = store(ORG_A, org_name="Acme", current=True)
    store(ORG_B, org_name="Bravo", api_url=STAGING)
    before = auth_file.AUTH_FILE_PATH.read_bytes()

    assert orgs.switch_org(asked) is None
    assert auth_file.AUTH_FILE_PATH.read_bytes() == before
    assert _loaded().current == a.key


def test_switch_with_nothing_stored_is_none() -> None:
    assert orgs.switch_org(ORG_A) is None


# --- listing ----------------------------------------------------------------------------


def test_list_orgs_marks_which_are_stored_at_the_readers_api_and_which_is_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = store(ORG_A, org_name="Acme", current=True)
    store(ORG_B, org_name="Bravo", api_url=STAGING)
    rows = [
        {"org_team_id": ORG_A, "org_name": "Acme", "role": "admin"},
        {"org_team_id": ORG_B, "org_name": "Bravo", "role": "member", "sso_required": True},
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"active_org_team_id": ORG_A, "memberships": rows})

    real = memberships.fetch_memberships
    monkeypatch.setattr(
        memberships,
        "fetch_memberships",
        lambda profile, **_kw: real(profile, transport=httpx.MockTransport(handler)),
    )

    listed = {row.org_team_id: row for row in orgs.list_orgs(reader)}

    assert (listed[ORG_A].stored, listed[ORG_A].current) == (True, True)
    assert (listed[ORG_B].stored, listed[ORG_B].current) == (False, False), (
        "a sign-in at another API is not a sign-in for this one"
    )
    assert listed[ORG_B].sso_required is True and listed[ORG_A].role == "admin"
    assert seen[0].headers["authorization"] == f"Bearer {reader.token}"


# --- signing out ----------------------------------------------------------------------

UNREACHABLE = make_jwt(org=ORG_B, email="unreachable@x.com")


@pytest.fixture
def revoked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Tokens revoked server-side; one named ``unreachable`` fails to revoke."""
    calls: list[str] = []

    def _revoke(_api: str, token: str, **_kw: Any) -> bool:
        calls.append(token)
        return token != UNREACHABLE

    monkeypatch.setattr(session, "revoke_session", _revoke)
    return calls


def test_sign_out_of_the_current_org_revokes_it_and_the_next_becomes_current(
    revoked: list[str],
) -> None:
    a = store(ORG_A, org_name="Acme", current=True)
    b = store(ORG_B, org_name="Bravo")

    done = orgs.sign_out()

    assert [p.key for p in done.removed] == [a.key] and revoked == [a.token]
    assert done.now_current is not None and done.now_current.key == b.key
    assert done.current_changed is True and done.revoke_unreachable is False
    assert [p.key for p in _loaded().profiles] == [b.key]


def test_sign_out_of_a_named_org_leaves_the_current_one(revoked: list[str]) -> None:
    a = store(ORG_A, org_name="Acme", current=True)
    b = store(ORG_B, org_name="Bravo")

    done = orgs.sign_out(org="bravo")

    assert [p.key for p in done.removed] == [b.key] and revoked == [b.token]
    assert done.current_changed is False
    assert _loaded().current == a.key


def test_sign_out_of_every_org_forgets_the_file_even_when_a_revoke_fails(
    revoked: list[str],
) -> None:
    store(ORG_A, current=True)
    store(ORG_B, token=UNREACHABLE)

    done = orgs.sign_out(every=True)

    assert len(done.removed) == 2 and done.revoke_unreachable is True
    assert done.now_current is None
    assert auth_file.load_profiles() is None, "an unreachable server never keeps a token"


@pytest.mark.parametrize(
    ("asked", "error"),
    [
        pytest.param("Zeta", NoStoredSignInError, id="no-sign-in-for-it"),
        pytest.param(ORG_B, NoStoredSignInError, id="stored-only-at-another-api"),
        pytest.param("Acme", AmbiguousOrgNameError, id="a-shared-name"),
    ],
)
def test_sign_out_of_an_org_this_api_cannot_name_changes_nothing(
    revoked: list[str], asked: str, error: type[Exception]
) -> None:
    store(ORG_A, org_name="Acme", current=True)
    store(ORG_X, org_name="acme")
    store(ORG_B, org_name="Bravo", api_url=STAGING)
    before = auth_file.AUTH_FILE_PATH.read_bytes()

    with pytest.raises(error):
        orgs.sign_out(org=asked)

    assert auth_file.AUTH_FILE_PATH.read_bytes() == before and revoked == []


def test_sign_out_with_nothing_stored(revoked: list[str]) -> None:
    done = orgs.sign_out()
    assert done.removed == () and done.now_current is None and revoked == []
    with pytest.raises(NoStoredSignInError):
        orgs.sign_out(org="Acme")
