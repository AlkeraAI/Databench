"""Where the dotenv files are read from.

``.env`` (committed shape), ``.env.workspace`` (generated per checkout) and
``.env.local`` (the developer's secrets) live at the root of a checkout. A
process may start anywhere below it: at the root, in ``apps/backend``, or inside
the open tree a private checkout carries. The files are found by one rule:

1. ``ALKERA_ENV_DIR`` when it is set (a Makefile that delegates into a nested
   tree passes its own root this way);
2. else the nearest directory at or above the working directory that holds
   ``.env`` or ``.env.workspace``, never climbing past the first repository root
   (a directory with ``.git``), so a ``.env`` above the checkout is never read;
3. else the working directory, as before.

This module imports nothing from the settings, so the test bootstrap can read
the same files before ``alkera_core.config`` caches an instance.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

ENV_FILE_NAMES = (".env", ".env.workspace", ".env.local")
ENV_DIR_VARIABLE = "ALKERA_ENV_DIR"
_MARKERS = (".env", ".env.workspace")


def env_dir(start: Path | None = None, environ: Mapping[str, str] = os.environ) -> Path:
    """The directory the dotenv files are read from (see the module docstring)."""
    explicit = environ.get(ENV_DIR_VARIABLE)
    if explicit:
        return Path(explicit)
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if any((candidate / marker).is_file() for marker in _MARKERS):
            return candidate
        if (candidate / ".git").exists():
            break
    return here


def env_files(
    start: Path | None = None, environ: Mapping[str, str] = os.environ
) -> tuple[Path, ...]:
    """The dotenv files in the order they apply; a later one wins per key."""
    directory = env_dir(start, environ)
    return tuple(directory / name for name in ENV_FILE_NAMES)
