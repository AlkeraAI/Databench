"""Sharing shapes: the principal a grant, a lease or an operation names."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar, Literal

from alkera_core.versioning import VersionedModel

PrincipalKind = Literal["user", "team", "org", "link"]


class Principal(VersionedModel):
    """Who a grant, a lease or an operation belongs to — ids only, never a name."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: PrincipalKind = "user"
    id: str = ""


FIXTURE_EXAMPLES: list[tuple[str, Callable[[], VersionedModel]]] = [
    ("principal", lambda: Principal(kind="team", id="6b1f0b1e-0000-4000-8000-000000000001")),
]
