"""The spec is a versioned persisted model: every fixture any version ever
wrote still loads, unknown fields from a newer writer survive a round trip,
and the current version has a fixture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.environment import parse_spec, summarize_spec
from alkera_cli.environment.recreate import TargetState, Tools, plan_recreate
from alkera_cli.environment.spec import EnvironmentSpec, normalize_name

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "environment"


def _fixtures() -> list[Any]:
    return [pytest.param(p, id=p.name) for p in sorted(FIXTURES.glob("v*.json"))]


def test_the_current_version_has_a_fixture() -> None:
    name = "v" + EnvironmentSpec.SCHEMA_VERSION.replace(".", "_") + ".json"
    assert (FIXTURES / name).is_file()


@pytest.mark.parametrize("path", _fixtures())
def test_every_fixture_loads_with_the_current_reader(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    spec = EnvironmentSpec.model_validate(raw)
    assert spec.packages, "a fixture with no packages proves nothing about packages"
    again = json.loads(spec.model_dump_json())
    assert again["schema_version"] == EnvironmentSpec.SCHEMA_VERSION
    assert len(again["packages"]) == len(raw["packages"])


def test_fields_from_a_newer_writer_survive_at_every_level() -> None:
    raw = json.loads((FIXTURES / "v1_0_0.json").read_text(encoding="utf-8"))
    raw["future_top"] = {"a": 1}
    raw["packages"][0]["future_pkg"] = "x"
    raw["conda"]["packages"][0]["future_conda"] = [1, 2]
    raw["indexes"][0]["future_index"] = True

    again = json.loads(EnvironmentSpec.model_validate(raw).model_dump_json())

    assert again["future_top"] == {"a": 1}
    assert again["packages"][0]["future_pkg"] == "x"
    assert again["conda"]["packages"][0]["future_conda"] == [1, 2]
    assert again["indexes"][0]["future_index"] is True


def test_an_unreadable_spec_reads_as_none() -> None:
    assert parse_spec("{not json") is None
    assert parse_spec('{"packages": "nope"}') is None


@pytest.mark.parametrize(
    "name,normalized",
    [("Foo_Bar", "foo-bar"), ("foo.bar", "foo-bar"), ("foo--_.bar", "foo-bar"), ("x", "x")],
)
def test_normalize_name(name: str, normalized: str) -> None:
    assert normalize_name(name) == normalized


def test_a_newer_writers_unknown_values_load_round_trip_and_are_skipped() -> None:
    raw = json.loads((FIXTURES / "v1_0_0.json").read_text(encoding="utf-8"))
    raw["env_kind"] = "pixi"
    raw["packages"].append({"name": "fromfuture", "version": "1", "source": "oci"})
    raw["project_files"][0]["kind"] = "pixi_lock"
    raw["indexes"].append({"url": "https://future.example/simple", "kind": "mirror"})
    raw["not_portable"].append({"kind": "gpu_driver", "name": "cuda"})

    spec = EnvironmentSpec.model_validate(raw)
    again = json.loads(spec.model_dump_json())

    assert again["env_kind"] == "pixi"
    assert again["packages"][-1]["source"] == "oci"
    assert again["project_files"][0]["kind"] == "pixi_lock"
    assert again["indexes"][-1]["kind"] == "mirror"
    assert again["not_portable"][-1]["kind"] == "gpu_driver"
    assert "gpu_driver" in summarize_spec(spec)

    state = TargetState(exists=True, python_version="3.12.4", implementation="cpython")
    plan = plan_recreate(spec, state, target_env="/e", target_root="/w", tools=Tools(uv="uv"))
    assert ("unknown_source", "fromfuture") in {(g.kind, g.name) for g in plan.gaps}
    assert plan.satisfied == 0
    files = next(s for s in plan.steps if s.purpose == "install_packages").command.files
    assert "future.example" not in files["requirements"]
    assert "fromfuture" not in files["requirements"]
