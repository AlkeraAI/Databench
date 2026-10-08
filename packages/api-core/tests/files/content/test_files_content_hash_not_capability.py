"""A content hash is an identifier, never a credential.

Knowing the hash of bytes that exist somewhere in the org must not be enough to
get a version pointing at them: the only way in is to stream the bytes. This is
checked twice — once against the module's own API surface, so a future parameter
called ``content_hash`` cannot be added quietly, and once by *doing* it: naming
an existing object's hash in every field a caller controls and asserting nothing
was created and nothing was linked.
"""

from __future__ import annotations

import hashlib
import inspect

import pytest
from alkera_core.config import settings
from alkera_core.files import content as content_module
from alkera_core.files.content import ContentService
from alkera_core.files.errors import InvalidRequest
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from tests.files.content.conftest import ContentRig, stream

SECRET = (hashlib.sha256(b"someone-elses-bytes").digest() * 4096)[
    : settings.files_inline_max_bytes + 512
]

#: Every word a "give me the version for this hash" parameter could be spelled.
HASH_WORDS = ("hash", "digest", "checksum", "etag_bytes", "content_address")


def public_callables() -> list[tuple[str, inspect.Signature]]:
    """Every entry point the module offers, with its signature."""
    found: list[tuple[str, inspect.Signature]] = []
    for name in content_module.__all__:
        obj = getattr(content_module, name)
        if inspect.isfunction(obj):
            found.append((name, inspect.signature(obj)))
        elif inspect.isclass(obj):
            for method_name, method in vars(obj).items():
                if method_name.startswith("_") or not callable(method):
                    continue
                found.append((f"{name}.{method_name}", inspect.signature(method)))
    return found


def test_no_public_entry_point_accepts_a_client_supplied_hash() -> None:
    """The API surface itself forbids the shortcut."""
    offenders = [
        f"{where}({parameter})"
        for where, signature in public_callables()
        for parameter in signature.parameters
        if any(word in parameter for word in HASH_WORDS)
    ]
    assert offenders == []
    # And the one write path takes bytes, positionally, with no way around it.
    write = inspect.signature(ContentService.put_version)
    assert list(write.parameters) == [
        "self",
        "node_id",
        "data",
        "size_declared",
        "if_match",
        "mime_hint",
        "lease",
        # Where the bytes go (the head, or a version beside it), never which.
        "park",
        # What the version records as its origin (an upload, a document's
        # write back), never anything about its bytes.
        "source",
        # Where in a co-edited document the bytes were taken from, recorded
        # on the version; never anything about the bytes themselves.
        "origin",
    ]
    assert write.parameters["data"].annotation == "AsyncIterator[bytes]"


async def test_a_known_hash_in_the_mime_hint_links_nothing(content_rig: ContentRig) -> None:
    """The hint is diagnostics. Naming a live object's hash there creates nothing."""
    stored = await content_rig.service.put_version(
        content_rig.node_id("b.bin"), stream(SECRET), size_declared=len(SECRET), if_match=0
    )

    # A caller who somehow learned the hash sends it as the only thing they can
    # control that reaches the version row, with no body at all.
    empty = await content_rig.service.put_version(
        content_rig.node_id("a.bin"),
        stream(b""),
        size_declared=0,
        if_match=0,
        mime_hint=stored.content_hash,
    )

    assert empty.size == 0
    assert empty.content_hash != stored.content_hash
    version = await content_rig.session.get(FileVersion, empty.id)
    assert version is not None
    # The bytes are the empty ones the caller actually sent; the hint is kept
    # only as a diagnostic and never becomes an address.
    assert version.inline_bytes == b""
    assert version.store_key is None
    assert version.version_metadata["mime_hint"] == stored.content_hash
    # And the hint is not what gets served.
    assert version.mime_sniffed == "application/octet-stream"


async def test_declaring_a_size_with_no_body_creates_nothing(content_rig: ContentRig) -> None:
    """A request that names bytes it does not send is refused, not fulfilled."""
    await content_rig.service.put_version(
        content_rig.node_id("b.bin"), stream(SECRET), size_declared=len(SECRET), if_match=0
    )

    with pytest.raises(InvalidRequest) as raised:
        await content_rig.service.put_version(
            content_rig.node_id("a.bin"),
            stream(b""),
            size_declared=len(SECRET),
            if_match=0,
        )
    assert raised.value.code == "files.size_mismatch"

    node = await content_rig.session.get(FileNode, content_rig.node_id("a.bin"))
    assert node is not None
    await content_rig.session.refresh(node)
    assert node.head_version_id is None
    assert node.etag == 0


async def test_the_sniffed_type_wins_over_the_client_hint(content_rig: ContentRig) -> None:
    """A PNG uploaded as text/html is served as image/png."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 512

    info = await content_rig.service.put_version(
        content_rig.node_id("a.bin"),
        stream(png),
        size_declared=len(png),
        if_match=0,
        mime_hint="text/html",
    )

    assert info.mime == "image/png"
    version = await content_rig.session.get(FileVersion, info.id)
    assert version is not None
    assert version.mime_sniffed == "image/png"
    assert version.version_metadata["mime_hint"] == "text/html"
