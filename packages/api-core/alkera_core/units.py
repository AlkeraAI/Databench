"""Storage sizes, in the units a person buys storage in.

One rule, everywhere a byte count is read or typed: **a gigabyte is
1,000,000,000 bytes**. Drives are sold that way, plans are priced that way, and
the storage line on an invoice is quoted that way, so a ceiling an operator
enters as "100 GB" is 100,000,000,000 bytes and reads back as "100 GB".

The alternative — storing 100 x 2^30 and dividing the display by 10^9 — is how
the same limit came to read as "100 GB" on the admin page and "107 GB" on the
dashboard. There is no third option that keeps both honest, so the binary
divisor is gone from storage entirely: no ``GiB`` label, and no 1024 in a
storage formatter or parser.

This module is the only place either direction is spelled for Python. The
TypeScript twin is ``apps/web/src/lib/format/bytes.ts``; the two tables must
agree, and both are pinned by tests that read like each other.
"""

from __future__ import annotations

import math
import re
from typing import Final

#: The unit ladder, smallest first. Each step is 1000 of the one below it.
UNITS: Final[tuple[str, ...]] = ("B", "KB", "MB", "GB", "TB", "PB")

#: Bytes in one of each unit — the multiplier an entered figure is scaled by.
UNIT_BYTES: Final[dict[str, int]] = {name: 1000**index for index, name in enumerate(UNITS)}

KB: Final = UNIT_BYTES["KB"]
MB: Final = UNIT_BYTES["MB"]
GB: Final = UNIT_BYTES["GB"]
TB: Final = UNIT_BYTES["TB"]
PB: Final = UNIT_BYTES["PB"]

#: The largest storage ceiling any figure field accepts: 1,000 PB. Far past any
#: real drive, and an order of magnitude inside the BigInteger the columns hold,
#: so a figure the schema admits can never overflow the row it lands in — the
#: bound is a validation answer, not a 500.
MAX_CEILING_BYTES: Final = 1000 * PB

#: What a bare number in a limit field means. Storage ceilings are written in
#: gigabytes far more often than in anything else, so "100" is 100 GB.
DEFAULT_UNIT: Final = "GB"

_ENTRY: Final = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]*)\s*$")

#: Spellings a person may type for each unit. ``B`` admits "byte"/"bytes"; the
#: rest admit the bare letter ("g", "t") because that is what gets typed.
_ALIASES: Final[dict[str, str]] = {
    "b": "B",
    "byte": "B",
    "bytes": "B",
    "k": "KB",
    "kb": "KB",
    "m": "MB",
    "mb": "MB",
    "g": "GB",
    "gb": "GB",
    "t": "TB",
    "tb": "TB",
    "p": "PB",
    "pb": "PB",
}


def _round1(value: float) -> float:
    """One decimal, halves away from zero — what a reader expects, and what the
    TypeScript twin's ``Math.round`` does. ``round()`` would break the tie to
    even and disagree with the other tree on exactly the values a table pins."""
    return math.floor(value * 10 + 0.5) / 10


def format_bytes(count: int | float | None) -> str | None:
    """A byte count as a person reads it — ``100_000_000_000`` -> ``"100 GB"``.

    The unit is the largest one that leaves a figure of at least 1, and the
    figure carries **at most one decimal**, halves up, with a trailing ``.0``
    trimmed: ``2_500_000_000`` -> ``"2.5 GB"``, ``812_000_000`` -> ``"812 MB"``.
    A figure that rounds up to 1000 moves to the next unit instead of printing
    ``"1000 MB"``, so ``999_999_999`` reads as ``"1 GB"``. Bytes never carry a
    decimal — they are whole things.

    ``None`` is not zero: it is "no ceiling", which only the caller can name, so
    it comes back as ``None`` and the copy stays with the surface.
    """
    if count is None or not math.isfinite(count) or count < 0:
        return None
    index = 0
    value = float(count)
    while index < len(UNITS) - 1:
        # Step up while the figure is at least 1000 of this unit, or still
        # rounds to it — so the reading is "1 GB" and never "1000 MB".
        if value < 1000 and _round1(value) < 1000:
            break
        value /= 1000
        index += 1
    if index == 0:
        return f"{int(value)} B"
    text = f"{_round1(value):.1f}".rstrip("0").rstrip(".")
    return f"{text} {UNITS[index]}"


def exact_bytes(count: int) -> str:
    """The exact count, grouped — what sits under a figure being edited."""
    return f"{count:,} bytes"


def parse_bytes(text: str, *, default_unit: str = DEFAULT_UNIT) -> int:
    """A typed storage figure as bytes: ``"1 TB"`` -> ``1_000_000_000_000``.

    Accepts a bare number (read in ``default_unit``), a number and a unit with
    or without a space, and any case. Raises :class:`ValueError` naming what was
    wrong — a ceiling that cannot be read must never be silently written as
    something else.

    A non-positive figure is refused: zero is not an edit, it is a lockout, and
    the surfaces that remove a ceiling say so in their own words.
    """
    match = _ENTRY.match(text)
    if match is None:
        raise ValueError(f"{text!r} is not a storage size — write it like '100 GB' or '1 TB'")
    amount, suffix = match.group(1), match.group(2).lower()
    unit = default_unit if suffix == "" else _ALIASES.get(suffix, "")
    if unit not in UNIT_BYTES:
        raise ValueError(f"{match.group(2)!r} is not a storage unit — use KB, MB, GB, TB or PB")
    scaled = float(amount) * UNIT_BYTES[unit]
    if scaled <= 0:
        raise ValueError("a storage size must be greater than zero")
    return round(scaled)


__all__ = [
    "DEFAULT_UNIT",
    "GB",
    "KB",
    "MAX_CEILING_BYTES",
    "MB",
    "PB",
    "TB",
    "UNITS",
    "UNIT_BYTES",
    "exact_bytes",
    "format_bytes",
    "parse_bytes",
]
