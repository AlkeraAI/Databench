"""Load the committed default model catalog into this deployment's database.

Usage:
    cd apps/backend && uv run python -m scripts.load_model_catalog

In a container (the backend image carries it):
    python -m scripts.load_model_catalog

A fresh database has no model, so no chat can be created (`no_model_offered`)
until one exists. The seeds load the catalog on every deployment; this runs the
same load on its own, for a database that was seeded before a model was added.
It writes every default model the database lacks, with its provider route, and
leaves every model it already has exactly as it is, so it is safe to re-run on
a live database. The deployment's extensions are installed first, so a biller
that prices the catalog does so in the same run.
"""

from __future__ import annotations

import asyncio
import sys

from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.model_catalog_defaults import load_default_catalog
from backend.composition import install_composition


async def main() -> int:
    install_composition()
    async with AsyncSessionLocal() as session:
        created = await load_default_catalog(session)
        await session.commit()
    print(f"==> load_model_catalog (APP_ENV={settings.app_env})")
    if created:
        print(f"  created {len(created)} model(s): {', '.join(created)}")
    else:
        print("  unchanged: every default model already exists")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
