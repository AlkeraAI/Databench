"""The key layout: portability properties and the hostile-shape refusals."""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
from alkera_core.files.store.errors import InvalidKey
from alkera_core.files.store.keys import (
    MAX_KEY_BYTES,
    MAX_RELATIVE_KEY_BYTES,
    absolute,
    deleted_key,
    erased_key,
    incoming_key,
    object_key,
    validate_relative_key,
)
from hypothesis import given
from hypothesis import strategies as st

_HASHES = st.binary(min_size=2, max_size=64)
_UUIDS = st.uuids(version=4)


def _is_portable(key: str) -> None:
    """Independently re-derive the portability contract from the key text."""
    assert key == key.lower()
    assert len(key.encode("utf-8")) < 1024
    assert not key.startswith("/")
    assert ".." not in key.split("/")
    assert "\x00" not in key
    assert "" not in key.split("/")


@given(content_hash=_HASHES)
def test_object_key_is_portable_and_sharded_by_its_own_hash(content_hash: bytes) -> None:
    key = object_key(content_hash)
    _is_portable(key)
    hexed = content_hash.hex()
    # the shards are read back off the key, not recomputed by object_key
    _, first, second, tail = key.split("/")
    assert tail == hexed
    assert first == hexed[:2]
    assert second == hexed[2:4]


@given(session_id=_UUIDS, part=st.integers(min_value=1, max_value=10_000))
def test_incoming_key_carries_only_the_session_and_a_driver_token(
    session_id: UUID, part: int
) -> None:
    key = incoming_key(session_id, str(part))
    _is_portable(key)
    assert key.split("/") == ["incoming", str(session_id), str(part)]


@given(content_hash=_HASHES)
def test_deleted_and_erased_keys_park_the_original_beneath_their_own_prefix(
    content_hash: bytes,
) -> None:
    original = object_key(content_hash)
    assert deleted_key(original) == f"deleted/{original}"
    assert erased_key(original) == f"erased/{original}"
    _is_portable(deleted_key(original))
    _is_portable(erased_key(original))


@given(domain=_UUIDS, other=_UUIDS, content_hash=_HASHES)
def test_absolute_is_prefixed_by_the_domain_asked_for_and_never_another(
    domain: UUID, other: UUID, content_hash: bytes
) -> None:
    relative = object_key(content_hash)
    key = absolute(domain, relative)  # type: ignore[arg-type]
    assert key.startswith(f"domains/{domain}/")
    assert key.endswith(relative)
    if other != domain:
        assert not key.startswith(f"domains/{other}/")


@given(content_hash=_HASHES)
def test_a_relative_key_never_carries_the_absolute_domains_prefix(content_hash: bytes) -> None:
    relative = object_key(content_hash)
    validate_relative_key(relative)
    with pytest.raises(InvalidKey):
        validate_relative_key(absolute(uuid.uuid4(), relative))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("../x", id="parent_first_segment"),
        pytest.param("objects/../../etc/passwd", id="parent_mid_path"),
        pytest.param("objects/ab/..", id="parent_last_segment"),
        pytest.param("/objects/ab/cd", id="absolute"),
        pytest.param("a//b", id="empty_segment"),
        pytest.param("a/b/", id="trailing_slash"),
        pytest.param("", id="empty_key"),
        pytest.param("a/\x00/b", id="nul_byte"),
        pytest.param("Objects/ab/cd", id="uppercase"),
        pytest.param("objects/AB/cd", id="uppercase_segment"),
        pytest.param("domains/1/objects/ab", id="domains_prefix"),
        pytest.param("./x", id="dot_segment"),
        pytest.param(".tmp-abc", id="leading_dot_is_the_drivers_scratch_namespace"),
        pytest.param("objects/.parts/1", id="leading_dot_deeper_in_the_key"),
        pytest.param("o" * (MAX_RELATIVE_KEY_BYTES + 1), id="one_over_the_relative_budget"),
        pytest.param("o" * (MAX_KEY_BYTES + 1), id="over_1_kib"),
        pytest.param(
            "é" * ((MAX_RELATIVE_KEY_BYTES // 2) + 1), id="over_the_budget_in_bytes_not_chars"
        ),
    ],
)
def test_validate_relative_key_refuses_every_hostile_shape(key: str) -> None:
    with pytest.raises(InvalidKey):
        validate_relative_key(key)


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("objects/ab/cd/abcd", id="object"),
        pytest.param("incoming/0-0/1", id="incoming_part"),
        pytest.param("deleted/objects/ab/cd/abcd", id="deleted"),
        pytest.param("a..b/c", id="dots_inside_a_segment"),
        pytest.param("objects/ab/cd/abcd.tmp", id="a_dot_inside_a_segment_is_not_leading"),
        pytest.param("o" * MAX_RELATIVE_KEY_BYTES, id="exactly_the_relative_budget"),
        pytest.param("domainsx/a", id="domains_is_a_segment_not_a_substring"),
    ],
)
def test_validate_relative_key_accepts_the_portable_shapes(key: str) -> None:
    validate_relative_key(key)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("../x", id="traversal"),
        pytest.param("Report.pdf", id="uppercase_user_name"),
        pytest.param("a/b", id="separator"),
        pytest.param("", id="empty"),
        pytest.param("naïve.txt", id="non_ascii_user_bytes"),
    ],
)
def test_incoming_key_refuses_a_user_supplied_name(name: str) -> None:
    with pytest.raises(InvalidKey):
        incoming_key(uuid.uuid4(), name)


@pytest.mark.parametrize(
    "content_hash",
    [
        pytest.param(b"", id="empty_hash"),
        pytest.param(b"\x01", id="one_byte_cannot_shard"),
    ],
)
def test_object_key_refuses_a_hash_it_cannot_shard(content_hash: bytes) -> None:
    with pytest.raises(InvalidKey):
        object_key(content_hash)


def test_a_hostile_original_cannot_be_laundered_through_deleted_or_erased() -> None:
    with pytest.raises(InvalidKey):
        deleted_key("../../etc/passwd")
    with pytest.raises(InvalidKey):
        erased_key("/etc/passwd")
    with pytest.raises(InvalidKey):
        absolute(uuid.uuid4(), "../other")  # type: ignore[arg-type]


# -- the 1 KiB ceiling binds the absolute key, not the relative one -------


@given(domain=_UUIDS)
def test_the_longest_legal_relative_key_is_exactly_1_kib_once_addressed(domain: UUID) -> None:
    """The relative budget is derived from the absolute limit, not guessed.

    A relative key is only ever addressed as ``domains/<uuid>/<relative>``, so
    a key validated at 1024 bytes became a 1069-byte request: a permanent 400
    on AWS and an unmapped 500 elsewhere, both after the caller was told the
    key was fine.
    """
    longest = "o" * MAX_RELATIVE_KEY_BYTES

    addressed = absolute(domain, longest)  # type: ignore[arg-type]

    assert len(addressed.encode("utf-8")) == MAX_KEY_BYTES


@given(domain=_UUIDS)
def test_one_byte_over_the_relative_budget_is_refused_before_any_request(domain: UUID) -> None:
    too_long = "o" * (MAX_RELATIVE_KEY_BYTES + 1)

    with pytest.raises(InvalidKey):
        validate_relative_key(too_long)
    with pytest.raises(InvalidKey):
        absolute(domain, too_long)  # type: ignore[arg-type]
