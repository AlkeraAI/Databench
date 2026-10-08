"""Output frames: ids the backend mints for a person's frames, and the page
the frames load.

A frame is attached at the engine's widget hub, which keys the frame's model
closure by ``frame_id`` and is the authority on which comms a frame owns (the
closure grows as updates reference new models). The backend keeps no frame
table: a frame id names its owner (the person who attached it), the socket it
is narrowed to (or ``any``), a nonce, and a signature over all of them and the
notebook, so every replica can tell, without a lookup, whether a request
naming a frame comes from its owner about its notebook, and to which sockets a
frame-addressed event goes.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from typing import Final

from alkera_core.config import settings

#: The socket tag of a frame that reaches every socket of its owner.
ANY_SOCKET: Final = "any"
_DOMAIN: Final = b"alkera.notebook.frame.v1"


def _key() -> bytes:
    return hmac.new(settings.effective_jwt_secret.encode("utf-8"), _DOMAIN, hashlib.sha256).digest()


def socket_tag(peer_id: str | None) -> str:
    """The tag a frame narrowed to the socket ``peer_id`` carries."""
    if not peer_id:
        return ANY_SOCKET
    return hashlib.sha256(peer_id.encode("utf-8")).hexdigest()[:12]


def _sign(owner: str, tag: str, nonce: str, item_id: uuid.UUID) -> str:
    message = f"{owner}.{tag}.{nonce}.{item_id.hex}".encode()
    return hmac.new(_key(), message, hashlib.sha256).hexdigest()[:32]


def mint(*, owner: uuid.UUID, item_id: uuid.UUID, peer_id: str | None) -> str:
    """A new frame id for ``owner``'s frame on ``item_id``."""
    tag = socket_tag(peer_id)
    nonce = secrets.token_hex(8)
    return f"{owner.hex}.{tag}.{nonce}.{_sign(owner.hex, tag, nonce, item_id)}"


@dataclass(frozen=True, slots=True)
class FrameRef:
    owner: uuid.UUID
    tag: str


def verify(frame_id: str, *, item_id: uuid.UUID) -> FrameRef | None:
    """Who a frame id belongs to, when the backend minted it for ``item_id``;
    ``None`` for anything else (another notebook's, forged, malformed)."""
    parts = frame_id.split(".")
    if len(parts) != 4:
        return None
    owner, tag, nonce, signature = parts
    try:
        owner_id = uuid.UUID(hex=owner)
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _sign(owner, tag, nonce, item_id)):
        return None
    return FrameRef(owner=owner_id, tag=tag)


def reaches(frame_id: str, *, user_id: uuid.UUID, peer_id: str | None) -> bool:
    """Whether an event addressed to ``frame_id`` goes to the socket of
    ``user_id`` named ``peer_id``: its owner's, and the one socket it was
    narrowed to when it was."""
    parts = frame_id.split(".")
    if len(parts) != 4 or parts[0] != user_id.hex:
        return False
    return parts[1] == ANY_SOCKET or parts[1] == socket_tag(peer_id)


def owner_prefix(*, owner: uuid.UUID, peer_id: str | None) -> str:
    """The prefix of every frame id a socket's frames carry: what the hub is
    told to detach when that socket leaves the notebook."""
    return f"{owner.hex}.{socket_tag(peer_id)}."


def output_frame_url() -> str | None:
    """The output frame page on the content origin, ``/c/nb-output/<build
    hash>``: the hash names the app build, so a page a newer build expects is
    never confused with an older one. ``None`` without a content origin."""
    base = settings.files_content_base_url
    if not base:
        return None
    build = f"{settings.app_version}:{settings.build_id or ''}".encode()
    return f"{base.rstrip('/')}/c/nb-output/{hashlib.sha256(build).hexdigest()[:16]}"


__all__ = [
    "ANY_SOCKET",
    "FrameRef",
    "mint",
    "output_frame_url",
    "owner_prefix",
    "reaches",
    "socket_tag",
    "verify",
]
