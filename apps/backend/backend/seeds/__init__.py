"""Idempotent seed registry.

Each seed is an `async (session) -> str` function that's safe to run any
number of times. To add one:

1. Write `apps/backend/backend/seeds/<your_seed>.py` exporting an
   `async def seed_xxx(session: AsyncSession) -> str`.
2. Append it to `SEEDS` below. A private extension registers its seed into
   `EXTENSION_SEEDS` instead; those run after every core seed, in
   registration order.
3. Re-run `make seed`.

Each seed returns a one-line summary that the runner prints — `"created"`,
`"unchanged"`, or `"skipped: <reason>"` are good defaults.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from alkera_core.extensions import ExtensionPoint
from alkera_core.model_catalog_defaults import seed_model_catalog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.seeds.compute import seed_compute
from backend.seeds.dev import seed_dev_admin

SeedFunction = Callable[[AsyncSession], Awaitable[str]]

SEEDS: list[tuple[str, SeedFunction]] = [
    ("dev_admin", seed_dev_admin),
    # The compute machine-type catalog (static snapshot; live prices when a key is set).
    ("compute_catalog", seed_compute),
    # The model catalog every deployment starts with (models and routes; an
    # installed biller prices them). Every environment: without it no chat can
    # be created.
    ("model_catalog", seed_model_catalog),
]


EXTENSION_SEEDS: ExtensionPoint[tuple[str, SeedFunction]] = ExtensionPoint("dev_seeds")
"""The seeds private extensions add. With none registered only ``SEEDS`` run."""


def all_seeds() -> list[tuple[str, SeedFunction]]:
    """Every seed in run order: the core ones, then the registered ones."""
    return [*SEEDS, *EXTENSION_SEEDS.items()]


async def run_seeds(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[tuple[str, str]]:
    """Run every registered seed in its own transaction. Returns
    (name, summary) tuples."""
    results: list[tuple[str, str]] = []
    for name, fn in all_seeds():
        async with session_factory() as session:
            summary = await fn(session)
            await session.commit()
            results.append((name, summary))
    return results


__all__ = [
    "EXTENSION_SEEDS",
    "SEEDS",
    "all_seeds",
    "run_seeds",
    "seed_compute",
    "seed_dev_admin",
]
