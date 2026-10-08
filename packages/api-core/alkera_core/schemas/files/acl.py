"""The interned ACL body: an ordered list of ACEs and the hash that interns it.

``file_acls.body`` is content-addressed, so a million nodes that share one
permission set share one row. :meth:`AclBody.body_hash` is what that addressing
means in code — SHA-256 over canonical JSON, which makes the hash depend on the
*entries and their order* and not at all on how a serializer happened to order
its keys. Two writers on different backend versions must intern to the same row.

The ACE union is discriminated on ``principal_kind`` with a :class:`RawAce`
fallback: the kind is an open registry (``agent``, ``service``, ``link`` and
``public`` are coming), and an older reader that met one of those must carry the
entry through untouched rather than raise and take out the whole listing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Discriminator, Field, Tag, field_validator

from alkera_core.files.authz.ladder import ROLE_OWNER, ROLE_READER, ROLE_WRITER
from alkera_core.schemas.files.principal import (
    DirectGrant,
    DriveDefaultGrant,
    GrantOrigin,
    InheritedGrant,
    validate_role,
)
from alkera_core.versioning import VersionedModel, make_unknown_tag_discriminator

KNOWN_PRINCIPAL_KINDS = {"user", "team", "org"}
"""What day one writes. The registry is open — everything else routes to RawAce."""


class AceBase(VersionedModel):
    """The fields every known ACE shares."""

    __abstract__: ClassVar[bool] = True

    principal_id: str
    role: str
    origin: GrantOrigin = Field(default_factory=DirectGrant)
    expires_at: datetime | None = None
    conditions: dict[str, Any] | None = None

    @field_validator("role")
    @classmethod
    def _role_in_ladder(cls, role: str) -> str:
        return validate_role(role)


class UserAce(AceBase):
    """A grant to one user."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    principal_kind: Literal["user"] = "user"


class TeamAce(AceBase):
    """A grant to a team — every member, and every ancestor admin by descent."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    principal_kind: Literal["team"] = "team"


class OrgAce(AceBase):
    """A grant to the org root team: anyone in the organization."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    principal_kind: Literal["org"] = "org"


class RawAce(VersionedModel):
    """A principal kind this reader does not know.

    Nothing but the tag is typed, and ``extra="allow"`` carries the rest, so the
    entry survives a read-modify-write by an older backend byte for byte. The
    role is deliberately NOT validated here — an unknown principal kind may well
    carry a role from a ladder this reader has not learned yet, and refusing it
    would turn a forward-compatible read into an outage.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    principal_kind: str = "__unknown__"


_ace_tag = make_unknown_tag_discriminator(KNOWN_PRINCIPAL_KINDS, field="principal_kind")

Ace = Annotated[
    Annotated[UserAce, Tag("user")]
    | Annotated[TeamAce, Tag("team")]
    | Annotated[OrgAce, Tag("org")]
    | Annotated[RawAce, Tag("__unknown__")],
    Discriminator(_ace_tag),
]


def canonical_json(payload: Any) -> str:
    """The one spelling of a body both backend versions must agree on."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class AclBody(VersionedModel):
    """The interned permission set of a node, in canonical order."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    entries: list[Ace] = Field(default_factory=list)

    def body_hash(self) -> str:
        """SHA-256 of the canonical JSON of the entries — the interning key.

        Only the entries are hashed. ``schema_version`` is deliberately excluded:
        a version bump that does not change the meaning of a body must not
        re-intern every ACL in the org into a duplicate row.
        """
        entries: list[Any] = [entry for entry in self.model_dump(mode="json")["entries"]]
        return hashlib.sha256(canonical_json(entries).encode("utf-8")).hexdigest()


def _acl_example() -> AclBody:
    return AclBody(
        entries=[
            UserAce(
                principal_id="e7c0f4a4-1f8e-4a6e-9b06-1f3d1e1a0001",
                role=ROLE_OWNER,
                origin=DirectGrant(),
            ),
            TeamAce(
                principal_id="6f1d2a3b-0c4e-4d5f-8a91-2b3c4d5e6f70",
                role=ROLE_WRITER,
                origin=InheritedGrant(ancestor_node_id="1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9"),
            ),
            OrgAce(
                principal_id="9c8b7a65-4321-4fed-8cba-098765432100",
                role=ROLE_READER,
                origin=DriveDefaultGrant(),
            ),
        ]
    )


FIXTURE_EXAMPLES: list[tuple[str, Callable[[], VersionedModel]]] = [
    ("acl_body", _acl_example),
]
