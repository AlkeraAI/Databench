"""The prefix every bearer credential the platform mints starts with.

One place for all of them, because more than one thing has to know the whole
set: each minting module stamps its own, and the log redactor recognises every
one of them in free text. A new kind of credential adds its prefix here; a
prefix spelled anywhere else is refused by a gate.

The prefixes are public, greppable markers (that is what they are for), not
secrets.
"""

from __future__ import annotations

from typing import Final

CI_TOKEN_PREFIX: Final = "alk_ci_"  # noqa: S105 - a public marker, not a secret
PAT_TOKEN_PREFIX: Final = "alk_pat_"  # noqa: S105 - a public marker, not a secret
MACHINE_TOKEN_PREFIX: Final = "alk_machine_"  # noqa: S105 - a public marker, not a secret
PROXY_TOKEN_PREFIX: Final = "alk_proxy_"  # noqa: S105 - a public marker, not a secret
SCIM_TOKEN_PREFIX: Final = "alk_scim_"  # noqa: S105 - a public marker, not a secret

#: Every prefix above.
TOKEN_PREFIXES: Final = (
    CI_TOKEN_PREFIX,
    PAT_TOKEN_PREFIX,
    MACHINE_TOKEN_PREFIX,
    PROXY_TOKEN_PREFIX,
    SCIM_TOKEN_PREFIX,
)

__all__ = [
    "CI_TOKEN_PREFIX",
    "MACHINE_TOKEN_PREFIX",
    "PAT_TOKEN_PREFIX",
    "PROXY_TOKEN_PREFIX",
    "SCIM_TOKEN_PREFIX",
    "TOKEN_PREFIXES",
]
