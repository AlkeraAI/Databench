from __future__ import annotations

import importlib
from logging.config import fileConfig
from typing import Any

# This chain's own models, so autogenerate and check see its tables.
import alkera_core.models  # noqa: F401
from alembic import context
from alkera_core.config import settings
from alkera_core.db.base import Base
from backend.migration_runner import run_offline, run_online

config = context.config

# A checkout that keeps more tables in this chain names the modules that map
# them: `alembic -x models=pkg.models,other.models check`. Upgrading needs none
# of them; only autogenerate and check read the models.
for _module in context.get_x_argument(as_dictionary=True).get("models", "").split(","):
    if _module.strip():
        importlib.import_module(_module.strip())

if config.config_file_name is not None:
    # fileConfig's default disable_existing_loggers=True would flip ``disabled`` on
    # every logger created before this point that the ini does not name -- a
    # problem whenever Alembic runs in-process (the test suite drives it that way)
    # rather than as its own command.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Override sqlalchemy.url from our pydantic settings (sync URL for Alembic).
config.set_main_option("sqlalchemy.url", settings.database_url_sync)

target_metadata = Base.metadata


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """A table in the database that no model here maps belongs to another chain
    (an installation's own, under its own version table): never ours to drop."""
    return not (type_ == "table" and reflected and compare_to is None)


if context.is_offline_mode():
    run_offline(context, target_metadata, include_object=include_object)
else:
    run_online(context, target_metadata, include_object=include_object)
