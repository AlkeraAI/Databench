# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from alkera_notebook._marimo._config.config import Theme
from alkera_notebook._marimo._config.manager import get_default_config_manager


def get_current_theme() -> Theme:
    config_manager = get_default_config_manager(current_path=None)
    return config_manager.theme
