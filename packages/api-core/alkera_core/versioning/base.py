"""`VersionedModel` — the base class for persisted Pydantic models.

The rules (``README.md`` beside this module has the full list):

- `extra="allow"` so unknown fields from newer writers survive an older
  reader's round-trip (the single most important rule).
- A per-subclass `SCHEMA_VERSION` (semver string) + `MIGRATIONS` ladder.
- A `@model_validator(mode="before")` walks the ladder from the file's
  recorded version up to the current `SCHEMA_VERSION` before normal
  field parsing kicks in.
- A `@field_serializer("schema_version")` always emits the current
  version so we never write stale stamps.
- `__init_subclass__` warns when a subclass forgets to override
  `SCHEMA_VERSION` (effective abstract-ness without `abc.ABC`, which
  clashes with Pydantic's metaclass).

The companion `make_unknown_tag_discriminator` factory builds tagged
union discriminators that route unknown tags to a fallback class
(e.g. `RawEvent` / `RawPart`) instead of raising — required so an old
reader doesn't crash on a brand-new event type written by a newer
writer.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from typing import Any, ClassVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    model_validator,
)

Migration = Callable[[dict[str, Any]], dict[str, Any]]
"""A migration is a pure dict-in / dict-out function.

It MUST be replayable from any state (no I/O, no DB lookups, no
globals). It MUST set ``schema_version`` on its return value to the
next version in the ladder.
"""


# Sentinel ClassVar default that subclasses are expected to override.
# We compare against this in __init_subclass__ to detect forgotten bumps.
_UNVERSIONED = "0.0.0"


class VersionedModel(BaseModel):
    """Base class for any Pydantic model that gets persisted.

    Concrete subclasses MUST:
      1. Override ``SCHEMA_VERSION`` to a semver string (e.g. ``"1.0.0"``).
      2. Register migrations in ``MIGRATIONS`` keyed by the *from* version
         when the shape changes in a breaking way.
      3. Add a fixture at
         ``packages/api-core/tests/fixtures/<model>/v<SCHEMA_VERSION>.json``
         that represents the current writer's output (the lineage
         regression net — see CLAUDE.md).

    `extra="allow"` is non-negotiable on this class hierarchy. Removing
    it on a subclass breaks forward compatibility — when an old reader
    encounters a field added by a newer writer, the unknown field would
    raise instead of riding along on round-trip.
    """

    model_config = ConfigDict(
        extra="allow",  # unknown fields from newer writers survive round-trip
        populate_by_name=True,
        validate_assignment=True,
    )

    SCHEMA_VERSION: ClassVar[str] = _UNVERSIONED
    """Current writer's version. Subclasses MUST override."""

    __abstract__: ClassVar[bool] = True
    """Marker that opts a class out of the missing-SCHEMA_VERSION warning.

    Intermediate base classes (e.g. ``VersionedChatModel``, ``EventBase``,
    ``PartBase``) set this to ``True`` so they don't trip the warning;
    concrete subclasses inherit ``False`` by default (set automatically
    in ``__init_subclass__``) and ARE expected to bump SCHEMA_VERSION.
    """

    MIGRATIONS: ClassVar[dict[str, Migration]] = {}
    """Linear ladder of upgrade migrations keyed by *from* version.

    Each entry takes a raw dict at version K, returns a raw dict at
    version K+1 (with the new ``schema_version`` set). Never edit
    existing entries — they're frozen history. Only add new ones.
    """

    schema_version: str = Field(default=_UNVERSIONED)
    """Stamped on every instance. Always equals the class's
    ``SCHEMA_VERSION`` after a successful load — earlier values get
    upgraded by the migration ladder. Persisted writes always emit the
    current version (see `_emit_current_version`)."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Open extension bag for experimental / harness-specific fields.

    Use this *before* promoting a field to a typed slot — avoids churn
    on the typed surface while a new field is still flexing. Promote to
    a real field once the shape stabilizes.
    """

    # ------------------------------------------------------------------
    # Subclass discipline
    # ------------------------------------------------------------------

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # If the subclass didn't EXPLICITLY set __abstract__, it's
        # concrete — flip the flag. (Inheriting `True` from a parent
        # wouldn't catch concrete leaves.)
        if "__abstract__" not in cls.__dict__:
            cls.__abstract__ = False
        # Concrete leaves MUST override SCHEMA_VERSION; abstract bases
        # are exempt. (`abstract_*` is a marker we control, not Python's
        # abc — Pydantic's metaclass clashes with abc.ABC, so we DIY.)
        if not cls.__abstract__ and cls.SCHEMA_VERSION == _UNVERSIONED:
            warnings.warn(
                f"{cls.__qualname__} inherits VersionedModel but did not "
                f"override SCHEMA_VERSION. Set SCHEMA_VERSION (e.g. '1.0.0') "
                f"or mark the class abstract with `__abstract__ = True`.",
                stacklevel=2,
            )

    # ------------------------------------------------------------------
    # Migration ladder
    # ------------------------------------------------------------------

    @classmethod
    def migrate(cls, data: dict[str, Any], from_version: str) -> dict[str, Any]:
        """Walk ``MIGRATIONS`` from ``from_version`` to ``SCHEMA_VERSION``.

        Subclasses may override for non-linear ladders (branching,
        side-effect-free composition, ...). The default implementation
        handles the linear semver case the codebase aims for.

        Raises ``RuntimeError`` on a migration cycle (a bug — should be
        caught in tests).
        """
        v = from_version
        visited: set[str] = {v}
        while v != cls.SCHEMA_VERSION and v in cls.MIGRATIONS:
            data = cls.MIGRATIONS[v](data)
            next_v = data.get("schema_version", cls.SCHEMA_VERSION)
            if next_v in visited:
                raise RuntimeError(
                    f"{cls.__qualname__}: migration cycle detected at {next_v} (already visited)"
                )
            visited.add(next_v)
            v = next_v
        return data

    @model_validator(mode="before")
    @classmethod
    def _run_migrations(cls, data: Any) -> Any:
        # We only know how to migrate dicts. Anything else (an existing
        # instance, e.g. during model_validate(obj)) skips this hook —
        # field-level validation handles those paths.
        if not isinstance(data, dict):
            return data
        incoming = data.get("schema_version", cls.SCHEMA_VERSION)
        if incoming != cls.SCHEMA_VERSION:
            data = cls.migrate(dict(data), incoming)
        # Stamp the current version on the in-memory instance — even if
        # the file was at an older version we just upgraded.
        data["schema_version"] = cls.SCHEMA_VERSION
        return data

    @field_serializer("schema_version")
    def _emit_current_version(self, _v: str) -> str:
        """Always serialize the current writer's version.

        If a future code path mutates ``self.schema_version`` to a stale
        value, we still emit the canonical one — defends against
        accidental persisted-version regressions.
        """
        return type(self).SCHEMA_VERSION


# ----------------------------------------------------------------------
# Tagged-union discriminator with unknown-tag fallback
# ----------------------------------------------------------------------


def make_unknown_tag_discriminator(
    known_tags: set[str],
    *,
    field: str = "type",
    fallback_tag: str = "__unknown__",
) -> Callable[[Any], str]:
    """Build a Pydantic ``Discriminator`` callable that routes unknown
    tag values to ``fallback_tag``.

    Why this exists: when a newer writer introduces a brand-new tag, an
    older reader's tagged union would fail with "no match for tag X" if
    we used a plain string discriminator. Routing unknowns to a permissive
    ``RawXxx`` variant (also tagged ``fallback_tag``) lets unknown
    payloads ride through round-trip without crashing.

    Example::

        class RawPart(VersionedChatModel):
            SCHEMA_VERSION = "1.0.0"
            type: str
            payload: dict[str, Any] = Field(default_factory=dict)

        _part_tag = make_unknown_tag_discriminator(
            {"text", "file", "tool_call", ...}
        )

        Part = Annotated[
            Union[
                Annotated[TextPart, Tag("text")],
                Annotated[FilePart, Tag("file")],
                ...
                Annotated[RawPart, Tag("__unknown__")],
            ],
            Discriminator(_part_tag),
        ]
    """

    def _discriminator(value: Any) -> str:
        if isinstance(value, dict):
            tag = value.get(field)
        else:
            tag = getattr(value, field, None)
        if isinstance(tag, str) and tag in known_tags:
            return tag
        return fallback_tag

    return _discriminator
