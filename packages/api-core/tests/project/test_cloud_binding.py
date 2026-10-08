"""The project's org pin (`.alkera/cloud.json`): its location, its persisted
shape across versions, and the comparison that decides whether a credential
may sync the project."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alkera_core.project import ProjectDirectory
from alkera_core.project.cloud_binding import CloudBinding

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cloud_binding"
API = "https://api.example.test"
ORG = "11111111-1111-4111-8111-111111111111"


def test_the_accessor_names_cloud_json_and_nothing_is_written_until_asked(tmp_path: Path) -> None:
    project = ProjectDirectory(tmp_path / ".alkera")
    store = project.cloud_binding()
    assert store.path == project.path / "cloud.json"
    assert not store.path.exists()
    assert store.read() is None


def test_a_written_binding_reads_back_and_clears(tmp_path: Path) -> None:
    store = ProjectDirectory(tmp_path / ".alkera").cloud_binding()
    store.write(CloudBinding(api_url=API, org_team_id=ORG, org_name="Acme"))
    read = store.read()
    assert read is not None and (read.org_team_id, read.org_name) == (ORG, "Acme")
    assert store.clear() is True
    assert store.read() is None
    assert store.clear() is False


@pytest.mark.parametrize(
    "content", ["{not json", "[1, 2]", '{"org_team_id": 5}'], ids=["garbage", "list", "mistyped"]
)
def test_an_unreadable_binding_is_a_binding_to_no_org(tmp_path: Path, content: str) -> None:
    store = ProjectDirectory(tmp_path / ".alkera").cloud_binding()
    store.path.write_text(content)
    read = store.read()
    assert read is not None, "a corrupt pin is not 'never synced'"
    assert not read.matches(api_url=API, org_team_id=ORG)


@pytest.mark.parametrize(
    ("api_url", "org_team_id", "matches"),
    [
        pytest.param(API, ORG, True, id="same"),
        pytest.param(API + "/", ORG, True, id="trailing-slash"),
        pytest.param(API, "22222222-2222-4222-8222-222222222222", False, id="other-org"),
        pytest.param("https://other.example.test", ORG, False, id="other-api"),
        pytest.param(API, "", False, id="unknown-org"),
        pytest.param(API, ORG.replace("-", ""), True, id="bare-hex-spelling"),
    ],
)
def test_matches(api_url: str, org_team_id: str, matches: bool) -> None:
    binding = CloudBinding(api_url=API, org_team_id=ORG, org_name="Acme")
    assert binding.matches(api_url=api_url, org_team_id=org_team_id) is matches


def test_a_binding_with_no_org_matches_nothing() -> None:
    assert not CloudBinding(api_url=API).matches(api_url=API, org_team_id="")


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("v*.json")), ids=lambda p: p.name)
def test_every_historical_fixture_loads_with_the_current_reader(fixture: Path) -> None:
    loaded = CloudBinding.model_validate(json.loads(fixture.read_text()))
    assert loaded.schema_version == CloudBinding.SCHEMA_VERSION
    assert loaded.matches(api_url=API, org_team_id=ORG)


def test_the_fixture_set_covers_the_current_writer() -> None:
    version = CloudBinding.SCHEMA_VERSION.replace(".", "_")
    assert (FIXTURES / f"v{version}.json").exists()


def test_an_unknown_field_from_a_newer_writer_survives(tmp_path: Path) -> None:
    store = ProjectDirectory(tmp_path / ".alkera").cloud_binding()
    store.path.write_text(
        json.dumps({"api_url": API, "org_team_id": ORG, "org_name": "Acme", "region": "eu"})
    )
    read = store.read()
    assert read is not None
    store.write(read)
    assert json.loads(store.path.read_text())["region"] == "eu"
