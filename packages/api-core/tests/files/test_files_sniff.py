"""Sniffing decides from bytes alone, and only the allowlist may be served inline.

The client's declared type never reaches this function, so there is nothing to spoof —
what matters is that the allowlist is narrow (a browser must not be handed markup it
would render) and that the positive cases have their negative twins: HTML is text and
stays an attachment, an inconsistent table is prose, a PNG with an HTML tail is a PNG.
"""

from __future__ import annotations

import json

import pytest
from alkera_core.files.sniff import SNIFF_BYTES, SniffResult
from alkera_core.files.sniff import sniff_bytes as sniff

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(32)
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(16)
GIF = b"GIF89a" + bytes(16)
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + bytes(16)
ZIP = b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + bytes(16)
GZIP = b"\x1f\x8b\x08\x00" + bytes(16)
TAR = bytes(257) + b"ustar\x0000" + bytes(64)
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"

INLINE_ALLOWLIST = {
    "application/json",
    "text/csv",
    "text/plain",
    "text/html",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/svg+xml",
    "video/mp4",
    "video/webm",
    "audio/mpeg",
    "audio/wav",
}


@pytest.mark.parametrize(
    ("head", "mime", "mime_class"),
    [
        pytest.param(PNG, "image/png", "image", id="png"),
        pytest.param(JPEG, "image/jpeg", "image", id="jpeg"),
        pytest.param(GIF, "image/gif", "image", id="gif"),
        pytest.param(b"GIF87a" + bytes(16), "image/gif", "image", id="gif87a"),
        pytest.param(WEBP, "image/webp", "image", id="webp"),
    ],
)
def test_image_magic_is_served_inline(head: bytes, mime: str, mime_class: str) -> None:
    assert sniff(head) == SniffResult(mime, mime_class, True)


def test_a_png_with_an_html_tail_is_still_a_png() -> None:
    assert sniff(PNG + b"<html><script>alert(1)</script>").mime == "image/png"


def test_riff_that_is_neither_a_picture_nor_a_sound_is_neither() -> None:
    avi = b"RIFF\x24\x00\x00\x00AVI LIST" + bytes(16)

    assert sniff(avi) == SniffResult("application/octet-stream", "other", False)


@pytest.mark.parametrize(
    ("head", "mime", "mime_class"),
    [
        pytest.param(ZIP, "application/zip", "archive", id="zip-local-header"),
        pytest.param(b"PK\x05\x06" + bytes(20), "application/zip", "archive", id="zip-empty"),
        pytest.param(b"PK\x07\x08" + bytes(20), "application/zip", "archive", id="zip-spanned"),
        pytest.param(GZIP, "application/gzip", "archive", id="gzip"),
        pytest.param(TAR, "application/x-tar", "archive", id="tar-ustar-at-257"),
        pytest.param(PDF, "application/pdf", "document", id="pdf"),
    ],
)
def test_binary_containers_are_attachments(head: bytes, mime: str, mime_class: str) -> None:
    assert sniff(head) == SniffResult(mime, mime_class, False)


@pytest.mark.parametrize(
    ("head", "mime"),
    [
        pytest.param(b"<html><body>hi</body></html>", "text/html", id="html"),
        pytest.param(b"<!DOCTYPE html>\n<title>x</title>", "text/html", id="doctype-mixed-case"),
        pytest.param(
            b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>',
            "image/svg+xml",
            id="svg",
        ),
    ],
)
def test_a_head_that_opens_with_markup_is_that_document(head: bytes, mime: str) -> None:
    assert sniff(head).mime == mime


@pytest.mark.parametrize(
    "head",
    [
        pytest.param(b"hello\n<script>fetch('/steal')</script>\n", id="script-in-prose"),
        pytest.param(b'{"note": "safe"}\n<script>x</script>', id="json-with-a-script-tail"),
        pytest.param(b"a,b\n1,<svg onload=x>\n", id="csv-carrying-svg"),
        pytest.param(b"a,b\n1,<html>\n2,<html>\n", id="csv-carrying-html"),
    ],
)
def test_markup_the_head_does_not_open_with_is_text_and_never_a_document(head: bytes) -> None:
    """Text that quotes markup is text.

    It is never the document or the drawing it quotes -- that is what keeps a
    table of user input from being rendered -- and it is never given a
    structured type either, so a reader is not handed it as JSON or as a grid.
    Calling it ``application/octet-stream`` said none of that: it said nothing
    true about the bytes and took away the only thing a person can do with a
    note, which is read it. The markdown a person writes almost always quotes
    something.
    """
    assert sniff(head) == SniffResult("text/plain", "text", True)


def test_a_note_that_quotes_a_script_is_still_only_text() -> None:
    """The case a person meets: a `.md` with a line of inline HTML in it."""
    notes = b"# Notes\n\nA snippet:\n\n    <script>alert(1)</script>\n\nDone.\n"
    assert sniff(notes).mime == "text/plain"


@pytest.mark.parametrize(
    "head",
    [
        pytest.param(b'{"a": 1, "b": [1, 2, 3]}', id="json-object"),
        pytest.param(b"[1, 2, 3]", id="json-array"),
        pytest.param(b'\n\t  {"a": 1}', id="json-after-leading-whitespace"),
        pytest.param(b'{"nested": {"deep": [true, false, null]}}', id="json-bare-literals"),
        pytest.param(b'{"unicode": "caf\xc3\xa9 \xe2\x9c\x93"}', id="json-utf8"),
    ],
)
def test_whole_json_documents_are_json(head: bytes) -> None:
    assert sniff(head) == SniffResult("application/json", "text", True)


def test_a_prefix_of_a_large_json_document_is_json() -> None:
    document = json.dumps([{"id": n, "name": f"row-{n}"} for n in range(2000)]).encode()
    head = document[:SNIFF_BYTES]

    assert len(document) > SNIFF_BYTES
    assert sniff(head).mime == "application/json"


def test_a_json_prefix_cut_inside_a_string_is_json() -> None:
    assert sniff(b'{"name": "a very long value that the head cut in ha').mime == "application/json"


@pytest.mark.parametrize(
    "head",
    [
        pytest.param(rb'{"a": "he said \"hi\" and then the head cut ', id="escaped-quotes"),
        pytest.param(rb'{"win": "C:\\dir\\", "next": [1, 2', id="escaped-backslash-then-quote"),
        pytest.param(
            rb'{"brackets": "]} inside a string", "rest": [', id="closers-inside-a-string"
        ),
    ],
)
def test_escapes_and_brackets_inside_strings_do_not_break_the_prefix_scan(head: bytes) -> None:
    """A `\\"` does not end the string, and structure inside one is not structure."""
    assert sniff(head).mime == "application/json"


def test_a_mismatched_closer_is_not_a_json_prefix() -> None:
    """Legal JSON characters throughout, but `[` closed by `}` — so it is not JSON."""
    assert sniff(b"[1, 2}").mime == "text/plain"


@pytest.mark.parametrize(
    "head",
    [
        pytest.param(b"[1, 2, <not json at all", id="brace-then-garbage"),
        pytest.param(b"{oops]", id="mismatched-closers"),
        pytest.param(b'{"a": 1} trailing words', id="closed-then-trailing-prose"),
        pytest.param(b'not json {"a": 1}', id="does-not-start-with-a-container"),
    ],
)
def test_json_negative_twins_fall_back_to_prose(head: bytes) -> None:
    assert sniff(head).mime != "application/json"


@pytest.mark.parametrize(
    "head",
    [
        pytest.param(b"a,b,c\n1,2,3\n4,5,6\n", id="csv-commas"),
        pytest.param(b"a\tb\tc\n1\t2\t3\n", id="csv-tabs"),
        pytest.param(b"a;b;c\n1;2;3\n", id="csv-semicolons"),
        pytest.param(b"a|b\n1|2\n", id="csv-pipes"),
        pytest.param(b"a,b\r\n1,2\r\n", id="csv-crlf"),
        pytest.param(b"a,b\n1,2\n3,4", id="csv-no-trailing-newline"),
    ],
)
def test_consistent_tables_are_csv(head: bytes) -> None:
    assert sniff(head) == SniffResult("text/csv", "tabular", True)


@pytest.mark.parametrize(
    "head",
    [
        pytest.param(b"a,b,c\n1,2\n3,4,5\n", id="inconsistent-column-counts"),
        pytest.param(b"just one line, with a comma\n", id="single-row-only"),
        pytest.param(b"no delimiter here\nnor on this line\n", id="no-delimiter"),
    ],
)
def test_table_negative_twins_are_plain_text(head: bytes) -> None:
    assert sniff(head) == SniffResult("text/plain", "text", True)


def test_prose_is_plain_text() -> None:
    head = "The quick brown fox.\nCafé — naïve.\n".encode()

    assert sniff(head) == SniffResult("text/plain", "text", True)


@pytest.mark.parametrize(
    "head",
    [
        pytest.param(b"", id="empty-head"),
        pytest.param(b"text\x00with a nul", id="nul-byte"),
        pytest.param(b"\xff\xfe\xfd\xfc", id="invalid-utf8"),
        pytest.param(bytes(range(1, 32)) * 4, id="mostly-control-characters"),
    ],
)
def test_unrecognised_bytes_are_octet_stream_attachments(head: bytes) -> None:
    assert sniff(head) == SniffResult("application/octet-stream", "other", False)


def test_a_codepoint_cut_by_the_head_boundary_is_still_text() -> None:
    head = ("x" * (SNIFF_BYTES - 1)).encode() + "é".encode()[:1]

    assert sniff(head).mime == "text/plain"


def test_only_the_allowlist_is_inline() -> None:
    heads = [
        PNG,
        JPEG,
        GIF,
        WEBP,
        ZIP,
        GZIP,
        TAR,
        PDF,
        b'{"a": 1}',
        b"a,b\n1,2\n",
        b"prose\n",
        b"<html>",
        b"\xff\xfe\xfd\xfc",
    ]

    for head in heads:
        result = sniff(head)
        assert result.inline_ok is (result.mime in INLINE_ALLOWLIST), result


def test_bytes_past_the_sniff_window_do_not_change_the_answer() -> None:
    prose = b"plain prose\n" * 800
    padded = prose + b"\x00" * 16 + b"<script>x</script>"

    assert len(prose) > SNIFF_BYTES
    assert sniff(padded) == SniffResult("text/plain", "text", True)
