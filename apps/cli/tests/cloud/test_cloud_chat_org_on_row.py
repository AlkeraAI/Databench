"""The box learns each chat's org from the server's row, and from nowhere else.

A pool box serves chats from many orgs on one daemon, so everything per tenant
it builds is keyed on the org the chat's row names. These cases drive the
production mirror factory with the record the backend really emits
(``ChatSessionRead``) and pin what the opened mirror holds: the row's org in its
canonical spelling, never the box's own identity, and no org at all for a row
that does not carry a usable one.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror_factory import chat_org_id
from alkera_cli.cloud.service import CloudMirrorService, MirrorSettings
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.host import paths
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.objects.api import ChatSessionRead

_T = datetime(2026, 10, 5, tzinfo=UTC)
CHAT_ID = "7b1c8c2e-4a4e-4f5e-9c0a-0f1a2b3c4d5e"
OWNER = "00000000-0000-4000-8000-000000000001"
ORG_A = "1f0e2d3c-4b5a-4968-8776-a5b4c3d2e1f0"
ORG_B = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"


def _record(**fields: Any) -> dict[str, Any]:
    """The chat as ``GET /api/v1/chats`` (an item) and ``GET /api/v1/chats/{id}``
    serialize it."""
    return ChatSessionRead(
        id=UUID(CHAT_ID),
        title="Ops",
        owner_user_id=UUID(OWNER),
        machine_id="machine:x",
        machine_status="ready",
        created_at=_T,
        updated_at=_T,
        last_seq=0,
        **fields,
    ).model_dump(mode="json")


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CloudMirrorService]:
    workspace = tmp_path / "work"
    workspace.mkdir()
    home = tmp_path / "alkera-home"
    home.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    runtime = HarnessRuntime(
        ProjectDirectory(workspace / ".alkera"),
        adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
    )
    rest = CloudRestClient(
        api_url="http://objects.test",
        token="box-jwt",
        agent_id="machine:x",
        transport=httpx.MockTransport(lambda _r: httpx.Response(404, json={})),
    )
    settings = MirrorSettings(
        api_url="http://objects.test",
        token="box-jwt",
        project_dir=workspace,
        machine_name="pool box",
        user_id=OWNER,
        provider_pod_id="pod-pool",
        machine_type_code="cpu3c",
    )
    yield CloudMirrorService(settings, runtime, rest=rest, socket=CloudSocket(rest))


@pytest.mark.parametrize("org", [ORG_A, ORG_B])
def test_the_mirror_holds_the_org_its_row_names(service: CloudMirrorService, org: str) -> None:
    """Two chats from two orgs on one box each open holding their own org."""
    mirror = service._default_mirror(CHAT_ID, _record(org_id=UUID(org)))
    assert mirror.org_id == org


def test_a_row_from_a_server_that_predates_the_field_opens_with_no_org(
    service: CloudMirrorService,
) -> None:
    """An older server sends no org. The box records that it has none rather
    than borrowing one from anywhere else, which would place the chat in a
    tenant the server never named."""
    record = _record()
    assert record["org_id"] is None
    del record["org_id"]
    mirror = service._default_mirror(CHAT_ID, record)
    assert mirror.org_id is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(ORG_A, ORG_A, id="canonical"),
        pytest.param(ORG_A.upper(), ORG_A, id="upper-case"),
        pytest.param(ORG_A.replace("-", ""), ORG_A, id="no-dashes"),
        pytest.param("{" + ORG_A + "}", ORG_A, id="braces"),
        pytest.param("", None, id="empty"),
        pytest.param("not-an-org", None, id="not-a-uuid"),
        pytest.param(ORG_B[:-1], None, id="truncated"),
        pytest.param(42, None, id="number"),
        pytest.param(None, None, id="null"),
        pytest.param({"id": ORG_A}, None, id="object"),
    ],
)
def test_the_org_is_read_in_one_spelling_or_not_at_all(raw: object, expected: str | None) -> None:
    """One org must never key two partitions because two reads spelled it
    differently, and a value that is not an org id is no org."""
    assert chat_org_id({"org_id": raw}) == expected


def test_a_row_with_no_org_key_has_no_org() -> None:
    assert chat_org_id({"owner_user_id": OWNER}) is None
