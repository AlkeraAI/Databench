"""Every id-taking route answers the three "not yours" classes identically.

The three classes are a nonexistent id, an id that belongs to another org, and
an id in the caller's own org that the caller may not read. The no-oracle contract says
all three must be one answer on every axis a caller can observe: status, headers,
body — and the amount of work the server did, because a query count is a
latency and a bill, and both are readable from outside.

The routes are enumerated as parameters rather than written out one test each,
so a lane that adds an id-taking route adds a row here and inherits the whole
contract. The shared probe lives in ``_oracle`` for the same reason.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from _oracle import (
    OPAQUE_NOT_FOUND_BODY,
    assert_no_oracle,
    counting,
    probe,
    quiesce_auth,
    work_report,
)
from alkera_core.auth import revocation
from alkera_core.db.session import engine
from alkera_core.files.acl_intern import ace_body, body_hash
from alkera_core.files.authz.grants import Grant, GrantOrigin, Principal
from alkera_core.models.files.acl import FileAcl
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import login

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    from .conftest import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"

#: The engine the probe counts statements on. The app and the fixtures share
#: one process-wide async engine, so its sync facade is what the listener binds.
SYNC_ENGINE = engine.sync_engine


def _item_slot(classes: ThreeClasses) -> tuple[str, str, str]:
    """The three classes as *item* ids — what an id-taking item route varies."""
    return classes.ids


def _drive_slot(classes: ThreeClasses) -> tuple[str, str, str]:
    """The three classes as *drive* ids.

    A drive-addressed listing names no item, so the thing the contract is about
    is the drive: one that does not exist, one that belongs to another org, and
    an id of the caller's own org that is not a drive at all.
    """
    return (classes.nonexistent, classes.other_org_drive, classes.unreadable)


@dataclass(frozen=True, slots=True)
class RouteCase:
    """One id-taking route, described well enough to drive it three times."""

    name: str
    method: str
    #: ``(drive_id, item_id) -> path``. Every case interpolates the *item* id;
    #: the drive is always the caller's own, so the only thing that varies
    #: between the three classes is the thing the contract is about.
    template: Callable[[str, str], str]
    json: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    #: Which id triple the template is handed. A drive-addressed listing puts
    #: the varying id in the drive slot instead of the item slot.
    probe_ids: Callable[[ThreeClasses], tuple[str, str, str]] = _item_slot


def _mutation_headers() -> dict[str, str]:
    """What a mutation must carry to get past the dependencies and reach the
    policy. Without them the route answers 428 for every class — identical, but
    identical for the wrong reason, and the policy would never run."""
    return {"Idempotency-Key": uuid.uuid4().hex, "If-Match": "1"}


ROUTES: tuple[RouteCase, ...] = (
    RouteCase(
        name="get-item",
        method="GET",
        template=lambda d, i: f"{BASE}/drives/{d}/items/{i}",
    ),
    RouteCase(
        name="list-children",
        method="GET",
        template=lambda d, i: f"{BASE}/drives/{d}/items/{i}/children",
    ),
    RouteCase(
        name="create-child",
        method="POST",
        template=lambda d, i: f"{BASE}/drives/{d}/items/{i}/children",
        json={"name": "probe.txt", "kind": "folder"},
    ),
    RouteCase(
        name="create-tree",
        method="POST",
        template=lambda d, i: f"{BASE}/drives/{d}/items/{i}/tree",
        json={"paths": ["a/b"]},
    ),
    # The holder's tree report. It carries the fence a holder would, so what
    # the three classes reach is the policy on the leased node, not a refusal
    # for a missing header that would be identical for the wrong reason.
    RouteCase(
        name="lease-tree",
        method="POST",
        template=lambda d, i: f"{BASE}/drives/{d}/items/{i}/lease/tree",
        json={
            "batch_id": "018f6e1a-0b6d-7b1e-8f1e-3c2a9b7d4e10",
            "entries": [{"op": "upsert", "path": "a/b.txt", "kind": "file", "size": 1}],
        },
        headers={"X-Alkera-Lease-Epoch": "1", "X-Alkera-Lease-Instance": "probe"},
    ),
    # The walk's digest request: a read, but of the same leased folder under
    # the same fence, so a stranger's id must cost what a missing one does.
    RouteCase(
        name="lease-tree-digests",
        method="POST",
        template=lambda d, i: f"{BASE}/drives/{d}/items/{i}/lease/tree/digests",
        json={"paths": ["", "a"]},
        headers={"X-Alkera-Lease-Epoch": "1", "X-Alkera-Lease-Instance": "probe"},
    ),
    RouteCase(
        name="patch-item",
        method="PATCH",
        template=lambda d, i: f"{BASE}/drives/{d}/items/{i}",
        json={"name": "renamed.txt"},
    ),
    RouteCase(
        name="delete-item",
        method="DELETE",
        template=lambda d, i: f"{BASE}/drives/{d}/items/{i}",
    ),
    RouteCase(
        name="item-activity",
        method="GET",
        template=lambda d, i: f"{BASE}/drives/{d}/items/{i}/activity",
    ),
    # The two reads of a file's bytes. Either may ask the machine holding the
    # folder for them, and neither may do so -- or read a lease, or name a
    # machine -- before the node is decided readable: a stranger's id must
    # cost what a missing one costs, whatever a holder has reported for it.
    RouteCase(
        name="get-content",
        method="GET",
        template=lambda d, i: f"{BASE}/drives/{d}/items/{i}/content",
    ),
    RouteCase(
        name="content-grant",
        method="POST",
        template=lambda d, i: f"{BASE}/drives/{d}/items/{i}/content-grants",
        json={"kind": "file"},
    ),
    # The two drive-addressed listings. Neither names an item, so the drive is
    # what varies: a stranger's drive must be as absent as one that was never
    # created. ``mine=true`` is what the leases listing needs to get past its
    # own argument check, so the class under test is the drive and not the flag.
    RouteCase(
        name="list-conflicts",
        method="GET",
        template=lambda d, i: f"{BASE}/drives/{i}/conflicts",
        probe_ids=_drive_slot,
    ),
    RouteCase(
        name="list-leases",
        method="GET",
        template=lambda d, i: f"{BASE}/drives/{i}/leases?mine=true",
        probe_ids=_drive_slot,
    ),
)


@dataclass(frozen=True, slots=True)
class ThreeClasses:
    """The caller and the three ids they will ask about."""

    client: AsyncClient
    drive_id: str
    nonexistent: str
    other_org: str
    unreadable: str
    #: Another org's real drive id — the "not yours" class for a drive slot.
    other_org_drive: str

    @property
    def ids(self) -> tuple[str, str, str]:
        return (self.nonexistent, self.other_org, self.unreadable)


#: What a holder's tree report leaves on a row it minted ahead of the bytes:
#: no head version, and the disk's size, time and the report's sequence.
REPORTED_COLUMNS: dict[str, Any] = {"holder_size": 9, "holder_mtime_ns": 1, "holder_seq": 1}


@pytest_asyncio.fixture(params=[pytest.param(False, id="rows"), pytest.param(True, id="reported")])
async def three_classes(
    request: pytest.FixtureRequest,
    client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    files_on: None,
    fx: FilesFixtures,
) -> ThreeClasses:
    """A member of the org, and one id of each "not yours" kind.

    The caller is a plain member rather than the org admin on purpose: an org
    admin has a standing floor on their own drive, so no node in their own org
    could ever be unreadable to them and the third class would be unbuildable.

    Built twice: once from ordinary rows, and once with the other org's row
    and the unreadable row both minted the way a holder's tree report mints
    them -- no bytes, a holder facet -- so a route that renders the facet
    before it decides, or reads the head a reported row lacks, is caught
    costing a different amount for the rows a report made.
    """
    reported: dict[str, Any] = REPORTED_COLUMNS if request.param else {}
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
    their_node = await theirs.node(b"theirs.txt", **reported)
    their_drive = await theirs.drive()

    # An interned ACL that names only the org admin. Pointing a node at it is
    # how a real restrictive share renders once the ACL cache is warm, so the
    # node is unreadable to the member through the same code path production
    # uses — not by a monkeypatched decider.
    body = ace_body(
        [
            Grant(
                principal=Principal(kind="user", id=files_org.org.admin_id),
                role="manager",
                origin=GrantOrigin.direct(),
            )
        ]
    )
    acl = FileAcl(
        id=uuid.uuid4(),
        org_team_id=files_org.org.org_id,
        body=body,
        body_hash=body_hash(body, org_team_id=files_org.org.org_id),
    )
    real_session.add(acl)
    await real_session.flush()
    hidden = await fx.node(b"hidden.txt", acl_id=acl.id, **reported)
    await real_session.commit()

    member_client = await login(client, files_org.member.email, files_org.member_password)
    drive = await member_client.get(f"{BASE}/drives")
    assert drive.status_code == 200, drive.text
    return ThreeClasses(
        client=member_client,
        drive_id=str(drive.json()["id"]),
        nonexistent=str(uuid.uuid4()),
        other_org=str(their_node.id),
        unreadable=str(hidden.id),
        other_org_drive=str(their_drive.id),
    )


#: Every route now answers the three classes identically: one enforcer turns
#: the engine's opaque refusal into the Files ``NotFound``, so the standing
#: strict xfail (and the per-route exemptions that narrowed it) is gone.
@pytest.mark.parametrize("case", ROUTES, ids=[route.name for route in ROUTES])
async def test_route_answers_the_three_not_yours_classes_identically(
    three_classes: ThreeClasses, case: RouteCase
) -> None:
    """Status, headers and body are byte-identical, and no id is echoed back."""
    headers = dict(case.headers)
    if case.method != "GET":
        headers.update(_mutation_headers())
    probes = await assert_no_oracle(
        three_classes.client,
        SYNC_ENGINE,
        case.method,
        [
            case.template(three_classes.drive_id, identifier)
            for identifier in case.probe_ids(three_classes)
        ],
        case.probe_ids(three_classes),
        json=case.json,
        headers=headers,
        labels=("nonexistent", "other-org", "unreadable"),
        # Work equality is asserted by its own test below, so a route whose
        # bodies match but whose query counts do not names the real defect
        # instead of failing this one under a misleading name.
        same_work=False,
    )
    assert probes[0].status == 404
    assert json.loads(probes[0].body) == OPAQUE_NOT_FOUND_BODY


@pytest.mark.parametrize("case", ROUTES, ids=[route.name for route in ROUTES])
async def test_route_does_the_same_work_for_the_three_classes(
    three_classes: ThreeClasses, case: RouteCase
) -> None:
    """The three classes cost the same number of SQL statements.

    An early return on "no such row" would make the nonexistent class cheaper
    than the unreadable one, and a caller who can time two requests would then
    have the existence oracle the identical bodies were meant to deny them.
    """
    headers = dict(case.headers)
    if case.method != "GET":
        headers.update(_mutation_headers())
    identifiers = case.probe_ids(three_classes)

    # Drive the route once before measuring anything: the first request of a
    # process pays for lookups every later one has cached, and that warm-up is
    # not part of the contract. Which class warms is arbitrary — all three
    # answer the same 404 — so it is the first.
    warm_headers = dict(headers, **({} if case.method == "GET" else _mutation_headers()))
    await three_classes.client.request(
        case.method,
        case.template(three_classes.drive_id, identifiers[0]),
        json=case.json,
        headers=warm_headers,
    )

    probes = []
    for identifier in identifiers:
        # The one thing about a request that is decided by the clock rather
        # than by the route: see ``quiesce_auth``.
        quiesce_auth()
        probes.append(
            await probe(
                three_classes.client,
                SYNC_ENGINE,
                case.method,
                case.template(three_classes.drive_id, identifier),
                json=case.json,
                headers=dict(headers, **({} if case.method == "GET" else _mutation_headers())),
            )
        )
    counts = [observed.statements for observed in probes]
    assert len(set(counts)) == 1, (
        f"{case.name}: statement counts differ across classes: {counts}\n"
        f"{work_report(probes, ['nonexistent', 'other-org', 'unreadable'])}"
    )


async def test_the_probe_itself_catches_a_route_that_leaks(
    three_classes: ThreeClasses,
) -> None:
    """The helper is not vacuous: a route that answers the readable node
    differently is caught by the same comparison the contract relies on.

    Without this, ``assert_no_oracle`` passing on every route would prove only
    that it never fails. The readable half is the drive listing rather than the
    drive root: a plain member holds no grant on the root, so since the opaque
    refusal was unified it is one more "not yours" and answers the same bytes.
    """
    readable = await three_classes.client.get(f"{BASE}/drives")
    assert readable.status_code == 200, readable.text
    with pytest.raises(AssertionError):
        await assert_no_oracle(
            three_classes.client,
            SYNC_ENGINE,
            "GET",
            [
                f"{BASE}/drives/{three_classes.drive_id}/items/{three_classes.nonexistent}",
                f"{BASE}/drives",
            ],
            [three_classes.nonexistent, three_classes.drive_id],
            # Not the usual three: a missing item and the caller's own drive
            # listing, so they name themselves rather than borrowing names that
            # would describe neither.
            labels=("nonexistent-item", "own-drive-listing"),
            same_work=False,
        )


async def test_a_stale_revocation_answer_costs_one_statement_the_route_did_not(
    three_classes: ThreeClasses, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The confound the comparison above holds still, measured.

    ``assert_token_active`` trusts a cached "not revoked" answer for a few
    seconds and re-checks it against Postgres once it is stale. That re-check
    is one statement, it belongs to the session and not to the route, and which
    request pays it is decided by how long ago the previous one was. Three
    probes of one comparison straddle that window on a loaded runner, and the
    work-equality assertion then reports a difference the server never made.

    The window is pinned open here so the second probe is certain to be served
    from the cache: what is being measured is the size of the confound, not the
    clock. It is exactly one, which is why a single straddled boundary is
    enough to split a three-way comparison.
    """
    monkeypatch.setattr(revocation, "_CACHE_TTL_SECONDS", 3600.0)
    url = f"{BASE}/drives/{three_classes.drive_id}/items/{three_classes.nonexistent}"
    await three_classes.client.get(url)  # warm everything that is not the answer

    revocation._cache.reset()
    cold = await probe(three_classes.client, SYNC_ENGINE, "GET", url)
    warm = await probe(three_classes.client, SYNC_ENGINE, "GET", url)

    assert cold.status == warm.status == 404
    assert cold.statements == warm.statements + 1, work_report([warm, cold], ["warm", "cold"])
    assert [text for text in cold.sql if "auth_tokens" in text], (
        "the extra statement a cold answer pays is the revocation re-check"
    )


async def test_the_statement_counter_counts_real_statements(
    three_classes: ThreeClasses,
) -> None:
    """The counter is wired to the engine the app actually uses.

    A listener attached to the wrong engine would count zero and make every
    work-equality assertion above pass for free.
    """
    with counting(SYNC_ENGINE) as statements:
        response = await three_classes.client.get(f"{BASE}/drives")
    assert response.status_code == 200
    assert statements, "no statements were observed for a request that reads the drive"
