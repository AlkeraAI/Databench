"""Nothing on the storage-ceiling path may speak in binary units again.

The dashboard and the admin page disagreed about the same 100 GB because two
modules each did their own arithmetic on a byte count. One formatter and one
parser fix that; this scan is what keeps a second one from appearing. It reads
the files rather than trusting a reviewer to notice, the same way the web tree's
own hygiene test does.

Binary units are still right in plenty of places — an upload part size, a memory
ceiling, a BigQuery scan priced per TiB — so the scan is deliberately scoped to
the modules that decide what a STORAGE ceiling is.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from alkera_core.config import settings
from alkera_core.units import GB

_ROOT = Path(__file__).resolve().parents[3]

#: Every module that reads, writes or decides a storage ceiling.
_STORAGE_MODULES = (
    "packages/api-core/alkera_core/units.py",
    "packages/api-core/alkera_core/billing/storage.py",
    "packages/api-core/alkera_core/files/quota.py",
    "packages/api-core/alkera_core/files/drives.py",
    "apps/backend/backend/services/org/storage_limits.py",
)

_BINARY_LABEL = re.compile(r"\b(?:KiB|MiB|GiB|TiB|PiB)\b")


def _code(path: Path) -> str:
    """The module with its prose stripped — a comment or a docstring may name
    the binary unit the code no longer uses, and saying so is the point of it."""
    source = re.sub(r'""".*?"""', "", path.read_text(), flags=re.DOTALL)
    return "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))


@pytest.mark.parametrize("module", _STORAGE_MODULES, ids=lambda m: Path(m).stem)
def test_no_storage_module_labels_a_size_in_binary_units(module: str) -> None:
    assert _BINARY_LABEL.search(_code(_ROOT / module)) is None


@pytest.mark.parametrize("module", _STORAGE_MODULES, ids=lambda m: Path(m).stem)
def test_no_storage_module_scales_a_size_by_1024(module: str) -> None:
    """A 1024 anywhere on this path is the bug coming back: it is either a
    second formatter or a ceiling written in a unit nothing else reads."""
    assert "1024" not in _code(_ROOT / module)


def test_the_shipped_default_and_the_example_env_are_the_same_decimal_figure() -> None:
    """The example file is what a fresh install copies, so a binary figure there
    seeds every new deployment with a ceiling 7.4% off what it claims. The
    example lists optional settings commented out at their defaults, so the
    figure counts there too."""
    example = (_ROOT / ".env.example").read_text(encoding="utf-8")
    match = re.search(r"^(?:# )?FILES_QUOTA_DEFAULT_BYTES=(\d+)$", example, re.MULTILINE)
    assert match is not None, "the example env no longer names the drive quota default"
    configured = int(match.group(1))
    assert configured == settings.files_quota_default_bytes
    assert configured == 100 * GB
    assert configured % GB == 0
