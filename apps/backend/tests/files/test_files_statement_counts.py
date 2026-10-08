"""The 404 path costs what the 200 path costs, minus the payload it does not build.

The no-oracle contract's "same work on every path" rule: a route always loads through the
repo, builds its facts and calls the policy before it branches, so "the 404
path runs the same queries as the 200 path minus the payload". A caller who can
time two requests — or read two lines of their own bill — must not be able to
tell a resource they may not see from one that does not exist, and a query
count is the cheapest proxy for both.

The delta between the two paths is *pinned*, not merely asserted to be small:
an unpinned "the 404 is no more expensive" would pass a route that stopped
loading the node at all, which is exactly the early return the contract bans.
A route that starts doing more work on the refusal path fails here with the two
numbers in the message.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import pytest
import pytest_asyncio
from _oracle import Probe, probe, work_report
from alkera_core.auth import revocation
from alkera_core.db.session import engine
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import login
from tests.files.conftest import FilesFixtures, FilesOrgFixture

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    pass

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"

#: The app and the fixtures share one process-wide async engine; its sync
#: facade is what the statement listener binds to.
SYNC_ENGINE = engine.sync_engine


@dataclass(frozen=True, slots=True)
class ReadCase:
    """One id-taking read, and how much cheaper its refusal is allowed to be."""

    name: str
    template: Callable[[str, str], str]
    #: ``statements(200) - statements(404)``. Pinned per route: it is the
    #: payload the refusal does not build, and nothing else.
    delta: int
    headers: dict[str, str] = field(default_factory=dict)


READS: tuple[ReadCase, ...] = (
    # Two. One: the lease governing the node. Everything else the body needs came
    # back with the rows ``authorize`` loaded for both answers, but a lease is
    # held on an ancestor and governs the subtree, so it cannot be read off the
    # node — it is the single statement over the chain that lets a reader be
    # told the folder is mounted somewhere. Two: the owner's name, which lives in
    # `users` and not in the tree at all, so the uuid on the row cannot become a
    # label without it. `users` is a platform table the tenant role cannot read,
    # so that lookup steps out of the role and back in — one single-statement
    # stamp each way — which is the two on top of the two reads; the refusal
    # never names an owner, so it pays neither.
    #
    # And three more, on the success only: the chain's own decision, batched, so
    # the path the body carries names no folder this caller may not read. A
    # decision per ancestor would be a statement per level of the tree; this is
    # the whole chain at once, and a refusal builds no path so it pays nothing.
    # The listing pays none of it — its chain rides in the batch that decides
    # the page's rows. The floor below keeps a route that executed nothing at
    # all from passing this.
    #
    # 6 -> 7. The batch reads the page's rows and their chains in two statements
    # rather than one: an ltree ancestry join is index-served for the table owner
    # and not for `alkera_files_app`, where FORCE row security stops PostgreSQL
    # promoting a non-leakproof qual — `subpath()` and ltree containment are not
    # leakproof — ahead of the policy's own, leaving a scan of the whole drive
    # per item. Addressed by `(drive_id, ino)` it keeps its unique index under
    # the role. The refusal decides nothing, so the whole increase lands here.
    #
    # +2 (both reads). Recording the refusal's DENY used to roll the request's
    # transaction back and re-stamp the Files role on the next statement, two
    # statements the 404 paid and the 200 did not. The DENY now goes through a
    # pool of its own and leaves the request's transaction alone, so the
    # refusal is two statements cheaper and the 200 is unchanged.
    #
    # -1 (both reads). That pool was one the count did not watch, so the DENY
    # row itself (one INSERT into the outbox) went uncounted and a refusal read
    # one statement cheaper than it is. The counter now watches the decision
    # pool beside the app's engine. Neither path changed: the 200 costs what
    # it did, and the 404's own row is counted.
    ReadCase(
        name="get-item",
        template=lambda d, i: f"{BASE}/drives/{d}/items/{i}",
        delta=8,
    ),
    # The page itself: the sibling anchor the marker resolves against, the child
    # page, the one lease lookup that covers every row on it — batched over the
    # chain the page already loaded — and the one owner-name lookup batched over
    # the ids the page rendered, so a page of a hundred rows still costs the two
    # — plus the role step-out and step-in around the owner-name read, one
    # statement each, as on the item read above.
    # Then the three the per-row decision costs: every listed row is rendered
    # from its OWN access — a node's flags are its own, so the folder's access
    # says nothing true about a sealed chat inside it — and that batch loads the
    # rows with their drives in one statement, their chains in a second and the
    # grants on those chains in a third. Three for any page size, not three per
    # row; a listed row carrying its own interned ACL would add a fourth, and
    # none of this fixture's rows does.
    # Everything before them is the load-and-decide both answers do.
    #
    # 8 -> 9. The chain used to ride on the statement that read the page, as an
    # ltree join. That join reaches its index for the table owner and not for
    # `alkera_files_app`: under FORCE row security PostgreSQL will not promote a
    # non-leakproof qual ahead of the policy's own, and neither `subpath()` nor
    # ltree containment is leakproof, so the plan fell back to reading every node
    # of the drive once per row of the page. `(drive_id, ino)` asks with integer
    # and uuid equality, which are leakproof, so the unique index answers under
    # the role — at the price of reading the page's rows first.
    #
    # +2 (both reads). Recording the refusal's DENY used to roll the request's
    # transaction back and re-stamp the Files role on the next statement, two
    # statements the 404 paid and the 200 did not. The DENY now goes through a
    # pool of its own and leaves the request's transaction alone, so the
    # refusal is two statements cheaper and the 200 is unchanged.
    #
    # -1 (both reads). That pool was one the count did not watch, so the DENY
    # row itself (one INSERT into the outbox) went uncounted and a refusal read
    # one statement cheaper than it is. The counter now watches the decision
    # pool beside the app's engine. Neither path changed: the 200 costs what
    # it did, and the 404's own row is counted.
    ReadCase(
        name="list-children",
        template=lambda d, i: f"{BASE}/drives/{d}/items/{i}/children",
        delta=10,
    ),
)


@dataclass(frozen=True, slots=True)
class Paths:
    """A caller, one readable id, and the three ids that are "not yours"."""

    client: AsyncClient
    drive_id: str
    readable: str
    nonexistent: str
    other_org: str

    @property
    def refusals(self) -> tuple[str, str]:
        return (self.nonexistent, self.other_org)


@pytest_asyncio.fixture
async def paths(
    client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    files_on: None,
    fx: FilesFixtures,
) -> Paths:
    """The org admin, who can read their own drive's root, plus two foreign ids.

    The caller is the admin rather than a member because this file is about the
    *200* path: the comparison needs a readable node, and the byte-identity of
    the three refusal classes is the sibling file's contract, not this one's.
    """
    from backend.services.org import teams as team_service

    other_org, other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"other-{uuid.uuid4().hex[:8]}",
        admin_email=f"other-{uuid.uuid4().hex[:8]}@test.dev",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await real_session.commit()
    theirs = FilesFixtures(real_session, other_org.id, other_admin.id)
    their_node = await theirs.node(b"theirs.txt")

    drive = await fx.drive()
    # Owned by a real user, so the payload pays for the owner-name read the
    # deltas below pin: a tree of ownerless rows would skip it and the pin
    # would cover a page no product surface serves.
    folder = await fx.node(b"folder", kind="folder", created_by=fx.actor_id)
    await fx.node(b"leaf.txt", parent=folder, created_by=fx.actor_id)

    caller = await login(client, files_org.org.admin_email, files_org.org.admin_password)
    return Paths(
        client=caller,
        drive_id=str(drive.id),
        readable=str(folder.id),
        nonexistent=str(uuid.uuid4()),
        other_org=str(their_node.id),
    )


async def _count(paths: Paths, case: ReadCase, identifier: str) -> tuple[int, int]:
    """Drive the route once to warm the per-connection and per-session caches,
    then measure: the first request of a process pays for lookups every later
    one has cached, and that warm-up cost is not part of the contract."""
    await paths.client.get(case.template(paths.drive_id, identifier), headers=case.headers)
    # The session-revocation answer is cached in-process for a few seconds, so
    # whether a measured request re-checks it depends on the clock. Resetting
    # the cache makes every measurement pay for it once, on both sides.
    revocation._cache.reset()
    observed = await probe(
        paths.client,
        SYNC_ENGINE,
        "GET",
        case.template(paths.drive_id, identifier),
        headers=case.headers,
    )
    return observed.status, observed.statements


@pytest.mark.parametrize("case", READS, ids=[read.name for read in READS])
async def test_the_refusal_costs_the_success_minus_its_pinned_payload(
    paths: Paths, case: ReadCase
) -> None:
    """One number per route, and both refusal classes hit it exactly."""
    allowed_status, allowed = await _count(paths, case, paths.readable)
    assert allowed_status == 200

    for identifier in paths.refusals:
        refused_status, refused = await _count(paths, case, identifier)
        assert refused_status == 404
        assert allowed - refused == case.delta, (
            f"{case.name}: 200 cost {allowed} statements, 404 cost {refused} "
            f"(delta {allowed - refused}, pinned {case.delta})"
        )


@pytest.mark.parametrize("case", READS, ids=[read.name for read in READS])
async def test_the_two_refusal_classes_cost_the_same(paths: Paths, case: ReadCase) -> None:
    """A nonexistent id and another org's id are the same amount of work.

    They answer the same body already; if one were cheaper, the timing would
    hand back the distinction the body refused to make.
    """
    counts = [(await _count(paths, case, identifier))[1] for identifier in paths.refusals]
    assert len(set(counts)) == 1, f"{case.name}: refusal classes cost {counts}"


async def test_the_pinned_delta_is_not_vacuous(paths: Paths) -> None:
    """The 200 path really does more work than nothing at all.

    A delta of zero on every route would also be satisfied by a route that
    executed no statements whatsoever, so the absolute cost is pinned as a
    floor here rather than left implicit in the deltas above.
    """
    _, allowed = await _count(paths, READS[0], paths.readable)
    assert allowed > 3, f"a successful item read executed only {allowed} statements"


# --------------------------------------------------------------------------
# the page route
# --------------------------------------------------------------------------

#: The content mount only answers under its own Host.
PAGE_CONTENT_HOST: Final = "files.localhost:8000"

#: ``statements(200) - statements(404)`` for one request under a page grant.
#: Pinned rather than merely bounded: the refusal pays for the redemption, the
#: whole walk and the whole decision, and what it then skips is the payload — the
#: version row, the object open behind it, and the loads ``authorize`` does for a
#: node that is THERE (its chain, its drive, the grants on that chain) which its
#: absent case has nothing to do. A route that learned to bail early on "no such
#: name" collapses this number and fails here with both counts in the message.
#: 7 -> 8: the refusal no longer rolls the request's transaction back to record
#: its DENY (a pool of its own writes it), one statement fewer on the 404 only.
#: 8 -> 7: the count now watches that pool, so the DENY row's own INSERT is
#: counted on the 404. Neither path changed.
PAGE_DELTA: Final = 7


@pytest_asyncio.fixture
async def page_grant(
    client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    files_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AsyncClient, str]:
    """A folder holding one served page, and a live grant over it."""
    from alkera_core.config import settings

    origin = f"http://{PAGE_CONTENT_HOST}"
    monkeypatch.setattr(settings, "files_content_base_url", origin)

    drive = await fx.drive()
    await real_session.execute(
        text("UPDATE file_drives SET quota_bytes = :b, quota_nodes = :n WHERE id = :id"),
        {
            "b": settings.files_quota_default_bytes,
            "n": settings.files_quota_default_nodes,
            "id": drive.id,
        },
    )
    await real_session.commit()

    folder = await fx.node(b"report", kind="folder", parent=await fx.shared())
    page = await fx.node(b"index.html", parent=folder)
    caller = await login(client, files_org.org.admin_email, files_org.org.admin_password)
    body = b"<!doctype html>\n<p>counted</p>\n"
    written = await caller.put(
        f"{BASE}/drives/{drive.id}/items/{page.id}/content",
        content=body,
        headers={
            "Idempotency-Key": uuid.uuid4().hex,
            "If-Match": f'"{page.etag}"',
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(body)),
        },
    )
    assert written.status_code == 201, written.text

    minted = await caller.post(
        f"{BASE}/drives/{drive.id}/items/{page.id}/content-grants", json={"kind": "page"}
    )
    assert minted.status_code == 201, minted.text
    url = str(minted.json()["url"])
    return caller, url.removeprefix(f"{origin}/c/p/").partition("/")[0]


async def _page_probe(client: AsyncClient, token: str, path: str) -> Probe:
    """Drive one path under a grant twice, and count the second."""
    await client.get(f"/c/p/{token}/{path}", headers={"Host": PAGE_CONTENT_HOST})
    return await probe(
        client,
        SYNC_ENGINE,
        "GET",
        f"/c/p/{token}/{path}",
        headers={"Host": PAGE_CONTENT_HOST},
    )


async def test_a_page_refusal_costs_the_serve_minus_the_bytes(
    page_grant: tuple[AsyncClient, str],
) -> None:
    """A name under a page grant that resolves and one that does not cost alike.

    The page route is the one read whose *path* is attacker-chosen, so its
    refusal is the one a prober can drive cheaply and repeatedly. It redeems,
    walks, decides and only then declines to open the object — and what is left
    between the two answers is the payload, nothing else.
    """
    client, token = page_grant
    served = await _page_probe(client, token, "index.html")
    assert served.status == 200

    missing = await _page_probe(client, token, "nowhere.html")
    assert missing.status == 404
    assert served.statements - missing.statements == PAGE_DELTA, (
        f"a served page cost {served.statements} statements and a refused name "
        f"{missing.statements} (pinned {PAGE_DELTA})"
    )


# ---------------------------------------------------------------------------
# The holder's tree report costs statements per kind, never per entry
# ---------------------------------------------------------------------------


def _report(entries: list[dict[str, object]]) -> dict[str, object]:
    return {"batch_id": str(uuid.uuid4()), "entries": entries}


def _reported_file(path: str) -> dict[str, object]:
    return {"op": "upsert", "path": path, "kind": "file", "size": 1, "mtime_ns": 1}


async def test_a_tree_report_of_two_thousand_files_costs_what_one_file_costs(
    client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    files_on: None,
    fx: FilesFixtures,
) -> None:
    """A clone arrives as reports of two thousand entries, and every one of
    them must be the handful of grouped statements one entry is -- an INSERT
    for the rows, one for their history, one for the folder counts -- or the
    report the holder sends fastest is the one the drive answers slowest.
    Both reports here mint files in folders that did not exist, so the only
    thing that differs between them is how many entries they carry."""
    from _live_holder import MockHolder
    from alkera_core.config import settings

    drive = await fx.drive()
    drive.quota_bytes = settings.files_quota_default_bytes
    drive.quota_nodes = settings.files_quota_default_nodes
    await real_session.commit()
    project = await fx.node(b"project", kind="folder", parent=await fx.shared())
    caller = await login(client, files_org.org.admin_email, files_org.org.admin_password)
    holder = MockHolder(caller, drive.id, project.id)
    taken = await holder.take(
        real_session, lambda: {"Idempotency-Key": uuid.uuid4().hex}, purpose="mount", live=True
    )
    assert taken.status_code == 200, taken.text
    url = f"{BASE}/drives/{drive.id}/items/{project.id}/lease/tree"
    warm = await caller.post(
        url, json=_report([_reported_file("warm/a.txt")]), headers=holder.fence
    )
    assert warm.status_code == 200, warm.text

    revocation._cache.reset()
    one = await probe(
        caller,
        SYNC_ENGINE,
        "POST",
        url,
        json=_report([_reported_file("one/a.txt")]),
        headers=holder.fence,
    )
    revocation._cache.reset()
    many = await probe(
        caller,
        SYNC_ENGINE,
        "POST",
        url,
        json=_report([_reported_file(f"pkg{i % 10}/mod{i}.py") for i in range(2000)]),
        headers=holder.fence,
    )
    assert (one.status, many.status) == (200, 200), (one.body, many.body)
    assert many.statements == one.statements, (
        f"one entry cost {one.statements} statements and two thousand cost {many.statements}\n"
        f"{work_report([one, many], ['one', 'many'])}"
    )
