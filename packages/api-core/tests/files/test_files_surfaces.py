"""Surface encoders are injective and invertible; unimplemented surfaces refuse."""

from __future__ import annotations

import os
import random
import re
from typing import Literal, cast

import pytest
from alkera_core.files.surfaces import (
    UnsupportedSurface,
    decode_from_surface,
    encode_for_surface,
)
from hypothesis import given, settings
from hypothesis import strategies as st

IMPLEMENTED: tuple[Literal["posix", "s3"], ...] = ("posix", "s3")
UNIMPLEMENTED = ("windows", "macos", "webdav")

# Pinned charset for the S3 surface: unreserved plus `!-_.*'()`, with `%` only ever
# introduced by the encoder itself.
S3_CHARSET = re.compile(r"^[A-Za-z0-9!\-_.*'()%]*$")

SAMPLE_SIZE = 10_000


def sample_names(seed: int, count: int = SAMPLE_SIZE) -> list[bytes]:
    """Deterministic hostile-ish names: arbitrary bytes, all lengths up to 255."""
    rng = random.Random(seed)
    seen: set[bytes] = set()
    while len(seen) < count:
        length = rng.choice([1, 1, 2, 3, 8, 32, 255])
        seen.add(bytes(rng.randrange(256) for _ in range(length)))
    return sorted(seen)


# --- round trip ----------------------------------------------------------------


@pytest.mark.parametrize("surface", IMPLEMENTED)
def test_decode_inverts_encode_over_ten_thousand_names(
    surface: Literal["posix", "s3"],
) -> None:
    for name in sample_names(seed=20260908):
        assert decode_from_surface(encode_for_surface(name, surface), surface) == name


@pytest.mark.parametrize("surface", IMPLEMENTED)
@settings(max_examples=500)
@given(name=st.binary(min_size=0, max_size=255))
def test_decode_inverts_encode_property(surface: Literal["posix", "s3"], name: bytes) -> None:
    assert decode_from_surface(encode_for_surface(name, surface), surface) == name


@pytest.mark.parametrize("surface", IMPLEMENTED)
def test_encoding_is_injective_over_ten_thousand_names(
    surface: Literal["posix", "s3"],
) -> None:
    names = sample_names(seed=4242)
    encoded = [encode_for_surface(name, surface) for name in names]
    assert len(set(encoded)) == len(names)


# --- the S3 surface ------------------------------------------------------------


def test_s3_output_stays_inside_the_pinned_charset() -> None:
    for name in sample_names(seed=99):
        assert S3_CHARSET.fullmatch(encode_for_surface(name, "s3")) is not None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param(b"", "", id="empty"),
        pytest.param(b"report.pdf", "report.pdf", id="an-ordinary-name-is-unchanged"),
        pytest.param(b"a-b_c.d*e'f(g)h!", "a-b_c.d*e'f(g)h!", id="the-safe-extras"),
        pytest.param(b"%", "%25", id="percent-is-ALWAYS-encoded-so-encode-is-injective"),
        pytest.param(b"%41", "%2541", id="an-already-encoded-looking-name-is-re-encoded"),
        pytest.param(b"~", "%7E", id="tilde-is-encoded-to-match-the-pinned-charset"),
        pytest.param(b"/", "%2F", id="the-separator-is-encoded"),
        pytest.param(b" ", "%20", id="space-is-encoded-never-plus"),
        pytest.param(b"+", "%2B", id="plus-is-encoded-so-it-cannot-mean-space"),
        pytest.param(b"\x00", "%00", id="NUL"),
        pytest.param(b"\xff", "%FF", id="a-byte-that-is-not-utf8"),
        pytest.param("é".encode(), "%C3%A9", id="utf8-is-encoded-byte-by-byte"),
    ],
)
def test_s3_encoding_is_pinned(name: bytes, expected: str) -> None:
    assert encode_for_surface(name, "s3") == expected
    assert decode_from_surface(expected, "s3") == name


def test_percent_encoding_uses_upper_case_hex() -> None:
    assert encode_for_surface(b"\xab", "s3") == "%AB"


def test_s3_names_that_differ_only_by_percent_do_not_collide() -> None:
    # Without escaping `%`, both of these would encode to "%41".
    assert encode_for_surface(b"%41", "s3") != encode_for_surface(b"A", "s3")


# --- the POSIX surface ---------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param(b"report.pdf", "report.pdf", id="ascii"),
        pytest.param("é".encode(), "é", id="valid-utf8-decodes-normally"),
        pytest.param(b"\xff", "\udcff", id="an-invalid-byte-becomes-a-surrogate"),
        pytest.param(b"a\xffb", "a\udcffb", id="surrogateescape-mid-name"),
        pytest.param(b"a\nb", "a\nb", id="a-control-byte-is-not-mangled"),
    ],
)
def test_posix_uses_surrogateescape(name: bytes, expected: str) -> None:
    assert encode_for_surface(name, "posix") == expected
    assert decode_from_surface(expected, "posix") == name


# --- surfaces that are not implemented yet -------------------------------------


@pytest.mark.parametrize("surface", UNIMPLEMENTED)
def test_unimplemented_surfaces_refuse_to_encode(surface: str) -> None:
    with pytest.raises(UnsupportedSurface) as excinfo:
        encode_for_surface(b"report.pdf", cast("Literal['windows']", surface))
    assert surface in str(excinfo.value)


@pytest.mark.parametrize("surface", UNIMPLEMENTED)
def test_unimplemented_surfaces_refuse_to_decode(surface: str) -> None:
    with pytest.raises(UnsupportedSurface):
        decode_from_surface("report.pdf", cast("Literal['windows']", surface))


def test_an_unknown_surface_is_refused_rather_than_silently_encoded() -> None:
    with pytest.raises(UnsupportedSurface):
        encode_for_surface(b"x", cast("Literal['posix']", "ftp"))


def test_the_posix_surface_does_not_borrow_the_hosts_filesystem_codec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`os.fsdecode` follows `sys.getfilesystemencodeerrors()`: surrogateescape
    on POSIX, but **surrogatepass** on Windows — and surrogatepass raises on
    exactly the undecodable bytes this surface exists to carry. A surface names
    a client façade, never the host, so stand in a filesystem codec that refuses
    and the spelling must be unmoved.
    """

    def refuses(*_args: object, **_kwargs: object) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(os, "fsdecode", refuses)
    monkeypatch.setattr(os, "fsencode", refuses)

    assert encode_for_surface(b"\xff", "posix") == "\udcff"
    assert decode_from_surface("\udcff", "posix") == b"\xff"
    assert decode_from_surface(encode_for_surface(b"a\x80b", "posix"), "posix") == b"a\x80b"
