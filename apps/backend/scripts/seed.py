"""Run idempotent seeds. Invoked by `make seed`.

The deployment's extensions are installed first (``BACKEND_INSTALL``, the open
platform's by default), so the seeds an installed extension registers run too.

Usage: `cd apps/backend && uv run python -m scripts.seed`
"""

from __future__ import annotations

import asyncio
import sys

from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from backend.composition import install_composition
from backend.seeds import run_seeds


async def main() -> int:
    print(f"==> Running seeds (APP_ENV={settings.app_env})")
    # The seeds create orgs, which start with the rows the installed domains
    # register.
    install_composition()
    results = await run_seeds(AsyncSessionLocal)
    for name, summary in results:
        print(f"  • {name}: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
