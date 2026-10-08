# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
"""Internal API for configuration management."""

from alkera_notebook._marimo._config.config import (
    DisplayConfig,
    MarimoConfig,
    PartialMarimoConfig,
)
from alkera_notebook._marimo._config.manager import (
    MarimoConfigManager,
    get_default_config_manager,
)

__all__ = [
    "DisplayConfig",
    "MarimoConfig",
    "MarimoConfigManager",
    "PartialMarimoConfig",
    "get_default_config_manager",
]
