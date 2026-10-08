"""Visibility tokens: who a shared object is for.

A token is a free string, ``"private"``, ``"team:<id>"`` or ``"org:<id>"``. It is
folded and parsed here, once, so a case or whitespace variant cannot slip past a
gate, and anything malformed parses as the most restrictive answer.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Visibility is a free string token: "private" | "team:<id>" | "org:<id>".
PRIVATE = "private"
#: What every unrecognized token classifies as, and the most restrictive answer.
UNKNOWN_SCOPE = "unknown"


def normalize_scope(scope: str) -> str:
    """A visibility token folded for comparison, ``"  ORG:5 "`` to ``"org:5"``. THE one
    place tokens fold, so a case or whitespace variant cannot slip past a gate."""
    return scope.strip().lower()


@dataclass(frozen=True, slots=True)
class VisibilityScope:
    """A parsed visibility token: who an item is for.

    An identifier is what makes ``team`` and ``org`` mean anything, so ``"team:"``
    and a bare ``"team"`` reach no team and parse as ``unknown``, which every gate
    treats as the most restrictive. That is the backend parser's rule too, so a
    token this layer accepts is one the server can resolve."""

    kind: str
    """``private`` | ``team`` | ``org`` | ``unknown``."""
    identifier: str = ""
    """The team id for a ``team``/``org`` scope, empty otherwise."""

    @property
    def shared(self) -> bool:
        """Whether this scope reaches anyone but the local user."""
        return self.kind in ("team", "org")


def parse_scope(scope: str) -> VisibilityScope:
    """``scope`` as a :class:`VisibilityScope`, fail-closed on anything malformed.
    The identifier keeps its own case, since team ids are opaque here and folding
    one would invent an identity rule the backend does not share."""
    lowered = normalize_scope(scope)
    if lowered == PRIVATE:
        return VisibilityScope(PRIVATE)
    for kind in ("team", "org"):
        if not lowered.startswith(f"{kind}:"):
            continue
        identifier = scope.strip()[len(kind) + 1 :].strip()
        if identifier:
            return VisibilityScope(kind, identifier)
    return VisibilityScope(UNKNOWN_SCOPE)


def scope_kind(scope: str) -> str:
    """A visibility token's kind, ``unknown`` for anything malformed."""
    return parse_scope(scope).kind


__all__ = [
    "PRIVATE",
    "UNKNOWN_SCOPE",
    "VisibilityScope",
    "normalize_scope",
    "parse_scope",
    "scope_kind",
]
