"""Where a spawned agent's own roots live on the host: ``<ALKERA_HOME>/harness/<session>``.

The agent server's config, cache and state roots sit under the daemon's own
home, deliberately outside the user's project: opencode discovers agent
definitions (``{agent,agents}/**/*.md``) and plugin tool modules
(``{tool,tools}/*.{js,ts}``, dynamically imported) under its config dir, and
resolves helper binaries out of ``<cache>/agent/bin``. While those roots sat
in ``<project>/.alkera/chats/<sid>/.runtime`` they were reachable by the
agent's own ``write`` tool, and one approved write of an ``agent/*.md`` whose
``permission:`` frontmatter merges after the ``ALKERA_PERMISSION`` ruleset
would have disabled the permission gate wholesale for every later turn. Under
``ALKERA_HOME`` the same write is a write into the Alkera credential
directory, which the sensitive-path floor prompts on (and refuses in
``read_only`` / ``plan``).

Both the adapter that spawns the agent and the tools that run commands beside
it (whose sandbox spells the agent's trees where the container holds them)
read the root from here, so the two never disagree about where it is.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from alkera_cli.host import paths

_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def agent_config_root(session_id: str) -> Path:
    """The agent's config / cache / state root for ``session_id``, read through
    ``paths.ALKERA_HOME`` at call time so a test's ``ALKERA_HOME`` or a
    monkeypatched module attribute is honoured."""
    slug = session_id if _SAFE_SLUG_RE.match(session_id) else ""
    if not slug:
        slug = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return agent_config_roots_dir() / slug


def agent_config_roots_dir() -> Path:
    """The parent of every per-chat agent config root: ``<ALKERA_HOME>/harness``,
    read from ``paths`` at call time so an override set after import holds."""
    return paths.ALKERA_HOME / "harness"


__all__ = ["agent_config_root", "agent_config_roots_dir"]
