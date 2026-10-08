"""Auto-discovering lineage tests for the persisted actor document.

Walks every fixture under `packages/api-core/tests/fixtures/authz/v*/` and asserts the
**current** `ActorChainRecord` reader loads it. The record is the `actor`
document on every event outbox row and org audit event, so a reader must
keep loading whatever a past writer emitted.

If a fixture stops loading, a backwards-compat break slipped in. The fix is
a `MIGRATIONS` entry from that old version to the current one — never an
edit to the fixture. A fresh fixture directory comes from
`packages/api-core/tests/fixtures/authz/generate.py`.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alkera_core.authz import ActorChainRecord, PrincipalRecord

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "authz"


def _discover() -> list[pytest.param]:
    """Every `*.json` under each `v<X_Y_Z>/`, named in the test id."""
    out: list[pytest.param] = []
    if not FIXTURE_ROOT.is_dir():
        return out
    for version_dir in sorted(FIXTURE_ROOT.iterdir()):
        if not version_dir.is_dir() or not version_dir.name.startswith("v"):
            continue
        for fixture in sorted(version_dir.rglob("*.json")):
            rel = fixture.relative_to(version_dir).with_suffix("").as_posix()
            out.append(pytest.param(fixture, id=f"{version_dir.name}/{rel}"))
    return out


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "authz_fixture_generate", FIXTURE_ROOT / "generate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("fixture_path", _discover())
def test_actor_chain_fixture_loads(fixture_path: Path) -> None:
    """Every historical writer's output loads with the current reader and
    carries the invariants a consumer relies on."""
    payload: dict[str, Any] = json.loads(fixture_path.read_text())
    record = ActorChainRecord.model_validate(payload)
    assert record.schema_version == ActorChainRecord.SCHEMA_VERSION
    assert record.chain, "a persisted chain is never empty"
    assert record.chain[-1] == record.acting
    assert all(isinstance(link, PrincipalRecord) for link in record.chain)
    if record.delegating_user is not None:
        assert record.chain[0] == record.delegating_user
        assert record.delegating_user.kind == "user"
    assert (record.agent_id_asserted_by == "client") is (record.acting.kind == "agent")


@pytest.mark.parametrize("fixture_path", _discover())
def test_actor_chain_fixture_re_dump_re_loads(fixture_path: Path) -> None:
    """Load → dump → load is stable and always emits the current version."""
    record = ActorChainRecord.model_validate(json.loads(fixture_path.read_text()))
    dumped = record.model_dump(mode="json")
    assert dumped["schema_version"] == ActorChainRecord.SCHEMA_VERSION
    assert ActorChainRecord.model_validate(dumped) == record


@pytest.mark.parametrize("fixture_path", _discover())
def test_unknown_fields_from_a_newer_writer_survive(fixture_path: Path) -> None:
    """The forward-compat guarantee: fields this reader does not know ride
    along on round-trip, at the top level and inside an embedded principal."""
    payload: dict[str, Any] = json.loads(fixture_path.read_text())
    payload["future_top_level"] = {"x": 1}
    payload["acting"]["future_link_field"] = "y"
    dumped = ActorChainRecord.model_validate(payload).model_dump(mode="json")
    assert dumped["future_top_level"] == {"x": 1}
    assert dumped["acting"]["future_link_field"] == "y"


def test_corpus_is_non_empty() -> None:
    """An empty corpus would make every lineage test pass vacuously."""
    assert _discover(), "no fixtures under packages/api-core/tests/fixtures/authz/"


def test_corpus_covers_every_credential_shape() -> None:
    """The current version directory holds one fixture per shape the backend
    resolves, so a new shape cannot ship without its lineage evidence."""
    generate = _load_generator()
    current = FIXTURE_ROOT / f"v{ActorChainRecord.SCHEMA_VERSION.replace('.', '_')}"
    assert current.is_dir(), f"missing fixture directory {current.name}"
    on_disk = {p.stem for p in current.glob("*.json")}
    assert on_disk == set(generate.contexts())
    assert {"user", "agent", "service_ci", "service_proxy", "pat"} <= on_disk


def test_current_writer_output_is_captured(tmp_path: Path) -> None:
    """The committed fixtures for the current version are byte-identical to
    what the writer emits today. A shape change that forgets to bump the
    version and regenerate fails here instead of silently rewriting history."""
    generate = _load_generator()
    fresh_dir = generate.regenerate(tmp_path)
    committed_dir = FIXTURE_ROOT / fresh_dir.name
    assert committed_dir.is_dir(), f"run generate.py: {committed_dir.name} is missing"
    fresh = {p.name: p.read_bytes() for p in fresh_dir.glob("*.json")}
    committed = {p.name: p.read_bytes() for p in committed_dir.glob("*.json")}
    assert fresh == committed
