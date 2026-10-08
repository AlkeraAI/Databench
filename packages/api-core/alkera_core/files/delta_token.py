"""The delta token: one opaque, signed, versioned cursor into a drive's feed.

The token is opaque to clients but persisted in their caches — a desktop client
holds one across a restart and a server upgrade — so it is a `VersionedModel`
rather than an ad-hoc struct: the wire payload carries its own
``schema_version`` and decoding walks the migration ladder, which is what lets a
token minted by yesterday's writer be resumed from instead of forcing a resync.

It is signed, not encrypted: a client may not forge a watermark (that would let
it skip a committed change, or read across into another drive's feed), but there
is nothing secret in it. Verification is `hmac.compare_digest`, so a caller
cannot learn a signature byte by byte from response timing.

It lives in the library rather than the schemas layer because the library is
what mints, encodes and honours it; the schemas layer re-exports it for the wire
documentation. The dependency may only run that way — `alkera_core.files`
reaches for nothing under `alkera_core.schemas`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime
from typing import Any, ClassVar, Final

from alkera_core.files.errors import InvalidRequest
from alkera_core.files.ids import DriveId
from alkera_core.versioning import VersionedModel
from alkera_core.versioning.base import Migration

#: What separates the payload from its signature in the encoded form.
_SEPARATOR: Final = "."

#: Bumped when the feed's meaning changes (a re-keyed drive, a rebuilt outbox,
#: a restore from backup). A token from an older generation names ids that no
#: longer mean what they did, so it cannot be interpreted against today's feed.
TOKEN_GENERATION: Final = 1


class InvalidDeltaToken(InvalidRequest, ValueError):  # noqa: N818 - the name the Files spec pins
    """The token was not produced by this deployment's key, or is malformed.

    Also an `InvalidRequest`, so the route layer maps it without a second
    handler: there is one token shape and therefore one refusal for it.
    """


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, TypeError) as exc:
        raise InvalidDeltaToken("token is not valid base64") from exc


def _mac(payload: bytes, key: str) -> bytes:
    return hmac.new(key.encode("utf-8"), payload, hashlib.sha256).digest()


def _cursor_had_no_transaction_half(data: dict[str, Any]) -> dict[str, Any]:
    """1.0.0 -> 1.1.0: a watermark that travelled by outbox id alone.

    The id half of such a cursor cannot be resumed from — a row with a lower id
    whose transaction committed later would be stepped over — so the reader has
    to read it as a catch-up. Stamping ``cursor_xid = 0`` here gives the "no
    transaction half" case exactly one representation, whether it arrives from
    an old writer or from a drive with nothing deliverable yet.
    """
    return {**data, "cursor_xid": 0, "schema_version": "1.1.0"}


class DeltaToken(VersionedModel):
    """A watermark into one drive's outbox.

    `generation` rises when the platform is restored from a backup, so a token
    minted before a restore can never be read as a valid watermark after one.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.1.0"
    MIGRATIONS: ClassVar[dict[str, Migration]] = {"1.0.0": _cursor_had_no_transaction_half}

    drive_id: DriveId | None = None
    outbox_id: int = 0
    issued_at: datetime | None = None
    generation: int = TOKEN_GENERATION
    #: The transaction half of the cursor. The outbox `id` is a `bigserial`
    #: allocated at INSERT, so it does not order transactions by commit: a
    #: cursor that travels by id alone steps over a row whose id is lower and
    #: whose transaction committed later, and that row is then never delivered.
    #: The feed orders and pages by `(xmin, id)`, so the watermark carries both.
    #: `0` is not a transaction id, so it also marks a token minted before this
    #: field existed; a reader treats one as a catch-up rather than a resume.
    cursor_xid: int = 0

    def encode(self, *, key: str) -> str:
        """This token as one URL-safe, signed string."""
        return encode_token(self, key=key)

    @staticmethod
    def decode(raw: str, *, key: str) -> DeltaToken:
        """The token ``raw`` names, or `InvalidDeltaToken` if it is not ours."""
        return decode_token(raw, key=key)


def encode_token(token: DeltaToken, *, key: str) -> str:
    """Serialize and sign a token into one URL-safe string."""
    body = json.dumps(token.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    payload = body.encode("utf-8")
    return f"{_b64encode(payload)}{_SEPARATOR}{_b64encode(_mac(payload, key))}"


def decode_token(raw: str, *, key: str) -> DeltaToken:
    """Verify and parse a token, or raise `InvalidDeltaToken`.

    Every failure — a bad shape, a foreign key, a flipped bit — raises the same
    exception, so a client learns nothing about the key from which one it got.
    """
    encoded_payload, separator, encoded_mac = raw.partition(_SEPARATOR)
    if not separator or not encoded_payload or not encoded_mac:
        raise InvalidDeltaToken("token is not <payload>.<signature>")
    payload = _b64decode(encoded_payload)
    signature = _b64decode(encoded_mac)
    if not hmac.compare_digest(signature, _mac(payload, key)):
        raise InvalidDeltaToken("signature does not verify")
    try:
        decoded: Any = json.loads(payload)
    except ValueError as exc:
        raise InvalidDeltaToken("payload is not JSON") from exc
    if not isinstance(decoded, dict):
        raise InvalidDeltaToken("payload is not an object")
    try:
        return DeltaToken.model_validate(decoded)
    except ValueError as exc:
        raise InvalidDeltaToken("payload is not a delta token") from exc


__all__ = [
    "TOKEN_GENERATION",
    "DeltaToken",
    "InvalidDeltaToken",
    "decode_token",
    "encode_token",
]
