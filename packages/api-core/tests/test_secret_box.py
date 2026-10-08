"""Secret box: encrypt/decrypt round-trip + tamper/foreign-key rejection."""

from __future__ import annotations

import pytest
from alkera_core.auth import secret_box as sb
from alkera_core.auth.secret_box import InvalidToken, decrypt_secret, encrypt_secret
from cryptography.fernet import Fernet


def test_round_trip() -> None:
    secret = "super-secret-oidc-client-secret-123"
    box = encrypt_secret(secret)
    assert box != secret  # actually encrypted
    assert decrypt_secret(box) == secret


def test_ciphertext_is_nondeterministic() -> None:
    # Fernet embeds a random IV, so the same plaintext encrypts differently.
    assert encrypt_secret("x") != encrypt_secret("x")


def test_tampered_ciphertext_rejected() -> None:
    box = encrypt_secret("x")
    with pytest.raises(InvalidToken):
        decrypt_secret(box[:-4] + "AAAA")


def test_garbage_rejected() -> None:
    with pytest.raises(InvalidToken):
        decrypt_secret("not-a-fernet-token")


# --------------------------------------------------------------------------- #
# customer-managed key (BYOK) + non-destructive rotation
# --------------------------------------------------------------------------- #


def _set(monkeypatch: pytest.MonkeyPatch, **kw: object) -> None:
    for k, v in kw.items():
        monkeypatch.setattr(sb.settings, k, v)


def test_roundtrip_with_customer_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, secret_box_key=Fernet.generate_key().decode(), secret_box_keys_previous="")
    assert decrypt_secret(encrypt_secret("s3cr3t")) == "s3cr3t"


def test_active_write_uses_the_customer_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # A new write must use the CMK, not the JWT-derived key: it opens with the CMK alone.
    key = Fernet.generate_key().decode()
    _set(monkeypatch, secret_box_key=key, secret_box_keys_previous="")
    ct = encrypt_secret("byok")
    assert Fernet(key.encode()).decrypt(ct.encode()).decode() == "byok"


def test_adopting_a_cmk_does_not_strand_derived_key_ciphertext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(monkeypatch, secret_box_key=None, secret_box_keys_previous="")
    legacy = encrypt_secret("legacy")  # written before BYOK (derived key)
    _set(monkeypatch, secret_box_key=Fernet.generate_key().decode())  # adopt a CMK
    assert decrypt_secret(legacy) == "legacy"  # old ciphertext still opens
    assert decrypt_secret(encrypt_secret("fresh")) == "fresh"


def test_cmk_rotation_window_then_retirement(monkeypatch: pytest.MonkeyPatch) -> None:
    k1 = Fernet.generate_key().decode()
    _set(monkeypatch, secret_box_key=k1, secret_box_keys_previous="")
    under_k1 = encrypt_secret("rotate-me")

    # Rotate: k2 active, k1 retired into the previous-keys decrypt set.
    k2 = Fernet.generate_key().decode()
    _set(monkeypatch, secret_box_key=k2, secret_box_keys_previous=k1)
    assert decrypt_secret(under_k1) == "rotate-me"  # written under k1, still opens
    under_k2 = encrypt_secret("post-rotate")

    # Fully retire k1: k2 ciphertext fine, k1 ciphertext now dead.
    _set(monkeypatch, secret_box_keys_previous="")
    assert decrypt_secret(under_k2) == "post-rotate"
    with pytest.raises(InvalidToken):
        decrypt_secret(under_k1)
