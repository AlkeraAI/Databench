"""Auto-discovering lineage tests for the persisted workspace-object shapes.

Two groups, both persisted and both read back by a process that did not write
them: ``specs`` is what lands in a ``workspace_objects.spec`` column or in a
blob a daemon wrote, and ``transcript`` is what lands in
``chat_messages.payload`` or rides the chat document as a relay body. A reader
must keep loading whatever a past writer emitted, so this walks every fixture
under ``packages/api-core/tests/fixtures/objects/v*/`` with today's reader; the day it fails is
the day a backward-compat break slipped in, and the fix is a ``MIGRATIONS``
entry, never an edit to a fixture.

The basename is ``test_objects_lineage`` rather than ``test_lineage``: the
chat schemas already own that name, and two test modules sharing a basename
abort the whole root pytest run at collection.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alkera_core.schemas.objects import (
    CHAT_RELAY_ADAPTER,
    AnswerRelay,
    BlobHandle,
    ChatModelPin,
    ChatPromptRecord,
    ChatSpec,
    ChatTemplateSpec,
    ChatTranscriptEntry,
    ModelRelay,
    ModeRelay,
    PromoteRelay,
    PromptRelay,
    QueryParam,
    QuerySpec,
    RawRelay,
    Receipt,
    ReportConnection,
    ReportRendering,
    ReportSpec,
    ReportStep,
    ResultBlobEnvelope,
    ResultColumn,
    ResultSpec,
    RunQueryRelay,
    StopRelay,
    WorkspaceSpec,
)
from alkera_core.versioning import VersionedModel

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "objects"

#: Which reader loads which fixture, per group, by stem prefix (longest prefix
#: wins, so ``query_param`` is not read as ``query``).
READERS: dict[str, dict[str, type[VersionedModel]]] = {
    "specs": {
        "blob_handle": BlobHandle,
        "chat_template": ChatTemplateSpec,
        "chat": ChatSpec,
        "workspace": WorkspaceSpec,
        "model_pin": ChatModelPin,
        "query_param": QueryParam,
        "query": QuerySpec,
        "receipt": Receipt,
        "report_connection": ReportConnection,
        "report_rendering": ReportRendering,
        "report_step": ReportStep,
        "report": ReportSpec,
        "result_column": ResultColumn,
        "result": ResultSpec,
        "envelope": ResultBlobEnvelope,
    },
    "transcript": {
        "transcript_entry": ChatTranscriptEntry,
        "prompt_record": ChatPromptRecord,
        "relay_prompt": PromptRelay,
        "relay_answer": AnswerRelay,
        "relay_mode": ModeRelay,
        "relay_model": ModelRelay,
        "relay_run_query": RunQueryRelay,
        "relay_stop": StopRelay,
        "relay_promote": PromoteRelay,
        "relay_unknown": RawRelay,
    },
}

GROUPS = sorted(READERS)


def _discover(*groups: str) -> list[pytest.param]:
    out: list[pytest.param] = []
    if not FIXTURE_ROOT.is_dir():
        return out
    for version_dir in sorted(FIXTURE_ROOT.iterdir()):
        if not version_dir.is_dir() or not version_dir.name.startswith("v"):
            continue
        for group in groups or GROUPS:
            category = version_dir / group
            if not category.is_dir():
                continue
            for fixture in sorted(category.glob("*.json")):
                out.append(pytest.param(fixture, id=f"{version_dir.name}/{group}/{fixture.stem}"))
    return out


def _reader(fixture_path: Path) -> type[VersionedModel]:
    group = READERS.get(fixture_path.parent.name)
    assert group is not None, f"no reader group for {fixture_path.parent.name!r}"
    stem = fixture_path.stem
    for prefix in sorted(group, key=len, reverse=True):
        if stem == prefix or stem.startswith(prefix + "_"):
            return group[prefix]
    raise AssertionError(f"no reader registered for fixture {stem!r}")


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "objects_fixture_generate", FIXTURE_ROOT / "generate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("fixture_path", _discover())
def test_fixture_loads_and_re_dumps(fixture_path: Path) -> None:
    reader = _reader(fixture_path)
    payload: dict[str, Any] = json.loads(fixture_path.read_text())
    model = reader.model_validate(payload)
    assert model.schema_version == reader.SCHEMA_VERSION
    assert reader.model_validate(model.model_dump(mode="json")) == model


@pytest.mark.parametrize("fixture_path", _discover())
def test_unknown_fields_from_a_newer_writer_survive(fixture_path: Path) -> None:
    """The single most important rule of the base class: a field a newer writer
    added rides along instead of raising."""
    reader = _reader(fixture_path)
    payload: dict[str, Any] = json.loads(fixture_path.read_text())
    payload["future_field"] = {"x": 1}
    dumped = reader.model_validate(payload).model_dump(mode="json")
    assert dumped["future_field"] == {"x": 1}


@pytest.mark.parametrize(
    "fixture_path",
    [p for p in _discover("specs") if _reader(p.values[0]) is ResultSpec],
)
def test_unknown_fields_inside_a_nested_model_survive(fixture_path: Path) -> None:
    """The case above only ever adds a TOP-LEVEL field, so a plain
    ``BaseModel`` in a slot could drop what a newer writer put there while the
    spec round-tripped looking complete. The result's blob handle is exactly
    that slot: a storage move behind an unchanged handle is the moment it
    grows a field, and a cloud that silently ate it would lose the pointer."""
    reader = _reader(fixture_path)
    payload: dict[str, Any] = json.loads(fixture_path.read_text())
    handle = payload.get("payload")
    assert isinstance(handle, dict), f"{fixture_path.stem} carries no blob handle"
    handle["future_field"] = {"x": 1}
    dumped = reader.model_validate(payload).model_dump(mode="json")
    assert dumped["payload"]["future_field"] == {"x": 1}


@pytest.mark.parametrize(
    "fixture_path",
    [p for p in _discover("transcript") if p.values[0].stem.startswith("relay_")],
)
def test_a_relay_fixture_routes_to_its_own_variant(fixture_path: Path) -> None:
    """A relay is read through the union, so the bytes on the wire have to reach
    the variant the writer meant — including the kind nobody knows, which must
    land on the catch-all rather than raising on an older box."""
    expected = _reader(fixture_path)
    payload: dict[str, Any] = json.loads(fixture_path.read_text())
    assert type(CHAT_RELAY_ADAPTER.validate_python(payload)) is expected


@pytest.mark.parametrize("group", GROUPS)
def test_corpus_is_non_empty(group: str) -> None:
    """An empty corpus would make every lineage test pass vacuously."""
    assert _discover(group), f"no fixtures under packages/api-core/tests/fixtures/objects/*/{group}"


@pytest.mark.parametrize("group", GROUPS)
def test_corpus_covers_every_persisted_model(
    group: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A model added to a persisted surface cannot ship without its evidence."""
    generate = _load_generator()
    current = FIXTURE_ROOT / generate.regenerate(Path(tmp_path_factory.mktemp("corpus"))).name
    assert current.is_dir(), f"run generate.py: {current.name} is missing"
    covered = {type(model) for model in generate.corpus()[group].values()}
    assert covered == set(READERS[group].values())


def _corpus_bytes(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*.json")}


def test_regenerate_never_rewrites_a_committed_corpus(tmp_path: Path) -> None:
    """A committed corpus is a past writer's output: a regenerate may only
    ADD a directory. Today's shapes differing from what is on disk at the
    stamp they name means the fresh corpus belongs in a NEW directory —
    rewriting the old one erases the evidence the lineage tests read."""
    root = tmp_path / "objects"
    shutil.copytree(FIXTURE_ROOT, root)
    before = _corpus_bytes(root)

    fresh_dir = _load_generator().regenerate(root)

    after = _corpus_bytes(root)
    assert {name: after.get(name) for name in before} == before
    assert list(fresh_dir.rglob("*.json")), "the regenerate wrote no corpus"


def test_current_writer_output_is_captured(tmp_path: Path) -> None:
    """The committed fixtures are byte-identical to what the writer emits
    today, so a shape change that forgets to bump and regenerate fails here
    instead of silently rewriting history."""
    generate = _load_generator()
    root = tmp_path / "objects"
    shutil.copytree(FIXTURE_ROOT, root)
    fresh_dir = generate.regenerate(root)
    committed_dir = FIXTURE_ROOT / fresh_dir.name
    assert committed_dir.is_dir(), f"run generate.py: {committed_dir.name} is missing"
    fresh = {p.relative_to(fresh_dir): p.read_bytes() for p in fresh_dir.rglob("*.json")}
    committed = {
        p.relative_to(committed_dir): p.read_bytes() for p in committed_dir.rglob("*.json")
    }
    assert fresh == committed
