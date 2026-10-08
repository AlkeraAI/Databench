"""Regenerate ``names_v3.json``, the conformance fixture for the naming contract.

Run from the repo root::

    uv run python packages/api-core/tests/fixtures/files/generate_names.py

The fixture pins the *outputs* of :mod:`alkera_core.files.names` for a few
hundred hostile inputs so a change to the normalizer is a visible diff rather
than a silent behaviour change. Changing the normalizer means writing a new
fixture version (``names_v4.json``), never re-blessing this one.

``names_v1.json`` and ``names_v2.json`` stay beside this file. v1 is the corpus
as it stood when a name could be 255 bytes and could carry a control character
or a surrounding space; v2 is the corpus as it stood when a name could carry a
bidirectional control. The naming test replays each against today's rule to say
exactly which entries the tightened rules took away — and that nothing else
moved with them.

Names are stored base64-encoded because a name is bytes and need not be UTF-8.
"""

from __future__ import annotations

import base64
import json
import unicodedata
from pathlib import Path
from typing import Any

from alkera_core.files.names import (
    FS_NAME_MAX_BYTES,
    NAME_MAX_BYTES,
    InvalidName,
    display,
    flags,
    name_key,
    validate,
)

FIXTURE_VERSION = 3
OUT = Path(__file__).with_name(f"names_v{FIXTURE_VERSION}.json")


def _inputs() -> list[bytes]:
    names: list[bytes] = []

    # What Linux refuses, at and around each boundary.
    names += [
        b"",
        b"\x00",
        b"a\x00b",
        b"\x00abc",
        b"abc\x00",
        b"/",
        b"a/b",
        b"/abc",
        b"abc/",
        b".",
        b"..",
        b"...",
        b".hidden",
        b"..hidden",
        b"a" * (NAME_MAX_BYTES + 1),
        b"a" * FS_NAME_MAX_BYTES,
        b"a" * 512,
        b"\xff" * 256,
    ]

    # Length boundaries that are accepted, each against the one byte over it.
    names += [
        b"a",
        b"a" * (NAME_MAX_BYTES - 1),
        b"a" * NAME_MAX_BYTES,
        "漢".encode() * (NAME_MAX_BYTES // 3),
        "漢".encode() * (NAME_MAX_BYTES // 3) + b"a",
    ]

    # The acceptance table: everything an older design refused and Linux allows.
    names += [
        b"aux.h",
        b"con.c",
        b"NUL.txt",
        "COM¹".encode(),
        b"foo.",
        b"foo ",
        b" foo",
        b"a:b",
        b"a\\b",
        b"~1",
        b"a\x01b",
        "re‮sumé".encode(),
        "name﻿".encode(),
        "tag\U000e0001".encode(),
        b"caf\xe9",
        "漢字".encode() * 40,
    ]

    # Surrounding whitespace, in the flavours a paste carries: the ASCII space,
    # a tab (which is also a control), and the two Unicode spaces a word
    # processor emits. The interior twins are ordinary names.
    names += [
        "\u00a0foo".encode(),
        "foo\u00a0".encode(),
        "\u3000foo".encode(),
        "foo\u2009".encode(),
        "a\u00a0b".encode(),
        "a\u3000b".encode(),
    ]

    # Every Windows-forbidden character, and its harmless twin.
    for ch in '<>:"|?*':
        names.append(f"a{ch}b".encode())
    names += [b"a\\b", b"ab"]

    # Reserved device stems: bare, with an extension, and the negative twins.
    for stem in ("CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9"):
        names += [
            stem.encode(),
            stem.lower().encode(),
            f"{stem}.txt".encode(),
            f"{stem}x".encode(),
            f"x{stem}".encode(),
        ]
    names += [b"COM0", b"COM10", b"LPT0", b"LPT10", b"COMx", b"CON1"]

    # Trailing dot / space, leading space, and the interior twins.
    names += [b"foo.", b"foo..", b"foo ", b"foo  ", b" foo", b"  foo", b"f oo", b"f.oo"]

    # Bidi controls and the default-ignorable set, one name each.
    for cp in [*range(0x202A, 0x202F), *range(0x2066, 0x206A)]:
        names.append(f"a{chr(cp)}b".encode())
    for cp in [*range(0x200B, 0x2010), 0x2060, 0xFEFF, 0xE0001, 0xE0020, 0xE007F]:
        names.append(f"a{chr(cp)}b".encode())

    # C0 and C1 controls.
    for cp in range(0x01, 0x20):
        names.append(bytes([0x61, cp, 0x62]))
    names.append(b"a\x7fb")
    for cp in (0x80, 0x85, 0x9F):
        names.append(f"a{chr(cp)}b".encode())

    # Bytes that are not UTF-8, in every shape the decoder meets.
    names += [
        b"\xff",
        b"\xfe\xff",
        b"\x80",
        b"\xc3",  # truncated two-byte sequence
        b"\xc3\x28",  # bad continuation
        b"\xe2\x82",  # truncated three-byte sequence
        b"\xed\xa0\x80",  # UTF-8-encoded surrogate
        b"\xf4\x90\x80\x80",  # above U+10FFFF
        b"pre\xfffix",
        b"\xc3\xa9caf\xe9",
    ]

    # Backslash handling: the escape character itself must round-trip.
    names += [
        b"\\",
        b"\\\\",
        b"a\\xb",
        rb"\x41",
        b"\\u202e",
        rb"\\x41",
        b"back\\slash",
    ]

    # Case and normalization families that fold together (and ones that do not).
    names += [
        b"README",
        b"readme",
        b"ReadMe",
        b"README.md",
        b"readme.md",
        "café".encode(),
        "café".encode(),
        "CAFÉ".encode(),
        "straße".encode(),
        b"STRASSE",
        b"strasse",
        "İstanbul".encode(),
        b"istanbul",
        b"Istanbul",
        "\u0131stanbul".encode(),
        "σος".encode(),
        "ΣΟΣ".encode(),
        "\uff21\uff22".encode(),
        b"AB",
    ]

    # Ordinary names, so the fixture is not made only of hostile input.
    names += [
        b"report.pdf",
        b"report (1).pdf",
        b"2026-09-08 notes.md",
        b".gitignore",
        b".git",
        b"a b c.txt",
        b"-",
        b"--flag",
        b"#hash#",
        b"100%",
        "\U0001f600.png".encode(),
        "åäö".encode(),
        "你好.txt".encode(),
        "مرحبا".encode(),
        "שלום".encode(),
        b"\xf0\x9f\x8f\xb3\xef\xb8\x8f\xe2\x80\x8d\xf0\x9f\x8c\x88",  # ZWJ sequence
    ]

    # Deterministic filler so the fixture stays around three hundred entries and
    # covers the ASCII printable range as both a stem and an extension.
    for cp in range(0x20, 0x7F):
        names.append(f"n{chr(cp)}.dat".encode())

    seen: set[bytes] = set()
    unique: list[bytes] = []
    for candidate in names:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def _entry(name: bytes) -> dict[str, Any]:
    row: dict[str, Any] = {"name_b64": base64.b64encode(name).decode("ascii")}
    try:
        validate(name)
    except InvalidName as exc:
        row["refused"] = exc.code
        return row
    computed = flags(name)
    row["refused"] = None
    row["windows_safe"] = computed.windows_safe
    row["display_warning"] = computed.display_warning
    row["display"] = display(name)
    row["name_key"] = name_key(name)
    return row


def main() -> None:
    payload = {
        "fixture_version": FIXTURE_VERSION,
        "unicode_version": unicodedata.unidata_version,
        "entries": [_entry(name) for name in _inputs()],
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(payload['entries'])} entries to {OUT}")


if __name__ == "__main__":
    main()
