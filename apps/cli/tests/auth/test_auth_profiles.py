"""`auth.yml` as a list of profiles, and which profile an operation acts as.

The file is the one source of truth for every sign-in on the machine. These
tests pin the persisted shape (the old single-account file still reads; an old
reader still reads the new file), the write guarantees (0600, atomic, the
mirror always the current profile), and the resolution order that decides
which org a command, a project or a chat acts in.
"""

from __future__ import annotations

import os
import stat
import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from _profiles import API, ORG_A, ORG_B, USER, make_jwt, store
from alkera_cli.account import auth_file
from alkera_cli.account.auth_file import (
    AuthFileV2,
    NoProfileForOrgError,
    ProjectOrgMismatchError,
    load_profiles,
    profile_key,
    resolve_profile,
    resolve_profile_with_source,
)
from alkera_core.project import ProjectDirectory
from alkera_core.project.cloud_binding import CloudBinding
from pydantic import BaseModel

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "auth_file"


class _OldStoredAuth(BaseModel):
    """A copy of the pre-profile reader: what an older CLI or extension parses."""

    api_url: str
    token: str
    expires_at: datetime


def _project(tmp_path: Path, pin_org: str | None = None, name: str = "proj") -> ProjectDirectory:
    project = ProjectDirectory(tmp_path / name / ".alkera")
    if pin_org is not None:
        project.cloud_binding().write(
            CloudBinding(api_url=API, org_team_id=pin_org, org_name=f"org-{pin_org[:4]}")
        )
    return project


# --- the persisted shape ------------------------------------------------------


def test_a_pre_profile_file_reads_as_one_current_profile_keyed_by_its_claims() -> None:
    token = make_jwt(org=ORG_A)
    auth_file.AUTH_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    auth_file.AUTH_FILE_PATH.write_text(
        yaml.safe_dump({"api_url": API, "token": token, "expires_at": "2100-01-01T00:00:00Z"})
    )

    loaded = load_profiles()

    assert loaded is not None
    assert [p.key for p in loaded.profiles] == [profile_key(API, USER, ORG_A)]
    current = loaded.current_profile
    assert current is not None
    assert (current.user_id, current.org_team_id, current.token) == (USER, ORG_A, token)


def test_a_pre_profile_token_that_is_not_a_jwt_is_kept_with_its_identity_blank() -> None:
    auth_file.AUTH_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    auth_file.AUTH_FILE_PATH.write_text(
        yaml.safe_dump({"api_url": API, "token": "opaque", "expires_at": "2100-01-01T00:00:00Z"})
    )
    loaded = load_profiles()
    assert loaded is not None and loaded.current_profile is not None
    assert loaded.current_profile.token == "opaque"
    assert (loaded.current_profile.user_id, loaded.current_profile.org_team_id) == ("", "")


def test_an_old_reader_parses_the_new_file_as_the_current_profile() -> None:
    store(ORG_A, org_name="Acme")
    b = store(ORG_B, org_name="Bravo", current=True)

    old = _OldStoredAuth.model_validate(yaml.safe_load(auth_file.AUTH_FILE_PATH.read_text()))

    assert old.token == b.token
    assert old.api_url == b.api_url


def test_every_write_mirrors_the_current_profile_at_the_top_level() -> None:
    a = store(ORG_A, current=True)
    b = store(ORG_B)
    assert yaml.safe_load(auth_file.AUTH_FILE_PATH.read_text())["token"] == a.token

    auth_file.set_current(b.key)
    assert yaml.safe_load(auth_file.AUTH_FILE_PATH.read_text())["token"] == b.token

    auth_file.remove_profile(b.key)
    doc = yaml.safe_load(auth_file.AUTH_FILE_PATH.read_text())
    assert doc["token"] == a.token
    assert doc["current"] == a.key


def test_removing_the_last_profile_removes_the_file_so_old_readers_see_signed_out() -> None:
    a = store(ORG_A, current=True)
    auth_file.remove_profile(a.key)
    assert not auth_file.AUTH_FILE_PATH.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_the_file_is_written_0600() -> None:
    store(ORG_A, current=True)
    store(ORG_B)
    mode = stat.S_IMODE(os.stat(auth_file.AUTH_FILE_PATH).st_mode)
    assert mode == 0o600


def test_a_write_killed_before_the_rename_leaves_the_previous_file_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store(ORG_A, current=True)
    before = auth_file.AUTH_FILE_PATH.read_bytes()

    def _killed(*_args: object, **_kwargs: object) -> None:
        raise OSError("killed mid-write")

    monkeypatch.setattr(os, "replace", _killed)
    with pytest.raises(OSError, match="killed"):
        store(ORG_B, current=True)

    assert auth_file.AUTH_FILE_PATH.read_bytes() == before
    leftovers = [p for p in auth_file.AUTH_FILE_PATH.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("v*.yml")), ids=lambda p: p.name)
def test_every_historical_fixture_loads_with_the_current_reader(fixture: Path) -> None:
    loaded = AuthFileV2.parse(yaml.safe_load(fixture.read_text()))
    assert loaded.schema_version == AuthFileV2.SCHEMA_VERSION
    assert loaded.profiles, "every fixture holds at least one sign-in"
    current = loaded.current_profile
    assert current is not None and current.org_team_id == ORG_A
    for profile in loaded.profiles:
        assert profile.key == profile_key(profile.api_url, profile.user_id, profile.org_team_id)


def test_the_fixture_set_covers_the_current_writer() -> None:
    version = AuthFileV2.SCHEMA_VERSION.replace(".", "_")
    assert (FIXTURES / f"v{version}.yml").exists(), "run fixtures/auth_file/generate.py"


def test_an_unknown_field_from_a_newer_writer_survives_a_round_trip() -> None:
    a = store(ORG_A, current=True)
    doc = yaml.safe_load(auth_file.AUTH_FILE_PATH.read_text())
    doc["future_field"] = {"kept": True}
    doc["profiles"][0]["future_profile_field"] = 7
    auth_file.AUTH_FILE_PATH.write_text(yaml.safe_dump(doc))

    store(ORG_B)  # a write by this reader

    after = yaml.safe_load(auth_file.AUTH_FILE_PATH.read_text())
    assert after["future_field"] == {"kept": True}
    kept = next(p for p in after["profiles"] if p["key"] == a.key)
    assert kept["future_profile_field"] == 7


def test_the_org_header_name_matches_the_servers() -> None:
    from alkera_core.auth.tenancy import ORG_HEADER

    assert auth_file.ORG_HEADER == ORG_HEADER


def test_a_profile_is_keyed_by_its_tokens_claims_not_by_what_was_asked() -> None:
    profile = auth_file.profile_from_token(API, make_jwt(org=ORG_B), org_name="Asked for A")
    assert profile.org_team_id == ORG_B
    assert profile.key == profile_key(API, USER, ORG_B)


def test_an_identity_learned_later_rekeys_the_profile_and_keeps_it_current() -> None:
    auth_file.save_auth(
        auth_file.StoredAuth(api_url=API, token="opaque", expires_at=datetime(2100, 1, 1))
    )
    blank = load_profiles()
    assert blank is not None and blank.current_profile is not None
    old_key = blank.current_profile.key

    updated = auth_file.update_profile_identity(
        old_key, user_id=USER, org_team_id=ORG_A, email="a@x.com", org_name="Acme"
    )

    assert updated is not None and updated.key == profile_key(API, USER, ORG_A)
    after = load_profiles()
    assert after is not None and after.current == updated.key
    assert [p.key for p in after.profiles] == [updated.key]


# --- resolution ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("env_token", "flag", "env_org", "pin", "expected_org", "expected_source"),
    [
        pytest.param(True, ORG_B, ORG_B, ORG_B, "env", "token_env", id="env-token-beats-all"),
        pytest.param(False, ORG_B, ORG_A, None, ORG_B, "flag", id="flag-beats-env-org"),
        pytest.param(False, ORG_B, None, ORG_B, ORG_B, "flag", id="flag-agreeing-with-pin"),
        pytest.param(False, None, ORG_B, None, ORG_B, "env", id="env-org-beats-current"),
        pytest.param(False, None, None, ORG_B, ORG_B, "pin", id="pin-beats-current"),
        pytest.param(False, None, None, None, ORG_A, "current", id="current-last"),
    ],
)
def test_resolution_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_token: bool,
    flag: str | None,
    env_org: str | None,
    pin: str | None,
    expected_org: str,
    expected_source: str,
) -> None:
    store(ORG_A, org_name="Acme", current=True)
    store(ORG_B, org_name="Bravo")
    env_org_value = "44444444-4444-4444-8444-444444444444"
    if env_token:
        monkeypatch.setenv(auth_file.TOKEN_ENV, make_jwt(org=env_org_value))
    if env_org:
        monkeypatch.setenv(auth_file.ORG_ENV, env_org)
    auth_file.set_invocation_org(flag)
    project = _project(tmp_path, pin)

    if env_token and pin is not None:
        # The env token is for a third org: the pinned project refuses it.
        with pytest.raises(ProjectOrgMismatchError):
            resolve_profile_with_source(project=project)
        resolved = resolve_profile_with_source()
    else:
        resolved = resolve_profile_with_source(project=project)

    assert resolved is not None
    expected = env_org_value if expected_org == "env" else expected_org
    assert resolved.profile.org_team_id == expected
    assert resolved.source == expected_source


def test_an_org_is_found_by_name_without_case() -> None:
    store(ORG_A, org_name="Acme", current=True)
    store(ORG_B, org_name="Bravo")
    profile = resolve_profile(org="bRAVO")
    assert profile is not None and profile.org_team_id == ORG_B


def test_an_asked_for_org_with_no_sign_in_is_refused_with_the_login_hint() -> None:
    store(ORG_A, org_name="Acme", current=True)
    with pytest.raises(
        NoProfileForOrgError, match=r"No sign-in for Zeta\. Run `alkera login --org Zeta`\."
    ):
        resolve_profile(org="Zeta")


def test_a_project_pinned_to_an_org_with_no_sign_in_refuses_the_current_one(
    tmp_path: Path,
) -> None:
    store(ORG_B, org_name="Bravo", current=True)
    project = _project(tmp_path, ORG_A)
    pin = project.cloud_binding().read()
    assert pin is not None
    with pytest.raises(
        ProjectOrgMismatchError,
        match=rf"This project belongs to {pin.org_name}\. Run `alkera org switch {pin.org_name}`\.",
    ):
        resolve_profile(project=project)


def test_a_flag_for_another_org_than_the_pin_is_refused(tmp_path: Path) -> None:
    store(ORG_A, current=True)
    store(ORG_B)
    with pytest.raises(ProjectOrgMismatchError):
        resolve_profile(org=ORG_B, project=_project(tmp_path, ORG_A))


def test_a_pin_on_another_api_does_not_match_the_same_org_id(tmp_path: Path) -> None:
    store(ORG_A, current=True, api_url="https://other.example.test")
    with pytest.raises(ProjectOrgMismatchError):
        resolve_profile(project=_project(tmp_path, ORG_A))


def test_a_corrupt_pin_matches_no_sign_in(tmp_path: Path) -> None:
    store(ORG_A, current=True)
    project = _project(tmp_path)
    project.cloud_binding().path.write_text("{not json")
    with pytest.raises(ProjectOrgMismatchError):
        resolve_profile(project=project)


def test_a_profile_whose_org_is_unknown_never_satisfies_a_pin(tmp_path: Path) -> None:
    auth_file.save_auth(
        auth_file.StoredAuth(api_url=API, token="opaque", expires_at=datetime(2100, 1, 1))
    )
    with pytest.raises(ProjectOrgMismatchError):
        resolve_profile(project=_project(tmp_path, ORG_A))


def test_nothing_stored_resolves_to_none_even_for_a_pinned_project(tmp_path: Path) -> None:
    assert resolve_profile(project=_project(tmp_path, ORG_A)) is None


def test_the_compatibility_shim_returns_the_current_profile() -> None:
    store(ORG_A, current=True)
    b = store(ORG_B)
    auth_file.set_current(b.key)
    legacy = auth_file.load_auth()
    assert legacy is not None and legacy.token == b.token


# --- one spelling per id ---------------------------------------------------------
# A session token carries ids as bare hex; the API answers them hyphenated. Found
# against the local stack: without one spelling the same org read as two.


def test_a_token_with_bare_hex_ids_keys_the_profile_by_the_hyphenated_ids() -> None:
    token = make_jwt(sub=USER.replace("-", ""), org=ORG_A.replace("-", ""))
    profile = auth_file.profile_from_token(API, token)
    assert (profile.user_id, profile.org_team_id) == (USER, ORG_A)
    assert profile.key == profile_key(API, USER, ORG_A)


def test_an_org_named_in_either_spelling_finds_the_same_profile(tmp_path: Path) -> None:
    store(ORG_A, current=True, token=make_jwt(org=ORG_A.replace("-", "")))
    assert resolve_profile(org=ORG_A) is not None
    assert resolve_profile(org=ORG_A.replace("-", "").upper()) is not None
    project = _project(tmp_path)
    project.cloud_binding().write(CloudBinding(api_url=API, org_team_id=ORG_A.replace("-", "")))
    assert resolve_profile(project=project) is not None


def test_a_file_written_with_bare_hex_keys_reads_back_with_current_intact() -> None:
    hex_key = f"{API}|{USER.replace('-', '')}|{ORG_A.replace('-', '')}"
    auth_file.AUTH_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    auth_file.AUTH_FILE_PATH.write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0.0",
                "current": hex_key,
                "profiles": [
                    {
                        "key": hex_key,
                        "api_url": API,
                        "user_id": USER.replace("-", ""),
                        "org_team_id": ORG_A.replace("-", ""),
                        "token": "t",
                        "expires_at": "2100-01-01T00:00:00Z",
                    }
                ],
            }
        )
    )
    loaded = load_profiles()
    assert loaded is not None and loaded.current_profile is not None
    assert loaded.current_profile.key == profile_key(API, USER, ORG_A)
