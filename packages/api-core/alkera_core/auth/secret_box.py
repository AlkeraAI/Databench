"""Symmetric encryption for secrets persisted at rest (e.g. a per-org SSO IdP
client secret).

Fernet (AES-128-CBC + HMAC). The ACTIVE (encryption) key is, in order of preference:

1. ``SECRET_BOX_KEY`` — a customer-managed key (a urlsafe-base64 32-byte Fernet key),
   so a security-conscious operator can hold their own key independent of the app's
   JWT secret (BYOK).
2. otherwise, a key DERIVED from ``AUTH_JWT_SECRET`` — no new key-management surface;
   wherever the JWT secret is set, the box works.

Rotation is non-destructive. A :class:`MultiFernet` DECRYPTS with any of {the active
key, ``SECRET_BOX_KEYS_PREVIOUS``, the JWT-derived key} while always ENCRYPTING with
the active one. So you can introduce or rotate ``SECRET_BOX_KEY`` and existing
ciphertext keeps opening — it re-encrypts under the active key on its next write. The
JWT-derived key always stays in the decrypt set, so adopting a ``SECRET_BOX_KEY`` never
strands ciphertext previously written under the derived key.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from alkera_core.config import settings

_DOMAIN = b"alkera-secret-box-v1:"


def _jwt_derived_key() -> bytes:
    # Domain-separate from any other use of the JWT secret, then take a 32-byte
    # SHA-256 → urlsafe-b64 → a valid Fernet key.
    digest = hashlib.sha256(_DOMAIN + settings.effective_jwt_secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> MultiFernet:
    jwt_key = Fernet(_jwt_derived_key())
    previous = [Fernet(k.encode("ascii")) for k in settings.secret_box_previous_keys_list]
    if settings.secret_box_key:
        # BYOK: encrypt with the customer key; keep previous + JWT-derived for decrypt.
        active = Fernet(settings.secret_box_key.encode("ascii"))
        return MultiFernet([active, *previous, jwt_key])
    # Default: encrypt with the JWT-derived key (unchanged behavior) + any previous.
    return MultiFernet([jwt_key, *previous])


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    """Raises ``InvalidToken`` if the ciphertext can't be opened by ANY configured key
    (corrupt, or written under a key no longer present)."""
    return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")


__all__ = ["InvalidToken", "decrypt_secret", "encrypt_secret"]
