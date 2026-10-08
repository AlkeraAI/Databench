"""The naming contract for Alkera Files — a name has to reach every machine.

A name is a sequence of bytes, not text. The floor is what Linux refuses
(empty, NUL, ``/``, ``.``, ``..``); on top of that the drive refuses the three
things a name can be that no machine could then *hold*: longer than
:data:`NAME_MAX_BYTES` (``NAME_MAX`` less the room the pull's sidecar needs), a
control character, or a leading/trailing space; and one thing a name can be
that no reader could then trust: a bidirectional control, which draws the name
as a different one. Everything else is accepted and flagged instead, so a
client can tell what a Windows or a display surface would struggle with.
Uniqueness is byte-exact and lives in the namespace layer; nothing here decides
it. The one comparison it asks of this module beyond the
bytes is :func:`normalization_key`: a person's create or rename is refused when
a sibling spells the same text in another Unicode normalization form, because a
macOS disk holds only one of the two.

:func:`validate` judges a name a caller is *proposing*. It is never run against
a row already in the tree: names stored before a rule tightened stay readable,
listable, searchable, downloadable and renameable-to-something-shorter, and
:func:`flags`, :func:`display`, :func:`name_key` and :func:`parse_display` are
total over any bytes at all precisely so that stays true.

Pure functions only: no I/O, no clock, no globals.

The display escape scheme
-------------------------
:func:`display` renders a name as text that :func:`parse_display` turns back
into the original bytes for *every* input, so it is safe to show, log and paste.
A backslash is the escape character:

===================  ==========================================
Sequence             Meaning
===================  ==========================================
``\\\\``               a literal backslash byte (0x5C)
``\\xNN``              one byte that is not part of valid UTF-8
``\\uNNNN``            a BMP code point that is invisible or a control
``\\UNNNNNNNN``        the same, above the BMP (tag characters)
===================  ==========================================

Hex digits are lowercase, and every other code point is rendered as itself. A
name that really contains the four characters ``\\x41`` displays as ``\\\\x41``,
so the escape is unambiguous in both directions.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

__all__ = [
    "BIDI_CONTROLS",
    "FS_NAME_MAX_BYTES",
    "NAME_MAX_BYTES",
    "PULL_PART_SUFFIX",
    "UNICODE_VERSION",
    "InvalidName",
    "NameFlags",
    "display",
    "escape_to_name",
    "flags",
    "folding_collisions",
    "is_control",
    "name_key",
    "normalization_key",
    "normalization_twin",
    "parse_display",
    "refused_in_a_name",
    "validate",
]

FS_NAME_MAX_BYTES: Final = 255
"""``NAME_MAX`` — the longest single path component Linux will write."""

PULL_PART_SUFFIX: Final = b".alkera-part"
"""The sidecar ``alkera files pull`` streams a file into beside its target.

It lives here, next to the rule that reserves room for it, because the two are
one decision: the pull promotes ``<name>.alkera-part`` into place once the hash
matches, so a name the server accepts is only reachable on a machine if the
sidecar for it also fits. `alkera_cli.files.pull` imports this rather than
spelling the suffix again.
"""

NAME_MAX_BYTES: Final = FS_NAME_MAX_BYTES - len(PULL_PART_SUFFIX)
"""The longest name the drive accepts — ``NAME_MAX`` less the sidecar's room.

Not ``NAME_MAX`` itself. A 255-byte name is legal on the filesystem and can
never be *pulled* onto one: the sidecar beside it is 267 bytes and the kernel
refuses it, so the file would exist in the drive and be unreachable from every
machine. Derived rather than written down, so lengthening the sidecar lowers
the ceiling with it instead of silently reopening the gap.
"""

UNICODE_VERSION: Final = "15.1.0"
"""CPython 3.13's Unicode database version; the conformance fixture asserts it."""


class InvalidName(ValueError):  # noqa: N818 - a validation verdict, not an error class
    """A name Linux itself would refuse. ``code`` names which rule refused it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def is_control(cp: int) -> bool:
    """C0, DEL and C1 — a code point no terminal, shell or field renders as itself.

    Public because a caller that *repairs* text into a name (a chat title
    becoming a folder name) has to sweep exactly what :func:`validate` refuses,
    and a second spelling of the range is a second answer.
    """
    return cp <= 0x1F or 0x7F <= cp <= 0x9F


def refused_in_a_name(cp: int) -> bool:
    """A code point :func:`validate` refuses anywhere in a name: a control
    (:func:`is_control`) or a bidirectional control (:data:`BIDI_CONTROLS`).

    Public for the same reason :func:`is_control` is: a caller that repairs
    text into a name sweeps exactly this set, never its own spelling of it."""
    return is_control(cp) or cp in BIDI_CONTROLS


def validate(name: bytes) -> None:
    """Raise :class:`InvalidName` if the drive would refuse ``name``, else return.

    Nothing outside this list is ever refused: reserved Windows device names,
    trailing dots, zero-width characters and bytes that are not UTF-8 are all
    legal and are reported through :func:`flags` instead. A bidi control is
    refused: ``report<U+202E>gnp.exe`` reads as ``reportexe.png`` in every
    listing, a name that lies about what it is. A name stored before that rule
    still displays escaped.

    The order is the order a person reads a field: a blank name is empty rather
    than too short, and a name is only measured once it is a name at all.
    """
    if not name:
        raise InvalidName("empty", "a name may not be empty")
    if b"\x00" in name:
        raise InvalidName("nul", "a name may not contain a NUL byte")
    if b"/" in name:
        raise InvalidName("separator", "a name may not contain '/'")
    if name in (b".", b".."):
        raise InvalidName("dot", "'.' and '..' are reserved by the filesystem")
    if len(name) > NAME_MAX_BYTES:
        raise InvalidName("too_long", f"a name may not exceed {NAME_MAX_BYTES} bytes")
    text = name.decode("utf-8", "surrogateescape")
    if any(is_control(ord(ch)) for ch in text):
        raise InvalidName("control", "a name may not contain a control character")
    if any(ord(ch) in BIDI_CONTROLS for ch in text):
        raise InvalidName(
            "bidi_control",
            "a name may not contain a bidirectional control character (such as U+202E), "
            "which makes it read as a different name",
        )
    if text != text.strip():
        raise InvalidName("surrounding_space", "a name may not start or end with a space")


#: The escape character of :func:`escape_to_name`. Always escaped itself, which
#: is what makes the escape injective.
_ESCAPE: Final = "%"

#: The digest a too-long escape ends with: ``~`` and this many hex digits.
_DIGEST_HEX: Final = 16


def escape_to_name(raw: bytes) -> bytes:
    """``raw`` made into a name :func:`validate` accepts — for a name the system
    derives from text nobody proposed as a name (an email address, a team name).

    Percent-escaping, byte by byte: ``%`` itself, the separator, NUL, every
    control character and every bidi control become ``%XX`` (uppercase hex, one per UTF-8 byte), and
    so do whitespace at either end and a whole name of ``.`` or ``..``.
    Everything else is kept as it is, so text that already is a valid name and
    holds no ``%`` comes back unchanged.

    Injective: ``%`` is always escaped, so reading ``%XX`` back as a byte
    recovers ``raw`` exactly, and two different inputs can never be given the
    same name. The one exception is length. An escape longer than
    :data:`NAME_MAX_BYTES` is cut on a character boundary and ends in ``~`` plus
    the first 64 bits of the SHA-256 of ``raw``. Two such inputs share a name
    only if they share that digest -- and a SHORT input that is itself already
    of that shape (a 240-243 byte ``<prefix>~<16 hex>``) escapes to itself, so
    it can equal a long input's escape. Addresses cannot reach that case (a
    local part is at most 64 bytes and a domain holds no ``~``).
    """
    text = raw.decode("utf-8", "surrogateescape")
    last = len(text) - 1
    pieces: list[bytes] = []
    for index, ch in enumerate(text):
        cp = ord(ch)
        if 0xDC80 <= cp <= 0xDCFF:
            # A byte that was not UTF-8, carried through by surrogateescape. The
            # grammar accepts it; keep the byte itself.
            pieces.append(bytes((cp - 0xDC00,)))
            continue
        piece = ch.encode("utf-8")
        at_an_end = index in (0, last)
        if ch in (_ESCAPE, "/") or refused_in_a_name(cp) or (at_an_end and ch.isspace()):
            piece = b"".join(b"%%%02X" % byte for byte in piece)
        pieces.append(piece)
    escaped = b"".join(pieces)
    if escaped in (b".", b".."):
        return b"%2E" * len(escaped)
    if len(escaped) <= NAME_MAX_BYTES:
        return escaped
    digest = b"~" + hashlib.sha256(raw).hexdigest()[:_DIGEST_HEX].encode("ascii")
    budget = NAME_MAX_BYTES - len(digest)
    kept: list[bytes] = []
    used = 0
    for piece in pieces:
        if used + len(piece) > budget:
            break
        kept.append(piece)
        used += len(piece)
    return b"".join(kept) + digest


@dataclass(frozen=True, slots=True)
class NameFlags:
    """What a client should know about an accepted name.

    ``macos_safe`` is deliberately absent: it is a property of a name *among its
    siblings* (a case- or normalization-folding collision), so the namespace
    layer derives it from :func:`folding_collisions`, not from one name alone.
    """

    windows_safe: bool
    display_warning: bool


_WINDOWS_FORBIDDEN: Final = frozenset('<>:"/\\|?*')
_WINDOWS_DEVICE_STEMS: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{d}" for d in range(1, 10)}
    | {f"LPT{d}" for d in range(1, 10)}
)

#: Unicode's Bidi_Control property, every code point of it. A name carrying one
#: can be drawn as a different name (``report<U+202E>gnp.exe`` reads
#: ``reportexe.png``), so each is escaped by :func:`display`. The web client's
#: own copy of this list (``apps/web/src/lib/files/shownName.ts``)
#: is pinned to this one by ``test_files_name_bidi.py``, and that list is pinned
#: to the Unicode property itself by the web suite.
BIDI_CONTROLS: Final = frozenset(
    {0x061C, 0x200E, 0x200F} | set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))
)
_BIDI_CONTROLS: Final = BIDI_CONTROLS
_INVISIBLES: Final = (
    frozenset(range(0x200B, 0x2010))
    | {0x2060, 0xFEFF, 0xE0001}
    | frozenset(range(0xE0020, 0xE0080))
)


def _is_suspicious(cp: int) -> bool:
    """A code point that must be rendered visibly rather than pasted through."""
    return is_control(cp) or cp in _BIDI_CONTROLS or cp in _INVISIBLES


def flags(name: bytes) -> NameFlags:
    """Report what surfaces would struggle with ``name``. Never a refusal."""
    text = name.decode("utf-8", "surrogateescape")
    undecodable = any(0xDC80 <= ord(ch) <= 0xDCFF for ch in text)

    windows_safe = True
    if any(ch in _WINDOWS_FORBIDDEN or ord(ch) <= 0x1F for ch in text):
        windows_safe = False
    elif text.endswith((".", " ")):
        windows_safe = False
    elif text.split(".", 1)[0].upper() in _WINDOWS_DEVICE_STEMS:
        # Windows resolves the device before the extension, so "NUL.txt" is the
        # NUL device while "COM10" and "COM¹" are ordinary names.
        windows_safe = False

    display_warning = undecodable or any(_is_suspicious(ord(ch)) for ch in text)
    return NameFlags(windows_safe=windows_safe, display_warning=display_warning)


def display(name: bytes) -> str:
    """Render ``name`` as text that :func:`parse_display` inverts exactly."""
    out: list[str] = []
    for ch in name.decode("utf-8", "surrogateescape"):
        cp = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif 0xDC80 <= cp <= 0xDCFF:
            out.append(f"\\x{cp - 0xDC00:02x}")
        elif _is_suspicious(cp):
            out.append(f"\\u{cp:04x}" if cp <= 0xFFFF else f"\\U{cp:08x}")
        else:
            out.append(ch)
    return "".join(out)


_ESCAPE_WIDTHS: Final = {"x": 2, "u": 4, "U": 8}


def parse_display(text: str) -> bytes:
    """The exact inverse of :func:`display`.

    Raises ``ValueError`` on a backslash sequence :func:`display` never emits,
    so a hand-typed name cannot silently decode to something else.
    """
    out = bytearray()
    i = 0
    end = len(text)
    while i < end:
        ch = text[i]
        if ch != "\\":
            out += ch.encode("utf-8")
            i += 1
            continue
        if i + 1 >= end:
            raise ValueError("a display name may not end with a lone backslash")
        marker = text[i + 1]
        if marker == "\\":
            out += b"\\"
            i += 2
            continue
        width = _ESCAPE_WIDTHS.get(marker)
        if width is None:
            raise ValueError(f"unknown escape '\\{marker}' in a display name")
        digits = text[i + 2 : i + 2 + width]
        if len(digits) != width or any(c not in "0123456789abcdef" for c in digits):
            raise ValueError(f"malformed '\\{marker}' escape in a display name")
        value = int(digits, 16)
        if marker == "x":
            out.append(value)
        else:
            if value > 0x10FFFF or 0xD800 <= value <= 0xDFFF:
                raise ValueError("a display escape may not name a surrogate or non-character")
            out += chr(value).encode("utf-8")
        i += 2 + width
    return bytes(out)


def name_key(name: bytes) -> str:
    """A folded key for search, sort and collision reports — never uniqueness.

    ``NFC(full_casefold(display(name)))``. Full casefold is locale-independent,
    so ``ß`` folds to ``ss`` while the Turkish dotted ``İ`` folds to ``i`` plus a
    combining dot and the dotless i (U+0131) folds to itself: neither equals ``i``.
    """
    return unicodedata.normalize("NFC", display(name).casefold())


def normalization_key(name: bytes) -> str:
    """``NFC(display(name))``: two names with one key are the same text in two
    Unicode normalization forms (``é`` as one code point, or ``e`` plus a
    combining accent). Case is kept: a case pair is two names on Linux and on a
    case-sensitive disk, and is flagged rather than refused."""
    return unicodedata.normalize("NFC", display(name))


def normalization_twin(siblings: Iterable[bytes], name: bytes) -> bytes | None:
    """The sibling that is ``name`` in another normalization form, or ``None``.

    The bytes themselves are not a twin: an exact match is the unique index's
    to refuse, with its own answer.
    """
    key = normalization_key(name)
    for sibling in siblings:
        if sibling != name and normalization_key(sibling) == key:
            return sibling
    return None


def folding_collisions(siblings: Iterable[bytes]) -> list[tuple[bytes, bytes]]:
    """Pairs a case- or normalization-insensitive client cannot both materialize.

    Every pair of distinct byte strings that share a :func:`name_key`, each pair
    once, ordered as a left-to-right scan of ``siblings`` encounters them.
    """
    names = list(siblings)
    keys = [name_key(n) for n in names]
    pairs: list[tuple[bytes, bytes]] = []
    for i, left in enumerate(names):
        for j in range(i + 1, len(names)):
            right = names[j]
            if left != right and keys[i] == keys[j]:
                pairs.append((left, right))
    return pairs
