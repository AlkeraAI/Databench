"""Quota holds, driven against their reference model on real Postgres.

The model is three numbers and a dict of holds; it never asks the service what
to expect. Every rule applies the same operation to both sides and asserts that
the service refuses exactly when the contract refuses — an accepted reserve that
the model says does not fit is a double spend, and a refused one that the model
says fits is a drive that lost room it owns. After every step the invariants
recompute ``Σ holds == Σ open sessions`` and ``used + held <= quota`` from the
rows themselves. The clock is injected, so expiry is crossed by advancing it
rather than by sleeping.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from alkera_core.files.errors import QuotaExceeded
from alkera_core.files.ids import SessionId
from alkera_core.files.quota import QuotaService, Usage
from alkera_core.files.uploads import UploadCompletion
from alkera_core.models.files.uploads import FileUploadSession
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule
from hypothesis.strategies import integers
from sqlalchemy import select, update
from tests.files.stateful._models import ModelRefusal, QuotaModel
from tests.files.stateful.conftest import StatefulRig, acting_context, open_rig, refusal_code

QUOTA_BYTES = 1_000
QUOTA_NODES = 20
TTL = timedelta(minutes=30)
#: The states in which a session still owns its hold.
LIVE_STATES = ("open", "uploading", "committing")


class QuotaReferenceModel(RuleBasedStateMachine):
    """Contract-equivalence for reserve / release / reconcile / expire."""

    def __init__(self) -> None:
        super().__init__()
        self.rig: StatefulRig = open_rig(
            label="quota-model", quota_bytes=QUOTA_BYTES, quota_nodes=QUOTA_NODES
        )
        self.model = QuotaModel(quota_bytes=QUOTA_BYTES, quota_nodes=QUOTA_NODES)
        self.expiries: dict[str, datetime] = {}
        self.ids: dict[str, uuid.UUID] = {}
        # Model handles are minted from a counter, never from the session's
        # uuid. A rule picks a hold by index into ``sorted(self.model.holds)``,
        # so random names make that index address a *different* hold each time
        # the same steps are replayed: a different size is released, a later
        # reserve stops fitting, ``holds`` empties on one run and not the other,
        # and the preconditions below flip — which is a rule-selection draw of a
        # different shape and the FlakyStrategyDefinition Hypothesis reports.
        self.minted = 0

    @initialize()
    def start(self) -> None:
        self.quota = QuotaService(self.rig.repo, acting_context(), self.rig.clock, None)
        # The production sweep: the sessions here stage no parts, so it never
        # reaches the store.
        self.sweeper = UploadCompletion(self.rig.repo, acting_context(), self.rig.clock, None)

    # ---- rules -----------------------------------------------------------

    @rule(size=integers(min_value=0, max_value=600), nodes=integers(min_value=0, max_value=4))
    def open_session(self, size: int, nodes: int) -> None:
        """Open a session and reserve for it; the model decides whether it fits."""
        session_id = uuid.uuid4()
        self.minted += 1
        handle = f"s{self.minted:04d}"
        fits = self.model.fits(size=size, nodes=nodes)
        outcome = self.rig.run(self._open(session_id, size, nodes))
        if fits:
            assert outcome is None, (
                f"{size}B/{nodes} nodes fit under {QUOTA_BYTES}/{QUOTA_NODES} "
                f"but the service refused with {outcome}"
            )
            self.model.reserve(handle, size=size, nodes=nodes)
            self.ids[handle] = session_id
            self.expiries[handle] = self.rig.clock.now() + TTL
            return
        try:
            self.model.reserve(handle, size=size, nodes=nodes)
        except ModelRefusal as refusal:
            assert outcome == refusal.code, (
                f"the service refused with {outcome}; the contract requires {refusal.code}"
            )
            return
        raise AssertionError("the model both refused and accepted the same reserve")

    async def _open(self, session_id: uuid.UUID, size: int, nodes: int) -> str | None:
        """Return ``None`` when the reserve was accepted, else its refusal code."""
        async with self.rig.repo.transaction():
            row = FileUploadSession(
                id=session_id,
                org_team_id=self.rig.org.org_team_id,
                drive_id=self.rig.drive_uuid,
                parent_id=self.rig.root_id,
                name=b"payload.bin",
                dedup_domain_id=self.rig.dedup_domain_uuid,
                state="open",
                declared_size=size,
                expires_at=self.rig.clock.now() + TTL,
            )
            await self.rig.repo.add(row)
            await self.rig.repo.flush()
            try:
                await self.quota.reserve(
                    self.rig.drive_id, bytes=size, nodes=nodes, session_id=SessionId(session_id)
                )
            except QuotaExceeded as refused:
                # A refused open leaves no session behind, so it can hold nothing.
                await self.rig.repo.session.execute(
                    update(FileUploadSession.__table__)
                    .where(FileUploadSession.id == session_id)
                    .values(state="aborted")
                )
                return refusal_code(refused)
            await self.rig.repo.session.execute(
                update(FileUploadSession.__table__)
                .where(FileUploadSession.id == session_id)
                .values(quota_hold_bytes=size, quota_hold_nodes=nodes)
            )
            return None

    @precondition(lambda self: bool(self.model.holds))
    @rule(pick=integers(min_value=0))
    def abort_session(self, pick: int) -> None:
        handle = sorted(self.model.holds)[pick % len(self.model.holds)]
        self.rig.run(self._abort(self.ids[handle]))
        self.model.release(handle)
        self.expiries.pop(handle, None)

    async def _abort(self, session_id: uuid.UUID) -> None:
        async with self.rig.repo.transaction():
            await self.quota.release(SessionId(session_id))
            await self.rig.repo.session.execute(
                update(FileUploadSession.__table__)
                .where(FileUploadSession.id == session_id)
                .values(state="aborted")
            )

    @precondition(lambda self: bool(self.model.holds))
    @rule(pick=integers(min_value=0), shrink=integers(min_value=0, max_value=200))
    def complete_session(self, pick: int, shrink: int) -> None:
        """Commit a session for at most what it held — the hold becomes usage."""
        handle = sorted(self.model.holds)[pick % len(self.model.holds)]
        held_bytes, held_nodes = self.model.holds[handle]
        actual_bytes = max(0, held_bytes - shrink)
        self.rig.run(self._complete(self.ids[handle], actual_bytes, held_nodes))
        self.model.reconcile(handle, actual_bytes=actual_bytes, actual_nodes=held_nodes)
        self.expiries.pop(handle, None)

    async def _complete(self, session_id: uuid.UUID, size: int, nodes: int) -> None:
        async with self.rig.repo.transaction():
            await self.quota.reconcile(SessionId(session_id), actual_bytes=size, actual_nodes=nodes)
            await self.rig.repo.session.execute(
                update(FileUploadSession.__table__)
                .where(FileUploadSession.id == session_id)
                .values(state="done")
            )

    @rule(minutes=integers(min_value=1, max_value=40))
    def advance_and_sweep(self, minutes: int) -> None:
        """Cross the TTL boundary on the injected clock, then sweep."""
        self.rig.clock.advance(timedelta(minutes=minutes))
        now = self.rig.clock.now()
        due = {handle for handle, deadline in self.expiries.items() if deadline <= now}
        swept = self.rig.run(self._sweep(now))
        assert swept == len(due), (
            f"the sweeper expired {swept} sessions; the contract says {len(due)} were past TTL"
        )
        self.model.expire(due)
        for handle in due:
            del self.expiries[handle]

    async def _sweep(self, now: datetime) -> int:
        return await self.sweeper.sweep_expired(now)

    # ---- invariants ------------------------------------------------------

    @invariant()
    def holds_equal_open_sessions(self) -> None:
        rows = self.rig.run(self._read_holds())
        expected = {self.ids[handle].hex: hold for handle, hold in self.model.holds.items()}
        assert rows == expected, f"holds diverged: rows {rows} vs model {expected}"

    @invariant()
    def usage_matches_and_stays_within_quota(self) -> None:
        usage = self.rig.run(self._read_usage())
        assert (usage.bytes, usage.nodes) == (self.model.used_bytes, self.model.used_nodes), (
            f"usage {usage.bytes}/{usage.nodes} vs model "
            f"{self.model.used_bytes}/{self.model.used_nodes}"
        )
        assert (usage.held_bytes, usage.held_nodes) == (
            self.model.held_bytes,
            self.model.held_nodes,
        )
        assert usage.bytes + usage.held_bytes <= usage.quota_bytes
        assert usage.nodes + usage.held_nodes <= usage.quota_nodes

    async def _read_holds(self) -> dict[str, tuple[int, int]]:
        result = await self.rig.session.execute(
            select(
                FileUploadSession.id,
                FileUploadSession.state,
                FileUploadSession.quota_hold_bytes,
                FileUploadSession.quota_hold_nodes,
            ).where(FileUploadSession.drive_id == self.rig.drive_uuid)
        )
        live: dict[str, tuple[int, int]] = {}
        for session_id, state, held_bytes, held_nodes in result.all():
            if state in LIVE_STATES:
                live[session_id.hex] = (held_bytes, held_nodes)
            else:
                assert (held_bytes, held_nodes) == (0, 0), (
                    f"a {state} session still holds {held_bytes}B/{held_nodes}"
                )
        return live

    async def _read_usage(self) -> Usage:
        async with self.rig.repo.transaction():
            return await self.quota.usage(self.rig.drive_id)

    def teardown(self) -> None:
        self.rig.close()


TestQuotaReferenceModel = QuotaReferenceModel.TestCase
TestQuotaReferenceModel.settings = hypothesis_settings(  # type: ignore[attr-defined]
    max_examples=30,
    stateful_step_count=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    print_blob=True,
)
