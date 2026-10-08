"""Regenerate the Files golden corpus at the CURRENT writer version.

Run from the repo root::

    uv run python packages/api-core/tests/fixtures/files/generate.py

Every persisted Files shape (ACE bodies, history snapshots,
upload session state, sidecars, delta tokens, journal records, scan verdicts)
registers itself in :data:`FIXTURES`, and the script writes one JSON document
per entry into ``v<SCHEMA_VERSION>/<name>.json`` with a ``$model`` key naming
the class that wrote it. The auto-discovering lineage test
(``packages/api-core/tests/files/test_files_lineage.py``) then loads every historical corpus with
today's reader — the day it fails is the day a backwards-compatibility
regression slipped in.

NEVER edit or delete an old fixture. A shape that changed gets a bumped
``SCHEMA_VERSION``, a ``MIGRATIONS`` entry and a NEW directory here.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Final

from alkera_core.versioning import VersionedModel
from pydantic import Field

#: The dotted module name this file is published under. It is not importable as
#: a package (the tests tree has no ``__init__.py``), so the lineage test loads
#: it by path and registers it here before resolving any ``$model`` key.
GENERATOR_MODULE: Final = "alkera_files_fixture_generator"

MODEL_KEY: Final = "$model"

_T: Final = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class _ProbeRecord(VersionedModel):
    """The corpus's self-test: a persisted shape that exists only to prove the
    pipeline works end to end while the real Files shapes are still being built.

    It stays here forever. When the first real shape lands it joins
    :data:`FIXTURES` beside this one, and the probe keeps proving that a model
    defined outside ``alkera_core`` still round-trips through the generator, the
    ``$model`` resolver and the lineage reader.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    name: str
    size: int = 0
    created_at: datetime | None = None
    tags: list[str] = Field(default_factory=list)


def _probe() -> _ProbeRecord:
    return _ProbeRecord(
        name="probe",
        size=65_536,
        created_at=_T,
        tags=["golden", "self-test"],
        metadata={"why": "proves the generator, the resolver and the reader agree"},
    )


def _mount_record() -> VersionedModel:
    """One mount as the machine that took the lease writes it to disk."""
    from alkera_cli.files.mount import MountRecord

    return MountRecord(
        instance_id="0f2c6b1e4a7d4c1fa1b2c3d4e5f60718",
        drive_id="1f8f6c2a-6f4a-4a1e-9a3a-9f0a1b2c3d4e",
        node_id="2a9b7d3b-7a5b-4b2f-8b4b-af1b2c3d4e5f",
        org_path="Shared/proj",
        local_root="/Users/ana/work/proj",
        machine="ana-macbook",
        purpose="mount",
        epoch=3,
        pid=4242,
        acquired_at=_T.isoformat(),
        expires_at=_T.isoformat(),
        heartbeat_every=15.0,
        sync_interval=30.0,
    )


def _node_map() -> VersionedModel:
    """Which node each file of a held folder is, as its holder writes it down."""
    from alkera_cli.files.nodemap import KnownNode, NodeMap

    return NodeMap(
        files={
            "notes.md": KnownNode(
                node_id="3b0c8e4c-8b6c-4c3a-9c5c-b02c3d4e5f60",
                size=41,
                content_hash="af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262",
                etag="3",
                etag_hash="af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262",
            ),
            "scratch/out.csv": KnownNode(node_id="4c1d9f5d-9c7d-4d4b-ad6d-c13d4e5f6071"),
        }
    )


FIXTURES: dict[str, tuple[type[VersionedModel], Callable[[], VersionedModel]]] = {
    "probe_record": (_ProbeRecord, _probe),
    "mount_record": (type(_mount_record()), _mount_record),
    "node_map": (type(_node_map()), _node_map),
}
"""``name -> (model class, factory)``. A new persisted shape adds one line.

``mount_record`` is registered by hand rather than discovered because the CLI
is where a leased folder's identity is persisted — ``alkera_cli.files.mount``
is not under :data:`SCHEMA_PACKAGE` — and a persisted shape with no fixture has
no lineage net at all."""

SCHEMA_PACKAGE: Final = "alkera_core.schemas.files"

EXAMPLES_ATTR: Final = "FIXTURE_EXAMPLES"
"""What a schema module publishes: ``[(name, factory)]``, discovered below."""


def discovered() -> dict[str, tuple[type[VersionedModel], Callable[[], VersionedModel]]]:
    """Every ``FIXTURE_EXAMPLES`` entry under :data:`SCHEMA_PACKAGE`.

    Discovery rather than a hand-kept list, because the failure mode of a list
    is a shape that quietly ships with no fixture and therefore no lineage net.
    A new schema module is covered by existing.
    """
    package = importlib.import_module(SCHEMA_PACKAGE)
    out: dict[str, tuple[type[VersionedModel], Callable[[], VersionedModel]]] = {}
    for info in pkgutil.iter_modules(list(package.__path__)):
        module = importlib.import_module(f"{SCHEMA_PACKAGE}.{info.name}")
        examples: list[tuple[str, Callable[[], VersionedModel]]] = getattr(
            module, EXAMPLES_ATTR, []
        )
        for name, factory in examples:
            key = f"{info.name}.{name}"
            if key in out:
                raise RuntimeError(f"duplicate fixture name {key!r}")
            out[key] = (type(factory()), factory)
    return out


def all_fixtures() -> dict[str, tuple[type[VersionedModel], Callable[[], VersionedModel]]]:
    """The registered probe plus everything discovered in the schema package."""
    merged = dict(FIXTURES)
    for key, entry in discovered().items():
        if key in merged:
            raise RuntimeError(f"discovered fixture {key!r} collides with a registered one")
        merged[key] = entry
    return merged


def dotted_path(model_type: type[VersionedModel]) -> str:
    """The importable ``module.QualName`` a lineage reader resolves ``$model`` with."""
    module = model_type.__module__
    if module in ("__main__", "generate", GENERATOR_MODULE):
        module = GENERATOR_MODULE
    return f"{module}.{model_type.__qualname__}"


def version_dir(root: Path, version: str) -> Path:
    return root / f"v{version.replace('.', '_')}"


def render(name: str) -> tuple[Path, str]:
    """The relative path and exact bytes the fixture ``name`` is written as."""
    model_type, factory = all_fixtures()[name]
    payload: dict[str, Any] = factory().model_dump(mode="json")
    payload[MODEL_KEY] = dotted_path(model_type)
    relative = Path(f"v{model_type.SCHEMA_VERSION.replace('.', '_')}") / f"{name}.json"
    return relative, json.dumps(payload, indent=2, sort_keys=True) + "\n"


def regenerate(root: Path | None = None) -> list[Path]:
    """Write every registered fixture under ``root``; return the files written."""
    root = root or Path(__file__).resolve().parent
    written: list[Path] = []
    for name in sorted(all_fixtures()):
        relative, text = render(name)
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        written.append(target)
    return written


if __name__ == "__main__":
    for path in regenerate():
        print(f"wrote {path}")
