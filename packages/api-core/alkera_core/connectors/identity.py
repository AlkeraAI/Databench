"""The observed-identity taint boundary, owned once for the whole stack.

A probed or read endpoint authors its own response, so a hostile one can echo the
credential it was dialed with as its cluster id. An identity becomes trustworthy only by
passing through ``bind_identity`` (shape plus secret-equality, at capture, where the dialed
secret is in scope) or ``rebind`` (shape only, at a cached read, where it is not). A URN
authority or a persisted probe value may be derived only from an ``ObservedIdentity``,
never from a raw ``str``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A physical-cluster identity is short and URN-safe. Real ids run to ~40 chars: a
#: 22-char base64url cluster uuid, ``lkc-xxxxx``, ``replica-set-<ObjectId hex>``.
IDENTITY_MAX_LEN = 128
#: `\Z`, not `$`: `$` matches before a trailing newline, so a reflected `secret\n` would
#: slip a shape check applied to the raw value. The charset excludes whitespace, so a
#: shape-valid value is already trimmed and nothing needs stripping before this check.
IDENTITY_CHARSET = re.compile(r"^[A-Za-z0-9._:-]+\Z")


def is_shape_ok(raw: str) -> bool:
    """Whether a raw identity is short and matches the URN-safe charset, no pre-strip."""
    return bool(raw) and len(raw) <= IDENTITY_MAX_LEN and bool(IDENTITY_CHARSET.match(raw))


@dataclass(frozen=True)
class ObservedIdentity:
    """A cluster identity that has passed the taint boundary.

    ``value`` is the empty string for a rejected or absent identity. This is the only
    type from which a URN authority or a persisted probe value may be built.
    """

    value: str = ""

    def __bool__(self) -> bool:
        return bool(self.value)

    def with_prefix(self, prefix: str) -> ObservedIdentity:
        """Re-validate ``prefix + value`` so a namespacing prefix cannot break the shape."""
        return rebind(f"{prefix}{self.value}") if self.value else self


NONE = ObservedIdentity()


def bind_identity(raw: str, *secrets: str | None) -> ObservedIdentity:
    """Capture-time bound: shape on the raw value, then refuse a reflected dialed secret.

    Compare the raw value against each dialed secret (stripping only the secret side, so a
    padded secret is still caught). A shape-valid value that equals no secret is trusted.
    """
    if not is_shape_ok(raw):
        return NONE
    if any(raw == (secret or "").strip() for secret in secrets if secret):
        return NONE
    return ObservedIdentity(raw)


def rebind(raw: str) -> ObservedIdentity:
    """Cached-read bound: shape only. The secret-equality half is impossible with no secret,
    so a value cached before the boundary existed is trusted solely on shape."""
    return ObservedIdentity(raw) if is_shape_ok(raw) else NONE
