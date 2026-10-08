"""The CRDT lane's tables as the migrated database has them.

``alembic check`` compares columns and named indexes but not CHECK constraints
or sequences, so this module asks the live database: what a row round-trips
as, what the table refuses on its own, and that a Loro peer id is drawn from a
sequence that never hands one out twice.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest
from alkera_core.models import CrdtDoc, CrdtPeer, CrdtUpdate
from alkera_core.models.crdt_doc import CRDT_FIRST_CLIENT_PEER, CRDT_LAST_PEER
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin

pytestmark = pytest.mark.asyncio


def _doc(org_id: uuid.UUID, doc_id: str, **overrides: Any) -> CrdtDoc:
    values: dict[str, Any] = {
        "org_id": org_id,
        "doc_type": "chat_workspace",
        "doc_id": doc_id,
        "loro_format": "1.16.2",
        "seeded_from": "empty",
    }
    values.update(overrides)
    return CrdtDoc(**values)


def _update(doc: CrdtDoc, log_seq: int, data: bytes = b"delta", **overrides: Any) -> CrdtUpdate:
    values: dict[str, Any] = {
        "org_id": doc.org_id,
        "doc_type": doc.doc_type,
        "doc_id": doc.doc_id,
        "epoch": doc.epoch,
        "log_seq": log_seq,
        "data": data,
        "size": len(data),
        "sha256": hashlib.sha256(data).digest(),
        "update_id": f"u-{log_seq}",
        "loro_peer": CRDT_FIRST_CLIENT_PEER,
    }
    values.update(overrides)
    return CrdtUpdate(**values)


async def test_a_document_its_log_and_a_peer_round_trip(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    doc_id = f"chat-{uuid.uuid4()}"
    doc = _doc(
        org_admin.org_id,
        doc_id,
        vv=b"\x01\x02",
        snapshot=b"snap",
        snapshot_bytes=4,
        projection={"text": "hello", "sha256": "x"},
    )
    real_session.add(doc)
    await real_session.flush()
    real_session.add(_update(doc, 1, author_user_id=org_admin.admin_id))
    peer = CrdtPeer(
        org_id=org_admin.org_id,
        doc_type="chat_workspace",
        doc_id=doc_id,
        user_id=org_admin.admin_id,
    )
    real_session.add(peer)
    await real_session.commit()
    loro_peer = peer.loro_peer

    real_session.expire_all()
    loaded = (
        await real_session.execute(
            select(CrdtDoc).where(CrdtDoc.org_id == org_admin.org_id, CrdtDoc.doc_id == doc_id)
        )
    ).scalar_one()
    assert (loaded.epoch, loaded.log_seq, loaded.doc_schema) == (1, 0, 1)
    assert (bytes(loaded.vv), bytes(loaded.snapshot)) == (b"\x01\x02", b"snap")
    assert loaded.projection == {"text": "hello", "sha256": "x"}
    assert loaded.quarantined_at is None
    row = (
        await real_session.execute(select(CrdtUpdate).where(CrdtUpdate.doc_id == doc_id))
    ).scalar_one()
    assert bytes(row.data) == b"delta"
    assert bytes(row.sha256) == hashlib.sha256(b"delta").digest()
    assert row.author_user_id == org_admin.admin_id
    assert CRDT_FIRST_CLIENT_PEER <= loro_peer <= CRDT_LAST_PEER


async def test_a_bare_document_row_takes_the_tables_defaults(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    doc_id = f"chat-{uuid.uuid4()}"
    await real_session.execute(
        text(
            "INSERT INTO crdt_docs (org_id, doc_type, doc_id, loro_format, seeded_from) "
            "VALUES (:org, 'chat_workspace', :doc, '1.16.2', 'empty')"
        ),
        {"org": org_admin.org_id, "doc": doc_id},
    )
    row = (
        await real_session.execute(
            text(
                "SELECT epoch, log_seq, vv, snapshot, snapshot_log_seq, snapshot_bytes, "
                "log_bytes, doc_schema, projection FROM crdt_docs WHERE doc_id = :doc"
            ),
            {"doc": doc_id},
        )
    ).one()
    assert tuple(row) == (1, 0, b"", b"", 0, 0, 0, 1, {})
    await real_session.rollback()


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        pytest.param({"doc_type": "chat"}, "ck_crdt_docs_doc_type", id="op-log-type"),
        pytest.param({"epoch": 0}, "ck_crdt_docs_epoch_positive", id="epoch-zero"),
        pytest.param({"log_seq": -1}, "ck_crdt_docs_log_seq_nonnegative", id="negative-log"),
        pytest.param(
            {"log_seq": 2, "snapshot_log_seq": 3},
            "ck_crdt_docs_snapshot_within_log",
            id="snapshot-past-the-log",
        ),
        pytest.param({"log_bytes": -1}, "ck_crdt_docs_bytes_nonnegative", id="negative-bytes"),
        pytest.param({"doc_schema": 0}, "ck_crdt_docs_doc_schema_positive", id="schema-zero"),
    ],
)
async def test_the_document_table_refuses_an_impossible_row(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    real_session.add(_doc(org_admin.org_id, f"chat-{uuid.uuid4()}", **overrides))
    with pytest.raises(IntegrityError, match=constraint):
        await real_session.flush()
    await real_session.rollback()


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        pytest.param({"log_seq": 0}, "ck_crdt_updates_log_seq_positive", id="log-seq-zero"),
        pytest.param({"size": -1}, "ck_crdt_updates_size_nonnegative", id="negative-size"),
        pytest.param({"sha256": b"x" * 31}, "ck_crdt_updates_sha256_length", id="short-digest"),
        pytest.param({"doc_id": "nobody"}, "fk_crdt_updates_doc", id="no-such-document"),
    ],
)
async def test_the_update_log_refuses_an_impossible_row(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    doc = _doc(org_admin.org_id, f"chat-{uuid.uuid4()}")
    real_session.add(doc)
    await real_session.flush()
    rest = {key: value for key, value in overrides.items() if key != "log_seq"}
    real_session.add(_update(doc, overrides.get("log_seq", 1), **rest))
    with pytest.raises(IntegrityError, match=constraint):
        await real_session.flush()
    await real_session.rollback()


async def test_a_documents_log_goes_with_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    doc = _doc(org_admin.org_id, f"chat-{uuid.uuid4()}")
    real_session.add(doc)
    await real_session.flush()
    real_session.add_all([_update(doc, 1), _update(doc, 2)])
    await real_session.flush()
    await real_session.execute(
        delete(CrdtDoc).where(CrdtDoc.org_id == doc.org_id, CrdtDoc.doc_id == doc.doc_id)
    )
    left = await real_session.scalar(
        select(func.count()).select_from(CrdtUpdate).where(CrdtUpdate.doc_id == doc.doc_id)
    )
    assert left == 0
    await real_session.rollback()


async def test_a_peer_id_is_never_handed_out_twice(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A Loro peer id two writers shared would mint colliding operation ids, so
    ids come from a sequence: increasing, and not reused after the row that
    held one is gone."""
    doc_id = f"chat-{uuid.uuid4()}"

    async def mint() -> int:
        peer = CrdtPeer(
            org_id=org_admin.org_id,
            doc_type="chat_workspace",
            doc_id=doc_id,
            user_id=org_admin.admin_id,
        )
        real_session.add(peer)
        await real_session.flush()
        return peer.loro_peer

    first = await mint()
    await real_session.execute(delete(CrdtPeer).where(CrdtPeer.loro_peer == first))
    await real_session.commit()
    second, third = await mint(), await mint()
    assert first < second < third
    assert CRDT_FIRST_CLIENT_PEER <= first
    await real_session.commit()


async def test_the_peer_sequence_stays_inside_a_javascript_number(
    real_session: AsyncSession,
) -> None:
    row = (
        await real_session.execute(
            text(
                "SELECT s.seqmin, s.seqmax, s.seqstart, s.seqcycle FROM pg_sequence s "
                "JOIN pg_class c ON c.oid = s.seqrelid WHERE c.relname = 'crdt_peer_seq'"
            )
        )
    ).one()
    assert tuple(row) == (CRDT_FIRST_CLIENT_PEER, 2**53 - 1, CRDT_FIRST_CLIENT_PEER, False)


@pytest.mark.parametrize(
    "loro_peer",
    [
        pytest.param(CRDT_FIRST_CLIENT_PEER - 1, id="server-range"),
        pytest.param(2**53, id="past-a-javascript-number"),
    ],
)
async def test_a_peer_outside_the_client_range_is_refused(
    real_session: AsyncSession, org_admin: OrgWithAdmin, loro_peer: int
) -> None:
    real_session.add(
        CrdtPeer(
            loro_peer=loro_peer,
            org_id=org_admin.org_id,
            doc_type="chat_workspace",
            doc_id="d",
            user_id=org_admin.admin_id,
        )
    )
    with pytest.raises(IntegrityError, match="ck_crdt_peers_loro_peer_range"):
        await real_session.flush()
    await real_session.rollback()
