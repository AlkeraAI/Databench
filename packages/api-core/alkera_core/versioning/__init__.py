"""Shared base for every persisted Pydantic model in the monorepo.

Every Pydantic model that gets serialized to disk (or to any storage
that outlives a single process) **must** inherit from `VersionedModel`.
HTTP request/response shapes that only live in-flight should keep using
bare `pydantic.BaseModel`.

See `packages/api-core/alkera_core/versioning/README.md` for the full
rationale + the discipline rules. The hard rules are also captured in
the repo-root `CLAUDE.md` so they show up in every code-assistant session.
"""

from __future__ import annotations

from alkera_core.versioning.base import (
    Migration,
    VersionedModel,
    make_unknown_tag_discriminator,
)
from alkera_core.versioning.corpus import (
    corpus_version,
    resolve_corpus_dir,
    version_dir_name,
)

__all__ = [
    "Migration",
    "VersionedModel",
    "corpus_version",
    "make_unknown_tag_discriminator",
    "resolve_corpus_dir",
    "version_dir_name",
]
