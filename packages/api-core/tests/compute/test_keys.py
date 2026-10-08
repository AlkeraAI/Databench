"""The per-allocation SSH keypair and how its private half rests.

The keypair is a real Ed25519 pair (the public line authenticates the private
key); the private key is sealed through ``secret_box`` and only ever comes back
through ``open_private_key``. Sealing refuses anything that is not a private
key, opening refuses an empty column, and ciphertext written under a rotated
key still opens (MultiFernet keeps the previous key in the decrypt set).
"""

from __future__ import annotations

import base64

import pytest
from alkera_core.compute.keys import (
    PRIVATE_KEY_HEADER,
    generate_ssh_keypair,
    open_private_key,
    seal_private_key,
)
from alkera_core.config import settings
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def test_generate_ssh_keypair_is_a_real_ed25519_pair() -> None:
    private_pem, public_line = generate_ssh_keypair(comment="unit-test")
    assert private_pem.startswith(PRIVATE_KEY_HEADER)
    assert private_pem.rstrip().endswith("-----END OPENSSH PRIVATE KEY-----")
    key_type, key_b64, comment = public_line.split(" ")
    assert key_type == "ssh-ed25519"
    assert comment == "unit-test"
    loaded = serialization.load_ssh_private_key(private_pem.encode("ascii"), password=None)
    assert isinstance(loaded, Ed25519PrivateKey)
    derived = loaded.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )
    assert derived.decode("ascii") == f"{key_type} {key_b64}"


def test_two_keypairs_never_collide() -> None:
    assert generate_ssh_keypair()[0] != generate_ssh_keypair()[0]


def test_seal_hides_the_key_and_open_restores_it_byte_for_byte() -> None:
    private_pem, _ = generate_ssh_keypair()
    sealed = seal_private_key(private_pem)
    assert sealed != private_pem
    assert PRIVATE_KEY_HEADER not in sealed
    # Fernet tokens are urlsafe-base64 and start with the version byte 0x80 ("gAAAA").
    assert sealed.startswith("gAAAA")
    body = base64.urlsafe_b64decode(sealed.encode("ascii"))
    assert private_pem.encode("ascii") not in body
    assert open_private_key(sealed) == private_pem


def test_sealing_the_same_key_twice_yields_distinct_ciphertext() -> None:
    """Fernet is randomised per token: two rows with one key never share bytes,
    so equal keys are not discoverable from the column."""
    private_pem, _ = generate_ssh_keypair()
    assert seal_private_key(private_pem) != seal_private_key(private_pem)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB comment", id="public-key"),
        pytest.param("-----BEGIN RSA PRIVATE KEY-----\nabc\n", id="not-openssh"),
        pytest.param("gAAAAABsealed", id="already-ciphertext"),
    ],
)
def test_seal_refuses_anything_that_is_not_an_openssh_private_key(value: str) -> None:
    with pytest.raises(ValueError, match="only an OpenSSH private key"):
        seal_private_key(value)


def test_open_refuses_an_empty_column() -> None:
    with pytest.raises(ValueError, match="no stored private key"):
        open_private_key("")


def test_open_refuses_ciphertext_no_configured_key_can_open() -> None:
    foreign = Fernet(Fernet.generate_key()).encrypt(b"-----BEGIN OPENSSH PRIVATE KEY-----")
    with pytest.raises(InvalidToken):
        open_private_key(foreign.decode("ascii"))


def test_a_key_sealed_before_a_rotation_still_opens_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adopting a customer-managed box key must not strand ciphertext written
    under the derived key: MultiFernet keeps the old key in the decrypt set."""
    private_pem, _ = generate_ssh_keypair()
    monkeypatch.setattr(settings, "secret_box_key", None)
    sealed_before = seal_private_key(private_pem)
    monkeypatch.setattr(settings, "secret_box_key", Fernet.generate_key().decode("ascii"))
    assert open_private_key(sealed_before) == private_pem
    sealed_after = seal_private_key(private_pem)
    assert sealed_after != sealed_before
    assert open_private_key(sealed_after) == private_pem
