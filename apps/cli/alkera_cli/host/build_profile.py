"""Build profile for the CLI/daemon endpoint defaults.

Committed as ``"dev"`` with no endpoints, so a source run and an open build
default to the local self-hosted stack. A distribution's release build rewrites
the literals below before Nuitka compiles (its profile name and the endpoints
its binaries default to), then restores them. Runtime env (``ALKERA_API_URL``
etc.) and ``/etc/alkera/config.yml`` still override the baked default (see
``alkera_cli.host.config``), so self-hosted deployments never need a custom build.

This file is intentionally a single literal so the build script can rewrite it
deterministically.
"""

from __future__ import annotations

BUILD_PROFILE = "dev"

# The endpoints a release build bakes in. Committed EMPTY: an empty value means
# the local self-hosted stack (``alkera_cli.host.endpoints``). Single literals,
# like ``BUILD_PROFILE``, so the build script can rewrite them deterministically.
API_URL = ""
FRONTEND_URL = ""
GATEWAY_URL = ""

# Baked Sentry DSN for production release binaries. Committed EMPTY, so dev/source
# builds send nothing. The production binary build rewrites this from the
# ``$ALKERA_PROD_SENTRY_DSN`` build secret and restores it afterwards; the literal
# DSN is never committed. A Sentry DSN is an ingest-only public key (safe to embed
# in a shipped client); runtime env (``ALKERA_SENTRY_DSN``) and
# ``/etc/alkera/config.yml`` still override it, and the server-side
# spike/rate-limit controls are the real kill-switch for shipped binaries. Kept a
# single literal so the build script can rewrite it deterministically (same rule
# as ``BUILD_PROFILE`` above).
SENTRY_DSN = ""

# The commit a compiled binary was built from (a short sha). Committed EMPTY, so a
# source run names no build. The binary build rewrites it from
# ``$ALKERA_BUILD_ID`` for every compile and restores it afterwards, so the binary
# itself says which build it is: a node's heartbeat and ``alkera version --json``
# report it, and a roll verifies each box against it rather than against the
# release label the roll wrote. A single literal, like the two above.
BUILD_ID = ""
