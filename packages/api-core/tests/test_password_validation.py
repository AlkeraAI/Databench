"""The pure half of the password policy — shape and identity similarity.

The rule that prompted this: an account was able to set its password to its own
registered email address. Length and character-class rules never catch that, and
the address is the one secret an attacker targeting the account already holds.
"""

from __future__ import annotations

import pytest
from alkera_core.validation.password import (
    DEFAULT_MIN_PASSWORD_LENGTH,
    PasswordPolicyError,
    check_password_shape,
)

EMAIL = "Quillfeather417@tideline.example"


@pytest.mark.parametrize(
    "password",
    [
        pytest.param(EMAIL, id="password-is-the-email-verbatim"),
        pytest.param(EMAIL.lower(), id="password-is-the-email-lowercased"),
        pytest.param("quillfeather417", id="password-is-the-local-part"),
        pytest.param("Quill.Feather.417", id="local-part-with-punctuation-inserted"),
        pytest.param("myquillfeather417pw", id="local-part-embedded"),
    ],
)
def test_password_derived_from_the_email_is_refused(password: str) -> None:
    """The reported bug, and the near misses around it — an attacker guessing
    from an address does not respect case or punctuation, so neither does this."""
    with pytest.raises(PasswordPolicyError, match="email"):
        check_password_shape(password, email=EMAIL)


def test_password_built_from_the_email_domain_is_refused() -> None:
    """The domain is effectively the service name, which NIST names alongside
    the username as a context-specific word to refuse."""
    with pytest.raises(PasswordPolicyError, match="email domain"):
        check_password_shape("my-tideline-account", email=EMAIL)


def test_short_shared_fragment_is_not_treated_as_reuse() -> None:
    """Asymmetry that matters: a three-letter overlap between an address and a
    password is coincidence. Refusing it would reject good passwords for no
    security gain."""
    check_password_shape("ham-and-cheese-sandwich", email="ham@example.com")


@pytest.mark.parametrize(
    "password",
    [
        pytest.param("robin-secure-pw", id="first-name"),
        pytest.param("secure-moss-2026", id="last-name"),
        pytest.param("ROBINsecurepass", id="name-different-case"),
    ],
)
def test_password_containing_the_users_name_is_refused(password: str) -> None:
    with pytest.raises(PasswordPolicyError, match="name"):
        check_password_shape(password, email="a@example.com", names=("Robin", "Moss"))


@pytest.mark.parametrize(
    "password", ["short", "elevenchar", "x" * (DEFAULT_MIN_PASSWORD_LENGTH - 1)]
)
def test_too_short_is_refused(password: str) -> None:
    with pytest.raises(PasswordPolicyError, match="at least"):
        check_password_shape(password)


def test_exactly_at_the_minimum_is_accepted() -> None:
    check_password_shape("g7v-Kq2m-Zt4")


def test_min_length_is_injectable() -> None:
    """A self-hosted deployment can raise the floor; the check has to honour it."""
    check_password_shape("g7v-Kq2m-Zt4", min_length=12)
    with pytest.raises(PasswordPolicyError, match="at least 20"):
        check_password_shape("g7v-Kq2m-Zt4", min_length=20)


@pytest.mark.parametrize(
    ("password", "reason"),
    [
        pytest.param("aaaaaaaaaaaa", "repeated character", id="single-repeated-char"),
        pytest.param("abcdefghijkl", "sequence", id="alphabet-run"),
        pytest.param("lkjihgfedcba", "sequence", id="descending-alphabet-run"),
        pytest.param("qwertyuiopas", "sequence", id="keyboard-row-run"),
        pytest.param("123456789012", "sequence", id="digit-run-that-wraps"),
        pytest.param("abcabcabcabc", "repeated over and over", id="repeating-block"),
        pytest.param("121212121212", "repeated over and over", id="two-char-block"),
    ],
)
def test_trivial_patterns_are_refused(password: str, reason: str) -> None:
    """Each of these is 12+ characters, so a length rule alone passes every one."""
    assert len(password) >= DEFAULT_MIN_PASSWORD_LENGTH
    with pytest.raises(PasswordPolicyError, match=reason):
        check_password_shape(password)


@pytest.mark.parametrize(
    "password",
    [
        pytest.param("correct-horse-battery-staple", id="passphrase"),
        pytest.param("Tr0ub4dor&3xyz", id="mixed-classes"),
        pytest.param("minimal-pass-12345", id="hyphenated-with-digits"),
        pytest.param("北京数据团队安全密码短语", id="non-latin-script"),
    ],
)
def test_reasonable_passwords_are_accepted(password: str) -> None:
    check_password_shape(password, email="someone@example.com", names=("Test", "User"))


def test_no_identity_supplied_still_checks_shape() -> None:
    """A context with no identity (a bootstrap path) still gets the shape rules;
    only the similarity checks are skipped."""
    check_password_shape("correct-horse-battery")
    with pytest.raises(PasswordPolicyError):
        check_password_shape("short")
