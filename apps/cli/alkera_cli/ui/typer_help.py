"""Brand Typer's rich help screens (``alkera``, ``alkera --help``, …).

Typer renders help through module-level style globals in
``typer.rich_utils`` and reads them at render time, so pointing them at
the active palette rebrands every help screen. Pure global assignment —
no I/O, safe to call at import.
"""

from __future__ import annotations

import typer.rich_utils as _rich_utils

from alkera_cli.ui.theme import BackgroundKind, palette_for


def apply_typer_brand(kind: BackgroundKind) -> None:
    """Point Typer's help styles at the palette for ``kind``."""
    p = palette_for(kind)
    _rich_utils.STYLE_OPTION = f"bold {p.brand}"
    _rich_utils.STYLE_SWITCH = f"bold {p.success}"
    _rich_utils.STYLE_NEGATIVE_OPTION = f"bold {p.danger}"
    _rich_utils.STYLE_NEGATIVE_SWITCH = f"bold {p.danger}"
    _rich_utils.STYLE_METAVAR = p.warning
    _rich_utils.STYLE_METAVAR_SEPARATOR = p.tertiary_text
    _rich_utils.STYLE_USAGE = f"bold {p.brand_strong}"
    _rich_utils.STYLE_USAGE_COMMAND = f"bold {p.primary_text}"
    _rich_utils.STYLE_HELPTEXT_FIRST_LINE = p.primary_text
    _rich_utils.STYLE_HELPTEXT = p.tertiary_text
    _rich_utils.STYLE_OPTION_DEFAULT = p.tertiary_text
    _rich_utils.STYLE_OPTION_ENVVAR = p.tertiary_text
    _rich_utils.STYLE_REQUIRED_SHORT = p.danger
    _rich_utils.STYLE_REQUIRED_LONG = p.danger
    _rich_utils.STYLE_OPTIONS_PANEL_BORDER = p.outline
    _rich_utils.STYLE_COMMANDS_PANEL_BORDER = p.outline
    _rich_utils.STYLE_ERRORS_PANEL_BORDER = p.danger
    _rich_utils.STYLE_COMMANDS_TABLE_FIRST_COLUMN = f"bold {p.brand}"
