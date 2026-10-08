"""Chats', compute's and the gate's entries in the cross-tenant parameter table.

``token_id``, ``run_id`` and ``waiver_id`` are generic names another area may
use for its own kinds, so they are scoped to the gate's prefixes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from route_matrix import TwoOrgWorld

GATE = "/api/v1/gate"

PARAM_OBJECTS: Mapping[str, Callable[[TwoOrgWorld], str]] = {
    "chat_id": lambda w: w.a["chat"],
    "template_id": lambda w: w.a["chat_template"],
    "workspace_id": lambda w: w.a["workspace"],
    "allocation_id": lambda w: w.a["allocation"],
    "machine_id": lambda w: w.a["machine"],
    "installation_id": lambda w: w.a["github_installation"],
    "move_id": lambda w: w.a["machine_move"],
    "box_id": lambda w: w.a["personal_box"],
}

SCOPED_PARAM_OBJECTS: Mapping[tuple[str, str], Callable[[TwoOrgWorld], str]] = {
    (f"{GATE}/tokens", "token_id"): lambda w: w.a["ci_token"],
    (f"{GATE}/runs", "run_id"): lambda w: w.a["gate_run"],
    (f"{GATE}/waivers", "waiver_id"): lambda w: w.a["gate_waiver"],
    # An org's machines are org machines, not the allocations behind them.
    ("/api/v1/org/machines", "machine_id"): lambda w: w.a["org_machine"],
}

CALLER_SCOPED_OPERATIONS: Mapping[str, str] = {
    "POST /api/v1/gate/github/installations/{installation_id}/unclaim": (
        "releases the caller's own org's claim on that GitHub installation id; an id "
        "another org holds is answered unclaimed=false and nothing moves"
    ),
}


NOT_TENANT_PARAMS: Mapping[str, str] = {
    # The node bundle a box fetches is the deployment's build for one platform
    # (linux-x64, linux-arm64), the same bytes for every org.
    "target": "a node bundle's platform target, the same for every org",
}
