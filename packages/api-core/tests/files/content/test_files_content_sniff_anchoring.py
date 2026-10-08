"""Markup counts only where a browser would parse it: at the very start of the head.

A page an agent writes is a deliverable and must render; a CSV cell containing
``<html`` is a CSV, and a JSON string containing ``<script>`` is JSON. The two
cases are told apart by position alone — the document types are recognised from
the head's first non-trivia bytes, and the same markers anywhere further in
still take the whole object off the allowlist. Position is the entire defence,
so every positive here has the negative twin that puts the identical marker one
byte later.

The media signatures next to them are byte magics with the same shape: each
positive is paired with a near-miss that flips one byte of the signature, so a
test cannot pass by recognising the payload rather than the magic.
"""

from __future__ import annotations

import pytest
from alkera_core.files.content import INLINE_MIME_TYPES
from alkera_core.files.sniff import SniffResult
from alkera_core.files.sniff import sniff_bytes as sniff

OCTET = SniffResult("application/octet-stream", "other", False)

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + bytes(32)
WEBM = b"\x1a\x45\xdf\xa3\x01\x00\x00\x00" + bytes(32)
MP3_ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x23" + bytes(32)
MP3_SYNC = b"\xff\xfb\x90\x64" + bytes(32)
WAV = b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00" + bytes(32)


@pytest.mark.parametrize(
    "head",
    [
        pytest.param(b"<html><body>hi</body></html>", id="bare-html-element"),
        pytest.param(b"<HTML LANG=en><body>hi</body></HTML>", id="upper-case-element"),
        pytest.param(b"<!DOCTYPE html>\n<title>x</title>", id="doctype-mixed-case"),
        pytest.param(b"<!doctype HTML>\n<p>x</p>", id="doctype-other-casing"),
        pytest.param(b"\xef\xbb\xbf<!doctype html>\n<p>x</p>", id="after-a-utf8-bom"),
        pytest.param(b"\n\n   <html>\n<p>x</p>", id="after-leading-whitespace"),
        pytest.param(b"<!-- built by the agent -->\n<html><p>x</p></html>", id="after-a-comment"),
        pytest.param(
            b"<!-- one --><!-- two --><!doctype html><p>x</p>", id="after-several-comments"
        ),
        pytest.param(
            b"<!doctype html>\n<script>document.title='x'</script>", id="carrying-a-script"
        ),
    ],
)
def test_a_head_that_starts_as_a_page_is_a_page(head: bytes) -> None:
    assert sniff(head) == SniffResult("text/html", "document", True)


@pytest.mark.parametrize(
    "head",
    [
        pytest.param(b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>', id="bare"),
        pytest.param(b"<SVG width='2'><rect/></SVG>", id="upper-case"),
        pytest.param(
            b'<?xml version="1.0" encoding="UTF-8"?>\n<svg><rect/></svg>',
            id="after-an-xml-declaration",
        ),
        pytest.param(
            b'<?xml version="1.0"?>\n<!-- Generator: a drawing tool -->\n<svg><rect/></svg>',
            id="after-a-declaration-and-a-comment",
        ),
        pytest.param(b"\xef\xbb\xbf  <svg><rect/></svg>", id="after-a-bom-and-whitespace"),
        pytest.param(b'<svg onload="steal()"><rect/></svg>', id="carrying-an-event-handler"),
    ],
)
def test_a_head_that_starts_as_a_drawing_is_a_drawing(head: bytes) -> None:
    assert sniff(head) == SniffResult("image/svg+xml", "image", True)


@pytest.mark.parametrize(
    "head",
    [
        pytest.param(b"name,body\na,<html>\nb,<html>\n", id="csv-cell-holding-an-html-tag"),
        pytest.param(b"name,body\na,<svg/>\nb,<svg/>\n", id="csv-cell-holding-an-svg-tag"),
        pytest.param(b'{"note": "<script>steal()</script>"}', id="json-string-holding-a-script"),
        pytest.param(b'{"page": "<html><body>x</body></html>"}', id="json-string-holding-html"),
        pytest.param(b"Notes about the format.\nA page starts with <html>.\n", id="html-mid-prose"),
        pytest.param(b"Read me first.\n<svg viewBox='0 0 1 1'/>\n", id="svg-mid-prose"),
        pytest.param(b"x<!doctype html>", id="one-byte-before-the-doctype"),
        pytest.param(b"<!-- unterminated comment <html><p>x</p>", id="comment-never-closed"),
        pytest.param(b"<?xml version='1.0'?><html><p>x</p>", id="xml-declaration-then-html"),
        pytest.param(b"<svgx><rect/></svgx>", id="a-longer-tag-name-starting-with-svg"),
        pytest.param(b"<htmlish><p>x</p></htmlish>", id="a-longer-tag-name-starting-with-html"),
    ],
)
def test_a_marker_anywhere_but_the_start_takes_the_object_off_the_allowlist(head: bytes) -> None:
    """Never the document or the drawing it quotes, and never a structured type.

    Text that merely CONTAINS markup is text, and is served as ``text/plain``
    under ``nosniff`` on an origin whose policy is ``default-src 'none';
    sandbox`` — so a browser handed it draws characters and cannot be talked
    into drawing a page. What the anchoring rule protects is the TYPE: this
    object is never ``text/html``, never ``image/svg+xml``, and never the JSON
    or the table it would otherwise have been read as.
    """
    assert sniff(head) == SniffResult("text/plain", "text", True)


def test_a_tag_that_merely_resembles_the_opening_one_is_not_a_document() -> None:
    """``<htm>`` carries no marker at all, so it is prose — and never a page."""
    assert sniff(b"<htm><p>x</p></htm>").mime == "text/plain"


def test_a_png_carrying_a_page_in_its_tail_is_still_a_png() -> None:
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(32)

    assert sniff(png + b"<!doctype html><script>alert(1)</script>").mime == "image/png"


@pytest.mark.parametrize(
    ("head", "mime", "mime_class"),
    [
        pytest.param(MP4, "video/mp4", "other", id="mp4"),
        pytest.param(WEBM, "video/webm", "other", id="webm"),
        pytest.param(MP3_ID3, "audio/mpeg", "other", id="mp3-with-an-id3-header"),
        pytest.param(MP3_SYNC, "audio/mpeg", "other", id="mp3-starting-on-a-frame"),
        pytest.param(WAV, "audio/wav", "other", id="wav"),
    ],
)
def test_the_media_magics_are_recognised(head: bytes, mime: str, mime_class: str) -> None:
    assert sniff(head) == SniffResult(mime, mime_class, True)


@pytest.mark.parametrize(
    "head",
    [
        # `ftyp` belongs at byte 4, behind the box length — at the very start it
        # is not a box header at all.
        pytest.param(b"ftypmp42\x00\x00\x00\x00mp42isom" + bytes(32), id="mp4-brand-at-offset-0"),
        pytest.param(b"\x00\x00\x00\x18ftyqmp42" + bytes(32), id="mp4-brand-misspelt"),
        pytest.param(b"\x1a\x45\xdf\xa4\x01\x00\x00\x00" + bytes(32), id="webm-magic-last-byte"),
        pytest.param(b"ID2\x04\x00\x00\x00\x00\x00\x23" + bytes(32), id="id3-tag-misspelt"),
        # A frame sync is eleven bits; the fields behind it are what tell an MP3
        # from binary noise that happens to open with 0xFF.
        pytest.param(b"\xff\xfb\xf0\x64" + bytes(32), id="mp3-reserved-bitrate-index"),
        pytest.param(b"\xff\xfb\x9c\x64" + bytes(32), id="mp3-reserved-sample-rate"),
        pytest.param(b"\xff\xf9\x90\x64" + bytes(32), id="mp3-reserved-layer"),
        pytest.param(b"\xff\xeb\x90\x64" + bytes(32), id="mp3-reserved-version"),
        pytest.param(b"\xff\xd0\x90\x64" + bytes(32), id="mp3-no-frame-sync"),
        pytest.param(b"RIFF\x24\x08\x00\x00AVI LIST" + bytes(32), id="riff-that-is-not-wave"),
    ],
)
def test_a_near_miss_of_a_media_magic_is_not_media(head: bytes) -> None:
    assert sniff(head) == OCTET


def test_the_riff_container_still_tells_a_picture_from_a_sound() -> None:
    webp = b"RIFF\x24\x00\x00\x00WEBPVP8 " + bytes(16)

    assert sniff(webp).mime == "image/webp"
    assert sniff(WAV).mime == "audio/wav"


@pytest.mark.parametrize(
    "mime",
    [
        pytest.param("text/html", id="html"),
        pytest.param("image/svg+xml", id="svg"),
        pytest.param("video/mp4", id="mp4"),
        pytest.param("video/webm", id="webm"),
        pytest.param("audio/mpeg", id="mpeg"),
        pytest.param("audio/wav", id="wav"),
    ],
)
def test_every_newly_recognised_type_is_one_the_content_route_renders(mime: str) -> None:
    """A type the sniffer learns is useless unless the route will serve it."""
    assert mime in INLINE_MIME_TYPES
