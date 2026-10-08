"""Terminal-client presentation layer.

Owns theming (adaptive palette), the banner and
the pure menu primitives. Pure presentation: no harness imports, no
event-loop ownership.
"""

from alkera_cli.ui.banner import TAGLINE, banner_rows, render_banner
from alkera_cli.ui.menu import MenuOption
from alkera_cli.ui.theme import (
    DARK,
    LIGHT,
    BackgroundKind,
    Palette,
    activate,
    apply_background,
    build_theme,
    console,
    detect_background,
)
from alkera_cli.ui.typer_help import apply_typer_brand

__all__ = [
    "DARK",
    "LIGHT",
    "TAGLINE",
    "BackgroundKind",
    "MenuOption",
    "Palette",
    "activate",
    "apply_background",
    "apply_typer_brand",
    "banner_rows",
    "build_theme",
    "console",
    "detect_background",
    "render_banner",
]
