"""Org administration's and connections' entries in the cross-tenant parameter table.

A team, a member, an invitation, a connection and a connection check are each
named by org A's own. On the person's own connection routes (``/me/...``) the id
is the two-org person's personal row in A, which they own: the sharpest target,
because ownership alone used to be enough to reach it from any org.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from route_matrix import TwoOrgWorld

PARAM_OBJECTS: Mapping[str, Callable[[TwoOrgWorld], str]] = {
    "team_id": lambda w: w.a["team"],
    # A member of A who belongs to no other org: A's admin.
    "user_id": lambda w: str(w.admin_a.id),
    "connection_id": lambda w: w.a["connection"],
    "invitation_id": lambda w: w.a["invitation"],
    "verification_id": lambda w: w.a["verification"],
}

SCOPED_PARAM_OBJECTS: Mapping[tuple[str, str], Callable[[TwoOrgWorld], str]] = {
    ("/api/v1/me/connections", "connection_id"): lambda w: w.a["personal_connection"],
    ("/api/v1/me/team-connections", "connection_id"): lambda w: w.a["personal_connection"],
}

NOT_TENANT_PARAMS: Mapping[str, str] = {
    "provider": "a model provider's name; which org's key it addresses is the credential's",
    "token": "an invitation link's own secret; a made-up one names no invitation",
}
