"""The core persisted Files schemas: forward compatibility, limits, interning.

Each test names the invariant it defends. The three that matter most are the
tolerant readers (an unknown principal kind, an unknown node kind and an unknown
origin tag must survive a read by a backend older than the writer),
``body_hash`` (two backends must intern an identical body to the same row), and
the ``extra="allow"`` walk — a subclass that narrows to ``forbid`` would make
every one of the others silently untrue for that shape.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import re
from collections.abc import Callable
from typing import Any

import pytest
from alkera_core.schemas.files import acl as acl_mod
from alkera_core.schemas.files import attrs as attrs_mod
from alkera_core.schemas.files.acl import AclBody, OrgAce, RawAce, TeamAce, UserAce
from alkera_core.schemas.files.attrs import (
    MAX_XATTR_NAME_BYTES,
    MAX_XATTR_TOTAL_BYTES,
    MAX_XATTR_VALUE_BYTES,
    AttrsPatch,
    NodeAttrs,
)
from alkera_core.schemas.files.history import HistoryKind, HistorySnapshot
from alkera_core.schemas.files.kinds import NodeKind, RawKind, parse_kind
from alkera_core.schemas.files.principal import (
    ROLE_LADDER,
    DirectGrant,
    Grant,
    InheritedGrant,
    Principal,
    RawGrantOrigin,
)
from alkera_core.versioning import VersionedModel
from pydantic import ValidationError

SCHEMA_PACKAGE = "alkera_core.schemas.files"


def _schema_modules() -> list[Any]:
    package = importlib.import_module(SCHEMA_PACKAGE)
    return [
        importlib.import_module(f"{SCHEMA_PACKAGE}.{info.name}")
        for info in pkgutil.iter_modules(list(package.__path__))
    ]


def _examples() -> list[Any]:
    out: list[Any] = []
    for module in _schema_modules():
        name = module.__name__.rsplit(".", 1)[-1]
        examples: list[tuple[str, Callable[[], VersionedModel]]] = getattr(
            module, "FIXTURE_EXAMPLES", []
        )
        for label, factory in examples:
            out.append(pytest.param(factory, id=f"{name}.{label}"))
    return out


EXAMPLES = _examples()


def test_every_schema_module_publishes_its_examples() -> None:
    """The generator discovers by attribute; an empty corpus would make it vacuous."""
    assert len(EXAMPLES) >= 6


@pytest.mark.parametrize("factory", EXAMPLES)
def test_a_model_round_trips_through_json(factory: Callable[[], VersionedModel]) -> None:
    """JSON out, JSON in, JSON out again — byte-identical, or the shape is lossy."""
    model = factory()
    once = model.model_dump(mode="json")
    twice = type(model).model_validate(json.loads(json.dumps(once))).model_dump(mode="json")
    assert twice == once


# ----------------------------------------------------------------------
# Tolerant readers
# ----------------------------------------------------------------------


def test_an_unknown_principal_kind_becomes_a_raw_ace_and_survives_byte_for_byte() -> None:
    """A future ``agent``/``link`` ACE read by today's backend must ride through.

    Not just "does not raise": every key the newer writer wrote has to come back
    out, or a read-modify-write by an older backend would silently drop a grant.
    """
    written = {
        "principal_kind": "agent",
        "principal_id": "aa11bb22-cc33-4d44-8e55-ff6677889900",
        "role": "auditor",
        "origin": {"kind": "session", "session_id": "s-1"},
        "expires_at": "2027-01-01T00:00:00Z",
        "conditions": {"network": "corp"},
        "scoped_subtree": "1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9",
        "schema_version": "1.0.0",
    }
    body = AclBody.model_validate({"entries": [written]})
    (entry,) = body.entries
    assert isinstance(entry, RawAce)

    dumped: dict[str, Any] = body.model_dump(mode="json")["entries"][0]
    for key, value in written.items():
        assert dumped[key] == value, key


def test_a_known_principal_kind_is_not_swallowed_by_the_fallback() -> None:
    """The negative twin: the fallback must not become the catch-all for everything."""
    body = AclBody(
        entries=[
            UserAce(principal_id="u1", role="owner"),
            TeamAce(principal_id="t1", role="writer"),
            OrgAce(principal_id="o1", role="reader"),
        ]
    )
    reread = AclBody.model_validate(body.model_dump(mode="json"))
    assert [type(entry) for entry in reread.entries] == [UserAce, TeamAce, OrgAce]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("folder", NodeKind.FOLDER, id="known-folder"),
        pytest.param("remote", NodeKind.REMOTE, id="known-remote-not-yet-written"),
        pytest.param("placeholder", RawKind("placeholder"), id="unknown-future-kind"),
        pytest.param("", RawKind(""), id="unknown-empty"),
        pytest.param("Folder", RawKind("Folder"), id="unknown-case-differs"),
    ],
)
def test_parse_kind_never_raises_on_a_stored_value(
    value: str, expected: NodeKind | RawKind
) -> None:
    assert parse_kind(value) == expected


def test_a_history_snapshot_reads_a_kind_this_reader_does_not_know() -> None:
    """Compliance evidence outlives the binary that wrote it."""
    snapshot = HistorySnapshot.model_validate({"kind": "placeholder", "name": "eA=="})
    assert snapshot.node_kind() == RawKind("placeholder")
    assert snapshot.name == b"x"
    assert HistorySnapshot().node_kind() is None


def test_an_unknown_grant_origin_tag_routes_to_the_raw_variant() -> None:
    grant = Grant.model_validate(
        {
            "principal": {"kind": "user", "id": "u1"},
            "role": "reader",
            "origin": {"kind": "policy_engine", "policy_id": "p-9"},
        }
    )
    assert isinstance(grant.origin, RawGrantOrigin)
    assert grant.model_dump(mode="json")["origin"]["policy_id"] == "p-9"


def test_a_known_origin_tag_keeps_its_typed_variant() -> None:
    grant = Grant.model_validate(
        {
            "principal": {"kind": "user", "id": "u1"},
            "role": "reader",
            "origin": {"kind": "inherited", "ancestor_node_id": "n-1"},
        }
    )
    assert isinstance(grant.origin, InheritedGrant)
    assert grant.origin.ancestor_node_id == "n-1"


# ----------------------------------------------------------------------
# Roles
# ----------------------------------------------------------------------


@pytest.mark.parametrize("role", list(ROLE_LADDER))
def test_every_ladder_role_is_accepted(role: str) -> None:
    assert Grant(principal=Principal(kind="user", id="u1"), role=role).role == role


@pytest.mark.parametrize(
    "role",
    [
        pytest.param("admin", id="a-role-from-another-vocabulary"),
        pytest.param("Reader", id="right-name-wrong-case"),
        pytest.param("", id="empty"),
        pytest.param("readerr", id="typo"),
    ],
)
def test_a_role_outside_the_ladder_is_refused(role: str) -> None:
    with pytest.raises(ValidationError):
        Grant(principal=Principal(kind="user", id="u1"), role=role)
    with pytest.raises(ValidationError):
        UserAce(principal_id="u1", role=role)


def test_an_unknown_principal_kind_is_free_of_the_role_ladder() -> None:
    """A future kind may carry a future role; refusing it would be an outage."""
    body = AclBody.model_validate(
        {
            "entries": [
                {"principal_kind": "link", "principal_id": "l1", "role": "viewer_no_download"}
            ]
        }
    )
    assert body.model_dump(mode="json")["entries"][0]["role"] == "viewer_no_download"


# ----------------------------------------------------------------------
# xattr limits — boundaries as separate cases, with their negative twins
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name_len", "accepted"),
    [
        pytest.param(MAX_XATTR_NAME_BYTES - 1, True, id="name-254-bytes"),
        pytest.param(MAX_XATTR_NAME_BYTES, True, id="name-255-bytes-at-the-limit"),
        pytest.param(MAX_XATTR_NAME_BYTES + 1, False, id="name-256-bytes-over"),
    ],
)
def test_the_xattr_name_limit_is_on_the_bytes(name_len: int, accepted: bool) -> None:
    name = "u" * name_len
    if accepted:
        assert NodeAttrs(xattrs={name: b"v"}).xattrs[name] == b"v"
    else:
        with pytest.raises(ValidationError):
            NodeAttrs(xattrs={name: b"v"})


def test_a_multibyte_name_is_measured_in_bytes_not_characters() -> None:
    """128 two-byte characters is 256 bytes: refused, though it is 128 characters."""
    with pytest.raises(ValidationError):
        NodeAttrs(xattrs={"é" * 128: b"v"})
    assert NodeAttrs(xattrs={"é" * 127: b"v"}).xattrs  # 254 bytes: accepted


@pytest.mark.parametrize(
    ("size", "accepted"),
    [
        pytest.param(MAX_XATTR_VALUE_BYTES - 1, True, id="value-64KiB-minus-1"),
        pytest.param(MAX_XATTR_VALUE_BYTES, True, id="value-64KiB-at-the-limit"),
        pytest.param(MAX_XATTR_VALUE_BYTES + 1, False, id="value-64KiB-plus-1"),
    ],
)
def test_the_per_value_xattr_limit(size: int, accepted: bool) -> None:
    payload = {"user.big": b"\x00" * size}
    if accepted:
        assert len(NodeAttrs(xattrs=payload).xattrs["user.big"]) == size
    else:
        with pytest.raises(ValidationError):
            NodeAttrs(xattrs=payload)


@pytest.mark.parametrize(
    ("first", "second", "accepted"),
    [
        pytest.param(
            MAX_XATTR_TOTAL_BYTES // 2, MAX_XATTR_TOTAL_BYTES // 2, True, id="total-at-the-limit"
        ),
        pytest.param(
            MAX_XATTR_TOTAL_BYTES // 2, MAX_XATTR_TOTAL_BYTES // 2 + 1, False, id="total-plus-1"
        ),
    ],
)
def test_the_per_node_xattr_total_is_summed_across_values(
    first: int, second: int, accepted: bool
) -> None:
    """Two values that each pass alone can still exceed what a node may hold."""
    payload = {"user.a": b"\x00" * first, "user.b": b"\x00" * second}
    if accepted:
        assert NodeAttrs(xattrs=payload).xattrs.keys() == {"user.a", "user.b"}
    else:
        with pytest.raises(ValidationError):
            NodeAttrs(xattrs=payload)


def test_xattr_bytes_survive_the_base64_round_trip() -> None:
    """Arbitrary bytes, including a NUL and invalid UTF-8, come back identical."""
    raw = b"\x00\xff\xfe binary \x80"
    dumped = NodeAttrs(xattrs={"user.raw": raw}).model_dump(mode="json")
    assert isinstance(dumped["xattrs"]["user.raw"], str)
    assert NodeAttrs.model_validate(dumped).xattrs["user.raw"] == raw


def test_the_patch_limits_are_the_same_as_the_snapshot_limits() -> None:
    with pytest.raises(ValidationError):
        AttrsPatch(xattrs={"user.big": b"\x00" * (MAX_XATTR_VALUE_BYTES + 1)})
    assert AttrsPatch(xattrs=None).xattrs is None


# ----------------------------------------------------------------------
# ctime is the server's
# ----------------------------------------------------------------------


@pytest.mark.parametrize("field", ["ctime", "ctime_ns"])
def test_the_patch_refuses_a_client_supplied_ctime(field: str) -> None:
    with pytest.raises(ValidationError):
        AttrsPatch.model_validate({field: 1_767_268_800_000_000_000})


def test_the_patch_accepts_every_client_settable_attribute() -> None:
    """The negative twin of the ctime refusal: forbid must not refuse the real fields."""
    patch = AttrsPatch.model_validate(
        {
            "mode": 0o100644,
            "uid": 501,
            "gid": 20,
            "owner": "u1",
            "atime_ns": 1,
            "mtime_ns": 2,
            "birthtime_ns": 3,
            "nlink": 1,
            "rdev": 0,
            "xattrs": {"user.tag": "dg=="},
        }
    )
    assert patch.mtime_ns == 2


def test_ctime_is_not_a_field_of_the_persisted_snapshot() -> None:
    assert "ctime_ns" not in NodeAttrs.model_fields
    assert "ctime" not in NodeAttrs.model_fields


# ----------------------------------------------------------------------
# Interning
# ----------------------------------------------------------------------


def test_body_hash_is_independent_of_json_key_order() -> None:
    """Two backends serialize keys differently; they must still intern to one row."""
    entries = [
        {
            "role": "owner",
            "principal_kind": "user",
            "principal_id": "u1",
            "origin": {"kind": "direct"},
        }
    ]
    shuffled = [
        {
            "origin": {"kind": "direct"},
            "principal_id": "u1",
            "principal_kind": "user",
            "role": "owner",
        }
    ]
    assert (
        AclBody.model_validate({"entries": entries}).body_hash()
        == AclBody.model_validate({"entries": shuffled}).body_hash()
    )


def test_body_hash_is_sensitive_to_entry_order() -> None:
    """The body is an ORDERED list; a reordering is a different body, not the same row."""
    a = UserAce(principal_id="u1", role="owner")
    b = TeamAce(principal_id="t1", role="reader")
    assert AclBody(entries=[a, b]).body_hash() != AclBody(entries=[b, a]).body_hash()


def test_body_hash_is_sensitive_to_every_field_that_changes_access() -> None:
    base = AclBody(entries=[UserAce(principal_id="u1", role="reader")])
    variants = [
        AclBody(entries=[UserAce(principal_id="u2", role="reader")]),
        AclBody(entries=[UserAce(principal_id="u1", role="writer")]),
        AclBody(entries=[TeamAce(principal_id="u1", role="reader")]),
        AclBody(
            entries=[
                UserAce(
                    principal_id="u1", role="reader", origin=InheritedGrant(ancestor_node_id="a")
                )
            ]
        ),
        AclBody(
            entries=[UserAce(principal_id="u1", role="reader", conditions={"network": "corp"})]
        ),
    ]
    hashes = {base.body_hash()} | {variant.body_hash() for variant in variants}
    assert len(hashes) == len(variants) + 1


def test_body_hash_ignores_the_schema_version_stamp() -> None:
    """A version bump that does not change meaning must not re-intern every ACL."""
    entries = [{"principal_kind": "user", "principal_id": "u1", "role": "owner"}]
    with_stamp = AclBody.model_validate({"schema_version": "1.0.0", "entries": entries})
    assert with_stamp.body_hash() == AclBody.model_validate({"entries": entries}).body_hash()


def test_body_hash_is_a_sha256_hex_digest() -> None:
    digest = AclBody(entries=[UserAce(principal_id="u1", role="owner")]).body_hash()
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_an_empty_body_hashes_stably() -> None:
    assert AclBody().body_hash() == AclBody(entries=[]).body_hash()


# ----------------------------------------------------------------------
# The rule the rest rests on
# ----------------------------------------------------------------------


def _versioned_subclasses() -> list[Any]:
    out: list[Any] = []
    for module in _schema_modules():
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, VersionedModel)
                and attr is not VersionedModel
                and attr.__module__.startswith(SCHEMA_PACKAGE)
            ):
                out.append(pytest.param(attr, id=attr.__qualname__))
    return out


VERSIONED = _versioned_subclasses()


@pytest.mark.parametrize("model_type", VERSIONED)
def test_extra_allow_is_never_narrowed_to_forbid(model_type: type[VersionedModel]) -> None:
    """Narrowing here would break forward compatibility for this shape and every
    shape that embeds it — the single most important rule of the versioning base."""
    assert model_type.model_config.get("extra") == "allow"


@pytest.mark.parametrize("model_type", VERSIONED)
def test_every_concrete_persisted_shape_declares_its_version(
    model_type: type[VersionedModel],
) -> None:
    if model_type.__abstract__:
        pytest.skip("an intermediate base carries no version of its own")
    # The version a shape declares is its own business — a bump is how an
    # additive field is announced — so the rule is that the class states one
    # itself, in semver, rather than inheriting the base's placeholder.
    declared = model_type.__dict__.get("SCHEMA_VERSION")
    assert declared is not None, (
        f"{model_type.__qualname__} inherits its version instead of declaring one"
    )
    assert re.fullmatch(r"\d+\.\d+\.\d+", declared), (
        f"{model_type.__qualname__} declares {declared!r}, which is not a semver"
    )
    assert model_type.SCHEMA_VERSION == declared


def test_the_versioned_walk_actually_found_the_models() -> None:
    found = {param.values[0] for param in VERSIONED}
    assert {NodeAttrs, Principal, Grant, AclBody, UserAce, RawAce, HistorySnapshot} <= found


def test_an_unknown_field_from_a_newer_writer_rides_through_a_nested_shape() -> None:
    """The rule above, proven at the level that matters: a nested model."""
    payload: dict[str, Any] = {
        "kind": "file",
        "attrs": {"mode": 33188, "selinux_context": "system_u:object_r:t"},
    }
    dumped = HistorySnapshot.model_validate(payload).model_dump(mode="json")
    assert dumped["attrs"]["selinux_context"] == "system_u:object_r:t"


def test_the_history_kinds_cover_every_mutation_that_gets_a_row() -> None:
    assert {kind.value for kind in HistoryKind} == {
        "create",
        "rename",
        "move",
        "attrs",
        "trash",
        "restore",
        "lock",
        "hold",
        "label",
        "acl",
    }


def test_the_modules_expose_the_seams_the_generator_discovers() -> None:
    """The generator keys on this attribute; a rename would silently empty the corpus."""
    assert hasattr(acl_mod, "FIXTURE_EXAMPLES")
    assert hasattr(attrs_mod, "FIXTURE_EXAMPLES")
    assert DirectGrant().kind == "direct"
