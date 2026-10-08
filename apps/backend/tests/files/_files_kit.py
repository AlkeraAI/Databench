"""Helpers the backend Files tests share.

A test never imports ``conftest`` by name: a bare module name resolves to
whichever ``conftest.py`` pytest imported into ``sys.modules`` first, so the
same import means different things depending on what else the session
collected. The shared classes live here instead, under a name that is unique
in the repository, and ``conftest.py`` builds its fixtures on them.
"""

from __future__ import annotations

import asyncio
import statistics
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.db.session import engine
from alkera_core.files import drives
from alkera_core.files.ids import OrgScope
from alkera_core.files.names import name_key
from alkera_core.files.path_labels import ino_label
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from alkera_core.models.user import User
from backend.services.files.context import ensure_store_row
from httpx import AsyncClient, Response
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin


@dataclass
class FilesOrgFixture:
    """One org with the three principals the isolation tests need."""

    org: OrgWithAdmin
    member: User
    member_password: str
    sub_team_id: uuid.UUID | None


class FilesFixtures:
    """Files rows for one org, built the way the product builds them.

    The drive comes from ``drives.ensure_org_drive`` rather than a hand-written
    row, so a test's tree hangs off the same skeleton a real first request
    creates.
    """

    def __init__(self, session: AsyncSession, org_team_id: uuid.UUID, actor_id: uuid.UUID) -> None:
        self._session = session
        self.org_team_id = org_team_id
        self.actor_id = actor_id
        self.repo = FilesRepo(session, OrgScope(org_team_id=org_team_id))
        self._drive: FileDrive | None = None

    async def drive(self) -> FileDrive:
        if self._drive is None:
            store = await ensure_store_row(self._session, settings)
            await self._session.commit()
            ctx = ActingContext.for_user(
                user_id=self.actor_id, org_id=self.org_team_id, email="fixture@test"
            )
            async with self.repo.transaction():
                self._drive = await drives.ensure_org_drive(
                    self.repo, ctx, self.org_team_id, store_id=store.id
                )
            await self._session.commit()
        return self._drive

    async def shared(self) -> FileNode:
        """``/Shared`` — the drive's top folder that actually takes children.

        The root, ``home/`` and ``Teams/`` are signposts: every direct write
        into one is refused, so a test that wants "a folder near the top of the
        drive" wants this one, not the root.
        """
        drive = await self.drive()
        assert drive.root_node_id is not None
        found = (
            await self._session.execute(
                select(FileNode).where(
                    FileNode.parent_id == drive.root_node_id,
                    FileNode.name == drives.SHARED_NAME,
                    FileNode.trashed_at.is_(None),
                )
            )
        ).scalar_one()
        return found

    async def home(self) -> FileNode:
        """The actor's own ``/home/<me>``, minted the way the drive mints it.

        A chat folder seeded raw at the drive root — or under ``/Shared`` — is
        reachable to the actor only through the org-admin floor, and the floor
        stops at a chat's folder. A test about a chat the actor OWNS seeds it
        here, where the home's default grant makes them its owner, which is
        what the product does when it files a chat.
        """
        drive = await self.drive()
        user = await self._session.get(User, self.actor_id)
        assert user is not None, self.actor_id
        ctx = ActingContext.for_user(
            user_id=self.actor_id, org_id=self.org_team_id, email=user.email
        )
        async with self.repo.transaction():
            home = await drives.ensure_home_folder(self.repo, ctx, self.actor_id)
        await self._session.commit()
        await self._session.refresh(drive)
        return home

    async def folder(self, node_id: uuid.UUID) -> FileNode:
        """The live row for a node a route made, for a test that seeds beside it."""
        loaded = await self._session.get(FileNode, node_id)
        assert loaded is not None, node_id
        return loaded

    async def node(
        self,
        name: bytes,
        *,
        kind: str = "file",
        parent: FileNode | None = None,
        **columns: Any,
    ) -> FileNode:
        """One child of ``parent`` (the drive root by default).

        Seeding a row under the root is a raw insert and stays allowed — it is
        how the live drive's own strays are reproduced. A *route* write there
        is refused, so a test whose node is then renamed, replaced or given a
        sibling by a route passes ``parent=await fx.shared()``.
        """
        drive = await self.drive()
        # `ensure_org_drive` allocated inos for the skeleton after this handle
        # cached the row, so ask the database what the next one really is.
        await self._session.refresh(drive)
        if parent is None:
            assert drive.root_node_id is not None
            loaded = await self._session.get(FileNode, drive.root_node_id)
            assert loaded is not None
            parent = loaded
        ino = drive.next_ino
        drive.next_ino += 1
        node = FileNode(
            id=uuid.uuid4(),
            ino=ino,
            drive_id=drive.id,
            org_team_id=self.org_team_id,
            parent_id=parent.id,
            kind=kind,
            name=name,
            name_display=name.decode("utf-8", "replace"),
            name_key=name_key(name),
            path_ids=f"{parent.path_ids}.{ino_label(ino)}",
            depth=parent.depth + 1,
            **columns,
        )
        self._session.add(node)
        await self._session.commit()
        return node

    async def version(self, node: FileNode, **columns: Any) -> FileVersion:
        defaults: dict[str, Any] = {
            "seq": 1,
            "size_bytes": 11,
            "content_hash": "ab" * 32,
            "mime_sniffed": "text/plain",
            "scan_state": "clean",
            "source": "upload",
        }
        defaults.update(columns)
        version = FileVersion(
            id=uuid.uuid4(),
            org_team_id=self.org_team_id,
            node_id=node.id,
            **defaults,
        )
        self._session.add(version)
        # Flushed before the node points at it: the node's foreign key would
        # otherwise be checked against a row this transaction has not written.
        await self._session.flush()
        node.head_version_id = version.id
        await self._session.commit()
        return version


async def node_etag(session: AsyncSession, node_id: uuid.UUID) -> str:
    """The etag the server currently issues for a node, as an ``If-Match``.

    Read from the database rather than from a handle a test is holding: a
    mutation the test already made moved the counter, and a stale handle would
    send the etag of a node state that no longer exists — which is a 412, not
    the case the test meant to write.
    """
    current = (
        await session.execute(select(FileNode.etag).where(FileNode.id == node_id))
    ).scalar_one()
    return str(current)


async def drive_root_etag(session: AsyncSession, drive: FileDrive) -> str:
    """The etag of the drive's root, for a mutation whose resource is the drive
    itself (an operation) rather than one node under it."""
    assert drive.root_node_id is not None
    return await node_etag(session, drive.root_node_id)


# -- the delta feed's own completion signal ---------------------------------


#: How long a caught-up read waits before asking the feed again. Not a deadline
#: and not a retry budget: the wait below never gives up. It only yields to the
#: transactions that are holding a row back instead of spinning the app.
_FEED_POLL_SECONDS = 0.02


def delta_url(drive_id: uuid.UUID, token: str | None = None) -> str:
    """The feed's URL for one drive. No token means the whole retained window."""
    url = f"/api/v1/files/drives/{drive_id}/delta"
    return url if token is None else f"{url}?token={token}"


async def delta_until(
    client: AsyncClient,
    drive_id: uuid.UUID,
    *,
    carries: str,
    token: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Read the feed from ``token`` until it has delivered node ``carries``.

    A committed outbox row is not yet a delivered one. The feed withholds every
    row whose transaction is not strictly older than the oldest write
    transaction still in flight *in this database* — the rule that stops a row
    whose ``bigserial`` id was allocated early but committed late from being
    skipped forever — and the writers holding that boundary down need not be
    this test's own: any transaction anywhere in the database counts, so under
    load a page that does not carry the change yet is a correct answer rather
    than a failed one. ``?token=latest`` mints its cursor *below* anything still
    behind the boundary for the same reason, so a single read can also come back
    with the change beside a setup row the cursor was minted underneath.

    So a test asks the feed instead of guessing: pages are read until the node
    it is waiting for has arrived AND the feed says it is caught up, and the
    items are folded latest-state-by-id exactly as one page already is. What
    comes back is that fold and the link to resume from. The wait is on the
    feed's own delivery, never on a clock — nothing here expires, so a node that
    truly never arrives is the session timeout's business rather than a deadline
    this helper invented and would have to keep tuning.
    """
    seen: dict[str, dict[str, Any]] = {}
    link = token
    while True:
        response = await client.get(delta_url(drive_id, link))
        assert response.status_code == 200, response.text
        body = response.json()
        for item in body["items"]:
            seen[item["id"]] = item
        more: str | None = body.get("nextLink")
        link = more if more is not None else body["deltaLink"]
        if more is not None:
            continue
        if carries in seen:
            assert link is not None
            return list(seen.values()), link
        await asyncio.sleep(_FEED_POLL_SECONDS)


# -- the benchmark harness the perf budget tests share -----------------------
#
# A budget row is two claims, and this machinery serves both: a p95 over
# repeated real HTTP requests through the app, and the number of SQL statements
# one such request costs. The statement count is the claim that actually
# catches regressions on a laptop — latency drifts with the machine, but a page
# that starts issuing a query per row does so on every machine.


#: Timed repetitions per row. A budget is a p95, and twenty samples put the
#: 95th percentile on the second-slowest observation — enough to catch a
#: regression, cheap enough to run on every PR.
REPEATS = 20


def timed_repeats(claim: str, *, most: int = REPEATS) -> int:
    """How many times a row asserting ``claim`` has to be timed.

    Not at all, when the claim is the statement count: that number comes from
    the one counted call, and the repetitions before it produce a p95 nothing
    reads. They are not free — a row whose operation drops a whole folder of
    files or moves a whole subtree spends minutes on them — so a row that is not
    going to state a latency does not pay for one. The p95 half still takes every
    repetition, which is the only claim that needs a distribution.

    This is the one place the rule lives: :class:`Bench` derives both its repeat
    count and its ``timed`` flag from it, and ``most`` caps the repetitions for a
    row whose every repetition re-posts a whole drop.
    """
    if claim == "wallclock":
        return most
    if claim != "statements":
        raise ValueError(f"unknown budget claim {claim!r}")
    return 0


#: Statements that carry no work and so are not counted against a budget.
_CONTROL = frozenset({"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"})

#: The one statement a request issues on a schedule the clock keeps rather than
#: on anything the request asked for: the session-revocation probe
#: (``alkera_core.auth.revocation``), whose "this token is not revoked" answer
#: is cached in process for five seconds. Two identical requests therefore cost
#: a different number of statements purely because more than five seconds
#: elapsed between them — which is what made a page over a hundred thousand
#: children look like it cost one statement more than the same page over two
#: hundred: seeding the large folder, and paging it, simply took longer than the
#: cache's lifetime. Excluded for the same reason ``_CONTROL`` is: a budget that
#: counted it would report how long the run took, not what the page asked the
#: database for. Matched on its exact shape, so anything else touching
#: ``auth_tokens`` — let alone a per-row query anywhere — is still counted.
_CLOCK_DRIVEN = ("FROM auth_tokens WHERE auth_tokens.jti", "revoked_at IS NOT NULL")


def _is_clock_driven(statement: str) -> bool:
    flattened = " ".join(statement.split())
    return all(fragment in flattened for fragment in _CLOCK_DRIVEN)


@contextmanager
def counting() -> Iterator[list[str]]:
    """Every statement the database actually executed while the block ran.

    ``before_cursor_execute`` fires once per statement sent to the server, so
    this counts the work a request caused rather than the ORM calls the code
    made — which is the difference between a page that joins and a page that
    queries per row.

    Two kinds of statement are left out: transaction control, and the
    clock-driven revocation probe described at :data:`_CLOCK_DRIVEN`. Neither
    is work the request asked for, and counting either makes the budget a
    function of how the session is scoped or of how long the run took.
    """
    seen: list[str] = []

    def record(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        # Transaction control is not work the budget is about: a route that
        # opens and commits is not doing a query per row, and counting BEGIN
        # would make the budget hostage to how the session is scoped.
        if statement.strip().split(" ", 1)[0].upper() in _CONTROL:
            return
        if _is_clock_driven(statement):
            return
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


@dataclass(slots=True)
class Sample:
    """What one benchmarked row observed."""

    label: str
    durations_ms: list[float] = field(default_factory=list)
    statements: int = 0

    @property
    def timed(self) -> bool:
        """Whether this sample carries latencies at all.

        A row measured for its statement count alone never runs the timed
        repetitions, so its percentiles have nothing behind them: a caller
        says there is no latency rather than printing a number it invented.
        """
        return bool(self.durations_ms)

    @property
    def p95_ms(self) -> float:
        """The 95th percentile, or NaN when the row was never timed.

        NaN rather than an exception because it is the answer that fails
        closed: every comparison against it is False, so an assertion that
        mistook an untimed row for a fast one fails instead of passing.
        """
        if not self.durations_ms:
            return float("nan")
        ordered = sorted(self.durations_ms)
        index = max(0, min(len(ordered) - 1, round(0.95 * (len(ordered) - 1))))
        return ordered[index]

    @property
    def median_ms(self) -> float:
        if not self.durations_ms:
            return float("nan")
        return statistics.median(self.durations_ms)


class Bench:
    """Runs one route call repeatedly and reports what it cost.

    ``call`` is handed the iteration number so a mutating row can aim each
    repetition at its own target: renaming the same node twenty times would
    measure twenty different etag states, not the same operation twenty times.

    ``claim`` says which half of a budget row this instance is serving, and it
    decides how much work :meth:`measure` does — the repeat count comes from
    :func:`timed_repeats`, so a caller states the claim once, when it builds
    the bench, and never the repetitions. The timed repetitions exist
    only to produce a percentile; a row asserting its statement count never
    reads one, so running them there is twenty extra round trips per label
    whose only product is discarded — and on the heaviest rows those round
    trips are not cheap (the move row relocates a ten-thousand-node subtree
    per repetition). Under the gate's fan-out that is what turned a statement
    budget into a detector of how busy the runner was. A statement claim
    therefore pays the untimed warm-up and the one counted call, and nothing
    else.
    """

    def __init__(self, multiplier: float, *, claim: str = "wallclock") -> None:
        self.multiplier = multiplier
        self.claim = claim
        self.samples: list[Sample] = []

    @property
    def timed(self) -> bool:
        """Whether this run's claim is the one the latencies are for."""
        return timed_repeats(self.claim) > 0

    async def measure(
        self,
        label: str,
        call: Callable[[int], Awaitable[Response]],
        *,
        expect: int | tuple[int, ...] = 200,
    ) -> Sample:
        """Warm up, time the repetitions this bench's claim asks for, then
        count what one more call costs.

        How many that is comes from :func:`timed_repeats`: a row asserting
        only a statement count runs the warm-up and the one counted call, and
        nothing else, because the count is read off that call alone.
        """
        ok = (expect,) if isinstance(expect, int) else expect
        sample = Sample(label=label)
        # One untimed warm-up: the first request through the app pays for
        # connection checkout and statement preparation, which is a cost of the
        # test process rather than of the operation being budgeted.
        first = await call(0)
        assert first.status_code in ok, f"{label}: {first.status_code} {first.text[:200]}"
        for iteration in range(1, timed_repeats(self.claim) + 1):
            started = time.perf_counter()
            response = await call(iteration)
            elapsed = (time.perf_counter() - started) * 1000.0
            assert response.status_code in ok, (
                f"{label}: {response.status_code} {response.text[:200]}"
            )
            sample.durations_ms.append(elapsed)
        # Always the same iteration number, timed or not, so a mutating row's
        # counted call aims at the same fresh target in both halves — the
        # twenty-second created node, the twenty-second home. The iteration is
        # what is pinned, not the state behind it: the statement half arrives
        # there having made one call and the wall-clock half twenty-one, so a
        # count sensitive to the row count at small N could still differ. At
        # these fixture sizes (~10^5 nodes) twenty rows is noise.
        with counting() as seen:
            response = await call(REPEATS + 1)
            assert response.status_code in ok
        sample.statements = len(seen)
        self.samples.append(sample)
        return sample

    def report(self, sample: Sample, *, budget_ms: float, statements: int) -> None:
        """Print the row with its measured p95 (the number is printed even on a
        pass) and then assert both halves of its budget."""
        allowed = budget_ms * self.multiplier
        print(
            f"\n[files-perf] {sample.label}: "
            f"p95={sample.p95_ms:.1f}ms median={sample.median_ms:.1f}ms "
            f"budget={budget_ms:.0f}ms allowed={allowed:.0f}ms "
            f"statements={sample.statements} (max {statements})"
        )
        assert sample.statements <= statements, (
            f"{sample.label} used {sample.statements} statements, budget {statements}"
        )
        if not self.timed:
            # A statement claim has no latency to assert: the count above is
            # the whole of its budget.
            return
        # A wall-clock claim holding an untimed sample is a bench built for the
        # wrong claim. Returning here instead would leave the latency budget
        # silently unasserted, which is the failure this whole split exists to
        # make impossible.
        assert sample.timed, (
            f"{sample.label} asserts a p95 but the row was not timed — the "
            "bench was built for the wrong claim"
        )
        assert sample.p95_ms <= allowed, (
            f"{sample.label} p95 {sample.p95_ms:.1f}ms over {allowed:.0f}ms"
        )


#: The refusal every "not yours" answer carries, as the error envelope says it
#: (``refusal`` drops the per-request trace id). A node that is not there, one
#: in another org and one the caller may not read all answer exactly this.
NOT_FOUND: dict[str, Any] = {
    "type": "urn:alkera:error:not_found",
    "code": "not_found",
    "status": 404,
    "message": "Not found.",
}


def refusal(response: Any) -> dict[str, Any]:
    """A refused response's error envelope without its trace id, which is the
    one member that differs from request to request."""
    error = dict(response.json()["error"])
    error.pop("trace_id", None)
    return error
