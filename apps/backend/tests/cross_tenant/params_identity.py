"""Identity's entries in the cross-tenant parameter table.

A session path id is the ``jti`` of the two-org person's own token for org A
(``seed_identity``): theirs, but minted for their membership of A. The join
selector carries org A's id: the person's membership of A is active, not
pending, so joining it from B's credential finds nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from route_matrix import TwoOrgWorld

PARAM_OBJECTS: Mapping[str, Callable[[TwoOrgWorld], str]] = {
    "jti": lambda w: w.a["session"],
}

SCOPED_PARAM_OBJECTS: Mapping[tuple[str, str], Callable[[TwoOrgWorld], str]] = {
    ("/api/v1/auth/memberships/join", "org_team_id"): lambda w: str(w.org_a),
}

NOT_TENANT_PARAMS: Mapping[str, str] = {}
