"""Server-side content sniffing over a short allowlist.

The client's declared type is never an input here and is never served: a PNG uploaded as
``text/html`` is served as ``image/png``, and anything the allowlist does not recognise
becomes ``application/octet-stream`` served as an attachment.

A page and a drawing are recognised only where a browser would find them: at the very
start of the head, behind nothing but a byte-order mark, whitespace, comments and — for a
drawing — an XML declaration. The same markers one byte further in prove nothing about
the object as a whole, so a CSV whose cell holds ``<html`` and a JSON string holding
``<script>`` leave the allowlist exactly as they did before. Position is the whole
distinction, and it is what keeps a table of user text from being handed to a browser as
a document. What the two types are then allowed to DO once served is not decided here: it
is the content domain's own policy, which grants a page an opaque origin and no script
and a drawing no network at all.
"""

from __future__ import annotations

import codecs
import json
from dataclasses import dataclass
from typing import Literal

SNIFF_BYTES = 8192

MimeClass = Literal["image", "tabular", "code", "archive", "document", "text", "other"]

__all__ = ["SNIFF_BYTES", "MimeClass", "SniffResult", "sniff_bytes"]


@dataclass(frozen=True, slots=True)
class SniffResult:
    mime: str
    mime_class: MimeClass
    inline_ok: bool


_OCTET = SniffResult("application/octet-stream", "other", False)

# (magic, offset, result). Order matters only in that the first match wins; the byte
# signatures are mutually exclusive.
_MAGIC: tuple[tuple[bytes, int, SniffResult], ...] = (
    (b"\x89PNG\r\n\x1a\n", 0, SniffResult("image/png", "image", True)),
    (b"\xff\xd8\xff", 0, SniffResult("image/jpeg", "image", True)),
    (b"GIF87a", 0, SniffResult("image/gif", "image", True)),
    (b"GIF89a", 0, SniffResult("image/gif", "image", True)),
    (b"%PDF-", 0, SniffResult("application/pdf", "document", False)),
    (b"PK\x03\x04", 0, SniffResult("application/zip", "archive", False)),
    (b"PK\x05\x06", 0, SniffResult("application/zip", "archive", False)),
    (b"PK\x07\x08", 0, SniffResult("application/zip", "archive", False)),
    (b"\x1f\x8b", 0, SniffResult("application/gzip", "archive", False)),
    (b"ustar", 257, SniffResult("application/x-tar", "archive", False)),
    # ``ftyp`` is the type of the first ISO base-media box, so it sits behind that
    # box's four-byte length and never at the start of the object.
    (b"ftyp", 4, SniffResult("video/mp4", "other", True)),
    (b"\x1a\x45\xdf\xa3", 0, SniffResult("video/webm", "other", True)),
)

_MPEG = SniffResult("audio/mpeg", "other", True)

#: The ID3v2 revisions that exist. A tag header is three letters and then a version, so
#: without this a table whose first column is named ``ID3`` would be claimed as a sound.
_ID3_MAJOR_VERSIONS = frozenset({2, 3, 4})

#: What an MPEG audio frame header may say once its eleven sync bits are set. The sync
#: alone is far too short to be evidence — plenty of binary noise opens with ``FF Ex`` —
#: so a frame counts only when the fields behind it are ones the format defines.
_MPEG_RESERVED_VERSION = 0b01
_MPEG_RESERVED_LAYER = 0b00
_MPEG_BAD_BITRATE_INDEX = 0b1111
_MPEG_RESERVED_SAMPLE_RATE = 0b11

_HTML = SniffResult("text/html", "document", True)
_SVG = SniffResult("image/svg+xml", "image", True)
_WAV = SniffResult("audio/wav", "other", True)

#: The opening bytes that make a head a page. Matched case-insensitively against the
#: text once the leading trivia is behind us, never anywhere else.
_HTML_OPENERS = ("<!doctype html", "<html")

#: What ``codecs.BOM_UTF8`` decodes to, spelled once so the source carries no invisible
#: character.
_BYTE_ORDER_MARK = codecs.BOM_UTF8.decode("utf-8")

# A text head carrying any of these anywhere but at its very start is a table, a
# document or a note that happens to quote markup — never the markup itself. It is
# therefore never a page and never a drawing, and it is never given a structured type
# either: a README that quotes ``<script>`` must not be read as JSON or as a table.
_MARKUP_MARKERS = ("<html", "<!doctype html", "<svg", "<script")

#: What such an object is: plain text, which is the only honest thing to call bytes that
#: decode, carry no control characters and hold prose. Calling it
#: ``application/octet-stream`` said nothing true about it and took away the one thing a
#: reader can do with a note, which is read it — every ``.md`` holding a line of inline
#: HTML was unviewable. Declaring it text is safe for the same reason declaring it an
#: octet-stream was: the response carries ``X-Content-Type-Options: nosniff`` under
#: ``default-src 'none'; sandbox`` on the content origin, so a browser handed
#: ``text/plain`` renders characters and never a document, whatever the characters spell.
_QUOTED_MARKUP = SniffResult("text/plain", "text", True)

_DELIMITERS = (",", ";", "\t", "|")

# Everything JSON may contain outside a string: structure, whitespace, numbers and the
# letters of the three bare literals.
_JSON_OUTSIDE_STRING = frozenset("{}[],: \t\r\n-+.0123456789eEtruefalsn")


def sniff_bytes(head: bytes) -> SniffResult:
    """Decide a MIME type from the first ``SNIFF_BYTES`` of an object, bytes only."""
    head = head[:SNIFF_BYTES]
    for magic, offset, result in _MAGIC:
        if head[offset : offset + len(magic)] == magic:
            return result
    if head[0:4] == b"RIFF":
        if head[8:12] == b"WEBP":
            return SniffResult("image/webp", "image", True)
        if head[8:12] == b"WAVE":
            return _WAV
    if _is_id3_tag(head) or _is_mpeg_frame(head):
        return _MPEG

    text = _decode_text(head)
    if text is None or not text:
        return _OCTET

    lowered = text.lower()
    if _opens_with(lowered, _HTML_OPENERS, xml_declaration=False):
        return _HTML
    if _opens_with(lowered, ("<svg",), xml_declaration=True):
        return _SVG
    quotes_markup = any(marker in lowered for marker in _MARKUP_MARKERS)

    # Control characters decide text before structure does: a binary blob that happens to
    # decode and to carry evenly spaced tabs is not a table.
    if _control_ratio(text) > 0.05:
        return _OCTET

    # Asked before the structured types, so a table or a document whose cells quote
    # markup leaves the allowlist for those exactly as it did before.
    if quotes_markup:
        return _QUOTED_MARKUP

    if _is_json(text):
        return SniffResult("application/json", "text", True)
    if _is_csv(text, truncated=not head.endswith(b"\n")):
        return SniffResult("text/csv", "tabular", True)
    return SniffResult("text/plain", "text", True)


def _is_id3_tag(head: bytes) -> bool:
    """An ID3v2 header: the letters, a version that exists, and a syncsafe size."""
    if head[0:3] != b"ID3" or len(head) < 10:
        return False
    if head[3] not in _ID3_MAJOR_VERSIONS or head[4] == 0xFF:
        return False
    return all(byte < 0x80 for byte in head[6:10])


def _is_mpeg_frame(head: bytes) -> bool:
    """An MPEG audio frame header, sync bits AND the fields the format defines."""
    if len(head) < 4 or head[0] != 0xFF or head[1] & 0b1110_0000 != 0b1110_0000:
        return False
    if (head[1] >> 3) & 0b11 == _MPEG_RESERVED_VERSION:
        return False
    if (head[1] >> 1) & 0b11 == _MPEG_RESERVED_LAYER:
        return False
    if head[2] >> 4 == _MPEG_BAD_BITRATE_INDEX:
        return False
    return (head[2] >> 2) & 0b11 != _MPEG_RESERVED_SAMPLE_RATE


def _opens_with(lowered: str, openers: tuple[str, ...], *, xml_declaration: bool) -> bool:
    """Whether the head's first real markup is one of ``openers``.

    Only a byte-order mark, whitespace, comments and — where the type admits one —
    a single XML declaration may precede it. An unterminated comment ends the walk
    with no answer, so a head that opens one and never closes it is never a document.
    """
    rest = lowered.lstrip(_BYTE_ORDER_MARK).lstrip()
    if xml_declaration and rest.startswith("<?xml"):
        end = rest.find("?>")
        if end < 0:
            return False
        rest = rest[end + 2 :].lstrip()
    while rest.startswith("<!--"):
        end = rest.find("-->", 4)
        if end < 0:
            return False
        rest = rest[end + 3 :].lstrip()
    return any(rest.startswith(opener) and _name_ends_at(rest, len(opener)) for opener in openers)


def _name_ends_at(rest: str, index: int) -> bool:
    """Whether the opener really ended there, rather than being a longer name's prefix.

    ``<svgx>`` opens no drawing and ``<htmlish>`` opens no page, so the character after
    the opener has to be one that ends a tag name.
    """
    return index >= len(rest) or rest[index] in ">/" or rest[index].isspace()


def _decode_text(head: bytes) -> str | None:
    """UTF-8 text with no NUL, tolerating a codepoint cut in half by the head boundary."""
    if b"\x00" in head:
        return None
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        return decoder.decode(head, False)
    except UnicodeDecodeError:
        return None


def _control_ratio(text: str) -> float:
    control = sum(1 for ch in text if (ch < " " and ch not in "\t\n\r") or ch == "\x7f")
    return control / len(text)


def _is_json(text: str) -> bool:
    """A whole JSON document, or a prefix of one the 8 KiB head cut short."""
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return False
    try:
        json.loads(stripped)
    except ValueError:
        pass
    else:
        return True
    return _open_json_prefix(stripped)


def _open_json_prefix(text: str) -> bool:
    """True when the text is consistent with JSON and still has a container open.

    A closed but unparseable document is not a prefix — it is something else that merely
    started with a brace.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack.pop() != ch:
                return False
        elif ch not in _JSON_OUTSIDE_STRING:
            return False
    return bool(stack)


def _is_csv(text: str, *, truncated: bool) -> bool:
    lines = text.split("\n")
    if truncated and lines:
        # The head almost certainly cut the last row in half; judging it would be noise.
        lines = lines[:-1]
    rows = [line.rstrip("\r") for line in lines if line.strip()]
    if len(rows) < 2:
        return False
    for delimiter in _DELIMITERS:
        counts = {row.count(delimiter) for row in rows}
        if len(counts) == 1 and counts.pop() >= 1:
            return True
    return False
