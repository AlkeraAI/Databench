"""The runtime and wire shapes: round trips, signatures, epochs, facets.

Every case here is about a property the shape must hold for a *different*
process to read what this one wrote — a rolling deploy's older backend, a
desktop client's cache, a pointer file sitting on someone's disk.
"""

from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_core.files import delta_token as token_module
from alkera_core.schemas.files import delta as delta_module
from alkera_core.schemas.files import item as item_module
from alkera_core.schemas.files import lease as lease_module
from alkera_core.schemas.files import operation as operation_module
from alkera_core.schemas.files import pointer as pointer_module
from alkera_core.schemas.files import session as session_module
from alkera_core.schemas.files import sharing as sharing_module
from alkera_core.schemas.files.delta import (
    DeltaPage,
    DeltaToken,
    InvalidDeltaToken,
    ResyncCode,
    decode_token,
    encode_token,
)
from alkera_core.schemas.files.item import (
    AttrsFacet,
    Capabilities,
    FileFacet,
    Item,
    LeaseFacet,
    NameFlagsWire,
    ObjectFacet,
    SymlinkFacet,
)
from alkera_core.schemas.files.lease import SEQ_MAX, epoch_for, split_epoch
from alkera_core.schemas.files.operation import (
    INVERSE_KINDS,
    MoveInverse,
    OperationInverse,
    OperationKind,
    OperationState,
    OperationStatus,
    RawInverse,
    RenameInverse,
)
from alkera_core.schemas.files.pointer import (
    POINTER_EXTENSIONS,
    InvalidPointer,
    PointerFile,
    sign,
    verify,
)
from alkera_core.schemas.files.session import PartRecord, SessionStatus, UploadSessionState
from alkera_core.versioning import VersionedModel
from pydantic import TypeAdapter

KEY = "a-test-signing-key"
OTHER_KEY = "a-different-deployments-key"

_MODULES = [
    delta_module,
    item_module,
    lease_module,
    operation_module,
    pointer_module,
    session_module,
    sharing_module,
]

_EXAMPLES: list[Any] = [
    pytest.param(factory(), id=name)
    for module in _MODULES
    for name, factory in sorted(module.FIXTURE_EXAMPLES, key=lambda entry: entry[0])
]


# --------------------------------------------------------------------------
# persisted shapes
# --------------------------------------------------------------------------


def test_every_module_publishes_fixture_examples() -> None:
    """The generator discovers fixtures through this mapping; an empty one would
    make every lineage case below vacuous."""
    for module in _MODULES:
        assert module.FIXTURE_EXAMPLES, f"{module.__name__} publishes no FIXTURE_EXAMPLES"
    assert len(_EXAMPLES) >= 15


@pytest.mark.parametrize("example", _EXAMPLES)
def test_a_runtime_shape_round_trips_through_json(example: VersionedModel) -> None:
    dumped = example.model_dump(mode="json")
    reloaded = type(example).model_validate(json.loads(json.dumps(dumped)))
    assert reloaded.model_dump(mode="json") == dumped
    assert dumped["schema_version"] == type(example).SCHEMA_VERSION


@pytest.mark.parametrize("example", _EXAMPLES)
def test_a_field_from_a_newer_writer_rides_along(example: VersionedModel) -> None:
    """`extra="allow"` is what lets a rolling deploy's older reader write the
    record back without dropping the newer writer's field."""
    payload = example.model_dump(mode="json")
    payload["a_field_from_a_newer_writer"] = {"n": 7}
    reloaded = type(example).model_validate(payload)
    assert reloaded.model_dump(mode="json")["a_field_from_a_newer_writer"] == {"n": 7}


# --------------------------------------------------------------------------
# operation inverses
# --------------------------------------------------------------------------

_INVERSE = TypeAdapter(OperationInverse)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(
            {"kind": "move", "node_id": "n", "previous_parent_id": "p"},
            MoveInverse,
            id="move",
        ),
        pytest.param(
            {"kind": "rename", "node_id": "n", "previous_name": "report.pdf"},
            RenameInverse,
            id="rename",
        ),
        pytest.param(
            {"kind": "trash", "trash_op_id": "t"}, operation_module.TrashInverse, id="trash"
        ),
        pytest.param(
            {"kind": "restore", "trash_op_id": "t"}, operation_module.RestoreInverse, id="restore"
        ),
        pytest.param(
            {"kind": "attrs", "node_id": "n", "before": {"mode": 1}},
            operation_module.AttrsInverse,
            id="attrs",
        ),
    ],
)
def test_a_known_inverse_routes_to_its_own_variant(
    payload: dict[str, Any], expected: type[VersionedModel]
) -> None:
    assert isinstance(_INVERSE.validate_python(payload), expected)


def test_an_inverse_kind_this_reader_never_heard_of_survives_as_raw() -> None:
    """A newer writer's operation kind must round-trip, not raise: an old reader
    that dropped it would silently lose an undo."""
    payload = {"kind": "quarantine", "node_id": "n-1", "vault": "v-9"}
    parsed = _INVERSE.validate_python(payload)
    assert isinstance(parsed, RawInverse)
    assert parsed.kind == "quarantine"
    dumped = parsed.model_dump(mode="json")
    assert dumped["node_id"] == "n-1"
    assert dumped["vault"] == "v-9"


def test_the_known_tags_are_exactly_the_variants_the_union_declares() -> None:
    assert INVERSE_KINDS == {"move", "rename", "trash", "restore", "attrs"}


def test_operation_state_carries_progress_and_no_names() -> None:
    state = OperationState(
        kind=OperationKind.TRASH, state=OperationStatus.RUNNING, done=3, total=10
    )
    dumped = state.model_dump(mode="json")
    assert dumped["state"] == "running"
    assert dumped["kind"] == "trash"
    assert set(OperationState.model_fields) >= {
        "state",
        "done",
        "total",
        "bytes",
        "skipped",
        "conflicts",
        "errors",
        "result_url",
        "result_url_expires_at",
        "heartbeat_at",
        "cursor",
    }


# --------------------------------------------------------------------------
# delta tokens
# --------------------------------------------------------------------------


def _token() -> DeltaToken:
    return DeltaToken(
        outbox_id=987_654_321,
        issued_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        generation=2,
    )


def test_a_delta_token_round_trips_through_its_signed_form() -> None:
    token = _token()
    decoded = decode_token(encode_token(token, key=KEY), key=KEY)
    assert decoded.model_dump(mode="json") == token.model_dump(mode="json")


def test_a_token_from_another_deployments_key_is_refused() -> None:
    encoded = encode_token(_token(), key=OTHER_KEY)
    with pytest.raises(InvalidDeltaToken):
        decode_token(encoded, key=KEY)


@pytest.mark.parametrize("part", [0, 1], ids=["payload", "signature"])
@pytest.mark.parametrize("bit", [0, 3, 7], ids=["bit0", "bit3", "bit7"])
def test_every_flipped_bit_invalidates_the_token(part: int, bit: int) -> None:
    """A client must not be able to move its own watermark: any single-bit edit,
    in the payload or in the signature, is refused."""
    pieces = encode_token(_token(), key=KEY).split(".")
    raw = bytearray(token_module._b64decode(pieces[part]))
    for index in range(len(raw)):
        tampered = bytearray(raw)
        tampered[index] ^= 1 << bit
        pieces_copy = list(pieces)
        pieces_copy[part] = token_module._b64encode(bytes(tampered))
        with pytest.raises(InvalidDeltaToken):
            decode_token(".".join(pieces_copy), key=KEY)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty"),
        pytest.param("no-separator", id="no_separator"),
        pytest.param(".sig", id="no_payload"),
        pytest.param("payload.", id="no_signature"),
        pytest.param("a.b", id="not_base64"),
    ],
)
def test_a_malformed_token_is_the_same_refusal(raw: str) -> None:
    with pytest.raises(InvalidDeltaToken):
        decode_token(raw, key=KEY)


def test_a_well_signed_payload_that_is_not_a_token_is_refused() -> None:
    """The signature proves provenance, not shape — the shape is checked too."""
    payload = b"[1, 2, 3]"
    encoded = (
        f"{token_module._b64encode(payload)}."
        f"{token_module._b64encode(token_module._mac(payload, KEY))}"
    )
    with pytest.raises(InvalidDeltaToken):
        decode_token(encoded, key=KEY)


def test_the_signature_check_is_a_constant_time_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one justified call assertion: a timing oracle leaves no observable
    state to assert on, so the only proof is that the constant-time primitive is
    what gets called."""
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def _spy(a: Any, b: Any) -> bool:
        calls.append((bytes(a), bytes(b)))
        return bool(real(a, b))

    monkeypatch.setattr(token_module.hmac, "compare_digest", _spy)
    decode_token(encode_token(_token(), key=KEY), key=KEY)
    assert len(calls) == 1
    assert calls[0][0] == calls[0][1]


def test_the_resync_codes_are_the_two_the_client_knows() -> None:
    assert {code.value for code in ResyncCode} == {
        "resync_apply_differences",
        "resync_upload_differences",
    }


def test_a_delta_page_serializes_camel_case_links() -> None:
    page = DeltaPage(items=[Item(id="n-1")], next_link="?token=a", delta_link=None)
    dumped = page.model_dump(mode="json", by_alias=True)
    assert dumped["nextLink"] == "?token=a"
    assert "next_link" not in dumped
    assert dumped["items"][0]["id"] == "n-1"


# --------------------------------------------------------------------------
# lease epochs
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("generation", "seq"),
    [
        pytest.param(0, 0, id="origin"),
        pytest.param(0, 1, id="first_seq"),
        pytest.param(0, SEQ_MAX, id="last_seq_in_generation"),
        pytest.param(1, 0, id="first_of_next_generation"),
        pytest.param(7, 123_456, id="mid"),
    ],
)
def test_an_epoch_splits_back_into_the_pair_that_built_it(generation: int, seq: int) -> None:
    assert split_epoch(epoch_for(generation, seq)) == (generation, seq)


def test_an_epoch_after_a_restore_is_above_every_epoch_the_restore_lost() -> None:
    """The whole point of the high half: a restore rewinds the per-node sequence,
    so a client fenced before the restore must not come back at a live epoch."""
    lost = [epoch_for(3, seq) for seq in (0, 1, 99, SEQ_MAX)]
    after_restore = epoch_for(4, 0)
    assert after_restore > max(lost)


@pytest.mark.parametrize(
    ("generation", "seq"),
    [
        pytest.param(-1, 0, id="negative_generation"),
        pytest.param(0, -1, id="negative_seq"),
        pytest.param(0, SEQ_MAX + 1, id="seq_overflows_its_half"),
    ],
)
def test_an_epoch_that_would_not_be_invertible_is_refused(generation: int, seq: int) -> None:
    """A seq one past the boundary would carry into the generation half and read
    back as a different generation — refuse it rather than mint it."""
    with pytest.raises(ValueError):
        epoch_for(generation, seq)


def test_a_negative_epoch_is_refused_by_the_inverse() -> None:
    with pytest.raises(ValueError):
        split_epoch(-1)


# --------------------------------------------------------------------------
# pointers
# --------------------------------------------------------------------------


def _pointer() -> PointerFile:
    return PointerFile(
        kind="chat",
        web_url="https://app.example.test/chats/1",
        app_url="alkera://chats/1",
        rendered_mime="text/markdown",
    )


def test_a_signed_pointer_verifies_back_to_the_same_record() -> None:
    verified = verify(sign(_pointer(), key=KEY), key=KEY)
    assert verified.model_dump(mode="json") == _pointer().model_dump(mode="json")


def test_a_pointer_edited_on_disk_no_longer_verifies() -> None:
    """`push` must be able to tell an edited pointer from one it wrote, so it can
    warn and skip instead of turning the edit into a version."""
    body = json.loads(sign(_pointer(), key=KEY))
    body["web_url"] = "https://evil.example.test/chats/1"
    with pytest.raises(InvalidPointer):
        verify(json.dumps(body), key=KEY)


def test_a_pointer_signed_by_another_deployment_is_refused() -> None:
    with pytest.raises(InvalidPointer):
        verify(sign(_pointer(), key=OTHER_KEY), key=KEY)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("not json", id="not_json"),
        pytest.param("[]", id="not_an_object"),
        pytest.param('{"kind": "chat"}', id="unsigned"),
        pytest.param('{"kind": "chat", "signature": 7}', id="signature_is_not_a_string"),
    ],
)
def test_a_malformed_pointer_is_refused(text: str) -> None:
    with pytest.raises(InvalidPointer):
        verify(text, key=KEY)


def test_every_pointer_kind_has_a_registered_extension() -> None:
    assert POINTER_EXTENSIONS["chat"] == ".alkerachat"
    assert all(ext.startswith(".alkera") for ext in POINTER_EXTENSIONS.values())
    assert len(set(POINTER_EXTENSIONS.values())) == len(POINTER_EXTENSIONS)


# --------------------------------------------------------------------------
# the item payload
# --------------------------------------------------------------------------


def test_an_item_serializes_every_multiword_field_in_camel_case() -> None:
    item = Item(
        id="n-1",
        drive_id="d-1",
        name_display="report.pdf",
        name_encoding="utf-8",
        path_bytes=b"/a/report.pdf",
        parent_id="p-1",
    )
    dumped = item.model_dump(mode="json", by_alias=True)
    for camel in ("driveId", "nameDisplay", "nameEncoding", "nameFlags", "pathBytes", "parentId"):
        assert camel in dumped, camel
    assert "drive_id" not in dumped
    assert dumped["nameFlags"]["windows_safe"] is True


def test_an_item_accepts_either_spelling_on_the_way_in() -> None:
    """`populate_by_name` keeps server-side construction in snake_case while the
    wire stays camelCase."""
    by_alias = Item.model_validate({"id": "n-1", "driveId": "d-1", "parentId": "p-1"})
    by_name = Item(id="n-1", drive_id="d-1", parent_id="p-1")
    assert by_alias.model_dump(mode="json") == by_name.model_dump(mode="json")


def test_the_object_facet_is_spelled_object_on_the_wire() -> None:
    item = Item(id="n-1", kind="object", object=ObjectFacet(type="chat", id="c-1"))
    dumped = item.model_dump(mode="json", by_alias=True)
    assert dumped["object"]["type"] == "chat"
    assert "object_" not in dumped


@pytest.mark.parametrize(
    ("item", "present"),
    [
        pytest.param(
            Item(id="n-1", kind="file", file=FileFacet(mime_type="application/pdf", size=1)),
            {"file"},
            id="file_has_a_file_facet",
        ),
        pytest.param(Item(id="n-2", kind="folder"), set(), id="folder_has_none_of_them"),
        pytest.param(
            Item(id="n-3", kind="symlink", symlink=SymlinkFacet(target="../x")),
            {"symlink"},
            id="symlink_has_a_symlink_facet",
        ),
        pytest.param(
            Item(id="n-4", kind="object", object=ObjectFacet(type="chat", id="c-1")),
            {"object"},
            id="object_has_an_object_facet",
        ),
        pytest.param(
            Item(id="n-5", kind="file", file=FileFacet(), lease=LeaseFacet(holder="u-1")),
            {"file", "lease"},
            id="a_leased_file_carries_the_lease_facet",
        ),
    ],
)
def test_a_facet_is_absent_rather_than_empty_when_it_does_not_apply(
    item: Item, present: set[str]
) -> None:
    dumped = item.model_dump(mode="json", by_alias=True)
    for facet in ("file", "symlink", "object", "lease"):
        if facet in present:
            assert dumped[facet] is not None, facet
        else:
            assert dumped[facet] is None, facet


def test_capabilities_carry_a_refusal_reason_per_denied_action() -> None:
    caps = Capabilities(can_read=True, refusals={"canWrite": "files.leased"})
    dumped = caps.model_dump(mode="json")
    assert dumped["can_read"] is True
    assert dumped["can_write"] is False
    assert dumped["refusals"]["canWrite"] == "files.leased"


@pytest.mark.parametrize(
    "facet",
    [
        pytest.param(AttrsFacet(), id="attrs"),
        pytest.param(FileFacet(), id="file"),
        pytest.param(SymlinkFacet(), id="symlink"),
        pytest.param(ObjectFacet(), id="object"),
        pytest.param(LeaseFacet(), id="lease"),
        pytest.param(Capabilities(), id="capabilities"),
        pytest.param(NameFlagsWire(), id="name_flags"),
    ],
)
def test_every_item_facet_is_a_versioned_model(facet: VersionedModel) -> None:
    """The item payload's facets are built from versioned internals, so a
    field added to one later is additive for every persisted copy."""
    assert isinstance(facet, VersionedModel)
    declared = type(facet).__dict__.get("SCHEMA_VERSION")
    assert declared is not None, (
        f"{type(facet).__qualname__} inherits its version instead of declaring one"
    )
    # The stamp travels with the payload: a facet that declared a version but
    # serialised something else would be unreadable by its own migrations.
    assert facet.model_dump(mode="json")["schema_version"] == declared


# --------------------------------------------------------------------------
# sessions and sharing
# --------------------------------------------------------------------------


def test_session_status_is_camel_case_on_the_wire() -> None:
    status = SessionStatus(session_id="s-1", offset=64, length=128, complete=False, parts_done=2)
    dumped = status.model_dump(mode="json", by_alias=True)
    assert dumped["partsDone"] == 2
    assert dumped["sessionId"] == "s-1"
    assert "parts_done" not in dumped


def test_a_session_keeps_each_accepted_parts_checksum() -> None:
    """The checksum is what lets a re-sent part with different bytes be refused
    rather than overwrite an accepted one."""
    state = UploadSessionState(
        name="report.pdf",
        parts=[
            PartRecord(part_no=1, size=8, checksum="aa"),
            PartRecord(part_no=2, size=8, checksum="bb"),
        ],
    )
    reloaded = UploadSessionState.model_validate(state.model_dump(mode="json"))
    assert [(p.part_no, p.checksum) for p in reloaded.parts] == [(1, "aa"), (2, "bb")]
