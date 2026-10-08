"""The desktop preferences file keeps the model-naming preferences per org.

``model_efforts``, ``default_chat_model`` and ``default_chat_effort`` name
something in one org's model catalog. A machine signed in to several orgs keeps
one copy of them per org in ``~/.alkera/preferences.yml`` (``orgs``), read and
written as the org the caller's sign-in acts in; every other preference is
shared. A file written before it was kept per org has its values moved into the
first org that reads it, and no other org ever reads them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from _profiles import ORG_A, ORG_B, store
from alkera_cli.account.binding import pin_project
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.host import paths
from alkera_cli.preferences import chat_defaults
from alkera_cli.preferences import user as preferences_file
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.preferences import DesktopPreferences, Preferences


@pytest.fixture(autouse=True)
def isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "alkera-home"
    home.mkdir()
    monkeypatch.setattr(paths, "PREFERENCES_FILE_PATH", home / "preferences.yml")
    monkeypatch.setattr(paths, "PREFERENCES_LOCK_PATH", home / ".preferences.lock")
    return home


def _file(home: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((home / "preferences.yml").read_text())
    return data


def _set(org: str | None, **fields: Any) -> Preferences:
    def mutate(current: Preferences) -> Preferences:
        return Preferences.model_validate({**current.model_dump(), **fields})

    return preferences_file.update_preferences(mutate, org_id=org)


@pytest.mark.parametrize(("writer", "reader"), [(ORG_A, ORG_B), (ORG_B, ORG_A)])
def test_a_model_preference_saved_in_one_org_is_not_read_in_the_other(
    writer: str, reader: str
) -> None:
    _set(writer, model_efforts={"gpt-5": "high"}, default_chat_model="gpt-5")
    mine = preferences_file.load_preferences(writer)
    other = preferences_file.load_preferences(reader)
    assert (mine.model_efforts, mine.default_chat_model) == ({"gpt-5": "high"}, "gpt-5")
    assert (other.model_efforts, other.default_chat_model) == ({}, None)


def test_a_shared_preference_saved_in_one_org_is_read_everywhere() -> None:
    _set(ORG_A, theme="alkera-slate", show_banner=False)
    for org in (ORG_B, None):
        prefs = preferences_file.load_preferences(org)
        assert (prefs.theme, prefs.show_banner) == ("alkera-slate", False)


def test_each_org_keeps_its_own_copy_through_the_others_writes(isolate_home: Path) -> None:
    _set(ORG_A, default_chat_model="claude-a")
    _set(ORG_B, default_chat_model="gpt-b")
    _set(ORG_A, show_banner=False)
    assert preferences_file.load_preferences(ORG_A).default_chat_model == "claude-a"
    assert preferences_file.load_preferences(ORG_B).default_chat_model == "gpt-b"
    orgs = _file(isolate_home)["orgs"]
    assert orgs[ORG_A]["default_chat_model"] == "claude-a"
    assert orgs[ORG_B]["default_chat_model"] == "gpt-b"


def test_the_org_key_is_the_canonical_id(isolate_home: Path) -> None:
    """A sign-in may spell the org id in upper case; it is one org either way."""
    _set(ORG_A.upper(), default_chat_model="claude-a")
    assert preferences_file.load_preferences(ORG_A).default_chat_model == "claude-a"
    assert list(_file(isolate_home)["orgs"]) == [ORG_A]


def test_a_never_per_org_file_moves_into_the_first_org_that_reads_it(isolate_home: Path) -> None:
    """The flat values belong to the org the machine was signed in to; the first
    org to read takes them, a second org reads its defaults, and the flat copy
    stays in the file for an older CLI."""
    (isolate_home / "preferences.yml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0.0",
                "default_chat_model": "claude-flat",
                "model_efforts": {"claude-flat": "low"},
                "theme": "alkera-light",
            }
        )
    )
    first = preferences_file.load_preferences(ORG_A)
    assert (first.default_chat_model, first.model_efforts) == (
        "claude-flat",
        {"claude-flat": "low"},
    )
    on_disk = _file(isolate_home)
    assert on_disk["orgs"][ORG_A]["default_chat_model"] == "claude-flat"
    assert on_disk["default_chat_model"] == "claude-flat", "the flat copy is kept"
    second = preferences_file.load_preferences(ORG_B)
    assert (second.default_chat_model, second.model_efforts) == (None, {})
    assert second.theme == "alkera-light"


def test_a_write_moves_a_never_per_org_file_too(isolate_home: Path) -> None:
    """A writer that reaches the file before any reader does the same move, so
    its own write never drops the values the file already held."""
    (isolate_home / "preferences.yml").write_text(
        yaml.safe_dump({"schema_version": "2.0.0", "model_efforts": {"claude-flat": "low"}})
    )
    _set(ORG_A, show_banner=False)
    assert preferences_file.load_preferences(ORG_A).model_efforts == {"claude-flat": "low"}
    assert preferences_file.load_preferences(ORG_B).model_efforts == {}


def test_a_read_with_no_file_writes_none(isolate_home: Path) -> None:
    assert preferences_file.load_preferences(ORG_A) == Preferences()
    assert not (isolate_home / "preferences.yml").exists()


def test_without_an_org_the_flat_copy_is_read_and_written(isolate_home: Path) -> None:
    """Nothing signed in: the file reads and writes as it did before it was
    kept per org, and every org's copy survives the write."""
    _set(ORG_A, default_chat_model="claude-a")
    _set(None, default_chat_model="flat-model")
    assert preferences_file.load_preferences(None).default_chat_model == "flat-model"
    assert preferences_file.load_preferences(ORG_A).default_chat_model == "claude-a"


def test_an_org_write_leaves_the_flat_copy_as_it_was(isolate_home: Path) -> None:
    _set(None, default_chat_model="flat-model")
    _set(ORG_A, default_chat_model="claude-a")
    on_disk = _file(isolate_home)
    assert on_disk["default_chat_model"] == "flat-model"
    assert on_disk["schema_version"] == DesktopPreferences.SCHEMA_VERSION


def test_the_per_org_map_never_reaches_a_reader() -> None:
    """A reader is handed one document; the map of other orgs' copies is the
    file's own business and must not ride out through the daemon."""
    _set(ORG_A, default_chat_model="claude-a")
    _set(ORG_B, default_chat_model="gpt-b")
    for org in (ORG_A, ORG_B, None):
        assert "orgs" not in preferences_file.load_preferences(org).model_dump()


def test_a_correction_lands_in_the_org_whose_catalog_made_it() -> None:
    """A stale default is corrected against the catalog of the org it is that
    org's default in; another org's saved pick is not touched."""
    _set(ORG_A, default_chat_model="retired")
    _set(ORG_B, default_chat_model="retired")
    offered = [GatewayModel(id="claude-new", display_name="Claude", wire="anthropic")]
    resolved = chat_defaults.resolve_and_persist_chat_defaults(offered, org_id=ORG_A)
    assert resolved.model_id == "claude-new"
    assert preferences_file.load_preferences(ORG_A).default_chat_model is None
    assert preferences_file.load_preferences(ORG_B).default_chat_model == "retired"


def test_set_preference_writes_the_orgs_copy() -> None:
    preferences_file.set_preference("default_chat_model", "gpt-5", org_id=ORG_B)
    assert preferences_file.load_preferences(ORG_B).default_chat_model == "gpt-5"
    assert preferences_file.load_preferences(ORG_A).default_chat_model is None


# --- which org a project reads ------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> ProjectDirectory:
    return ProjectDirectory(tmp_path / "workspace" / ".alkera")


def test_a_pinned_project_reads_its_pins_org_not_the_current_one(
    project: ProjectDirectory,
) -> None:
    a = store(ORG_A)
    store(ORG_B, current=True)
    pin_project(project, a)
    assert preferences_file.preferences_org(project) == ORG_A
    assert preferences_file.preferences_org(None) == ORG_B


def test_with_nothing_signed_in_no_org_is_read(project: ProjectDirectory) -> None:
    assert preferences_file.preferences_org(project) is None


def test_a_project_pinned_to_an_org_with_no_sign_in_reads_no_org(
    project: ProjectDirectory,
) -> None:
    from alkera_cli.account import auth_file

    a = store(ORG_A)
    store(ORG_B, current=True)
    pin_project(project, a)
    auth_file.remove_profile(a.key)
    assert preferences_file.preferences_org(project) is None


def test_a_chats_slash_commands_read_and_set_as_its_bound_profiles_org() -> None:
    """A chat bound to A's profile (while B is current) reads and sets A's
    copy; a credential that names no org (a cloud chat's own token) reads and
    sets the file's top-level copy."""
    from alkera_cli.account.binding import FixedCredential, ProfileBinding

    on_a = ProfileBinding(store(ORG_A), "current")
    store(ORG_B, current=True)
    preferences_file.set_preference_as(on_a, "default_chat_model", "claude-a")
    assert preferences_file.load_preferences_as(on_a).default_chat_model == "claude-a"
    assert preferences_file.load_preferences(ORG_B).default_chat_model is None
    token_only = FixedCredential("tok")
    preferences_file.set_preference_as(token_only, "default_chat_model", "flat-model")
    assert preferences_file.load_preferences(None).default_chat_model == "flat-model"
    assert preferences_file.load_preferences_as(None).default_chat_model == "flat-model"
    assert preferences_file.load_preferences(ORG_A).default_chat_model == "claude-a"
