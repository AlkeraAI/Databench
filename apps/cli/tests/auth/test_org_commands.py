"""`alkera login --org`, `whoami`, `org list|switch`, `logout --org|--all`, the
global `--org` / `ALKERA_ORG` as `whoami` reads them, and `project unpin`.

The backend is a local fake: the memberships route answers through an
``httpx.MockTransport`` behind the real client (so the parser and the org
assertion header are exercised), and the device grant returns tokens whose
claims name the org a real approval would have minted for.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from _profiles import API, ORG_A, ORG_B, USER, make_jwt, store
from alkera_cli import main as cli_main
from alkera_cli.account import auth_file, device_flow, memberships, session
from alkera_cli.account import login as login_flow
from alkera_cli.account.login import GatewayCheck
from alkera_cli.account.session import CurrentUser
from alkera_cli.commands import account as account_command
from alkera_core.project import ProjectDirectory
from alkera_core.project.cloud_binding import CloudBinding
from typer.testing import CliRunner

runner = CliRunner()


def _settings_caches() -> list[Any]:
    """Every ``get_settings`` the commands under test call. A module that
    imported the name holds the function object it saw, which an earlier
    test's reload of the config module may have replaced since."""
    from alkera_cli.host import config

    return [config.get_settings, cli_main.get_settings, account_command.get_settings]


@pytest.fixture(autouse=True)
def _api_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ALKERA_API_URL", API)
    for cached in _settings_caches():
        cached.cache_clear()
    yield
    for cached in _settings_caches():
        cached.cache_clear()


@pytest.fixture
def me(monkeypatch: pytest.MonkeyPatch) -> dict[str, CurrentUser]:
    """``/auth/me`` per token: the org it answers for is the token's own."""
    users: dict[str, CurrentUser] = {}

    def _resolve(_api: str, token: str, **_kw: Any) -> CurrentUser | None:
        return users.get(token)

    monkeypatch.setattr(session, "resolve_user", _resolve)
    monkeypatch.setattr(login_flow, "check_gateway", lambda _token: GatewayCheck(True))
    return users


@pytest.fixture
def device(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A device grant that approves with ``state['token']`` and records the
    approval URL the CLI opened."""
    state: dict[str, Any] = {"token": None, "opened": []}

    def _request(*_a: object, **_k: object) -> device_flow.DeviceCodeResponse:
        return device_flow.DeviceCodeResponse(
            device_code="dc",
            user_code="ABCD-1234",
            verification_uri="https://app.example.test/device",
            verification_uri_complete="https://app.example.test/device?user_code=ABCD-1234",
            expires_in=600,
            interval=1,
        )

    monkeypatch.setattr(device_flow, "request_device_code", _request)
    monkeypatch.setattr(device_flow, "poll_for_token", lambda *_a, **_k: state["token"])
    monkeypatch.setattr(account_command.webbrowser, "open", state["opened"].append)
    return state


@pytest.fixture
def membership_server(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    seen: list[httpx.Request] = []
    rows = [
        {"org_team_id": ORG_A, "org_name": "Acme", "role": "admin", "sso_required": False},
        {"org_team_id": ORG_B, "org_name": "Bravo", "role": "member", "sso_required": True},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        token = request.headers["authorization"].removeprefix("Bearer ")
        claims_org = ORG_A if token == _token_for(ORG_A) else ORG_B
        return httpx.Response(200, json={"active_org_team_id": claims_org, "memberships": rows})

    real = memberships.fetch_memberships

    def _fetch(profile: auth_file.Profile, **kw: Any) -> memberships.Memberships:
        return real(profile, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(memberships, "fetch_memberships", _fetch)
    return seen


def _token_for(org: str) -> str:
    return make_jwt(org=org, exp=4102444800)


def _user(org: str, name: str) -> CurrentUser:
    return CurrentUser(id=USER, email="a@x.com", display_name="A", org_team_id=org, org_name=name)


# --- login --------------------------------------------------------------------


def test_login_with_org_preselects_it_and_saves_the_profile_the_token_names(
    me: dict[str, CurrentUser], device: dict[str, Any]
) -> None:
    # Asked for A by id; the approver picked B on the page, so the token is B's.
    device["token"] = _token_for(ORG_B)
    me[device["token"]] = _user(ORG_B, "Bravo")

    result = runner.invoke(cli_main.app, ["login", "--org", ORG_A])

    assert result.exit_code == 0, result.output
    assert device["opened"] == [f"https://app.example.test/device?user_code=ABCD-1234&org={ORG_A}"]
    assert f"org={ORG_A}" in "".join(result.output.split()), "the printed link pre-selects it too"
    stored = auth_file.load_profiles()
    assert stored is not None and stored.current_profile is not None
    assert stored.current_profile.org_team_id == ORG_B
    assert stored.current_profile.key == auth_file.profile_key(API, USER, ORG_B)
    assert "Bravo" in result.output


def test_login_by_org_name_resolves_the_id_through_the_stored_sign_in(
    me: dict[str, CurrentUser], device: dict[str, Any], membership_server: list[httpx.Request]
) -> None:
    store(ORG_A, org_name="Acme", current=True, token=_token_for(ORG_A))
    device["token"] = _token_for(ORG_B)
    me[device["token"]] = _user(ORG_B, "Bravo")

    result = runner.invoke(cli_main.app, ["login", "--org", "bravo"])

    assert result.exit_code == 0, result.output
    assert device["opened"][0].endswith(f"&org={ORG_B}")
    assert membership_server[0].headers["x-alkera-org"] == ORG_A
    profiles = auth_file.load_profiles()
    assert profiles is not None
    assert {p.org_team_id for p in profiles.profiles} == {ORG_A, ORG_B}


def test_login_with_an_unknown_org_name_and_no_sign_in_passes_it_through(
    me: dict[str, CurrentUser], device: dict[str, Any]
) -> None:
    device["token"] = _token_for(ORG_A)
    me[device["token"]] = _user(ORG_A, "Acme")
    result = runner.invoke(cli_main.app, ["login", "--org", "Some Org"])
    assert result.exit_code == 0, result.output
    assert device["opened"][0].endswith("&org=Some+Org")


def test_login_as_another_person_is_another_profile(
    me: dict[str, CurrentUser], device: dict[str, Any]
) -> None:
    store(ORG_A, org_name="Acme", current=True)
    other = make_jwt(sub="88888888-8888-4888-8888-888888888888", org=ORG_A, email="b@x.com")
    device["token"] = other
    me[other] = CurrentUser(
        id="88888888-8888-4888-8888-888888888888",
        email="b@x.com",
        display_name="B",
        org_team_id=ORG_A,
        org_name="Acme",
    )
    result = runner.invoke(cli_main.app, ["login", "--force"])
    assert result.exit_code == 0, result.output
    profiles = auth_file.load_profiles()
    assert profiles is not None and len(profiles.profiles) == 2
    assert profiles.current_profile is not None and profiles.current_profile.email == "b@x.com"


# --- whoami ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "env", "pinned", "expected_org", "expected_source"),
    [
        pytest.param([], {}, False, "Acme", "current", id="current"),
        pytest.param(["--org", "Bravo"], {}, False, "Bravo", "flag (--org)", id="flag"),
        pytest.param([], {"ALKERA_ORG": "Bravo"}, False, "Bravo", "env (ALKERA_ORG)", id="env"),
        pytest.param([], {}, True, "Bravo", "pin (.alkera/cloud.json)", id="pin"),
    ],
)
def test_whoami_names_the_org_and_where_the_sign_in_came_from(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    me: dict[str, CurrentUser],
    args: list[str],
    env: dict[str, str],
    pinned: bool,
    expected_org: str,
    expected_source: str,
) -> None:
    a = store(ORG_A, org_name="Acme", current=True, token=_token_for(ORG_A))
    b = store(ORG_B, org_name="Bravo", token=_token_for(ORG_B))
    me[a.token], me[b.token] = _user(ORG_A, "Acme"), _user(ORG_B, "Bravo")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    project_root = tmp_path / "proj"
    project = ProjectDirectory(project_root / ".alkera")
    if pinned:
        project.cloud_binding().write(
            CloudBinding(api_url=API, org_team_id=ORG_B, org_name="Bravo")
        )

    result = runner.invoke(cli_main.app, [*args, "whoami", "-p", str(project_root)])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert f"Organization: {expected_org}" in lines
    assert f"Source: {expected_source}" in lines
    assert "Email: a@x.com" in lines
    assert f"API: {API}" in lines


def test_whoami_on_a_project_pinned_to_an_org_without_a_sign_in_refuses(
    tmp_path: Path, me: dict[str, CurrentUser]
) -> None:
    store(ORG_A, org_name="Acme", current=True)
    project_root = tmp_path / "proj"
    ProjectDirectory(project_root / ".alkera").cloud_binding().write(
        CloudBinding(api_url=API, org_team_id=ORG_B, org_name="Bravo")
    )
    result = runner.invoke(cli_main.app, ["whoami", "-p", str(project_root)])
    assert result.exit_code == 1
    assert "This project belongs to Bravo. Run `alkera org switch Bravo`." in result.output


# --- org list / switch ----------------------------------------------------------


def test_org_list_marks_the_current_org_and_which_are_signed_in(
    membership_server: list[httpx.Request],
) -> None:
    store(ORG_A, org_name="Acme", current=True, token=_token_for(ORG_A))

    result = runner.invoke(cli_main.app, ["org", "list"])

    assert result.exit_code == 0, result.output
    cells = {}
    for line in result.output.splitlines():
        words = [w for w in line.replace("│", " ").replace("┃", " ").split() if w]
        for name in ("Acme", "Bravo"):
            if name in words:
                cells[name] = words
    assert cells["Acme"][0] == "*" and "yes" in cells["Acme"]
    assert cells["Bravo"][0] == "Bravo" and "no" in cells["Bravo"]
    assert membership_server[0].headers["authorization"] == f"Bearer {_token_for(ORG_A)}"


def test_org_switch_to_a_stored_profile_makes_it_current_without_a_login(
    device: dict[str, Any],
) -> None:
    store(ORG_A, org_name="Acme", current=True)
    b = store(ORG_B, org_name="Bravo")

    result = runner.invoke(cli_main.app, ["org", "switch", "Bravo"])

    assert result.exit_code == 0, result.output
    assert "Current organization: Bravo" in result.output
    after = auth_file.load_profiles()
    assert after is not None and after.current == b.key
    assert device["opened"] == [], "no device login for a stored sign-in"


def test_org_switch_to_an_org_without_a_sign_in_starts_a_login_for_it(
    me: dict[str, CurrentUser], device: dict[str, Any]
) -> None:
    store(ORG_A, org_name="Acme", current=True)
    device["token"] = _token_for(ORG_B)
    me[device["token"]] = _user(ORG_B, "Bravo")

    result = runner.invoke(cli_main.app, ["org", "switch", ORG_B])

    assert result.exit_code == 0, result.output
    assert device["opened"][0].endswith(f"&org={ORG_B}")
    after = auth_file.load_profiles()
    assert after is not None and after.current_profile is not None
    assert after.current_profile.org_team_id == ORG_B


STAGING = "https://staging.example.test"


def test_org_switch_by_id_never_moves_to_a_sign_in_at_another_api(
    me: dict[str, CurrentUser], device: dict[str, Any]
) -> None:
    """The only stored sign-in for the org is at another deployment: switching
    signs in to it here instead of quietly moving the person to that API."""
    store(ORG_A, org_name="Acme", current=True)
    staged = store(ORG_B, org_name="Bravo", api_url=STAGING)
    device["token"] = _token_for(ORG_B)
    me[device["token"]] = _user(ORG_B, "Bravo")

    result = runner.invoke(cli_main.app, ["org", "switch", ORG_B])

    assert result.exit_code == 0, result.output
    assert device["opened"], "a device login for the org at this API"
    after = auth_file.load_profiles()
    assert after is not None and after.current_profile is not None
    assert after.current_profile.api_url == API
    assert after.current_profile.org_team_id == ORG_B
    assert staged.key in {p.key for p in after.profiles}, "the other API's sign-in is kept"


def test_org_switch_by_a_name_two_orgs_share_is_refused(device: dict[str, Any]) -> None:
    a = store(ORG_A, org_name="Acme", current=True)
    store(ORG_B, org_name="acme")
    before = auth_file.AUTH_FILE_PATH.read_bytes()

    result = runner.invoke(cli_main.app, ["org", "switch", "ACME"])

    assert result.exit_code == 1
    assert "More than one organization you are signed in to is named ACME" in result.output
    assert auth_file.AUTH_FILE_PATH.read_bytes() == before
    after = auth_file.load_profiles()
    assert after is not None and after.current == a.key
    assert device["opened"] == [], "an ambiguous name never starts a login either"


# --- logout -----------------------------------------------------------------------


@pytest.fixture
def revoked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _revoke(_api: str, token: str, **_kw: Any) -> bool:
        calls.append(token)
        return True

    monkeypatch.setattr(session, "revoke_session", _revoke)
    return calls


def test_logout_org_removes_and_revokes_only_that_profile(revoked: list[str]) -> None:
    a = store(ORG_A, org_name="Acme", current=True)
    b = store(ORG_B, org_name="Bravo")

    result = runner.invoke(cli_main.app, ["logout", "--org", "Bravo"])

    assert result.exit_code == 0, result.output
    after = auth_file.load_profiles()
    assert after is not None and [p.key for p in after.profiles] == [a.key]
    assert after.current == a.key
    assert revoked == [b.token]


def test_logout_without_options_signs_out_of_the_current_org_only(revoked: list[str]) -> None:
    a = store(ORG_A, org_name="Acme", current=True)
    b = store(ORG_B, org_name="Bravo")
    result = runner.invoke(cli_main.app, ["logout"])
    assert result.exit_code == 0, result.output
    after = auth_file.load_profiles()
    assert after is not None and [p.key for p in after.profiles] == [b.key]
    assert revoked == [a.token]
    assert "Current organization: Bravo" in result.output


def test_logout_all_removes_every_profile(revoked: list[str]) -> None:
    store(ORG_A, current=True)
    store(ORG_B)
    result = runner.invoke(cli_main.app, ["logout", "--all"])
    assert result.exit_code == 0, result.output
    assert auth_file.load_profiles() is None
    assert len(revoked) == 2


def test_logout_of_an_org_with_no_sign_in_changes_nothing(revoked: list[str]) -> None:
    store(ORG_A, current=True)
    before = auth_file.AUTH_FILE_PATH.read_bytes()
    result = runner.invoke(cli_main.app, ["logout", "--org", "Zeta"])
    assert result.exit_code == 1
    assert auth_file.AUTH_FILE_PATH.read_bytes() == before
    assert revoked == []


@pytest.mark.parametrize(
    ("asked", "message"),
    [
        pytest.param("Acme", "named Acme", id="a-name-two-orgs-share"),
        pytest.param(ORG_B, f"No sign-in for {ORG_B}", id="an-org-stored-only-at-another-api"),
    ],
)
def test_logout_of_an_org_this_api_cannot_name_removes_nothing(
    revoked: list[str], asked: str, message: str
) -> None:
    store(ORG_A, org_name="Acme", current=True)
    store("abcdef12-3456-4789-8abc-def123456789", org_name="ACME")
    store(ORG_B, org_name="Bravo", api_url=STAGING)
    before = auth_file.AUTH_FILE_PATH.read_bytes()

    result = runner.invoke(cli_main.app, ["logout", "--org", asked])

    assert result.exit_code == 1
    assert message in result.output
    assert auth_file.AUTH_FILE_PATH.read_bytes() == before
    assert revoked == []


# --- project unpin --------------------------------------------------------------------


def test_project_unpin_releases_the_pin(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    project = ProjectDirectory(project_root / ".alkera")
    project.cloud_binding().write(CloudBinding(api_url=API, org_team_id=ORG_A, org_name="Acme"))

    result = runner.invoke(cli_main.app, ["project", "unpin", "-p", str(project_root)])

    assert result.exit_code == 0, result.output
    assert project.cloud_binding().read() is None
    again = runner.invoke(cli_main.app, ["project", "unpin", "-p", str(project_root)])
    assert "not linked" in again.output
