"""Regenerate the realtime lineage fixtures at the CURRENT writer version.

Run from the repo root::

    uv run python packages/api-core/tests/fixtures/realtime/generate.py

Writes one ``DocEnvelope`` dump per envelope kind (``envelope/``), one frame
per client and server tag (``frames/client/``, ``frames/server/``) and one
dump per kind-specific payload model (``payloads/``) into
``packages/api-core/tests/fixtures/realtime/v<SCHEMA_VERSION>/``. Commit the diff alongside the
schema change that prompted the regeneration.

NEVER edit old fixture files by hand. Migrations go in the model's
``MIGRATIONS`` dict; fixtures stay frozen as historical evidence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from alkera_core.schemas.realtime import (
    CRDT_CHUNK_BYTES,
    CRDT_PROTOCOL,
    SERVER_PEER_ID,
    AckPayload,
    CrdtAckPayload,
    CrdtChunk,
    CrdtEphemeralPayload,
    CrdtGonePayload,
    CrdtHelloPayload,
    CrdtLimits,
    CrdtSaving,
    CrdtSavingPayload,
    CrdtSyncPayload,
    CrdtUpdatePayload,
    DocEnvelope,
    DocFrame,
    ErrorFrame,
    ErrorPayload,
    FieldWrite,
    HelloPayload,
    OpPayload,
    PingFrame,
    PongFrame,
    PresenceCursor,
    PresenceCursorFrame,
    PresenceFrame,
    PresenceHeartbeatFrame,
    PresenceJoinFrame,
    PresenceLeaveFrame,
    PresencePayload,
    PresencePeer,
    PublisherFrame,
    ReloadPayload,
    ResetFrame,
    SnapshotPayload,
    SocketLimits,
    SubscribedFrame,
    SubscribeFrame,
    UnsubscribeFrame,
    WelcomeFrame,
)
from alkera_core.versioning import VersionedModel

# Fixed values so a regenerate is byte-identical until the shape changes.
_T = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
CHAT_ID = "sess-01J7Q3M8"
ARTIFACT_ID = "0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f"
PEER = "p:3f9a1c2b4d6e"
OTHER_PEER = "p:a1b2c3d4e5f6"
USER_ID = "00000000-0000-4000-8000-000000000001"
USER_EMAIL = "dana.okafor@acme.test"
CHAT_CHANNEL = f"doc:chat:{CHAT_ID}"
ARTIFACT_CHANNEL = f"doc:artifact:{ARTIFACT_ID}"
WORKSPACE_CHANNEL = f"doc:chat_draft:{CHAT_ID}"
LORO_PEER = 4_503_599_627_370_517
# Opaque stand-ins: the schemas check the encoding, the sandbox the contents.
VV_B64 = "AZWDgICAgIAIAg=="
UPDATE_B64 = "bG9yby11cGRhdGU="
EPHEMERAL_B64 = "ZXBoZW1lcmFs"
CHUNK_SHA_B64 = "47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="


def _peer(peer_id: str = PEER) -> PresencePeer:
    return PresencePeer(peer_id=peer_id, user_id=USER_ID, last_seen_at=_T, email=USER_EMAIL)


def _cursor() -> PresenceCursor:
    return PresenceCursor(offset=11, anchor=5, before="what about", after=" last quarter?")


def payloads() -> dict[str, VersionedModel]:
    """One instance per kind-specific payload model."""
    return {
        "hello": HelloPayload(since_seq=None),
        "hello_with_since_seq": HelloPayload(since_seq=41),
        "snapshot_chat": SnapshotPayload(
            state={
                "meta": {"session_id": CHAT_ID, "title": "Investigate slow query"},
                "events": [{"event_id": "e1", "event_type": "message.created"}],
                "ids": {"e1": 0},
            },
            seq=7,
        ),
        "snapshot_artifact": SnapshotPayload(
            state={
                "kb_version": 3,
                "fields": {
                    "title": {"value": "Orders fact", "ts": 1757073600.0, "peer_id": PEER},
                    "body": {"value": "orders are unique", "ts": 1757073600.0, "peer_id": PEER},
                },
            },
            seq=2,
        ),
        "field_write": FieldWrite(value="Orders fact, revised", ts=1757073660.5),
        "op_append": OpPayload(
            op_id="op-append-1",
            intent="append",
            events=[{"event_id": "e2", "event_type": "message.completed"}],
        ),
        "op_set_meta": OpPayload(op_id="op-meta-1", intent="set_meta", meta={"title": "Renamed"}),
        "op_user_message": OpPayload(
            op_id="op-user-1",
            intent="user_message",
            events=[{"event_id": "e3", "event_type": "message.created", "text": "hello"}],
        ),
        "op_chunk": OpPayload(
            op_id="op-chunk-1",
            intent="chunk",
            events=[{"event_id": "e4", "event_type": "agent.message_chunk", "delta": "Th"}],
        ),
        "op_set_fields": OpPayload(
            op_id="op-fields-1",
            intent="set_fields",
            fields={"title": FieldWrite(value="Orders fact, revised", ts=1757073660.5)},
        ),
        "ack": AckPayload(op_id="op-append-1", seq=8, changed=True),
        "ack_noop": AckPayload(op_id="op-fields-2", seq=2, changed=False),
        "reload": ReloadPayload(epoch=3, reason="publisher_snapshot"),
        "reload_stale": ReloadPayload(epoch=2, reason="stale_epoch"),
        "reload_saving_paused": ReloadPayload(
            epoch=4, reason="compacted", saving=CrdtSaving.of("no_writer")
        ),
        "error": ErrorPayload(code="stale_epoch", message="the document moved on"),
        "error_for_op": ErrorPayload(
            code="op_too_large",
            message="the operation would be 20000 bytes in the event log; the cap is 16384",
            op_id="op-append-1",
        ),
        "presence_peer": _peer(),
        "presence_peer_with_cursor": _peer().model_copy(update={"cursor": _cursor()}),
        "presence_cursor": _cursor(),
        "presence": PresencePayload(peers=[_peer(), _peer(OTHER_PEER)]),
        "error_crdt_busy": ErrorPayload(
            code="crdt_busy",
            message="the document is busy; send again shortly",
            update_id="u-7",
            reason="queue_full",
            retry_after_ms=250,
        ),
        "crdt_hello": CrdtHelloPayload(proto=CRDT_PROTOCOL, loro="1.16.4", doc_schema=1),
        "crdt_hello_resume": CrdtHelloPayload(
            proto=CRDT_PROTOCOL,
            loro="1.16.4",
            doc_schema=1,
            vv_b64=VV_B64,
            loro_peer=LORO_PEER,
            epoch_seen=2,
        ),
        "crdt_limits": _limits(),
        "crdt_sync_updates": CrdtSyncPayload(
            mode="updates",
            vv_b64=VV_B64,
            loro_peer=LORO_PEER,
            doc_schema=1,
            limits=_limits(),
            data_b64=UPDATE_B64,
        ),
        "crdt_sync_snapshot_chunk": CrdtSyncPayload(
            mode="snapshot",
            vv_b64=VV_B64,
            loro_peer=LORO_PEER,
            doc_schema=1,
            limits=_limits(),
            chunk=_chunk(),
        ),
        "crdt_sync_saving_paused": CrdtSyncPayload(
            mode="snapshot",
            vv_b64=VV_B64,
            loro_peer=LORO_PEER,
            doc_schema=1,
            limits=_limits(),
            data_b64=UPDATE_B64,
            saving=CrdtSaving.of("files.frozen"),
        ),
        "crdt_saving_state": CrdtSaving.of(None),
        "crdt_chunk": _chunk(),
        "crdt_update": CrdtUpdatePayload(update_id="u-7", data_b64=UPDATE_B64),
        "crdt_update_broadcast": CrdtUpdatePayload(
            update_id="u-7",
            data_b64=UPDATE_B64,
            loro_peer=LORO_PEER,
            user_id=USER_ID,
            vv_b64=VV_B64,
        ),
        "crdt_update_by_ref": CrdtUpdatePayload(
            update_id="u-8", loro_peer=LORO_PEER, user_id=USER_ID, vv_b64=VV_B64, log_ref=42
        ),
        "crdt_ephemeral": CrdtEphemeralPayload(
            data_b64=EPHEMERAL_B64,
            loro_peer=LORO_PEER,
            user_id=USER_ID,
            display_name="Dana Okafor",
            email=USER_EMAIL,
        ),
        "crdt_ack": CrdtAckPayload(update_id="u-7", changed=True, vv_b64=VV_B64),
        "crdt_gone": CrdtGonePayload(loro_peer=LORO_PEER),
        "crdt_saving": CrdtSavingPayload(state="paused", reason="leased"),
    }


def _limits() -> CrdtLimits:
    return CrdtLimits(
        chunk_bytes=CRDT_CHUNK_BYTES,
        max_update_bytes=512 * 1024,
        max_doc_bytes=2 * 1024 * 1024,
        max_text_bytes=16 * 1024,
    )


def _chunk() -> CrdtChunk:
    return CrdtChunk(
        xfer_id="sync-1",
        index=0,
        count=3,
        total_bytes=2 * CRDT_CHUNK_BYTES + 1,
        sha256_b64=CHUNK_SHA_B64,
        data_b64=UPDATE_B64,
    )


def envelopes() -> dict[str, DocEnvelope]:
    """One envelope per kind (plus the hello with no state)."""
    p = payloads()
    return {
        "hello": DocEnvelope(
            doc_id=CHAT_ID, doc_type="chat", epoch=0, peer_id=PEER, seq=0, kind="hello"
        ),
        "hello_resume": DocEnvelope(
            doc_id=CHAT_ID,
            doc_type="chat",
            epoch=2,
            peer_id=PEER,
            seq=0,
            kind="hello",
            payload=p["hello_with_since_seq"].model_dump(mode="json"),
        ),
        "snapshot": DocEnvelope(
            doc_id=CHAT_ID,
            doc_type="chat",
            epoch=2,
            peer_id=SERVER_PEER_ID,
            seq=7,
            kind="snapshot",
            payload=p["snapshot_chat"].model_dump(mode="json"),
        ),
        "op_append": DocEnvelope(
            doc_id=CHAT_ID,
            doc_type="chat",
            epoch=2,
            peer_id=PEER,
            seq=8,
            kind="op",
            payload=p["op_append"].model_dump(mode="json"),
        ),
        "op_set_meta": DocEnvelope(
            doc_id=CHAT_ID,
            doc_type="chat",
            epoch=2,
            peer_id=PEER,
            seq=9,
            kind="op",
            payload=p["op_set_meta"].model_dump(mode="json"),
        ),
        "op_user_message": DocEnvelope(
            doc_id=CHAT_ID,
            doc_type="chat",
            epoch=2,
            peer_id=OTHER_PEER,
            seq=0,
            kind="op",
            payload=p["op_user_message"].model_dump(mode="json"),
        ),
        "op_chunk": DocEnvelope(
            doc_id=CHAT_ID,
            doc_type="chat",
            epoch=2,
            peer_id=PEER,
            seq=0,
            kind="op",
            payload=p["op_chunk"].model_dump(mode="json"),
        ),
        "op_set_fields": DocEnvelope(
            doc_id=ARTIFACT_ID,
            doc_type="artifact",
            epoch=1,
            peer_id=PEER,
            seq=3,
            kind="op",
            payload=p["op_set_fields"].model_dump(mode="json"),
        ),
        "ack": DocEnvelope(
            doc_id=CHAT_ID,
            doc_type="chat",
            epoch=2,
            peer_id=SERVER_PEER_ID,
            seq=8,
            kind="ack",
            payload=p["ack"].model_dump(mode="json"),
        ),
        "presence": DocEnvelope(
            doc_id=ARTIFACT_ID,
            doc_type="artifact",
            epoch=1,
            peer_id=SERVER_PEER_ID,
            seq=0,
            kind="presence",
            payload=p["presence"].model_dump(mode="json"),
        ),
        "reload": DocEnvelope(
            doc_id=CHAT_ID,
            doc_type="chat",
            epoch=3,
            peer_id=SERVER_PEER_ID,
            seq=0,
            kind="reload",
            payload=p["reload"].model_dump(mode="json"),
        ),
        "error": DocEnvelope(
            doc_id=CHAT_ID,
            doc_type="chat",
            epoch=2,
            peer_id=SERVER_PEER_ID,
            seq=0,
            kind="error",
            payload=p["error"].model_dump(mode="json"),
        ),
        "crdt_hello": DocEnvelope(
            doc_id=CHAT_ID,
            doc_type="chat_draft",
            epoch=0,
            peer_id=PEER,
            seq=0,
            kind="hello",
            payload=p["crdt_hello"].model_dump(mode="json"),
        ),
        "crdt_sync": DocEnvelope(
            doc_id=CHAT_ID,
            doc_type="chat_draft",
            epoch=2,
            peer_id=SERVER_PEER_ID,
            seq=0,
            kind="snapshot",
            payload=p["crdt_sync_updates"].model_dump(mode="json"),
        ),
        "crdt_update": DocEnvelope(
            doc_id=CHAT_ID,
            doc_type="chat_draft",
            epoch=2,
            peer_id=PEER,
            seq=0,
            kind="crdt",
            payload=p["crdt_update_broadcast"].model_dump(mode="json"),
        ),
        "crdt_ephemeral": DocEnvelope(
            doc_id=CHAT_ID,
            doc_type="chat_draft",
            epoch=2,
            peer_id=PEER,
            seq=0,
            kind="crdt",
            payload=p["crdt_ephemeral"].model_dump(mode="json"),
        ),
        "crdt_notebook_hello": DocEnvelope(
            doc_id="7c1f8a52-6a0e-4b8f-9d3a-2f6e1c0b9a11",
            doc_type="notebook",
            epoch=0,
            peer_id=PEER,
            seq=0,
            kind="hello",
            payload=p["crdt_hello"].model_dump(mode="json"),
        ),
        "crdt_ack": DocEnvelope(
            doc_id=CHAT_ID,
            doc_type="chat_draft",
            epoch=2,
            peer_id=SERVER_PEER_ID,
            seq=0,
            kind="ack",
            payload=p["crdt_ack"].model_dump(mode="json"),
        ),
    }


def client_frames() -> dict[str, VersionedModel]:
    return {
        "subscribe": SubscribeFrame(channel=CHAT_CHANNEL),
        "unsubscribe": UnsubscribeFrame(channel=CHAT_CHANNEL),
        "presence_join": PresenceJoinFrame(channel=ARTIFACT_CHANNEL),
        "presence_leave": PresenceLeaveFrame(channel=ARTIFACT_CHANNEL),
        "presence_heartbeat": PresenceHeartbeatFrame(channel=ARTIFACT_CHANNEL),
        "presence_cursor": PresenceCursorFrame(channel=CHAT_CHANNEL, cursor=_cursor()),
        "ping": PingFrame(),
        "doc": DocFrame(envelope=envelopes()["op_append"]),
    }


def server_frames() -> dict[str, VersionedModel]:
    return {
        "welcome": WelcomeFrame(
            peer_id=PEER,
            server_time=_T,
            instance="b53991b7",
            min_client_generation=1,
            limits=SocketLimits(
                frames_per_window=200,
                bytes_per_window=4 * 1024 * 1024,
                window_seconds=10.0,
                max_frame_bytes=2 * 1024 * 1024 + 64 * 1024,
                ephemeral_max_bytes=4096,
                doc_max_bytes=4 * 1024 * 1024,
                presence_ttl_seconds=45.0,
            ),
        ),
        "subscribed": SubscribedFrame(channel=CHAT_CHANNEL, can_write=False),
        "presence": PresenceFrame(
            channel=ARTIFACT_CHANNEL, event="roster", peers=[_peer(), _peer(OTHER_PEER)]
        ),
        "reset": ResetFrame(reason="overflow"),
        "error": ErrorFrame(code="forbidden", message="not the owner", channel=ARTIFACT_CHANNEL),
        "pong": PongFrame(),
        "doc": DocFrame(envelope=envelopes()["snapshot"]),
        "publisher": PublisherFrame(channel=CHAT_CHANNEL, state="gone", at=_T),
    }


def corpus() -> dict[str, dict[str, VersionedModel]]:
    """Every fixture by (subdirectory, name)."""
    return {
        "envelope": dict(envelopes()),
        "payloads": payloads(),
        "frames/client": client_frames(),
        "frames/server": server_frames(),
    }


def _corpus_version() -> str:
    """The corpus is stamped with the MAX SCHEMA_VERSION across every model
    it contains, so a bump on any of them writes a fresh directory."""
    versions = {
        tuple(int(part) for part in type(model).SCHEMA_VERSION.split("."))
        for group in corpus().values()
        for model in group.values()
    }
    return ".".join(str(part) for part in max(versions))


def _version_dir(root: Path, version: str) -> Path:
    return root / f"v{version.replace('.', '_')}"


def regenerate(root: Path | None = None) -> Path:
    """Write every fixture under ``<root>/v<X_Y_Z>/<group>/<name>.json``;
    returns the version directory."""
    root = root or Path(__file__).parent
    version_dir = _version_dir(root, _corpus_version())
    for group, models in corpus().items():
        target = version_dir / group
        target.mkdir(parents=True, exist_ok=True)
        for name, model in models.items():
            payload = model.model_dump(mode="json")
            # LF on every platform: the lineage test compares these bytes with
            # the committed corpus, and text mode would write CRLF on Windows.
            (target / f"{name}.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", newline="\n"
            )
    return version_dir


if __name__ == "__main__":
    print(f"wrote fixtures under {regenerate()}")
