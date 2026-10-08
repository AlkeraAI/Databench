"""What a client claims about a type is diagnostics; what the bytes say is served.

A hint can be a lie — the interesting case is a PNG announced as ``text/html``,
which is exactly how a stored object becomes stored XSS if the hint is trusted.
So the sniffed type is the one on the version row and the one the disposition
table is keyed on, and the hint survives only under ``metadata.mime_hint``.
"""

from __future__ import annotations

import pytest
from alkera_core.files.content import disposition_for
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import select
from tests.files.content.conftest import ContentRig, stream

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(range(200))
GIF = b"GIF89a" + bytes(range(200))
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(200))
JSON = b'{"drive": "docs", "nodes": [1, 2, 3], "ok": true}'
CSV = b"name,size,mime\na.bin,10,text/plain\nb.bin,20,text/csv\n"
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + bytes(200)
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n" + bytes(200)
ZIP = b"PK\x03\x04" + bytes(range(200))
TAR = bytes(257) + b"ustar\x0000" + bytes(200)
UNKNOWN = bytes(range(256)) * 4
HTML = b"<!doctype html>\n<title>Q3</title>\n<p>Revenue rose.</p>\n"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>'
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + bytes(200)
WEBM = b"\x1a\x45\xdf\xa3\x01\x00\x00\x00" + bytes(200)
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x23" + bytes(200)
WAV = b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00" + bytes(200)
PROSE = b"The quick brown fox.\nJumped over the lazy dog.\n"


async def put(rig: ContentRig, name: str, payload: bytes, hint: str | None) -> FileVersion:
    node = rig.files[name]
    await rig.service.put_version(
        rig.node_id(name),
        stream(payload),
        size_declared=len(payload),
        if_match=node.etag,
        mime_hint=hint,
    )
    rows = await rig.session.execute(
        select(FileVersion).where(FileVersion.node_id == node.id).order_by(FileVersion.seq.desc())
    )
    version = rows.scalars().first()
    assert version is not None
    return version


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(PNG, "image/png", id="png"),
        pytest.param(GIF, "image/gif", id="gif"),
        pytest.param(JPEG, "image/jpeg", id="jpeg"),
        pytest.param(JSON, "application/json", id="json"),
        pytest.param(CSV, "text/csv", id="csv"),
        pytest.param(ZIP, "application/zip", id="zip"),
        pytest.param(UNKNOWN, "application/octet-stream", id="unknown-bytes"),
    ],
)
async def test_the_sniffed_type_is_stored_whatever_the_hint_claimed(
    content_rig: ContentRig, payload: bytes, expected: str
) -> None:
    # Every case is uploaded under the same wrong hint, so the only thing that
    # can explain a different stored type is the sniff of the bytes themselves.
    version = await put(content_rig, "a.bin", payload, "text/html")

    assert version.mime_sniffed == expected
    assert version.version_metadata["mime_hint"] == "text/html"


async def test_the_hint_is_absent_when_the_client_sent_none(content_rig: ContentRig) -> None:
    version = await put(content_rig, "a.bin", PNG, None)

    assert version.mime_sniffed == "image/png"
    assert "mime_hint" not in version.version_metadata


async def test_a_new_version_is_never_scanned_yet(content_rig: ContentRig) -> None:
    # The model spells the "no scanner has looked at these bytes" state
    # ``pending``; nothing may count as clean because it was just written.
    version = await put(content_rig, "a.bin", PNG, None)

    assert version.scan_state == "pending"


@pytest.mark.parametrize(
    ("payload", "mime", "expected"),
    [
        pytest.param(JSON, "application/json", "inline", id="json-inline"),
        pytest.param(CSV, "text/csv", "inline", id="csv-inline"),
        pytest.param(PROSE, "text/plain", "inline", id="text-inline"),
        pytest.param(PNG, "image/png", "inline", id="png-inline"),
        pytest.param(JPEG, "image/jpeg", "inline", id="jpeg-inline"),
        pytest.param(GIF, "image/gif", "inline", id="gif-inline"),
        pytest.param(WEBP, "image/webp", "inline", id="webp-inline"),
        # The artefact formats an agent is told to write all render in the
        # browser, each under its own CSP keyed on this same sniffed value: a
        # PDF drops ``sandbox`` (which blocks the viewer), a page keeps it and
        # is allowed only the inline CSS and ``data:`` assets it carries, a
        # drawing gets neither script nor a network of its own.
        pytest.param(PDF, "application/pdf", "inline", id="pdf-inline"),
        pytest.param(HTML, "text/html", "inline", id="html-inline"),
        pytest.param(SVG, "image/svg+xml", "inline", id="svg-inline"),
        pytest.param(MP4, "video/mp4", "inline", id="mp4-inline"),
        pytest.param(WEBM, "video/webm", "inline", id="webm-inline"),
        pytest.param(MP3, "audio/mpeg", "inline", id="mpeg-inline"),
        pytest.param(WAV, "audio/wav", "inline", id="wav-inline"),
        pytest.param(ZIP, "application/zip", "attachment", id="zip-attachment"),
        pytest.param(UNKNOWN, "application/octet-stream", "attachment", id="octet-attachment"),
        pytest.param(TAR, "application/x-tar", "attachment", id="tar-attachment"),
    ],
)
async def test_the_disposition_table_is_keyed_on_what_the_bytes_turned_out_to_be(
    content_rig: ContentRig, payload: bytes, mime: str, expected: str
) -> None:
    """The served table, driven end to end by bytes rather than by a type name.

    Every row is written under the same wrong hint, so a row can only be
    explained by the sniff: the type on the version row is what the disposition
    is read from, and the name a client typed reaches neither.
    """
    version = await put(content_rig, "a.bin", payload, "application/octet-stream")

    assert version.mime_sniffed == mime
    assert disposition_for(version.mime_sniffed) == expected


async def test_a_hinted_type_can_never_reach_the_disposition_table(
    content_rig: ContentRig,
) -> None:
    # The bytes are a zip; the hint says text/html, which the table now renders.
    # Trusting the hint would hand a browsing context to an archive nobody can
    # sniff, so the disposition is read from the sniffed column and the hint
    # buys the object nothing.
    version = await put(content_rig, "a.bin", ZIP, "text/html")

    assert version.mime_sniffed == "application/zip"
    assert disposition_for(version.mime_sniffed) == "attachment"
    assert disposition_for(version.version_metadata["mime_hint"]) == "inline"
