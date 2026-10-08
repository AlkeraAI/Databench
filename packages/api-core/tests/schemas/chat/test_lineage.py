"""Auto-discovering lineage tests — the backward-compat regression net.

Walks every fixture under `packages/api-core/tests/fixtures/chat/v*/` and asserts the
**current** reader can load it without exceptions. This is the
mechanical enforcement of the rule in `CLAUDE.md`:

> Whenever a `VersionedModel` subclass changes shape, bump
> `SCHEMA_VERSION` and add a new fixture under
> `packages/api-core/tests/fixtures/chat/v<NEW>/`. Never delete or edit old fixtures —
> they're the regression history.

If this test ever fails, it means the current reader can't load some
historical writer's output. That's a backwards-compat break. The fix
is to add a `MIGRATIONS` entry from that old version to the current.

Adding a fresh fixture is a one-liner: rerun
`packages/api-core/tests/fixtures/chat/generate.py` and commit.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alkera_core.schemas.chat import ChatManifest, Event, Part, TaskList
from pydantic import TypeAdapter

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "chat"


def _discover(subdir: str) -> list[pytest.param]:
    """Discover every `*.json` under each `v<X_Y_Z>/<subdir>/`. Returns
    pytest params that name the fixture in the test ID for traceability.
    """
    out: list[pytest.param] = []
    if not FIXTURE_ROOT.is_dir():
        return out
    for version_dir in sorted(FIXTURE_ROOT.iterdir()):
        if not version_dir.is_dir() or not version_dir.name.startswith("v"):
            continue
        category_dir = version_dir / subdir
        if not category_dir.is_dir():
            continue
        for fixture in sorted(category_dir.glob("*.json")):
            label = f"{version_dir.name}/{subdir}/{fixture.stem}"
            out.append(pytest.param(fixture, id=label))
    return out


EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)
PART_ADAPTER: TypeAdapter[Part] = TypeAdapter(Part)


@pytest.mark.parametrize("fixture_path", _discover("events"))
def test_event_fixture_loads(fixture_path: Path) -> None:
    """Every event fixture in every historical version directory must
    load with the current reader."""
    payload: dict[str, Any] = json.loads(fixture_path.read_text())
    # No exception = pass. The test ID names which fixture failed.
    EVENT_ADAPTER.validate_python(payload)


@pytest.mark.parametrize("fixture_path", _discover("parts"))
def test_part_fixture_loads(fixture_path: Path) -> None:
    payload: dict[str, Any] = json.loads(fixture_path.read_text())
    PART_ADAPTER.validate_python(payload)


@pytest.mark.parametrize("fixture_path", _discover("manifest"))
def test_manifest_fixture_loads(fixture_path: Path) -> None:
    payload: dict[str, Any] = json.loads(fixture_path.read_text())
    ChatManifest.model_validate(payload)


@pytest.mark.parametrize("fixture_path", _discover("tasks"))
def test_tasks_fixture_loads(fixture_path: Path) -> None:
    """Every task-list fixture in every historical version must load with the
    current reader (the unified TODO system's backward-compat net)."""
    payload: dict[str, Any] = json.loads(fixture_path.read_text())
    TaskList.model_validate(payload)


def test_corpus_is_non_empty() -> None:
    """Sanity check — if the corpus is empty, every lineage test
    passes vacuously, which would be a silent regression net. Failing
    here forces someone to regenerate fixtures on a first-time setup."""
    events = _discover("events")
    parts = _discover("parts")
    manifests = _discover("manifest")
    tasks = _discover("tasks")
    assert events, "no event fixtures under packages/api-core/tests/fixtures/chat/"
    assert parts, "no part fixtures under packages/api-core/tests/fixtures/chat/"
    assert manifests, "no manifest fixtures under packages/api-core/tests/fixtures/chat/"
    assert tasks, "no task fixtures under packages/api-core/tests/fixtures/chat/"


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "chat_fixture_generate", FIXTURE_ROOT / "generate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _corpus_bytes(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*.json")}


def test_regenerate_never_rewrites_a_committed_corpus(tmp_path: Path) -> None:
    """A committed corpus is a past writer's output, so a regenerate may
    only ADD a directory. When today's shapes differ from what is on disk
    at the stamp they name, the fresh corpus belongs in a new directory —
    rewriting the old one erases the evidence the lineage tests read."""
    root = tmp_path / "chat"
    shutil.copytree(FIXTURE_ROOT, root)
    before = _corpus_bytes(root)

    fresh_dir = _load_generator().regenerate(root)

    after = _corpus_bytes(root)
    assert {name: after.get(name) for name in before} == before
    assert list(fresh_dir.rglob("*.json")), "the regenerate wrote no corpus"


def test_current_writer_output_is_captured(tmp_path: Path) -> None:
    """The committed corpus at today's stamp is byte-identical to what the
    writer emits now, so a shape change that forgets to regenerate fails
    here instead of drifting silently."""
    root = tmp_path / "chat"
    shutil.copytree(FIXTURE_ROOT, root)
    fresh_dir = _load_generator().regenerate(root)
    committed_dir = FIXTURE_ROOT / fresh_dir.name
    assert committed_dir.is_dir(), f"run generate.py: {committed_dir.name} is missing"
    fresh = {p.relative_to(fresh_dir): p.read_bytes() for p in fresh_dir.rglob("*.json")}
    committed = {
        p.relative_to(committed_dir): p.read_bytes() for p in committed_dir.rglob("*.json")
    }
    assert fresh == committed
