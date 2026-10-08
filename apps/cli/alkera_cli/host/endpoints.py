"""Default endpoint URLs for the CLI + ``alkera serve`` daemon.

A source run and an open build default to the local self-hosted stack. A
release build bakes its own endpoints into ``build_profile.py``; a baked value
wins over the local default. These are only *fallback* defaults: runtime env
(``ALKERA_API_URL`` / ``ALKERA_FRONTEND_URL`` / ``ALKERA_GATEWAY_URL``) and
``/etc/alkera/config.yml`` still win (see :mod:`alkera_cli.host.config`), so a
self-hosted operator overrides them without a rebuild.
"""

from __future__ import annotations

from alkera_cli.host import build_profile
from alkera_cli.host.build_profile import SENTRY_DSN as _BAKED_SENTRY_DSN

#: The local self-hosted stack: what every build without baked endpoints uses.
SELF_HOSTED = {
    "api_url": "http://localhost:8000",
    "frontend_url": "http://localhost:5173",
    "gateway_url": "http://localhost:8081",
}

DEFAULT_API_URL = build_profile.API_URL or SELF_HOSTED["api_url"]
DEFAULT_FRONTEND_URL = build_profile.FRONTEND_URL or SELF_HOSTED["frontend_url"]
DEFAULT_GATEWAY_URL = build_profile.GATEWAY_URL or SELF_HOSTED["gateway_url"]

# Baked Sentry DSN (release builds only; empty in dev/source builds). An empty
# bake means "Sentry disabled", so normalize "" -> None. Runtime env
# (ALKERA_SENTRY_DSN) / system config still override (see alkera_cli.host.config).
DEFAULT_SENTRY_DSN: str | None = _BAKED_SENTRY_DSN or None
