"""The personal-access-token secret: its prefix, its randomness, its stored digest."""

from __future__ import annotations

import pytest
from alkera_core.auth.ci_token import CI_TOKEN_PREFIX, mint_ci_token
from alkera_core.auth.pat_token import (
    PAT_TOKEN_PREFIX,
    hash_pat_token,
    looks_like_pat_token,
    mint_pat_token,
)
from alkera_core.auth.proxy_token import PROXY_TOKEN_PREFIX
from alkera_core.auth.token_hash import hash_lookup_token, lookup_token_digests


def test_the_prefix_is_the_documented_greppable_marker() -> None:
    assert PAT_TOKEN_PREFIX == "alk_pat_"
    raw, _digest = mint_pat_token()
    assert raw.startswith(PAT_TOKEN_PREFIX)


def test_a_minted_secret_carries_at_least_384_bits_after_the_prefix() -> None:
    raw, _digest = mint_pat_token()
    # token_urlsafe(48) is 64 URL-safe characters: the same entropy as the CI
    # and proxy tokens, far past any brute-force budget.
    assert len(raw) - len(PAT_TOKEN_PREFIX) >= 64
    assert len(raw) >= 64


def test_two_mints_never_collide() -> None:
    first, first_digest = mint_pat_token()
    second, second_digest = mint_pat_token()
    assert first != second
    assert first_digest != second_digest


def test_the_stored_digest_is_the_keyed_lookup_hash_of_the_raw_secret() -> None:
    raw, digest = mint_pat_token()
    assert digest == hash_lookup_token(raw)
    assert digest == hash_pat_token(raw)
    assert len(digest) == 64
    int(digest, 16)  # hex


def test_the_write_digest_is_the_first_read_digest_so_a_lookup_finds_it() -> None:
    raw, digest = mint_pat_token()
    assert lookup_token_digests(raw)[0] == digest


def test_the_raw_secret_is_never_the_digest() -> None:
    raw, digest = mint_pat_token()
    assert raw != digest
    assert digest not in raw


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(mint_pat_token()[0], True, id="a-minted-pat"),
        pytest.param("alk_pat_", True, id="bare-prefix-still-reads-as-pat-shaped"),
        pytest.param("alk_pat_x", True, id="short-but-prefixed"),
        pytest.param(mint_ci_token()[0], False, id="a-ci-token"),
        pytest.param(CI_TOKEN_PREFIX + "abc", False, id="ci-prefix"),
        pytest.param(PROXY_TOKEN_PREFIX + "abc", False, id="proxy-prefix"),
        pytest.param("", False, id="empty"),
        pytest.param("ALK_PAT_UPPER", False, id="wrong-case-prefix"),
        pytest.param(" alk_pat_leading-space", False, id="leading-space"),
        pytest.param("eyJhbGciOiJIUzI1NiJ9.e30.x", False, id="a-jwt-shape"),
        pytest.param("alk_pa_t", False, id="near-miss-prefix"),
    ],
)
def test_looks_like_pat_token(raw: str, expected: bool) -> None:
    assert looks_like_pat_token(raw) is expected


def test_the_three_bearer_prefixes_are_pairwise_distinct() -> None:
    """The resolver dispatches on the prefix, so no two may share one."""
    prefixes = (PAT_TOKEN_PREFIX, CI_TOKEN_PREFIX, PROXY_TOKEN_PREFIX)
    assert len(set(prefixes)) == 3
    for a in prefixes:
        for b in prefixes:
            if a != b:
                assert not a.startswith(b)
