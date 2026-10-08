# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
"""Internal API for package management."""

from alkera_notebook._marimo._runtime.packages.package_manager import (
    PackageDescription,
    PackageManager,
)
from alkera_notebook._marimo._runtime.packages.package_managers import create_package_manager

__all__ = [
    "PackageDescription",
    "PackageManager",
    "create_package_manager",
]
