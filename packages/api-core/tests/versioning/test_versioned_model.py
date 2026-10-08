"""Tests for `alkera_core.versioning.base.VersionedModel`.

Covers:
- `extra="allow"` round-trip (unknown fields survive).
- Migration ladder runs in order; updates `schema_version` to current.
- Cycle detection in `migrate()`.
- `__init_subclass__` warns when SCHEMA_VERSION isn't overridden.
- `_emit_current_version` always writes the canonical version.
- `make_unknown_tag_discriminator` routes unknown tags to fallback.
"""

from __future__ import annotations

import warnings
from typing import Annotated, Any, ClassVar, Literal

import pytest
from alkera_core.versioning import (
    Migration,
    VersionedModel,
    make_unknown_tag_discriminator,
)
from pydantic import Discriminator, Field, Tag, TypeAdapter


class _Thing(VersionedModel):
    """Test fixture: a concrete versioned model with one typed field."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    name: str = ""


# ---------------------------------------------------------------------------
# extra="allow" round-trip
# ---------------------------------------------------------------------------


def test_unknown_fields_survive_round_trip() -> None:
    """Fields a newer writer added that we don't know about MUST ride
    through a load + re-dump unchanged. This is the forward-compat
    foundation.
    """
    payload = {
        "schema_version": "1.0.0",
        "name": "abc",
        "future_field": "a value the current schema doesn't know about",
        "nested_future": {"k": 1},
    }
    inst = _Thing.model_validate(payload)
    out = inst.model_dump(mode="json")
    assert out["future_field"] == "a value the current schema doesn't know about"
    assert out["nested_future"] == {"k": 1}


def test_metadata_bag_round_trip() -> None:
    inst = _Thing.model_validate({"name": "x", "metadata": {"experimental_flag": True}})
    out = inst.model_dump(mode="json")
    assert out["metadata"]["experimental_flag"] is True


# ---------------------------------------------------------------------------
# Migration ladder
# ---------------------------------------------------------------------------


def _migrate_1_0_to_1_1(d: dict[str, Any]) -> dict[str, Any]:
    return {**d, "extra_field": d.get("extra_field", "added-in-1.1"), "schema_version": "1.1.0"}


def _migrate_1_1_to_2_0(d: dict[str, Any]) -> dict[str, Any]:
    out = {**d}
    out["renamed_field"] = out.pop("extra_field", "default")
    out["schema_version"] = "2.0.0"
    return out


class _Ladder(VersionedModel):
    SCHEMA_VERSION: ClassVar[str] = "2.0.0"
    MIGRATIONS: ClassVar[dict[str, Migration]] = {
        "1.0.0": _migrate_1_0_to_1_1,
        "1.1.0": _migrate_1_1_to_2_0,
    }

    name: str = ""
    renamed_field: str = ""


def test_migration_ladder_runs_from_old_to_current() -> None:
    """A v1.0.0 payload should pass through the v1.0→1.1 migrator AND
    the v1.1→2.0 migrator before validation, ending at SCHEMA_VERSION."""
    inst = _Ladder.model_validate({"schema_version": "1.0.0", "name": "x"})
    assert inst.schema_version == "2.0.0"
    assert inst.renamed_field == "added-in-1.1"


def test_migration_skipped_when_already_current() -> None:
    inst = _Ladder.model_validate(
        {"schema_version": "2.0.0", "name": "x", "renamed_field": "explicit"}
    )
    assert inst.renamed_field == "explicit"


def test_migration_ladder_cycle_detection() -> None:
    """A cycle in the ladder must raise `RuntimeError` — caught in tests
    rather than infinite-looping in production."""

    class _Cyclic(VersionedModel):
        SCHEMA_VERSION: ClassVar[str] = "2.0.0"
        MIGRATIONS: ClassVar[dict[str, Migration]] = {
            "1.0.0": lambda d: {**d, "schema_version": "1.1.0"},
            # cycle: 1.1.0 → 1.0.0
            "1.1.0": lambda d: {**d, "schema_version": "1.0.0"},
        }

    with pytest.raises(RuntimeError, match="cycle"):
        _Cyclic.model_validate({"schema_version": "1.0.0"})


# ---------------------------------------------------------------------------
# Serialization always emits the current version
# ---------------------------------------------------------------------------


def test_serialized_version_is_canonical_even_if_field_is_mutated() -> None:
    """Even if the in-memory instance has a stale schema_version (e.g.
    someone mutated it), serialization must emit the canonical current
    version."""
    inst = _Thing(name="x")
    inst.schema_version = "0.9.9"  # validate_assignment lets this through
    out = inst.model_dump(mode="json")
    assert out["schema_version"] == "1.0.0"


# ---------------------------------------------------------------------------
# __init_subclass__ warning
# ---------------------------------------------------------------------------


def test_subclass_without_schema_version_warns() -> None:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")

        class _Forgotten(VersionedModel):
            name: str = ""

        # We DECLARED but didn't instantiate — declaration alone should warn.
        assert any("did not override SCHEMA_VERSION" in str(w.message) for w in captured), [
            str(w.message) for w in captured
        ]
        # Silence ruff unused-var: the test is the side effect of class creation.
        _ = _Forgotten


def test_subclass_with_schema_version_does_not_warn() -> None:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")

        class _OK(VersionedModel):
            SCHEMA_VERSION: ClassVar[str] = "1.0.0"

        assert not any("did not override SCHEMA_VERSION" in str(w.message) for w in captured)
        _ = _OK


# ---------------------------------------------------------------------------
# Unknown-tag discriminator fallback
# ---------------------------------------------------------------------------


class _KnownA(VersionedModel):
    SCHEMA_VERSION: ClassVar[str] = "1.0.0"
    type: Literal["a"] = "a"
    value: str = ""


class _KnownB(VersionedModel):
    SCHEMA_VERSION: ClassVar[str] = "1.0.0"
    type: Literal["b"] = "b"
    value: int = 0


class _RawFallback(VersionedModel):
    SCHEMA_VERSION: ClassVar[str] = "1.0.0"
    type: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


_tag = make_unknown_tag_discriminator({"a", "b"})

_Union = Annotated[
    (
        Annotated[_KnownA, Tag("a")]
        | Annotated[_KnownB, Tag("b")]
        | Annotated[_RawFallback, Tag("__unknown__")]
    ),
    Discriminator(_tag),
]


def test_known_tag_routes_to_matching_class() -> None:
    adapter = TypeAdapter(_Union)
    a = adapter.validate_python({"type": "a", "value": "hello"})
    assert isinstance(a, _KnownA)
    assert a.value == "hello"
    b = adapter.validate_python({"type": "b", "value": 42})
    assert isinstance(b, _KnownB)
    assert b.value == 42


def test_unknown_tag_routes_to_fallback() -> None:
    """A tag we've never seen before (e.g. a brand-new variant from a
    future writer) must NOT raise — it lands in the raw fallback."""
    adapter = TypeAdapter(_Union)
    parsed = adapter.validate_python({"type": "future_variant", "payload": {"some_data": 1}})
    assert isinstance(parsed, _RawFallback)
    assert parsed.type == "future_variant"
    assert parsed.payload == {"some_data": 1}


def test_missing_tag_routes_to_fallback() -> None:
    adapter = TypeAdapter(_Union)
    parsed = adapter.validate_python({"value": "no type at all"})
    assert isinstance(parsed, _RawFallback)
