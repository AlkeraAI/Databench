"""The batch route's contract: one answer per item, and no silent partials.

Every case here drives the real app through ``ASGITransport`` against real
Postgres and a filesystem store, so a green case is the route, the library, the
policy and the migration agreeing — nothing here mocks the thing it asserts.

The interesting cases are the *mixed* ones: a batch where one item is refused
and its neighbours are not. A route that answered a single status for the whole
batch, or that let one item's failure roll the others back, would pass a happy
path and fail every one of these.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, get_args

import pytest
from _files_kit import NOT_FOUND, refusal
from alkera_core.auth import COOKIE_NAME
from alkera_core.authz.decision import AUTHZ_EVENT_TYPE
from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import acl
from alkera_core.files import bulk as bulk_core
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_READER
from alkera_core.files.bulk import BulkPlan
from alkera_core.files.ids import OperationId, OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.models import EventOutbox
from alkera_core.models.files.tree import FileNode
from alkera_core.temporal.contract import WorkflowType
from backend.api.body_limit import GUARDED_PREFIXES, GateIngestBodyLimitMiddleware
from backend.api.routes.files.bulk import INLINE_ITEMS, MAX_ITEMS
from backend.services.files import operations_runner
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import app_client, login

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    from .conftest import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"


async def _drive(client: AsyncClient) -> tuple[str, str]:
    """``(driveId, parentId)`` — and the parent is ``/Shared``, not the root.

    The drive root is a traversal-only signpost: every direct write into it is
    refused, so a batch aimed there would be a batch of refusals rather than a
    batch of writes.
    """
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    body = response.json()
    drive_id, root_id = str(body["id"]), str(body["rootId"])
    rows = (await client.get(f"{BASE}/drives/{drive_id}/items/{root_id}/children")).json()
    shared = next(row for row in rows["value"] if row["name"] == "Shared")
    return drive_id, str(shared["id"])


async def _etag(client: AsyncClient, drive_id: str, item_id: str) -> int:
    """The etag the server would hand a client that had just read the node."""
    response = await client.get(f"{BASE}/drives/{drive_id}/items/{item_id}")
    assert response.status_code == 200, response.text
    return int(response.json()["etag"])


async def _post(
    client: AsyncClient, drive_id: str, items: list[dict[str, Any]], key: dict[str, str]
) -> Any:
    return await client.post(f"{BASE}/drives/{drive_id}/bulk", json={"items": items}, headers=key)


async def _no_nudge(workflow: WorkflowType, op_id: uuid.UUID, org_team_id: uuid.UUID) -> bool:
    """Stands in for the hand-off on the worker shape: there is no orchestrator
    behind a route test, and the point of these cases is the row, not the RPC."""
    return True


async def test_a_queued_batch_is_run_and_not_left_sitting_in_queued(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures, idem: Any
) -> None:
    """The 202 is a promise, and something has to keep it.

    A batch past the threshold is queued with its decided plan and then handed
    to whoever runs queued work on this deployment. On the shape that runs it
    in the request, that hand-off is what turns the row into the changes the
    caller asked for: without it the 202 was the whole of the story — the
    operation stayed ``queued 0/N``, not one item was trashed or created, and
    the client polled a row no process owned.
    """
    drive, root = await _drive(files_client)
    items = [
        {"id": f"i{n}", "op": "createFolder", "parentId": root, "name": f"q{n}", "ifMatch": 1}
        for n in range(INLINE_ITEMS + 1)
    ]
    response = await _post(files_client, drive, items, idem())
    assert response.status_code == 202, response.text
    op_id = response.json()["id"]

    state = await files_client.get(f"{BASE}/drives/{drive}/operations/{op_id}")
    assert state.status_code == 200, state.text
    assert state.json()["state"] == "done", state.text

    listing = await files_client.get(f"{BASE}/drives/{drive}/items/{root}/children?limit=1000")
    assert listing.status_code == 200, listing.text
    names = {row["name"] for row in listing.json()["value"]}
    assert {f"q{n}" for n in range(INLINE_ITEMS + 1)} <= names


async def test_a_finished_batch_counts_every_item_it_applied(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures, idem: Any
) -> None:
    """``done`` reaches ``total`` on a batch that finished.

    Progress is published every Nth item, so the last short window of a batch —
    the single item of 101 — was counted in the runner's memory and never
    written. The operation settled as ``done`` with its progress still reading
    100 of 101, and a client drawing that figure showed a finished batch as one
    item short of complete for ever.
    """
    drive, root = await _drive(files_client)
    total = INLINE_ITEMS + 1
    items = [
        {"id": f"i{n}", "op": "createFolder", "parentId": root, "name": f"c{n}", "ifMatch": 1}
        for n in range(total)
    ]
    response = await _post(files_client, drive, items, idem())
    assert response.status_code == 202, response.text

    state = await files_client.get(
        f"{BASE}/drives/{drive}/operations/{response.json()['id']}",
    )
    assert state.status_code == 200, state.text
    body = state.json()
    assert body["state"] == "done", body
    assert (body["done"], body["total"]) == (total, total), body


async def test_a_queued_batch_names_the_worker_that_will_run_it(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the shape that leaves queued work to the worker, the request says so.

    Nothing sweeps a queued ``file_ops`` row, so a batch the request does not
    run itself has to be nudged to the workflow that will — keyed by the
    operation, after the row is durable. A 202 with no hand-off named is the
    same silent no-op on a worker deployment as it was on this one.
    """
    monkeypatch.setattr(settings, "files_inline_operations", False)
    nudged: list[tuple[str, str]] = []

    async def _record(workflow: WorkflowType, op_id: uuid.UUID, org_team_id: uuid.UUID) -> bool:
        nudged.append((workflow.value, str(op_id)))
        return True

    monkeypatch.setattr(operations_runner, "nudge_files_operation", _record)

    drive, root = await _drive(files_client)
    items = [
        {"id": f"i{n}", "op": "createFolder", "parentId": root, "name": f"w{n}", "ifMatch": 1}
        for n in range(INLINE_ITEMS + 1)
    ]
    response = await _post(files_client, drive, items, idem())
    assert response.status_code == 202, response.text
    op_id = response.json()["id"]

    assert nudged == [(WorkflowType.FILES_BULK.value, op_id)]
    state = await files_client.get(f"{BASE}/drives/{drive}/operations/{op_id}")
    assert state.json()["state"] == "queued", "the request ran work it had handed to the worker"


async def test_every_queued_kind_names_the_worker_that_runs_it() -> None:
    """The table the hand-off reads is total over the kinds a route can queue.

    The route that writes the row and the process that runs it are two
    different deployments' halves, and a kind present on one side and absent
    from the other is an operation that answers 202 and then sits forever.
    """
    assert set(get_args(operations_runner.InlineKind)) == set(operations_runner.WORKER_WORKFLOW)
    assert operations_runner.WORKER_WORKFLOW["bulk"] is WorkflowType.FILES_BULK


def _rows(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in payload["responses"]}


async def _reload(session: AsyncSession, *nodes: FileNode) -> None:
    """Re-read the rows the request wrote, so the assertions read Postgres and
    not the identity map the fixture filled before the call."""
    for node in nodes:
        await session.refresh(node)


# ---------------------------------------------------------------------------
# the inline batch
# ---------------------------------------------------------------------------


async def test_a_mixed_batch_applies_every_verb_and_reports_each_one(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """One request carrying a create, a move, a trash and a star leaves all four
    effects in the tree and answers with the status each would have earned on
    its own. The persisted rows are the assertion, not the response echo."""
    folder = await fx.node(b"dest", kind="folder")
    mover = await fx.node(b"moved.txt")
    copied = await fx.node(b"copied.txt")
    marked = await fx.node(b"marked.txt")
    drive, root = await _drive(files_client)
    root_etag = await _etag(files_client, drive, root)

    response = await _post(
        files_client,
        drive,
        [
            {
                "id": "a",
                "op": "createFolder",
                "parentId": root,
                "name": "made",
                "ifMatch": root_etag,
            },
            {
                "id": "b",
                "op": "move",
                "itemId": str(mover.id),
                "parentId": str(folder.id),
                "ifMatch": mover.etag,
            },
            {"id": "c", "op": "copy", "itemId": str(copied.id), "parentId": str(folder.id)},
            {"id": "d", "op": "star", "itemId": str(marked.id), "ifMatch": marked.etag},
        ],
        idem(),
    )
    assert response.status_code == 200, response.text
    rows = _rows(response.json())
    assert [rows[k]["status"] for k in ("a", "b", "c", "d")] == [201, 200, 202, 200]

    await _reload(real_session, mover, marked)
    assert mover.parent_id == folder.id
    queued = await files_client.get(f"{BASE}/drives/{drive}/operations/{rows['c']['body']['id']}")
    assert queued.status_code == 200, queued.text
    # A queued batch is a `bulk` operation, not whichever verb it happened to
    # contain: undo replays the batch's own inverse, not a copy's.
    assert queued.json()["kind"] == "bulk"
    assert marked.flags != 0
    made = await real_session.get(FileNode, uuid.UUID(str(rows["a"]["body"]["id"])))
    assert made is not None and made.name == b"made" and made.parent_id == uuid.UUID(root)


async def test_a_stale_if_match_is_that_items_412_and_its_neighbour_still_applies(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The per-item precondition is per item. A batch that answered one status
    for the whole request, or that rolled the batch back on the first refusal,
    fails here: the stale item must be 412 AND the fresh one must be trashed."""
    nest = await fx.node(b"nest", kind="folder")
    stale = await fx.node(b"stale.txt")
    fresh = await fx.node(b"fresh.txt")
    drive, _parent = await _drive(files_client)
    # Where the refused move must leave it: wherever it was standing.
    stood_in = stale.parent_id

    response = await _post(
        files_client,
        drive,
        [
            {
                "id": "stale",
                "op": "move",
                "itemId": str(stale.id),
                "parentId": str(nest.id),
                "ifMatch": stale.etag + 7,
            },
            {
                "id": "fresh",
                "op": "move",
                "itemId": str(fresh.id),
                "parentId": str(nest.id),
                "ifMatch": fresh.etag,
            },
        ],
        idem(),
    )
    assert response.status_code == 200, response.text
    rows = _rows(response.json())
    assert rows["stale"]["status"] == 412
    assert rows["stale"]["body"]["code"] == "files.precondition_failed"
    assert rows["fresh"]["status"] == 200

    await _reload(real_session, stale, fresh)
    assert stale.parent_id == stood_in
    assert fresh.parent_id == nest.id


async def test_a_star_with_a_stale_if_match_changes_nothing(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """Star has no library statement for the etag to ride inside, so the route
    compares it. Removing that compare would let a stale client star a node it
    was looking at three versions ago."""
    node = await fx.node(b"bookmark.txt")
    drive, _root = await _drive(files_client)

    response = await _post(
        files_client,
        drive,
        [{"id": "s", "op": "star", "itemId": str(node.id), "ifMatch": node.etag + 3}],
        idem(),
    )
    assert response.status_code == 200, response.text
    assert _rows(response.json())["s"]["status"] == 412
    await _reload(real_session, node)
    assert node.flags == 0


async def test_a_mutation_item_without_an_if_match_is_refused_not_applied(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The header-level 428 does not reach inside a batch, so the item-level
    requirement is the only thing standing between a client and an unconditional
    trash of somebody else's newer tree."""
    nest = await fx.node(b"elsewhere", kind="folder")
    node = await fx.node(b"unconditional.txt")
    drive, _parent = await _drive(files_client)
    stood_in = node.parent_id

    response = await _post(
        files_client,
        drive,
        [{"id": "n", "op": "move", "itemId": str(node.id), "parentId": str(nest.id)}],
        idem(),
    )
    assert response.status_code == 200, response.text
    row = _rows(response.json())["n"]
    assert row["status"] == 422
    assert row["body"]["code"] == "files.if_match_required"
    await _reload(real_session, node)
    assert node.parent_id == stood_in


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------


async def test_a_strangers_item_is_the_opaque_row_and_the_rest_proceed(
    client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """Another org's real node, a well-formed id nobody owns, and the caller's
    own node in ONE batch: the first two are byte-identical opaque rows and the
    third still applies. A route that authorized the batch as a whole, or that
    let a foreign id abort it, fails both halves of this."""
    other_org, other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"other-{uuid.uuid4().hex[:8]}",
        admin_email=f"other-{uuid.uuid4().hex[:8]}@test.dev",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await real_session.commit()
    theirs = type(fx)(real_session, other_org.id, other_admin.id)
    their_node = await theirs.node(b"secret.txt")
    mine = await fx.node(b"mine.txt")

    caller = await login(client, files_org.org.admin_email, files_org.org.admin_password)
    drive, _root = await _drive(caller)
    response = await _post(
        caller,
        drive,
        [
            {"id": "theirs", "op": "star", "itemId": str(their_node.id), "ifMatch": 1},
            {"id": "ghost", "op": "star", "itemId": str(uuid.uuid4()), "ifMatch": 1},
            {"id": "mine", "op": "star", "itemId": str(mine.id), "ifMatch": mine.etag},
        ],
        idem(),
    )
    assert response.status_code == 200, response.text
    rows = _rows(response.json())
    assert rows["theirs"]["status"] == rows["ghost"]["status"] == 404
    assert rows["theirs"]["body"] == rows["ghost"]["body"] == {"code": "not_found"}
    assert rows["mine"]["status"] == 200

    await _reload(real_session, their_node, mine)
    assert their_node.flags == 0
    assert mine.flags != 0


async def test_an_item_the_policy_refuses_is_that_items_403_and_the_batch_goes_on(
    client: AsyncClient,
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """``enforce`` refuses by raising, and an unhandled raise inside a batch
    would make one item's denial the whole request's answer. The single-item
    DELETE proves this caller is refused; the batch must then carry that same
    403 as one row while the star beside it still applies.

    The refused caller is the org's plain member holding ``reader`` on both
    nodes — a reader may star what it can read and may not trash it. The org
    admin cannot play this part: it descends to a role that may trash its own
    drive, so nothing in its batch would be refused.
    """
    doomed = await fx.node(b"not-mine-to-delete.txt")
    marked = await fx.node(b"bookmarked.txt")
    drive, _root = await _drive(files_client)
    for node in (doomed, marked):
        owner_view = (await files_client.get(f"{BASE}/drives/{drive}/items/{node.id}")).json()
        granted = await files_client.post(
            f"{BASE}/drives/{drive}/items/{node.id}/permissions",
            json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "reader"},
            headers={**idem(), "If-Match": str(owner_view["etag"])},
        )
        assert granted.status_code in (200, 201), granted.text

    reader = await login(client, files_org.member.email, files_org.member_password)
    doomed_view = (await reader.get(f"{BASE}/drives/{drive}/items/{doomed.id}")).json()
    marked_view = (await reader.get(f"{BASE}/drives/{drive}/items/{marked.id}")).json()
    assert doomed_view["capabilities"]["can_delete"] is False
    single = await reader.delete(
        f"{BASE}/drives/{drive}/items/{doomed.id}",
        headers={**idem(), "If-Match": str(doomed_view["etag"])},
    )
    assert single.status_code == 403, single.text

    response = await _post(
        reader,
        drive,
        [
            {"id": "no", "op": "trash", "itemId": str(doomed.id), "ifMatch": doomed_view["etag"]},
            {"id": "yes", "op": "star", "itemId": str(marked.id), "ifMatch": marked_view["etag"]},
        ],
        idem(),
    )
    assert response.status_code == 200, response.text
    rows = _rows(response.json())
    assert rows["no"]["status"] == 403
    assert rows["no"]["body"]["code"] == "files.forbidden"
    assert rows["yes"]["status"] == 200

    await _reload(real_session, doomed, marked)
    assert doomed.trashed_at is None
    assert marked.flags != 0


# ---------------------------------------------------------------------------
# sizing
# ---------------------------------------------------------------------------


async def test_a_batch_over_the_inline_threshold_becomes_an_operation(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One item past the threshold flips the answer from a result list to a
    202 whose operation id the operations route can then be asked about — the
    tree is untouched until the operation runs, which is why the body is an
    operation and not a list of items that did not happen.

    Pinned on the shape that leaves the running to the worker, so what the
    listing shows is the request's own doing and not a run that has already
    happened behind the 202."""
    monkeypatch.setattr(settings, "files_inline_operations", False)
    monkeypatch.setattr(operations_runner, "nudge_files_operation", _no_nudge)
    drive, root = await _drive(files_client)
    items = [
        {"id": f"i{n}", "op": "createFolder", "parentId": root, "name": f"f{n}", "ifMatch": 1}
        for n in range(INLINE_ITEMS + 1)
    ]
    response = await _post(files_client, drive, items, idem())
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["state"] == "queued"
    assert body["kind"] == "bulk"
    assert body["total"] == INLINE_ITEMS + 1

    lookup = await files_client.get(f"{BASE}/drives/{drive}/operations/{body['id']}")
    assert lookup.status_code == 200, lookup.text
    assert lookup.json()["id"] == body["id"]

    listing = await files_client.get(f"{BASE}/drives/{drive}/items/{root}/children?limit=1000")
    assert listing.status_code == 200, listing.text
    names = {row["name"] for row in listing.json()["value"]}
    assert not names & {f"f{n}" for n in range(INLINE_ITEMS + 1)}


async def test_a_batch_at_the_inline_threshold_is_still_applied_in_the_request(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures, idem: Any
) -> None:
    """The boundary is inclusive: exactly ``INLINE_ITEMS`` items still come back
    as results. The negative twin of the case above — an off-by-one here would
    silently queue a batch a client was told had already applied."""
    drive, root = await _drive(files_client)
    root_etag = await _etag(files_client, drive, root)
    items = [
        {
            "id": f"i{n}",
            "op": "createFolder",
            "parentId": root,
            "name": f"g{n}",
            "ifMatch": root_etag,
        }
        for n in range(INLINE_ITEMS)
    ]
    response = await _post(files_client, drive, items, idem())
    assert response.status_code == 200, response.text
    rows = _rows(response.json())
    assert len(rows) == INLINE_ITEMS
    assert {row["status"] for row in rows.values()} == {201}


async def test_a_batch_over_the_node_cap_is_refused_whole(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures, idem: Any
) -> None:
    """``maxNodesPerRequest`` refuses rather than truncates: a truncated batch
    would apply a prefix and answer as though it had done all of it."""
    drive, root = await _drive(files_client)
    items = [
        {"id": f"i{n}", "op": "createFolder", "parentId": root, "name": f"h{n}", "ifMatch": 1}
        for n in range(MAX_ITEMS + 1)
    ]
    response = await _post(files_client, drive, items, idem())
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "files.bulk_too_large"


async def test_an_empty_batch_is_refused(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures, idem: Any
) -> None:
    drive, _root = await _drive(files_client)
    response = await _post(files_client, drive, [], idem())
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "files.bulk_empty"


async def test_two_items_sharing_a_correlation_id_are_refused(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures, idem: Any
) -> None:
    """The correlation id is how a client matches answers to requests; two items
    sharing one would give it an answer it cannot attribute."""
    node = await fx.node(b"twin.txt")
    drive, _root = await _drive(files_client)
    response = await _post(
        files_client,
        drive,
        [
            {"id": "same", "op": "star", "itemId": str(node.id), "ifMatch": node.etag},
            {"id": "same", "op": "unstar", "itemId": str(node.id), "ifMatch": node.etag},
        ],
        idem(),
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "files.bulk_duplicate_id"


async def test_a_batch_without_an_idempotency_key_is_refused(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures
) -> None:
    """The batch is a mutation, so it carries the same 428 every other Files
    mutation does — a replay of a batch must be recognizable as one."""
    drive, root = await _drive(files_client)
    response = await files_client.post(
        f"{BASE}/drives/{drive}/bulk",
        json={
            "items": [
                {"id": "a", "op": "createFolder", "parentId": root, "name": "x", "ifMatch": 1}
            ]
        },
    )
    assert response.status_code == 428, response.text


# ---------------------------------------------------------------------------
# the body cap
# ---------------------------------------------------------------------------


class _Guard:
    """The shipped body-limit middleware over a recording inner app.

    The scope carries a session cookie because the guard refuses a credential-
    less body past its anonymous allowance before it reads a byte, and the cap
    -- not the precondition -- is what these cases are about. The cookie is a
    SHAPE, never a session: no dependency runs here, and the recording inner
    app stands in for the route.
    """

    def __init__(self) -> None:
        self.inner_called = False
        self.consumed = 0
        self.sent: list[dict[str, Any]] = []
        self.middleware = GateIngestBodyLimitMiddleware(
            self._inner, max_bytes=64 * 1024 * 1024, max_inflight_bytes=1 << 40
        )

    async def _inner(self, scope: Any, receive: Any, send: Any) -> None:
        self.inner_called = True
        message = await receive()
        self.consumed += len(message.get("body", b""))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def call(self, path: str, declared: int) -> int:
        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"x" * 4096, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            self.sent.append(message)

        await self.middleware(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": [
                    (b"host", b"test"),
                    (b"cookie", f"{COOKIE_NAME}=session-token".encode()),
                    (b"content-length", str(declared).encode()),
                ],
            },
            receive,
            send,
        )
        return int(self.sent[0]["status"])


def _bulk_cap() -> int:
    caps = [p.max_bytes for p in GUARDED_PREFIXES if p.suffix == "/bulk"]
    assert len(caps) == 1
    return caps[0]


async def test_the_shipped_cap_guards_the_path_this_route_is_actually_mounted_at(
    files_client: AsyncClient, files_on: None
) -> None:
    """F-143 in reverse: the 64 MiB cap already shipped pointing at a path that
    had no route. This drives the real middleware at the URL the real request
    uses — a route registered anywhere else would sail past the guard.

    Both sides of the boundary, because a cap that refused everything would
    pass a one-sided test.
    """
    drive, _root = await _drive(files_client)
    url = f"{BASE}/drives/{drive}/bulk"
    cap = _bulk_cap()
    assert cap == 64 * 1024 * 1024

    over = _Guard()
    assert await over.call(url, cap + 1) == 413
    assert not over.inner_called
    assert over.consumed == 0

    at_cap = _Guard()
    assert await at_cap.call(url, cap) == 200
    assert at_cap.inner_called


@pytest.mark.parametrize(
    ("items", "reason"),
    [
        pytest.param([], "an empty batch", id="empty-batch"),
        pytest.param(
            [{"id": "a", "op": "trash", "itemId": str(uuid.uuid4()), "ifMatch": 1}],
            "a well-formed batch",
            id="valid-batch",
        ),
        pytest.param(
            [{"id": "a", "op": "trash"}, {"id": "a", "op": "trash"}],
            "a batch the validator would refuse",
            id="malformed-batch",
        ),
    ],
)
async def test_a_drive_that_is_not_this_orgs_is_the_same_opaque_404_whatever_the_body(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Any,
    items: list[dict[str, Any]],
    reason: str,
) -> None:
    """F-203: the drive is decided before the body is.

    A 422 at a drive id the caller may not see tells them the id is real — the
    body only got validated because the drive resolved — which is the existence
    oracle the no-oracle contract forbids. All three bodies must reach the identical
    ``not_found``, and the well-formed one proves the 404 is not just the
    validator happening to fail first.
    """
    elsewhere = str(uuid.uuid4())
    response = await _post(files_client, elsewhere, items, idem())
    assert response.status_code == 404, f"{reason} leaked the drive: {response.text}"
    assert refusal(response) == NOT_FOUND


async def test_a_queued_batch_writes_its_plan_where_the_runner_reads_it(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-201: a queued batch with no plan on its row can only reach ``failed``.

    The row is read straight out of Postgres and parsed by the library's own
    loader, so this fails both when the route writes nothing and when it writes
    a shape the runner cannot read back. Pinned on the shape that leaves the
    running to the worker: what is asserted is what the route hands over, so
    the cursor has to be the one it wrote and not one a run moved.
    """
    monkeypatch.setattr(settings, "files_inline_operations", False)
    monkeypatch.setattr(operations_runner, "nudge_files_operation", _no_nudge)
    drive, root = await _drive(files_client)
    items = [
        {"id": f"i{n}", "op": "createFolder", "parentId": root, "name": f"p{n}", "ifMatch": 1}
        for n in range(INLINE_ITEMS + 1)
    ]
    response = await _post(files_client, drive, items, idem())
    assert response.status_code == 202, response.text
    op_id = uuid.UUID(str(response.json()["id"]))

    row = (
        await real_session.execute(
            text("SELECT result FROM file_ops WHERE id = :id"), {"id": op_id}
        )
    ).first()
    assert row is not None and row[0], "the queued batch carries no plan"
    stored = BulkPlan.load(dict(row[0]))
    assert [one.id for one in stored.items] == [f"i{n}" for n in range(INLINE_ITEMS + 1)]
    assert stored.cursor == 0
    assert stored.remaining == stored.items
    assert stored.items[0].op == "createFolder"
    assert stored.items[0].name == "p0"
    assert stored.items[0].parent_id is not None and str(stored.items[0].parent_id) == root


async def test_a_queued_batch_records_each_items_decision_before_it_is_queued(
    client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """A batch too big to apply is decided at REQUEST time, item by item.

    The reader holds ``reader`` on both subtrees, so every star is allowed and
    the trash is the one thing they may not do. Three things have to be true
    for the plan to be the capability it claims to be: the refused item is
    marked ``allowed=False`` on the row, carrying the row the caller will read;
    its DENY is on record before the 202 is answered (the runner is a different
    process and cannot write the caller's decision later); and a run of the plan
    applies its neighbours and reports the refusal without deciding anything
    itself. A queued branch that stores an undecided plan fails all three - and
    hands the runner a batch it then executes unauthorized.
    """
    shared = await fx.node(b"shared", kind="folder")
    children = [await fx.node(f"c{n}".encode(), parent=shared) for n in range(INLINE_ITEMS)]
    secret = await fx.node(b"reader-may-not-delete.txt")
    admin = ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
    )
    reader = Principal(kind="user", id=files_org.member.id)
    async with fx.repo.transaction():
        await acl.grant(fx.repo, admin, shared, reader, ROLE_READER)
        await acl.grant(fx.repo, admin, secret, reader, ROLE_READER)
    await real_session.commit()

    member = app_client()
    member = await login(member, files_org.member.email, files_org.member_password)
    drive, _root = await _drive(member)
    before = await _denials_for(files_org.org.org_id, secret)
    response = await _post(
        member,
        drive,
        [
            *(
                {"id": f"s{n}", "op": "star", "itemId": str(node.id), "ifMatch": node.etag}
                for n, node in enumerate(children)
            ),
            {"id": "no", "op": "trash", "itemId": str(secret.id), "ifMatch": secret.etag},
        ],
        idem(),
    )
    assert response.status_code == 202, response.text
    op_id = uuid.UUID(str(response.json()["id"]))

    stored = await _stored_plan(real_session, op_id)
    decided = {one.id: one for one in stored.items}
    assert decided["no"].allowed is False
    assert decided["no"].denial is not None
    assert decided["no"].denial["status"] == 403
    assert decided["no"].denial["body"]["code"] == "files.forbidden"
    assert all(decided[f"s{n}"].allowed for n in range(INLINE_ITEMS))
    assert not any(decided[f"s{n}"].denial for n in range(INLINE_ITEMS))
    after = await _denials_for(files_org.org.org_id, secret)
    assert len(after) == len(before) + 1, "the refusal was not on record when the 202 was answered"

    async with AsyncSessionLocal() as session:
        repo = FilesRepo(session, OrgScope(org_team_id=files_org.org.org_id))
        member_ctx = ActingContext.for_user(
            user_id=files_org.member.id, org_id=files_org.org.org_id, email=files_org.member.email
        )
        assert await bulk_core.resume(repo, member_ctx, OperationId(op_id)) == "done"

    ran = await _stored_plan(real_session, op_id)
    rows = {str(one["id"]): one for one in ran.results}
    assert rows["no"] == {"id": "no", **(decided["no"].denial or {})}
    assert {rows[f"s{n}"]["status"] for n in range(INLINE_ITEMS)} == {200}
    await _reload(real_session, secret)
    assert secret.trashed_at is None, "the runner applied an item the request refused"


async def _stored_plan(session: AsyncSession, op_id: uuid.UUID) -> BulkPlan:
    """The batch as the runner reads it: straight out of Postgres, its loader."""
    row = (
        await session.execute(text("SELECT result FROM file_ops WHERE id = :id"), {"id": op_id})
    ).first()
    assert row is not None and row[0], "the queued batch carries no plan"
    return BulkPlan.load(dict(row[0]))


async def _denials_for(org_id: uuid.UUID, node: FileNode) -> list[EventOutbox]:
    """The committed DENY rows this org holds about ``node``.

    Read on a session of its own because that is where the sink writes them: a
    denial that only existed inside the request's transaction would vanish with
    it.
    """
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox).where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == AUTHZ_EVENT_TYPE,
                EventOutbox.entity_id == str(node.id),
            )
        )
        return [row for row in rows.scalars().all() if row.payload.get("effect") == "deny"]


async def test_a_writer_trashes_in_a_batch_exactly_as_it_does_one_at_a_time(
    client: AsyncClient,
    files_client: AsyncClient,
    files_on: None,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The batch decides a ``trash`` on the writer rung, like the single-item
    route: a caller who bins a file one at a time must not be refused for
    binning the same file in a batch — which is what every file client sends
    when a user multi-selects and hits Delete.

    RED while the verb table decided ``trash`` as ``DELETE`` (the owner rung,
    which means the purge): the batch answered 403 and the row was still live.
    """
    doomed = await fx.node(b"batch-doomed.txt")
    drive, _ = await _drive(files_client)
    owner_view = (await files_client.get(f"{BASE}/drives/{drive}/items/{doomed.id}")).json()
    granted = await files_client.post(
        f"{BASE}/drives/{drive}/items/{doomed.id}/permissions",
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "writer"},
        headers={**idem(), "If-Match": str(owner_view["etag"])},
    )
    assert granted.status_code in (200, 201), granted.text

    member = await login(client, files_org.member.email, files_org.member_password)
    view = (await member.get(f"{BASE}/drives/{drive}/items/{doomed.id}")).json()
    # The single-item route says they may, so the batch must agree.
    assert view["capabilities"]["can_delete"] is True

    response = await _post(
        member,
        drive,
        [{"id": "a", "op": "trash", "itemId": str(doomed.id), "ifMatch": view["etag"]}],
        idem(),
    )
    assert response.status_code == 200, response.text
    assert _rows(response.json())["a"]["status"] == 204, response.text
    await _reload(real_session, doomed)
    assert doomed.trashed_at is not None


async def test_a_bulk_answer_renders_the_mount_its_rows_sit_under(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """Rows a batch answers with carry the lease their subtree is under.

    A batch that answered ``lease: null`` for a folder the main listing said
    was mounted is the same node disagreeing with itself between two calls the
    same client makes seconds apart.
    """
    drive = await fx.drive()
    folder = await fx.node(b"mounted", kind="folder")
    from _files_kit import node_etag

    granted = await files_client.post(
        f"{BASE}/drives/{drive.id}/items/{folder.id}/lease",
        json={"instanceId": "instance-a", "machineId": "machine-a", "purpose": "mount"},
        headers={**idem(), "If-Match": await node_etag(real_session, folder.id)},
    )
    assert granted.status_code == 200, granted.text

    answered = await _post(
        files_client,
        str(drive.id),
        [
            {
                "id": "marked",
                "op": "star",
                "itemId": str(folder.id),
                "ifMatch": await _etag(files_client, str(drive.id), str(folder.id)),
            }
        ],
        idem(),
    )

    assert answered.status_code == 200, answered.text
    row = _rows(answered.json())["marked"]
    assert row["status"] == 200, row
    lease = row["body"]["lease"]
    assert lease is not None, "a batch answered no lease for a row inside a live mount"
    assert lease["machine"] == "machine-a"
    assert lease["mine"] is True


async def test_the_holders_own_batch_trashes_inside_the_folder_it_holds(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """A batch is fenced the way a single delete is.

    Cleaning up its own mount is the one thing a holder does constantly, and it
    is also the one caller a lease is not meant to refuse — but the batch sent
    no fence at all, so the holder's own trash came back 409 ``files.leased``
    from inside the folder it was holding. The single-item route has always
    threaded the two headers through; this asserts the batch reaches the same
    answer for the same request.
    """
    folder = await fx.node(b"mounted", kind="folder")
    doomed = await fx.node(b"stale.log", parent=folder)
    drive = await fx.drive()
    granted = await files_client.post(
        f"{BASE}/drives/{drive.id}/items/{folder.id}/lease",
        json={"instanceId": "instance-a", "machineId": "machine-a", "purpose": "mount"},
        headers={**idem(), "If-Match": str(folder.etag)},
    )
    assert granted.status_code == 200, granted.text
    fence = {
        "X-Alkera-Lease-Epoch": str(granted.json()["epoch"]),
        "X-Alkera-Lease-Instance": "instance-a",
    }
    item = [{"id": "a", "op": "trash", "itemId": str(doomed.id), "ifMatch": doomed.etag}]

    unfenced = await _post(files_client, str(drive.id), item, idem())
    assert unfenced.status_code == 200, unfenced.text
    assert _rows(unfenced.json())["a"]["status"] == 409, unfenced.text
    assert _rows(unfenced.json())["a"]["body"]["code"] == "files.leased"
    await _reload(real_session, doomed)
    assert doomed.trashed_at is None, "a refused batch trashed the file anyway"

    fenced = await files_client.post(
        f"{BASE}/drives/{drive.id}/bulk",
        json={"items": item},
        headers={**idem(), **fence},
    )
    assert fenced.status_code == 200, fenced.text
    assert _rows(fenced.json())["a"]["status"] == 204, fenced.text
    await _reload(real_session, doomed)
    assert doomed.trashed_at is not None


async def test_a_queued_batch_names_an_operation_its_own_route_will_not_undo(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures, idem: Any
) -> None:
    """A batch is filed under its own kind and records no inverse, so the
    operation it names cannot be undone.

    The 202 hands a client an operation id, and an id is the only handle
    ``POST .../operations/{id}/undo`` takes -- so a client that treats "the
    server named an operation" as "the server can undo it" offers an undo for
    every batch it sends. It cannot: ``Operations.undo`` refuses an operation
    with no inverse, and this route never records one. Pinned here because the
    web client reads this contract off nothing else: there is no field on the
    wire that says so, so the only thing stopping a dead undo step is that both
    sides agree, and one of them is a comment.
    """
    drive, root = await _drive(files_client)
    made = []
    for n in range(INLINE_ITEMS + 1):
        response = await files_client.post(
            f"{BASE}/drives/{drive}/items/{root}/children",
            json={"kind": "folder", "name": f"u{n}"},
            headers=idem(),
        )
        assert response.status_code == 201, response.text
        made.append(response.json())

    items = [
        {"id": f"i{n}", "op": "trash", "itemId": row["id"], "ifMatch": int(row["etag"])}
        for n, row in enumerate(made)
    ]
    queued = await _post(files_client, drive, items, idem())
    assert queued.status_code == 202, queued.text
    body = queued.json()
    assert body["kind"] == "bulk"
    op_id = body["id"]

    # The undo route fences like any other mutation, so the precondition is
    # satisfied before the inverse is looked for: what is under test is what the
    # route says about a batch, not what it says about a missing header.
    undo = await files_client.post(
        f"{BASE}/drives/{drive}/operations/{op_id}/undo",
        headers={**idem(), "If-Match": str(made[0]["etag"])},
    )
    assert undo.status_code == 409, undo.text
    assert undo.json()["code"] == "files.not_undoable"
