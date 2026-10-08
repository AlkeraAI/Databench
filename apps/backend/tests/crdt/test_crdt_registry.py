"""What the lane derives from its registry rather than spelling out: which
socket traffic is CRDT traffic, and which announcement makes a held file's
session look for an outside change. A type the registry does not hold is not
served, and a new type needs no edit outside its own registration."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from alkera_core.events import EventType, HubEvent
from backend.services.crdt.docs import CrdtDocs
from backend.services.crdt.gateway import CrdtSocket
from backend.services.crdt.registry import ChatWorkspaceType, CrdtRegistry, FileDocType
from backend.services.crdt.sandbox.pool import PoolConfig, SandboxPool

pytestmark = [pytest.mark.spread]


def _socket(registry: CrdtRegistry) -> CrdtSocket:
    # Neither path asked here touches the pool or the host.
    docs = CrdtDocs(pool=SandboxPool(PoolConfig(workers=1, command=["true"])), registry=registry)
    return CrdtSocket(host=None, docs=docs)  # type: ignore[arg-type]


def _doc_op(doc_type: str) -> HubEvent:
    return HubEvent(
        lane="durable",
        org_id=uuid.uuid4(),
        type=EventType.DOC_OP.value,
        entity="doc",
        entity_id=f"doc:{doc_type}:x",
        version=0,
        visibility="org",
        payload={"envelope": {"doc_type": doc_type, "doc_id": "x"}},
        channel=f"doc:{doc_type}:x",
    )


@pytest.mark.parametrize(
    ("types", "doc_type", "wanted"),
    [
        pytest.param(("chat_draft", "file"), "file", True, id="a-registered-file"),
        pytest.param(("chat_draft", "file"), "chat_draft", True, id="the-draft"),
        pytest.param(("chat_draft",), "file", False, id="a-type-not-registered"),
        pytest.param(("chat_draft", "file"), "chat", False, id="the-op-log-chat"),
    ],
)
def test_crdt_traffic_is_whatever_the_registry_serves(
    types: tuple[str, ...], doc_type: str, wanted: bool
) -> None:
    every: dict[str, Any] = {"chat_draft": ChatWorkspaceType(), "file": FileDocType()}
    socket = _socket(CrdtRegistry({name: every[name] for name in types}))
    assert socket.wants(_doc_op(doc_type)) is wanted
    assert socket.serves(doc_type) is wanted


@pytest.mark.parametrize(
    ("event_type", "payload", "changed"),
    [
        pytest.param(
            EventType.FILE_NODE_CHANGED.value, {"node_id": "n-1"}, "n-1", id="a-file-node-changed"
        ),
        pytest.param(EventType.FILE_NODE_CHANGED.value, {}, None, id="naming-no-node"),
        pytest.param(EventType.DOC_OP.value, {"node_id": "n-1"}, None, id="another-event"),
    ],
)
def test_a_files_session_looks_when_the_drive_announces_its_node_changed(
    event_type: str, payload: dict[str, Any], changed: str | None
) -> None:
    source = FileDocType().source
    assert source is not None
    assert source.changed(event_type, payload) == changed


def test_the_draft_has_no_source_and_a_file_does() -> None:
    assert ChatWorkspaceType().source is None
    assert FileDocType().source is not None
    assert FileDocType().session_policy == "ephemeral_session"
