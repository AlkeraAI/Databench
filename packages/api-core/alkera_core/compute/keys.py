"""Per-allocation SSH keypairs, and how the private half rests.

Generated server-side with ``cryptography`` (already a dependency) — no
ssh-keygen subprocess, so it works identically in the image and on a laptop.
Ed25519: small keys, accepted by every modern sshd.

The private key is stored only as ``secret_box`` ciphertext (MultiFernet:
rotation-safe, see ``alkera_core.auth.secret_box``). ``seal_private_key`` is
the one way a key goes into a row and ``open_private_key`` the one way it comes
out, so no code path can persist or read plaintext by accident.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from alkera_core.auth.secret_box import decrypt_secret, encrypt_secret

PRIVATE_KEY_HEADER = "-----BEGIN OPENSSH PRIVATE KEY-----"


def generate_ssh_keypair(comment: str = "alkera-compute") -> tuple[str, str]:
    """Returns ``(private_key_pem, public_key_openssh)``.

    The private key is OpenSSH-format PEM (what ``ssh -i`` expects); the public
    key is a one-line ``ssh-ed25519 AAAA... <comment>`` authorized_keys entry.
    """
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_line = (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode("ascii")
    )
    return private_pem, f"{public_line} {comment}"


def seal_private_key(private_key_pem: str) -> str:
    """The ciphertext a private key is stored as. Refuses a value that does not
    look like a private key, so a caller cannot seal a public key (or nothing)
    into the private-key column."""
    if not private_key_pem.startswith(PRIVATE_KEY_HEADER):
        raise ValueError("only an OpenSSH private key may be sealed into the key column")
    return encrypt_secret(private_key_pem)


def open_private_key(ciphertext: str) -> str:
    """The plaintext private key for a stored ciphertext. Raises
    ``cryptography.fernet.InvalidToken`` for ciphertext no configured key opens,
    and ``ValueError`` for an empty column (an allocation that never had a key)."""
    if not ciphertext:
        raise ValueError("this allocation has no stored private key")
    return decrypt_secret(ciphertext)


__all__ = [
    "PRIVATE_KEY_HEADER",
    "generate_ssh_keypair",
    "open_private_key",
    "seal_private_key",
]
