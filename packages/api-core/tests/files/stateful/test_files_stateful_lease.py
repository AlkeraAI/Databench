"""Folder leases, driven against their reference model on real Postgres.

The model is one holder and one epoch per node; it never asks the service what
to expect. Every rule applies the same operation to both sides and asserts the
service refuses exactly when the contract refuses — and with the *same* code,
because ``files.leased`` ("someone else has this folder") and
``files.lease_fenced`` ("you had it and no longer do") are different answers a
client acts on differently. After every step the invariants re-read the rows:
at most one live holder per node, no two live leases on one path, and an epoch
that never goes backwards for a node.

The deadline lives in Postgres ``now()`` rather than in the injected clock, so
the ``expire`` rule ages the row the way wall time would — never a sleep.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from alkera_core.files import errors
from alkera_core.files.ids import NodeId
from alkera_core.files.leases import LeaseService, fenced_write_for
from alkera_core.files.namespace import Namespace
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule
from hypothesis.strategies import booleans, integers, sampled_from
from sqlalchemy import text
from tests.files._kit.fencing import held_by
from tests.files.stateful._models import LeaseModel, ModelRefusal
from tests.files.stateful.conftest import StatefulRig, open_rig, refusal_code

#: The tree every run builds: two branches, one nested, so an overlap refusal
#: has both an ancestor case and a descendant case to find.
TREE: tuple[tuple[str, str], ...] = (("a", "root"), ("b", "a"), ("c", "root"))

#: Two machines belonging to two people. Same principal on two instances is the
#: case that must still be refused; different principals nested is the one the
#: overlap rule is allowed to permit.
HOLDERS: tuple[str, ...] = ("h0", "h1")

#: The grant delay the machine runs the service at. The delay is wall-clock and
#: the model has no clock, so "lapsed a second ago" has to still be inside it
#: however slowly Hypothesis schedules the steps that follow. Five minutes is
#: that, with room to spare; what the machine pins is the delay's rule, not its
#: length — the committed default is asserted directly in the leases cases.
GRANT_DELAY = timedelta(minutes=5)

#: How far back the ``expire`` rule ages the deadline for each of its two
#: cases: well inside ``GRANT_DELAY``, and well past it.
LAPSE_INTERVALS: dict[bool, str] = {False: "1 second", True: "1 hour"}


def _ctx_for(rig: StatefulRig, holder: str) -> Any:
    from alkera_core.authz.enums import CredentialKind, PrincipalKind
    from alkera_core.authz.principal import ActingContext, Principal

    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(rig.org.admin_id if holder == "h0" else rig.org.member_id),
            org_id=rig.org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


class LeaseReferenceModel(RuleBasedStateMachine):
    """Contract-equivalence for acquire / heartbeat / expire / release / force."""

    def __init__(self) -> None:
        super().__init__()
        self.rig: StatefulRig = open_rig(label="lease-model")
        self.model = LeaseModel()
        self.ids: dict[str, uuid.UUID] = {}
        #: Every epoch the service has ever handed out for a node, in order.
        self.issued: dict[str, list[int]] = {}

    @initialize()
    def build_tree(self) -> None:
        self.ids["root"] = self.rig.root_id
        self.model.parents["root"] = None
        for name, parent in TREE:
            made = self.rig.run(self._create(parent, name))
            self.ids[name] = made
            self.model.parents[name] = parent

    async def _create(self, parent: str, name: str) -> uuid.UUID:
        async with self.rig.repo.transaction():
            namespace = Namespace(self.rig.repo, _ctx_for(self.rig, "h0"), self.rig.clock, None)
            node = await namespace.create(
                self.rig.drive_id, NodeId(self.ids[parent]), "folder", name.encode()
            )
            return node.id

    # ---- the service under test ------------------------------------------

    def _leases(self, holder: str) -> LeaseService:
        return LeaseService(
            self.rig.repo,
            _ctx_for(self.rig, holder),
            self.rig.clock,
            grant_delay=GRANT_DELAY,
        )

    def _nodes(self) -> list[str]:
        return sorted(self.model.parents)

    def _held(self) -> list[str]:
        return sorted(self.model.live_leases())

    def _expect(self, call: Any, code: str) -> None:
        try:
            call()
        except errors.FilesError as refused:
            assert refusal_code(refused) == code, (
                f"the service refused with {refusal_code(refused)}; the contract requires {code}"
            )
            return
        raise AssertionError(f"the service accepted what the contract refuses with {code}")

    # ---- rules -----------------------------------------------------------

    @rule(pick=integers(min_value=0), holder=sampled_from(HOLDERS), instance=integers(0, 1))
    def acquire(self, pick: int, holder: str, instance: int) -> None:
        nodes = self._nodes()
        node = nodes[pick % len(nodes)]
        instance_id = f"{holder}-{instance}"

        def apply() -> Any:
            return self.rig.run(self._acquire(node, holder, instance_id))

        try:
            _epoch, resumed = self.model.acquire(node, instance_id, holder)
        except ModelRefusal as refusal:
            self._expect(apply, refusal.code)
            return
        lease = apply()
        seen = self.issued.setdefault(node, [])
        if resumed:
            # The mount that came back keeps the epoch it was already writing
            # under, so this grant is not a new one and does not join the run.
            assert lease.epoch == seen[-1], (
                f"the resumed lease on {node} came back at epoch {lease.epoch}; "
                f"it was writing under {seen[-1]}"
            )
        else:
            assert not seen or lease.epoch > seen[-1], (
                f"epoch {lease.epoch} on {node} is not above the {seen[-1]} already issued"
            )
            seen.append(lease.epoch)
        # The floor an epoch starts from is the platform's restore generation,
        # which the model has no business knowing; what it pins is that the
        # holder, the instance and the monotonicity are the contract's.
        self.model.leases[node] = replace(self.model.leases[node], epoch=lease.epoch)
        self.model.hwm[node] = lease.epoch

    async def _acquire(self, node: str, holder: str, instance_id: str) -> Any:
        async with self.rig.repo.transaction():
            return await self._leases(holder).acquire(
                NodeId(self.ids[node]), instance_id=instance_id, machine_id=f"m-{instance_id}"
            )

    @precondition(lambda self: bool(self._held()))
    @rule(pick=integers(min_value=0), wrong_epoch=booleans(), caller=sampled_from(HOLDERS))
    def heartbeat(self, pick: int, wrong_epoch: bool, caller: str) -> None:
        """A beat, from a caller sampled independently of who holds the folder.

        The route decides a beat on the rung a plain writer holds, and the pair
        it carries is derivable from what the product serves — so half of these
        are a colleague keeping somebody else's lease alive, which is the case
        the holder half of the fence exists for.
        """
        held = self._held()
        node = held[pick % len(held)]
        lease = self.model.leases[node]
        epoch = lease.epoch + (1 if wrong_epoch else 0)

        def apply() -> Any:
            return self.rig.run(self._heartbeat(node, lease.instance, epoch, caller))

        try:
            forced = self.model.heartbeat(node, epoch, lease.instance, caller)
        except ModelRefusal as refusal:
            self._expect(apply, refusal.code)
            return
        beat = apply()
        assert beat.forced == forced, (
            f"the service reports forced={beat.forced}; the contract says {forced}"
        )
        assert beat.epoch == lease.epoch

    async def _heartbeat(self, node: str, instance_id: str, epoch: int, holder: str) -> Any:
        async with self.rig.repo.transaction():
            return await self._leases(holder).heartbeat(
                NodeId(self.ids[node]), epoch=epoch, instance_id=instance_id
            )

    @precondition(lambda self: bool(self._held()))
    @rule(pick=integers(min_value=0), wrong_instance=booleans(), caller=sampled_from(HOLDERS))
    def release(self, pick: int, wrong_instance: bool, caller: str) -> None:
        """A hand-back, from a caller sampled independently of the holder.

        The dangerous half: a release ends a live lease with no grace and makes
        the folder re-grantable at once, and the route decides it one rung below
        the manager verb the product reserves for taking a folder away. So the
        model draws a caller the way ``write`` does, and a run that let a
        stranger's release through fails on the very next invariant.
        """
        held = self._held()
        node = held[pick % len(held)]
        lease = self.model.leases[node]
        instance_id = lease.instance + ("x" if wrong_instance else "")

        def apply() -> Any:
            return self.rig.run(self._release(node, instance_id, lease.epoch, caller))

        try:
            self.model.release(node, lease.epoch, instance_id, caller)
        except ModelRefusal as refusal:
            self._expect(apply, refusal.code)
            return
        apply()

    async def _release(self, node: str, instance_id: str, epoch: int, holder: str) -> None:
        async with self.rig.repo.transaction():
            await self._leases(holder).release(
                NodeId(self.ids[node]), epoch=epoch, instance_id=instance_id
            )

    @precondition(lambda self: bool(self._held()))
    @rule(pick=integers(min_value=0))
    def force(self, pick: int) -> None:
        held = self._held()
        node = held[pick % len(held)]
        self.rig.run(self._force(node))
        self.model.force(node)

    async def _force(self, node: str) -> None:
        async with self.rig.repo.transaction():
            await self._leases("h0").force_release(NodeId(self.ids[node]))

    @precondition(lambda self: bool(self._held()))
    @rule(pick=integers(min_value=0), past_delay=booleans())
    def expire(self, pick: int, past_delay: bool) -> None:
        """The deadline passes with the holder still believing it holds the lease.

        ``past_delay`` picks which side of the grant delay the lapse falls on,
        because they are two different answers: inside it the folder is still
        the lapsed holder's and no one else may take it, past it the next asker
        gets it. Both are drawn, so a service that dropped the delay entirely —
        or held one forever — diverges here either way.
        """
        held = self._held()
        node = held[pick % len(held)]
        self.rig.run(self._age(node, past_delay))
        self.model.expire(node, past_delay=past_delay)

    async def _age(self, node: str, past_delay: bool = False) -> None:
        ago = LAPSE_INTERVALS[past_delay]
        async with self.rig.repo.transaction():
            await self.rig.repo.session.execute(
                text(
                    "UPDATE file_leases "
                    f"SET expires_at = now() - interval '{ago}', "
                    f"grantable_after = now() - interval '{ago}' "
                    "WHERE node_id = :n"
                ),
                {"n": self.ids[node]},
            )

    @rule(
        pick=integers(min_value=0),
        claim=sampled_from(("none", "held", "stale")),
        writer=sampled_from(HOLDERS),
    )
    def write(self, pick: int, claim: str, writer: str) -> None:
        """A write into the tree, with the lease the caller believes it holds.

        ``writer`` is sampled independently of who holds the folder, so half
        the ``held`` claims are a colleague replaying a pair they can read off
        the product — the case the fence's holder half exists for.
        """
        nodes = self._nodes()
        node = nodes[pick % len(nodes)]
        covering = self.model.covering(node)
        epoch: int | None = None
        instance: str | None = None
        if claim != "none" and covering is not None:
            _, lease = covering
            epoch = lease.epoch + (1 if claim == "stale" else 0)
            instance = lease.instance
        elif claim != "none":
            epoch, instance = 1, "ghost"

        def apply() -> Any:
            return self.rig.run(self._write(node, epoch, instance, writer))

        try:
            self.model.write(node, epoch, instance, writer)
        except ModelRefusal as refusal:
            self._expect(apply, refusal.code)
            return
        apply()

    async def _write(self, node: str, epoch: int | None, instance: str | None, writer: str) -> None:
        async with self.rig.repo.transaction():
            target = await self.rig.repo.node(NodeId(self.ids[node]))
            assert target is not None
            context = (
                None if epoch is None else held_by(_ctx_for(self.rig, writer), epoch, instance)
            )
            await fenced_write_for(self.rig.repo, target, context)

    # ---- invariants ------------------------------------------------------

    @invariant()
    def one_live_holder_per_node(self) -> None:
        rows = self.rig.run(self._live_rows())
        live = {str(row.node_id): (row.epoch, row.holder_instance_id) for row in rows}
        expected = {
            str(self.ids[node]): (lease.epoch, lease.instance)
            for node, lease in self.model.live_leases().items()
        }
        assert live == expected, f"the rows hold {live}; the contract says {expected}"

    async def _live_rows(self) -> Any:
        async with self.rig.repo.transaction():
            return (
                await self.rig.repo.session.execute(
                    text(
                        "SELECT node_id, epoch, holder_instance_id FROM file_leases "
                        "WHERE org_team_id = :org AND released_at IS NULL "
                        "AND expires_at > now()"
                    ),
                    {"org": self.rig.org.org_team_id},
                )
            ).all()

    @invariant()
    def no_two_live_leases_on_one_path(self) -> None:
        live = self.model.live_leases()
        for node in live:
            for other in live:
                if other == node:
                    continue
                overlapping = other in self.model.descendants(
                    node
                ) or node in self.model.descendants(other)
                if not overlapping:
                    continue
                deeper = (
                    node if len(self.model.chain(node)) > len(self.model.chain(other)) else other
                )
                shallower = other if deeper == node else node
                assert live[deeper].principal == live[shallower].principal, (
                    f"{deeper} and {shallower} are on one path and held by two principals"
                )

    @invariant()
    def epochs_never_go_backwards(self) -> None:
        for node, seen in self.issued.items():
            assert seen == sorted(seen), f"epochs on {node} came back out of order: {seen}"
            assert len(set(seen)) == len(seen), f"an epoch on {node} was reissued: {seen}"

    def teardown(self) -> None:
        self.rig.close()


LeaseReferenceModel.TestCase.settings = hypothesis_settings(
    max_examples=8,
    stateful_step_count=14,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

TestLeaseReferenceModel = LeaseReferenceModel.TestCase


async def _make_folder(rig: StatefulRig, parent: uuid.UUID, name: str) -> uuid.UUID:
    async with rig.repo.transaction():
        namespace = Namespace(rig.repo, _ctx_for(rig, "h0"), rig.clock, None)
        node = await namespace.create(rig.drive_id, NodeId(parent), "folder", name.encode())
        return node.id


def _refused(call: Any, code: str) -> None:
    try:
        call()
    except errors.FilesError as refusal:
        assert refusal_code(refusal) == code, (
            f"the service refused with {refusal_code(refusal)}; the contract requires {code}"
        )
        return
    raise AssertionError(f"the service accepted what the contract refuses with {code}")


def test_resume_replay_keeps_the_epoch_until_the_folder_is_forced() -> None:
    """The same-instance resume, replayed step by step against both sides.

    The machine reaches this sequence only when its draws line up, so it is
    pinned here to run on every push: a killed mount comes back to the epoch it
    was already writing under, a second instance of the same person does not
    get it, a live lease under the folder still blocks the resume, a forced
    lease is nobody's to resume while the grace lasts, and the epoch handed out
    after the grace runs out is above the one that was fenced.
    """
    rig = open_rig(label="lease-resume-replay")
    try:
        model = LeaseModel()
        model.parents["root"] = None
        ids: dict[str, uuid.UUID] = {"root": rig.root_id}
        for name, parent in TREE:
            ids[name] = rig.run(_make_folder(rig, ids[parent], name))
            model.parents[name] = parent

        def service(holder: str) -> LeaseService:
            return LeaseService(rig.repo, _ctx_for(rig, holder), rig.clock)

        async def _acquire(node: str, holder: str, instance: str) -> Any:
            async with rig.repo.transaction():
                return await service(holder).acquire(
                    NodeId(ids[node]), instance_id=instance, machine_id=f"m-{instance}"
                )

        def acquire(node: str, holder: str, instance: str) -> Any:
            return rig.run(_acquire(node, holder, instance))

        async def _heartbeat(node: str, holder: str, instance: str, epoch: int) -> Any:
            async with rig.repo.transaction():
                return await service(holder).heartbeat(
                    NodeId(ids[node]), epoch=epoch, instance_id=instance
                )

        async def _force(node: str) -> None:
            async with rig.repo.transaction():
                await service("h0").force_release(NodeId(ids[node]))

        async def _age(node: str) -> None:
            async with rig.repo.transaction():
                await rig.repo.session.execute(
                    text(
                        "UPDATE file_leases SET expires_at = now() - interval '1 second', "
                        "grantable_after = now() - interval '1 second' WHERE node_id = :n"
                    ),
                    {"n": ids[node]},
                )

        def sync(node: str, epoch: int) -> None:
            """Pin the model's epoch to the one the platform's floor produced."""
            model.leases[node] = replace(model.leases[node], epoch=epoch)
            model.hwm[node] = max(model.hwm.get(node, 0), epoch)

        # 1. h0's first machine takes the folder.
        _epoch, resumed = model.acquire("a", "h0-0", "h0")
        assert resumed is False
        first = acquire("a", "h0", "h0-0").epoch
        sync("a", first)

        # 2. That machine is killed and comes back: same lease, same epoch.
        replayed, resumed = model.acquire("a", "h0-0", "h0")
        assert resumed is True and replayed == first
        assert acquire("a", "h0", "h0-0").epoch == first, (
            "the resumed mount was handed a new epoch, fencing its own in-flight writes"
        )

        # 3. Its in-flight epoch is therefore still the one that heartbeats.
        beat = rig.run(_heartbeat("a", "h0", "h0-0", first))
        assert beat.epoch == first and beat.forced is False

        # 4. A *second* instance of the same person is not a resume.
        for holder, instance in (("h0", "h0-1"), ("h1", "h1-0")):
            with pytest.raises(ModelRefusal) as refusal:
                model.acquire("a", instance, holder)
            assert refusal.value.code == "files.leased"
            _refused(lambda h=holder, i=instance: acquire("a", h, i), "files.leased")

        # 5. A live lease *under* the folder blocks the resume too: granting
        #    above it would put two writers on the same bytes.
        under, _under_resumed = model.acquire("b", "h0-9", "h0")
        sync("b", acquire("b", "h0", "h0-9").epoch)
        assert under
        with pytest.raises(ModelRefusal) as refusal:
            model.acquire("a", "h0-0", "h0")
        assert refusal.value.code == "files.leased"
        _refused(lambda: acquire("a", "h0", "h0-0"), "files.leased")
        rig.run(_age("b"))
        model.expire("b", past_delay=False)

        # 6. Forced: the grace period is nobody's to take, the holder included.
        rig.run(_force("a"))
        model.force("a")
        assert rig.run(_heartbeat("a", "h0", "h0-0", first)).forced is True
        assert model.heartbeat("a", first, "h0-0") is True
        with pytest.raises(ModelRefusal) as refusal:
            model.acquire("a", "h0-0", "h0")
        assert refusal.value.code == "files.leased"
        _refused(lambda: acquire("a", "h0", "h0-0"), "files.leased")

        # 7. Once the grace runs out the folder is grantable again - and the
        #    epoch it is handed is above the one that was just fenced.
        rig.run(_age("a"))
        model.expire("a", past_delay=False)
        after, resumed = model.acquire("a", "h0-0", "h0")
        assert resumed is False and after > first
        granted = acquire("a", "h0", "h0-0").epoch
        assert granted > first, f"epoch {granted} does not supersede the fenced {first}"
    finally:
        rig.close()
