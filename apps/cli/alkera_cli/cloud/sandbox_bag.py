"""What a cloud chat's manifest carries on its harness bag from the box.

The mirror lays the chat row's sandbox figures and, for a member of a
workspace, the workspace's custody key (``sandbox_scope``) on the manifest the
session is built from. The custody key holds only while the chat is a member:
a chat that left its workspace must stop running under it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from alkera_cli.harness.sandbox_scope import SCOPE_KEY

#: The harness bag keys that hold only while the chat is a member of a
#: workspace: one the mirror's sandbox no longer names is taken off the
#: manifest when the chat is opened again.
WORKSPACE_SANDBOX_KEYS: frozenset[str] = frozenset({SCOPE_KEY})


def sandbox_bag_on(harness: Mapping[str, Any], sandbox: Mapping[str, Any]) -> dict[str, Any]:
    """``harness`` with ``sandbox`` laid over it, less every workspace key
    ``sandbox`` does not name: a chat that left its workspace must not keep
    running under the workspace's custody key its manifest still carried.
    Every other key is kept as it was."""
    kept = {k: v for k, v in harness.items() if k not in WORKSPACE_SANDBOX_KEYS or k in sandbox}
    return {**kept, **sandbox}


__all__ = ["WORKSPACE_SANDBOX_KEYS", "sandbox_bag_on"]
