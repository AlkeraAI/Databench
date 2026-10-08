"""The mirror's guards, without a gateway: what a relay must prove before the
daemon acts on it, and what an answer to a parked ask may say.

* a ``run_query`` / ``promote`` relay names UUIDs, an object that exists, is
  bound to THIS chat (and to the promoted event) and — when the object says —
  belongs to the chat's owner; anything else is logged and ignored, and a
  malformed id never even reaches REST;
* a relayed answer settles only the ask whose ``request_id`` is outstanding,
  only with an option that ask offered, and never approves a write-class ask;
* a chunk is split by the bytes the server measures.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import (
    WRITE_CLASS_KINDS,
    ChatMirror,
    RelayRefusedError,
    _authorizes_a_write,
    _Interrupt,
    _split_chunk,
    chunk_size,
)
from alkera_cli.cloud.transport import FALLBACK_EPHEMERAL_MAX_BYTES, FALLBACK_SIZES
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.config import get_settings
from alkera_core.models import User, WorkspaceObject
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import PermissionOption, PermissionRequest
from alkera_core.schemas.realtime import DocEnvelope
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on, share_chat  # noqa: F401
from tests.conftest import OrgWithAdmin, login, make_member

_T = datetime(2026, 9, 6, tzinfo=UTC)
CHAT_ID = "chat-guard"
#: The piece budget a server that names no notify cap leaves.
CHUNK_MAX_BYTES = FALLBACK_SIZES.chunk_max_bytes
OWNER = "00000000-0000-4000-8000-000000000001"
OTHER_USER = "00000000-0000-4000-8000-000000000002"


class _Objects:
    """A REST stand-in: serves the objects a test seeds, records every call."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        parts = request.url.path.split("/")
        object_id = parts[4] if len(parts) > 4 else ""
        if request.method == "GET" and object_id in self.objects:
            return httpx.Response(200, json=self.objects[object_id])
        if request.method == "POST" and object_id in self.objects:
            return httpx.Response(200, json={"id": object_id, "status": "ready"})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": ""}})

    def seed(
        self,
        *,
        kind: str = "result",
        chat_id: str | None = CHAT_ID,
        event_id: str | None = "res-1",
        owner: str | None = OWNER,
        status: str = "pending_upload",
        spec: dict[str, Any] | None = None,
    ) -> str:
        object_id = str(uuid4())
        full_spec: dict[str, Any] = dict(spec or {})
        if chat_id is not None:
            full_spec["source_chat_id"] = chat_id
        if event_id is not None:
            full_spec["source_event_id"] = event_id
        self.objects[object_id] = {
            "id": object_id,
            "type": kind,
            "title": "T",
            "status": status,
            "spec": full_spec,
            "owner_user_id": owner,
        }
        return object_id


def _mirror(
    tmp_path: Path,
    objects: _Objects,
    *,
    owner_user_id: str | None = OWNER,
    chat_id: str = CHAT_ID,
) -> ChatMirror:
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"), adapter_factory=FakeAdapterFactory(FakeAdapter)
    )
    rest = CloudRestClient(
        api_url="http://objects.test",
        token="device-jwt",
        agent_id=chat_id,
        transport=httpx.MockTransport(objects),
    )
    mirror = ChatMirror(
        chat_id=chat_id,
        runtime=runtime,
        socket=cast(CloudSocket, object()),
        rest=rest,
        user_id=OWNER,
        owner_user_id=owner_user_id,
    )
    # The relay path runs once the mirror is ready; these tests drive the
    # guards without a session, so a relay that passes them stops at the
    # session check — the guards are what is under test.
    mirror._ready.set()
    return mirror


QUERY_SPEC = {
    "sql_template": "select * from orders where customer = {customer}",
    "connection": "pg-main",
    "engine": "postgres",
}


# --------------------------------------------------------------------------- #
# run_query / promote relays
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "relay",
    [
        pytest.param(
            {"kind": "run_query", "object_id": "q-1", "run_id": str(uuid4())},
            id="object-id-not-a-uuid",
        ),
        pytest.param(
            {"kind": "run_query", "object_id": str(uuid4()), "run_id": "r1"}, id="run-id-not-a-uuid"
        ),
        pytest.param({"kind": "run_query", "object_id": str(uuid4())}, id="run-id-missing"),
        pytest.param({"kind": "run_query", "run_id": str(uuid4())}, id="object-id-missing"),
        pytest.param(
            {
                "kind": "run_query",
                "object_id": str(uuid4()),
                "run_id": str(uuid4()),
                "params": ["a"],
            },
            id="params-a-list",
        ),
        pytest.param(
            {
                "kind": "run_query",
                "object_id": str(uuid4()),
                "run_id": str(uuid4()),
                "params": "x=1",
            },
            id="params-a-string",
        ),
        pytest.param(
            {"kind": "promote", "object_id": "obj-9", "event_id": "res-1"}, id="promote-object-id"
        ),
        pytest.param(
            {"kind": "promote", "object_id": str(uuid4()), "event_id": ""}, id="promote-empty-event"
        ),
        pytest.param(
            {"kind": "promote", "object_id": str(uuid4()), "event_id": "../etc"},
            id="promote-event-chars",
        ),
        pytest.param(
            {"kind": "promote", "object_id": str(uuid4()), "event_id": "e" * 129},
            id="promote-event-long",
        ),
        pytest.param(
            {"kind": "promote", "object_id": str(uuid4()), "event_id": 7}, id="promote-event-int"
        ),
    ],
)
async def test_a_malformed_relay_is_ignored_before_any_rest_call(
    tmp_path: Path, relay: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    objects = _Objects()
    mirror = _mirror(tmp_path, objects)
    with caplog.at_level(logging.WARNING, logger="alkera_cli.cloud.mirror"):
        await mirror._handle_relay(relay)
    assert objects.calls == [], "a relay that fails validation never reaches REST"
    assert mirror.ignored_relays == 1
    assert any("relay ignored" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("kind", "seed", "reason"),
    [
        pytest.param(
            "run_query",
            {"kind": "query", "chat_id": "chat-other", "spec": QUERY_SPEC},
            "another chat",
            id="query-bound-elsewhere",
        ),
        pytest.param(
            "run_query",
            {"kind": "query", "chat_id": None, "spec": QUERY_SPEC},
            "names no chat",
            id="query-unbound",
        ),
        pytest.param(
            "run_query",
            {"kind": "query", "owner": OTHER_USER, "spec": QUERY_SPEC},
            "not owned by the chat's owner",
            id="query-owned-by-another",
        ),
        pytest.param(
            "run_query",
            {"kind": "result", "spec": QUERY_SPEC},
            "is not a query",
            id="query-wrong-type",
        ),
        pytest.param(
            "promote",
            {"kind": "result", "chat_id": "chat-other"},
            "another chat",
            id="result-bound-elsewhere",
        ),
        pytest.param(
            "promote", {"kind": "result", "chat_id": None}, "names no chat", id="result-unbound"
        ),
        pytest.param(
            "promote",
            {"kind": "result", "owner": OTHER_USER},
            "not owned by the chat's owner",
            id="result-owned-by-another",
        ),
        pytest.param(
            "promote",
            {"kind": "result", "status": "ready"},
            "not awaiting a payload",
            id="result-already-ready",
        ),
        pytest.param(
            "promote",
            {"kind": "result", "event_id": "res-other"},
            "another event",
            id="result-other-event",
        ),
        pytest.param(
            "promote",
            {"kind": "result", "event_id": None},
            "names no transcript event",
            id="result-no-event",
        ),
        pytest.param("promote", {"kind": "query"}, "is not a result", id="result-wrong-type"),
    ],
)
async def test_an_object_that_is_not_this_chats_is_ignored_after_the_read(
    tmp_path: Path, kind: str, seed: dict[str, Any], reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    objects = _Objects()
    object_id = objects.seed(**seed)
    mirror = _mirror(tmp_path, objects)
    relay: dict[str, Any] = {"kind": kind, "object_id": object_id}
    if kind == "run_query":
        relay["run_id"] = str(uuid4())
        relay["params"] = {"customer": "acme"}
    else:
        relay["event_id"] = "res-1"
    with caplog.at_level(logging.WARNING, logger="alkera_cli.cloud.mirror"):
        await mirror._handle_relay(relay)
    assert objects.calls == [("GET", f"/api/v1/objects/{object_id}")], "read, then refused"
    assert mirror.ignored_relays == 1
    assert any(reason in r.getMessage() for r in caplog.records), caplog.text


async def test_an_object_the_cloud_does_not_know_is_ignored(tmp_path: Path) -> None:
    objects = _Objects()
    mirror = _mirror(tmp_path, objects)
    missing = str(uuid4())
    await mirror._handle_relay({"kind": "promote", "object_id": missing, "event_id": "res-1"})
    assert objects.calls == [("GET", f"/api/v1/objects/{missing}")]
    assert mirror.ignored_relays == 1


async def test_an_object_with_an_owner_needs_a_known_chat_owner(tmp_path: Path) -> None:
    objects = _Objects()
    object_id = objects.seed(kind="result")
    mirror = _mirror(tmp_path, objects, owner_user_id=None)
    mirror._user_id = ""
    mirror._owner_user_id = ""
    await mirror._handle_relay({"kind": "promote", "object_id": object_id, "event_id": "res-1"})
    assert mirror.ignored_relays == 1


async def test_an_object_that_names_no_owner_is_judged_by_its_binding_alone(
    tmp_path: Path,
) -> None:
    """Until the object read model exposes ``owner_user_id`` the chat binding
    is the whole check — a relay for a bound object is not ignored."""
    objects = _Objects()
    object_id = objects.seed(kind="result", owner=None)
    mirror = _mirror(tmp_path, objects)
    await mirror._handle_relay({"kind": "promote", "object_id": object_id, "event_id": "res-1"})
    assert mirror.ignored_relays == 0


# --------------------------------------------------------------------------- #
# answers to a parked ask
# --------------------------------------------------------------------------- #


def _ask(
    request_id: str = "perm-1",
    *,
    kind: str = "other",
    options: tuple[str, ...] = ("allow_once", "reject_once"),
    subject: dict[str, Any] | None = None,
) -> PermissionRequest:
    return PermissionRequest(
        event_id=request_id,
        time=_T,
        session_id=CHAT_ID,
        request_id=request_id,
        permission_kind=kind,
        canonical_kind=kind,  # type: ignore[arg-type]
        options=[PermissionOption(option_id=o, name=o) for o in options],  # type: ignore[arg-type]
        subject=subject,
    )


async def _park(mirror: ChatMirror, request: PermissionRequest) -> asyncio.Task[Any]:
    task = asyncio.get_running_loop().create_task(mirror._resolve_permission(request))
    await asyncio.sleep(0)
    assert request.request_id in mirror.pending_interrupts
    return task


async def _settle(task: asyncio.Task[Any]) -> Any:
    return await asyncio.wait_for(task, 1.0)


async def test_an_answer_for_an_ask_that_is_not_outstanding_is_ignored(tmp_path: Path) -> None:
    mirror = _mirror(tmp_path, _Objects())
    task = await _park(mirror, _ask("perm-1"))
    mirror._answer_interrupt("perm-stale", {"option_id": "allow_once"})
    assert not task.done() and mirror.pending_interrupts == ["perm-1"]
    assert mirror.ignored_relays == 1
    mirror._answer_interrupt("perm-1", {"option_id": "reject_once"})
    assert await _settle(task) == "reject_once"


@pytest.mark.parametrize(
    ("offered", "answer"),
    [
        pytest.param(("allow_once", "reject_once"), "allow_always", id="always-not-offered"),
        pytest.param(("reject_once",), "allow_once", id="allow-not-offered"),
        pytest.param((), "allow_once", id="nothing-offered"),
        pytest.param(
            ("allow_once", "reject_once"), "reject_always", id="reject-always-not-offered"
        ),
    ],
)
async def test_an_option_the_ask_did_not_offer_is_refused(
    tmp_path: Path, offered: tuple[str, ...], answer: str
) -> None:
    mirror = _mirror(tmp_path, _Objects())
    task = await _park(mirror, _ask(options=offered))
    mirror._answer_interrupt("perm-1", {"option_id": answer})
    assert not task.done(), "a forged option leaves the ask parked"
    assert mirror.ignored_relays == 1
    mirror._answer_interrupt(
        "perm-1", {"option_id": "reject_once"} if "reject_once" in offered else {"text": "x"}
    )
    if "reject_once" in offered:
        assert await _settle(task) == "reject_once"
    else:
        task.cancel()


@pytest.mark.parametrize(
    "ask",
    [
        pytest.param(
            _ask(kind="edit", options=("allow_once", "allow_always", "reject_once")), id="edit"
        ),
        pytest.param(_ask(kind="shell", options=("allow_once", "reject_once")), id="shell"),
        pytest.param(_ask(kind="task", options=("allow_once", "reject_once")), id="task"),
        pytest.param(_ask(kind="external", options=("allow_once", "reject_once")), id="external"),
        pytest.param(
            _ask(
                kind="other",
                options=("allow_once", "reject_once"),
                subject={"capability": "sql", "effect": "write"},
            ),
            id="sql-write-by-subject",
        ),
        pytest.param(
            _ask(
                kind="other",
                options=("allow_once", "reject_once"),
                subject={"capability": "sql", "effect": "destroy"},
            ),
            id="sql-destroy-by-subject",
        ),
        pytest.param(
            _ask(
                kind="network",
                options=("allow_once", "reject_once"),
                subject={"capability": "http", "effect": "egress"},
            ),
            id="egress-by-subject",
        ),
    ],
)
async def test_no_answer_may_authorize_a_write_class_ask(
    tmp_path: Path, ask: PermissionRequest, caplog: pytest.LogCaptureFixture
) -> None:
    mirror = _mirror(tmp_path, _Objects())
    task = await _park(mirror, ask)
    with caplog.at_level(logging.WARNING, logger="alkera_cli.cloud.mirror"):
        mirror._answer_interrupt("perm-1", {"option_id": "allow_once"})
        if "allow_always" in {o.option_id for o in ask.options}:
            mirror._answer_interrupt("perm-1", {"option_id": "allow_always"})
    assert not task.done(), "the ask is still parked: nothing was approved"
    assert any("never approves" in r.getMessage() for r in caplog.records)
    # A refusal is always accepted.
    mirror._answer_interrupt("perm-1", {"option_id": "reject_once"})
    assert await _settle(task) == "reject_once"


async def test_a_read_class_ask_may_be_allowed_with_an_offered_option(tmp_path: Path) -> None:
    mirror = _mirror(tmp_path, _Objects())
    task = await _park(mirror, _ask(kind="other", subject={"capability": "sql", "effect": "read"}))
    mirror._answer_interrupt("perm-1", {"option_id": "allow_once"})
    assert await _settle(task) == "allow_once"
    assert mirror.ignored_relays == 0


async def test_a_relayed_text_that_is_not_an_option_leaves_the_ask_parked(tmp_path: Path) -> None:
    mirror = _mirror(tmp_path, _Objects())
    task = await _park(mirror, _ask())
    mirror._answer_interrupt("perm-1", {"text": "maybe"})
    mirror._answer_interrupt("perm-1", {"option_id": 42})
    assert not task.done()
    task.cancel()


# --------------------------------------------------------------------------- #
# chunks are measured in the server's bytes
# --------------------------------------------------------------------------- #


def _chunk(text: str) -> dict[str, Any]:
    return {
        "event_type": "agent.message_chunk",
        "event_id": "k-1",
        "time": "2026-09-06T00:00:00Z",
        "session_id": "0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f",
        "message_id": "m-1",
        "part_id": "p-1",
        "sequence": 9,
        "text": text,
        "is_final": True,
        "schema_version": "1.0.0",
    }


@pytest.mark.parametrize(
    ("text", "label"),
    [
        pytest.param("x" * 1499, "ascii-under-the-old-char-limit"),
        pytest.param("字" * 600, "cjk-600-chars-3600-escaped-bytes"),
        pytest.param("\U0001f600" * 400, "emoji-400-chars-4800-escaped-bytes"),
        pytest.param("café " * 400, "latin1-mixed"),
        pytest.param(("字" * 300 + "a" * 300) * 3, "mixed-runs"),
    ],
    ids=lambda v: v if isinstance(v, str) and len(v) < 60 else None,
)
def test_pieces_are_bounded_by_bytes_not_characters(text: str, label: str) -> None:
    pieces = _split_chunk(_chunk(text), CHUNK_MAX_BYTES)
    assert "".join(p["text"] for p in pieces) == text
    for piece in pieces:
        assert chunk_size([piece]) <= CHUNK_MAX_BYTES, label
    assert [p.get("is_final", True) for p in pieces] == [False] * (len(pieces) - 1) + [True]
    assert all(p["sequence"] == 9 for p in pieces)


def test_a_chunk_that_fits_is_not_split() -> None:
    event = _chunk("字" * 100)
    assert _split_chunk(event, CHUNK_MAX_BYTES) == [event]


def test_the_headroom_covers_what_the_server_wraps_around_a_piece() -> None:
    """The server measures the WHOLE notification — hub envelope, doc
    envelope, op — with ASCII escapes; the daemon's cap on its events leaves
    room for all of that, with a UUID chat id and a team id."""
    from uuid import uuid4 as _uuid

    from alkera_core.events.listener import ephemeral_payload
    from backend.services.realtime import presence
    from backend.services.realtime.channels import ChannelGrant, parse_channel

    # The first piece of a long run is filled to the cap by construction.
    piece = _split_chunk(_chunk("字" * 2000), CHUNK_MAX_BYTES)[0]
    assert CHUNK_MAX_BYTES - 8 <= chunk_size([piece]) <= CHUNK_MAX_BYTES
    doc_id = str(_uuid())
    envelope = DocEnvelope(
        doc_id=doc_id,
        doc_type="chat",
        epoch=12345,
        peer_id="p:" + "f" * 12,
        seq=0,
        kind="op",
        payload={"op_id": "chunk-" + "a" * 8, "intent": "chunk", "events": [piece], "meta": {}},
    )
    grant = ChannelGrant(
        channel=parse_channel(f"doc:chat:{doc_id}"),
        org_id=_uuid(),
        can_write=True,
        owner_user_id=_uuid(),
        team_id=_uuid(),
        visibility="team",
    )
    event = presence.ephemeral_event(
        grant=grant,
        type=presence.CHUNK_EVENT_TYPE,
        body={"envelope": envelope.model_dump(mode="json")},
    )
    encoded = ephemeral_payload(event)  # raises above the server's cap
    assert len(encoded.encode("utf-8")) <= FALLBACK_EPHEMERAL_MAX_BYTES


# ---------------------------------------------------------------------------
# Agreement with the real read model: what REST's promote writes, the guard
# accepts — for exactly that chat and that owner
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("files_on")
async def test_the_guard_accepts_what_the_real_promote_route_writes_and_nothing_else(
    tmp_path: Path, client: httpx.AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """An org admin promotes out of a MEMBER's chat. REST allows it and files
    the result under the chat's owner; the daemon knows the chat's owner and
    nothing about who pressed promote. Reading the REAL object back, the guard
    accepts the relay for that chat and that owner, and refuses it for a
    mirror that believes the admin owns the chat, or that serves another chat.

    The member shares the chat first: promoting lifts content out of a
    conversation, so it sits below the chat's read gate and an admin nobody
    shared it with is told the chat does not exist. What is under test here is
    who OWNS what comes out, not who may reach in.
    """
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    async with httpx.AsyncClient(
        transport=client._transport,
        base_url=str(client.base_url),
        timeout=60.0,
        headers={"Origin": get_settings().frontend_base_url},
    ) as theirs:
        await login(theirs, member.email, password)
        created = await theirs.post("/api/v1/chats", json={"title": "Ops"})
        assert created.status_code == 201, created.text
        chat = created.json()

    chat_row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    admin_user = await real_session.get(User, org_admin.admin_id)
    assert chat_row is not None and admin_user is not None
    await share_chat(real_session, chat=chat_row, owner=member, user=admin_user, role="reader")

    await login(client, org_admin.admin_email, org_admin.admin_password)
    promoted = await client.post(
        f"/api/v1/chats/{chat['id']}/promote", json={"event_id": "res-1", "title": "Rows"}
    )
    assert promoted.status_code == 201, promoted.text
    read = await client.get(f"/api/v1/objects/{promoted.json()['id']}")
    assert read.status_code == 200
    obj = read.json()
    assert obj["owner_user_id"] == chat["owner_user_id"] == str(member.id)

    objects = _Objects()
    objects.objects[obj["id"]] = obj
    relay = {"kind": "promote", "object_id": obj["id"], "event_id": "res-1"}

    honest = _mirror(tmp_path, objects, owner_user_id=chat["owner_user_id"], chat_id=chat["id"])
    await honest._on_promote(relay)  # accepted: stops at the session check, refuses nothing

    as_the_admin = _mirror(
        tmp_path, objects, owner_user_id=str(org_admin.admin_id), chat_id=chat["id"]
    )
    with pytest.raises(RelayRefusedError, match="not owned by the chat's owner"):
        await as_the_admin._on_promote(relay)

    another_chat = _mirror(tmp_path, objects, owner_user_id=chat["owner_user_id"])
    with pytest.raises(RelayRefusedError, match="belongs to another chat"):
        await another_chat._on_promote(relay)

    assert [call for call in objects.calls if call[0] == "POST"] == [], "nothing was uploaded"


@pytest.mark.parametrize(
    ("mode", "approved"),
    [
        pytest.param("read_only", False, id="an-analysts-chat-approves-no-write"),
        pytest.param("plan", False, id="a-planning-chat-approves-no-write"),
        pytest.param("default", True, id="a-prompting-chat-approves-a-write-it-asked-about"),
        pytest.param("auto", True, id="an-auto-chat-approves-the-risky-step-it-paused-on"),
        pytest.param("bypass", True, id="a-bypass-chat-approves-a-write-it-still-asked-about"),
    ],
)
async def test_whether_an_answer_may_approve_a_write_is_the_modes_decision(
    tmp_path: Path, mode: str, approved: bool
) -> None:
    """The write-class refusal is not absolute — it is what ``read_only`` means.

    A cloud chat used to refuse every relayed ``allow`` outright, which was the
    only honest thing while every cloud chat was an analyst's. Now the browser
    can move a chat into ``default``, and refusing there would make the reader's
    permission prompt a button that does nothing. The same holds at the other
    end: an ask that still SURFACES in ``bypass`` is one the box chose to
    raise, so its answer has to land.

    ``auto`` is here because the browser reads this decision too, and read it
    wrong: it withheld the Allow in ``auto`` for a box that settles it. Auto
    pauses on the destroy/egress floor and acts on the answer, so this is the
    committed proof of the premise the card's table is derived from.
    """
    mirror = _mirror(tmp_path, _Objects())
    mirror._mode = mode  # type: ignore[assignment]
    inside = str(mirror.chat_folder / "scratch" / "plan.md")
    ask = _ask(
        kind="edit",
        subject={"capability": "fs", "effect": "write", "raw": inside, "targets": []},
    )
    task = await _park(mirror, ask)

    mirror._answer_interrupt("perm-1", {"option_id": "allow_once"})

    if approved:
        assert await _settle(task) == "allow_once"
    else:
        assert not task.done(), "the ask is still parked: nothing was approved"
        mirror._answer_interrupt("perm-1", {"option_id": "reject_once"})
        assert await _settle(task) == "reject_once"


WRITE_CLASS_ASKS = json.loads(
    (Path(__file__).parent / "fixtures" / "write_class_asks.json").read_text(encoding="utf-8")
)


def _fixture_ask(case: dict[str, Any]) -> PermissionRequest:
    subject = (
        None
        if "effect" not in case
        else {
            "capability": "shell",
            "operation": "run",
            "effect": case["effect"],
            "targets": [],
        }
    )
    return _ask(kind=case["canonical_kind"], subject=subject)


def test_the_write_class_kinds_are_the_ones_the_browser_reads() -> None:
    """One set, two languages. The browser cannot import this one, so the
    fixture both sides are driven over carries it and each holds its own copy to
    it — a kind added on one side alone changes who is offered an approval."""
    assert set(WRITE_CLASS_ASKS["write_class_kinds"]) == set(WRITE_CLASS_KINDS)


@pytest.mark.parametrize(
    "case",
    WRITE_CLASS_ASKS["cases"],
    ids=[case["id"] for case in WRITE_CLASS_ASKS["cases"]],
)
def test_whether_an_ask_authorizes_a_write_is_the_table_the_browser_reads(
    case: dict[str, Any],
) -> None:
    """The box's half of the reading the browser makes to decide whether to
    OFFER the approval this gate would then act on.

    They were not the same reading: this one takes the classifier's verdict
    first and the kind only where there is none, while the browser answered by
    kind first — so a command the classifier had called a read was answerable
    here and offered no Allow there. The cases live in the fixture, and each
    side is driven over them, because a rule spelled twice is a rule that
    drifts.
    """
    assert _authorizes_a_write(_fixture_ask(case)) is case["authorizes_a_write"]


async def test_no_mode_lets_an_answer_approve_a_write_outside_the_chat_folder(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The boundary the mode does not move.

    ``_resolve_permission`` already turns such an ask away before it is parked,
    so an ask reaching this point had to be planted — which is exactly why the
    second line is worth having on the one path a browser can drive.
    """
    mirror = _mirror(tmp_path, _Objects())
    mirror._mode = "default"
    ask = _ask(
        kind="edit",
        subject={
            "capability": "fs",
            "effect": "write",
            "raw": str(tmp_path / "elsewhere.txt"),
            "targets": [],
        },
    )
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    mirror._interrupts["perm-1"] = _Interrupt("permission", future, ask)

    with caplog.at_level(logging.WARNING, logger="alkera_cli.cloud.mirror"):
        mirror._answer_interrupt("perm-1", {"option_id": "allow_once"})

    assert not future.done(), "a write outside the chat's folder is never approved"
    assert any("never approves" in r.getMessage() for r in caplog.records)
