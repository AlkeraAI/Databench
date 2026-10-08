"""The key layout: the single spelling authority for object-store keys.

Keys are portable plain strings: lowercase, under 1 KiB, no object metadata,
no user bytes. A ``DomainStore`` takes and returns *relative* keys and
prepends its own ``domains/<domain uuid>/`` prefix; :func:`absolute` is for
the admin handle only.
"""

from __future__ import annotations

import re
from typing import Final, Literal
from uuid import UUID

from alkera_core.files.ids import DomainId
from alkera_core.files.store.errors import InvalidKey

KeyLayout = Literal["domain", "bucket"]
"""Which key namespace a driver owns: one domain's own, or the whole bucket."""

MAX_KEY_BYTES: Final = 1024
"""S3's hard ceiling, measured on the **bucket-absolute** key in UTF-8 bytes."""

DOMAIN_PREFIX: Final = "domains/"

_DOMAIN_PREFIX_BYTES: Final = len(DOMAIN_PREFIX) + 36 + 1
"""``domains/`` + a canonical uuid + the separating ``/``: what :func:`absolute`
prepends to every relative key a ``DomainStore`` is handed."""

MAX_RELATIVE_KEY_BYTES: Final = MAX_KEY_BYTES - _DOMAIN_PREFIX_BYTES
"""The budget a relative key actually has.

A relative key is only ever addressed through its absolute form, so the limit
that binds it is 1024 minus the domain prefix. Validating the relative key at
1024 let a legal key become an illegal 1069-byte request — a permanent 400 on
AWS, an unmapped 500 that the retry loop chews on elsewhere.
"""

_SEGMENT_TOKEN: Final = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def validate_relative_key(key: str) -> None:
    """Raise :class:`InvalidKey` unless ``key`` is a portable relative key.

    Refused: an empty key, a leading ``/``, an empty segment (``a//b`` or a
    trailing slash), a ``.`` or ``..`` segment, a NUL byte, an uppercase
    character, a ``domains/`` prefix (that is the absolute namespace, never a
    relative key) and anything whose absolute form would not fit in 1 KiB
    (:data:`MAX_RELATIVE_KEY_BYTES`, the 1 KiB ceiling less the domain prefix
    :func:`absolute` prepends).
    """
    if not key:
        raise InvalidKey("empty key")
    if "\x00" in key:
        raise InvalidKey("key contains NUL")
    if key.startswith("/"):
        raise InvalidKey("key is absolute")
    encoded = len(key.encode("utf-8"))
    if encoded > MAX_RELATIVE_KEY_BYTES:
        raise InvalidKey(f"key is {encoded} bytes, over the {MAX_RELATIVE_KEY_BYTES} byte limit")
    if key != key.lower():
        raise InvalidKey("key is not lowercase")
    if key == DOMAIN_PREFIX.rstrip("/") or key.startswith(DOMAIN_PREFIX):
        raise InvalidKey("relative key may not carry the domains/ prefix")
    for segment in key.split("/"):
        if not segment:
            raise InvalidKey("key contains an empty segment")
        if segment in (".", ".."):
            raise InvalidKey(f"key contains a {segment!r} segment")
        if segment.startswith("."):
            # Dot-leading segments are the drivers' own scratch namespace
            # (``.parts/<upload id>/`` staging, ``.<name>.tmp`` half-written
            # publishes). Refusing them here is what keeps a caller from
            # naming an object that a reconciler would then have to tell
            # apart from a driver's own leftovers.
            raise InvalidKey(f"segment {segment!r} is in the reserved dot namespace")


def validate_absolute_key(key: str) -> None:
    """Raise :class:`InvalidKey` unless ``key`` is a bucket-absolute domain key.

    The shape is ``domains/<domain uuid>/<relative key>``: the admin handle
    addresses the bucket, so it is the one place a key legitimately carries a
    domain prefix, and everything after that prefix is held to exactly the
    relative-key rules.
    """
    if not key.startswith(DOMAIN_PREFIX):
        raise InvalidKey(f"absolute key {key!r} does not start with {DOMAIN_PREFIX!r}")
    encoded = len(key.encode("utf-8"))
    if encoded > MAX_KEY_BYTES:
        raise InvalidKey(f"key is {encoded} bytes, over the {MAX_KEY_BYTES} byte limit")
    rest = key[len(DOMAIN_PREFIX) :]
    domain, _, relative = rest.partition("/")
    try:
        UUID(domain)
    except ValueError as exc:
        raise InvalidKey(f"absolute key {key!r} names no domain uuid") from exc
    if not relative:
        raise InvalidKey(f"absolute key {key!r} names a domain but no object")
    validate_relative_key(relative)


def split_absolute_key(key: str) -> tuple[str, str]:
    """``domains/<uuid>/<relative>`` split into its domain prefix and remainder."""
    validate_absolute_key(key)
    rest = key[len(DOMAIN_PREFIX) :]
    domain, _, relative = rest.partition("/")
    return f"{DOMAIN_PREFIX}{domain}", relative


def validate_key(key: str, layout: KeyLayout) -> None:
    """Validate ``key`` for the namespace a driver was opened on."""
    if layout == "bucket":
        validate_absolute_key(key)
    else:
        validate_relative_key(key)


def object_key(content_hash: bytes) -> str:
    """The sharded, content-addressed key for a blob."""
    if not content_hash:
        raise InvalidKey("content hash is empty")
    hexed = content_hash.hex()
    if len(hexed) < 4:
        raise InvalidKey("content hash is too short to shard")
    return f"objects/{hexed[:2]}/{hexed[2:4]}/{hexed}"


def incoming_key(session_id: UUID, name: str) -> str:
    """A part or whole object staged under an upload session.

    ``name`` is a driver-chosen token (a part number, ``object``), never a
    user-supplied file name — that is why it is validated as a token.
    """
    if not _SEGMENT_TOKEN.match(name):
        raise InvalidKey(f"incoming name {name!r} is not a lowercase token")
    return f"incoming/{session_id}/{name}"


def deleted_key(original: str) -> str:
    """Where a two-phase delete parks an object before the GC sweep."""
    validate_relative_key(original)
    return f"deleted/{original}"


def erased_key(original: str) -> str:
    """Where an erasure parks an object for its short expiry."""
    validate_relative_key(original)
    return f"erased/{original}"


def absolute(domain_id: DomainId, relative: str) -> str:
    """The bucket-absolute key for ``relative`` inside ``domain_id``.

    Admin handles only: a request-path service holds a ``DomainStore`` and
    never spells a domain prefix itself.
    """
    validate_relative_key(relative)
    return f"{DOMAIN_PREFIX}{domain_id}/{relative}"
