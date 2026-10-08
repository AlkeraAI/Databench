"""The registered policies.

Importing this package registers every open policy with the engine;
:mod:`alkera_core.authz` imports it so :func:`~alkera_core.authz.authorize`
sees them all. One module per resource type, each ending in a single
``register(...)`` call. A private domain's policy (billing's team pool cap, the
knowledge base's promote scope) is registered by that domain's extension
instead, never by importing this package.
"""

from __future__ import annotations

from alkera_core.authz.policies import (
    account,
    chat,
    chat_template,
    compute,
    compute_grant,
    compute_offering,
    connector,
    file_lease,
    files,
    machine_credential,
    notebook,
    org,
    org_audit,
    org_machine,
    personal_box,
    platform_ban,
    platform_billing,
    platform_machine,
    platform_org_live_editing,
    platform_org_slack,
    platform_org_sso_domains,
    platform_org_storage,
    team,
    team_allocation,
    team_membership,
    team_storage_cap,
    workspace,
    workspace_machine,
    workspace_object,
)

__all__ = [
    "account",
    "chat",
    "chat_template",
    "compute",
    "compute_grant",
    "compute_offering",
    "connector",
    "file_lease",
    "files",
    "machine_credential",
    "notebook",
    "org",
    "org_audit",
    "org_machine",
    "personal_box",
    "platform_ban",
    "platform_billing",
    "platform_machine",
    "platform_org_live_editing",
    "platform_org_slack",
    "platform_org_sso_domains",
    "platform_org_storage",
    "team",
    "team_allocation",
    "team_membership",
    "team_storage_cap",
    "workspace",
    "workspace_machine",
    "workspace_object",
]
