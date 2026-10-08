"""The observed-identity taint boundary, owned once in ``alkera_core.connectors.identity``.

A probed or read endpoint authors its own response, so a hostile one can echo the
credential it was dialed with back as its cluster id. Every identity crosses the
boundary through one of two funnels. ``bind_identity`` runs at capture where the dialed
secret is in scope. It shapes the RAW value, then rejects any value equal to a dialed
secret (the secret side is stripped). ``rebind`` runs at a cached read where no secret
is available, so it shapes only.

A rejected identity degrades to the empty string, never to an exception. Only an
``ObservedIdentity`` may build a URN authority or a persisted probe value.
"""

from __future__ import annotations

import dataclasses

import pytest
from alkera_core.connectors.identity import (
    NONE,
    ObservedIdentity,
    bind_identity,
    is_shape_ok,
    rebind,
)

#: A dialed secret that is itself URN-shaped, so a reflection would pass shape and only
#: the secret-equality half of ``bind_identity`` can catch it.
_SECRET = "s3cr3t.cluster-Id_42"


# --- bind_identity: shape on the raw value, then secret-equality --------------


@pytest.mark.parametrize(
    ("value", "secrets", "expected"),
    [
        # Shape-valid values equal to no secret are trusted verbatim.
        pytest.param("lkc-abc123", (), "lkc-abc123", id="plain-valid-id"),
        pytest.param("cluster-uuid_22.chars:x", (), "cluster-uuid_22.chars:x", id="full-charset"),
        pytest.param("A" * 128, (), "A" * 128, id="128-chars-pass"),
        pytest.param("A" * 129, (), "", id="129-chars-rejected"),
        pytest.param("", (), "", id="empty-stays-empty"),
        pytest.param("   ", (), "", id="whitespace-only-rejected"),
        # Anti-reflection: raw equal to a dialed secret rejects.
        pytest.param(_SECRET, (_SECRET,), "", id="equals-secret"),
        pytest.param(_SECRET, (f"  {_SECRET}  ",), "", id="secret-surrounding-whitespace-stripped"),
        pytest.param(_SECRET, ("other-id", _SECRET), "", id="equals-one-of-many-secrets"),
        pytest.param("lkc-live", (None, "lkc-live"), "", id="none-secret-skipped-real-one-matches"),
        # ASYMMETRIC: only equality rejects. A value that merely CONTAINS a secret
        # substring is NOT a reflection -- the funnel is not a blocklist.
        pytest.param(
            f"prefix-{_SECRET}-suffix",
            (_SECRET,),
            f"prefix-{_SECRET}-suffix",
            id="contains-secret-is-kept",
        ),
        pytest.param("lkc-abc123", (_SECRET,), "lkc-abc123", id="distinct-from-secret-passes"),
        pytest.param("lkc-abc123", ("",), "lkc-abc123", id="empty-secret-ignored"),
        pytest.param("lkc-live", (None,), "lkc-live", id="none-secret-ignored"),
        # Shape: anything outside the URN-safe charset degrades to "".
        pytest.param("a/b", (), "", id="slash-rejected"),
        pytest.param("a=b", (), "", id="equals-sign-rejected"),
        pytest.param("a b", (), "", id="space-rejected"),
        pytest.param('a"b', (), "", id="quote-rejected"),
        pytest.param("a\nb", (), "", id="embedded-newline-rejected"),
        pytest.param("abc\n", (), "", id="trailing-newline-rejected"),
        pytest.param("héllo", (), "", id="non-ascii-rejected"),
    ],
)
def test_bind_identity_shape_then_secret_equality(
    value: str, secrets: tuple[str | None, ...], expected: str
) -> None:
    result = bind_identity(value, *secrets)
    assert isinstance(result, ObservedIdentity)
    assert result.value == expected


def test_bind_identity_does_not_strip_the_value_side() -> None:
    # The secret side is stripped; the RAW value is not. A padded value fails on SHAPE
    # (whitespace is outside the charset) before the equality test ever runs. The
    # boundary never trusts a value it had to trim.
    assert bind_identity(f"  {_SECRET}  ", _SECRET).value == ""


# --- rebind: shape only, no secret at a cached read ---------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("cluster-uuid_1", "cluster-uuid_1", id="valid-preserved"),
        pytest.param("A" * 128, "A" * 128, id="128-preserved"),
        pytest.param("A" * 129, "", id="over-length-clamped"),
        pytest.param("has/slash", "", id="slash-clamped"),
        pytest.param("has space", "", id="space-clamped"),
        pytest.param("cluster\n", "", id="trailing-newline-clamped"),
        pytest.param("", "", id="empty-stays-empty"),
    ],
)
def test_rebind_is_shape_only(value: str, expected: str) -> None:
    assert rebind(value).value == expected


def test_rebind_trusts_a_value_equal_to_a_secret_because_no_secret_exists() -> None:
    # rebind is the cached-read funnel. The dialed secret is gone by the time a value is
    # read back, so the equality half is impossible and a shape-valid value is trusted on
    # shape alone. A value that ``bind_identity`` would have rejected as a reflection now
    # passes, because the two funnels are deliberately not the same guard.
    assert rebind(_SECRET).value == _SECRET
    assert bind_identity(_SECRET, _SECRET).value == ""


# --- is_shape_ok: the raw shape check both funnels share ----------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("lkc-abc123", True, id="valid"),
        pytest.param("A" * 128, True, id="128-ok"),
        pytest.param("A" * 129, False, id="129-too-long"),
        pytest.param("", False, id="empty"),
        pytest.param("has/slash", False, id="slash"),
        pytest.param("has space", False, id="space"),
        pytest.param("has=equals", False, id="equals"),
        pytest.param("  padded  ", False, id="surrounding-whitespace"),
        pytest.param("mid\nline", False, id="embedded-newline"),
        pytest.param("trailing\n", False, id="trailing-newline"),
        pytest.param("héllo", False, id="non-ascii"),
    ],
)
def test_is_shape_ok(value: str, expected: bool) -> None:
    assert is_shape_ok(value) is expected


# --- ObservedIdentity: truthiness, prefixing, and the NONE sentinel -----------


@pytest.mark.parametrize(
    ("identity", "truthy"),
    [
        pytest.param(ObservedIdentity("x"), True, id="non-empty-is-truthy"),
        pytest.param(ObservedIdentity(""), False, id="empty-is-falsy"),
        pytest.param(ObservedIdentity(), False, id="default-is-falsy"),
        pytest.param(NONE, False, id="NONE-is-falsy"),
    ],
)
def test_observed_identity_bool(identity: ObservedIdentity, truthy: bool) -> None:
    assert bool(identity) is truthy


def test_none_sentinel_is_the_empty_identity() -> None:
    assert NONE.value == ""
    assert not NONE


@pytest.mark.parametrize(
    ("value", "prefix", "expected"),
    [
        pytest.param("64ab0c", "replica-set-", "replica-set-64ab0c", id="prefix-reshapes"),
        pytest.param("cluster1", "sharded-cluster-", "sharded-cluster-cluster1", id="sharded"),
        pytest.param("x", "bad/", "", id="prefix-breaks-charset-rejected"),
        pytest.param("A" * 120, "replica-set-", "", id="prefix-overflows-length"),
        pytest.param("", "replica-set-", "", id="empty-stays-empty-through-prefix"),
    ],
)
def test_with_prefix_re_validates(value: str, prefix: str, expected: str) -> None:
    # Mongo namespaces a bound id as ``replica-set-<id>``: the prefix is re-validated so a
    # prefix that would break the URN-safe shape (or overflow the length) collapses to "".
    assert ObservedIdentity(value).with_prefix(prefix).value == expected


def test_observed_identity_is_frozen() -> None:
    identity = ObservedIdentity("locked")
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.value = "mutated"  # type: ignore[misc]
