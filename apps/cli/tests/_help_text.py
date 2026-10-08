"""Reading a typer ``--help`` screen as text rather than as a terminal picture.

Typer renders help through rich, so the raw ``Result.output`` carries SGR escape
sequences and is hard-wrapped to the console width. Both are properties of the
machine the test happens to run on — a Windows runner colours and wraps where a
Linux one does not — so a test that asserts a flag is *offered* must read the
text, not the picture.
"""

from __future__ import annotations

import re
from typing import Final

#: The environment that makes rich render plain, wide text.
#: ``NO_COLOR``/``TERM`` turn styling off, ``COLUMNS`` stops the options panel
#: from wrapping a long flag name across two lines.
HELP_ENV: Final[dict[str, str]] = {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}

_SGR = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def plain(output: str) -> str:
    """``output`` with any SGR escape sequence removed."""
    return _SGR.sub("", output)
