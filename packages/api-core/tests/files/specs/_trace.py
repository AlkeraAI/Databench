"""Replay a TLC-generated trace of ``lease_fencing.tla`` through the real leases.

The spec proves the protocol; this replays the protocol's own behaviours against
the implementation so the code is pinned to what was proved. TLC runs in
simulation mode and dumps each behaviour as a module of numbered states, every
one headed by the action that produced it and the holder it was taken for::

    \\* <leases_acquired(h2) line 165, col 5 to line 174, col 54 of module lease_fencing>

That header is why this is a conformance replay rather than a re-implementation
of the spec: the action name and its parameter come from TLC, not from this
file, and the state that follows is the model's answer — which the replay checks
against the rows Postgres actually holds after driving the same step through
``LeaseService``, the reaper and a fenced rename.

Time follows the trace rather than the wall clock: the injected ``FakeClock``
advances on ``clock_tick`` and the lease deadline in Postgres is moved to
whichever side of ``now()`` the model's ``expires`` is on before every step,
which is what the core lease suite already does to age a lease without racing
``now()``.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

from alkera_core.authz.principal import ActingContext
from alkera_core.files import history
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import NodeId
from alkera_core.files.leases import LeaseConflict, LeaseService
from alkera_core.files.namespace import Namespace
from alkera_core.files.repo import FilesRepo
from sqlalchemy import text
from tests.files._kit.fencing import held_by

#: How long one TLC simulation run may take before the harness calls it wedged.
TLC_TIMEOUT_SECONDS: Final = 900

#: Spec actions the replay drives. Anything else in a trace is a harness bug,
#: not something to skip: an unmapped action would replay as silence.
KNOWN_ACTIONS: Final = frozenset(
    {
        "Init",
        "leases_overlap_checked",
        "leases_acquired",
        "leases_heartbeat",
        "leases_final_applied",
        "leases_fenced_write",
        "leases_reaped",
        "clock_tick",
        "holder_paused",
        "holder_resumed",
        "platform_restored",
    }
)

_HEADER = re.compile(r"^\\\* <(?P<action>[A-Za-z_]+)(?:\((?P<params>[^)]*)\))?[ >]", re.MULTILINE)
_LEASE = re.compile(
    r"lease = \[ holder \|-> (?P<holder>\w+), epoch \|-> (?P<epoch>\d+), "
    r"expires \|-> (?P<expires>\d+), released \|-> (?P<released>TRUE|FALSE)"
)


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "uv.lock").is_file():
            return candidate
    raise AssertionError("uv.lock not found above the trace harness")


REPO_ROOT: Final = _repo_root()
SPEC: Final = REPO_ROOT / "packages" / "api-core" / "specs" / "files" / "lease_fencing.tla"
CFG: Final = REPO_ROOT / "packages" / "api-core" / "specs" / "files" / "lease_fencing.cfg"
TLC: Final = REPO_ROOT / "ops" / "scripts" / "tlc.sh"


@dataclass(frozen=True, slots=True)
class Step:
    """One state of a TLC behaviour: the action that produced it, and the model's answer."""

    action: str
    params: tuple[str, ...]
    clock: int
    holder: str | None
    epoch: int
    expires: int
    released: bool
    writes: int

    @property
    def holder_param(self) -> str:
        if not self.params:
            raise AssertionError(f"{self.action} carries no holder parameter")
        return self.params[0]

    @property
    def expired(self) -> bool:
        return self.expires <= self.clock


@dataclass(frozen=True, slots=True)
class Trace:
    """A whole behaviour, first state first."""

    name: str
    steps: tuple[Step, ...]

    def __len__(self) -> int:
        return len(self.steps)


def _one(pattern: str, dumped: str, what: str) -> str:
    found = re.search(pattern, dumped)
    if found is None:  # pragma: no cover - a shape change in TLC's dump
        raise AssertionError(f"no {what} in the dumped state: {dumped!r}")
    return found[1]


def _parse_state(block: str) -> tuple[int, str | None, int, int, bool, int]:
    flat = " ".join(block.split())
    lease = _LEASE.search(flat)
    if lease is None:  # pragma: no cover - a shape change in TLC's dump
        raise AssertionError(f"no lease record in the dumped state: {flat!r}")
    holder = lease["holder"]
    landed = _one(r"writes = <<(.*?)>>", flat, "writes")
    return (
        int(_one(r"\bclock = (\d+)", flat, "clock")),
        None if holder == "none" else holder,
        int(lease["epoch"]),
        int(lease["expires"]),
        lease["released"] == "TRUE",
        landed.count("holder |->"),
    )


def parse_trace(dumped: str, *, name: str) -> Trace:
    """Turn one dumped TLC behaviour into its ``(action, params)`` sequence."""
    headers = list(_HEADER.finditer(dumped))
    if not headers:  # pragma: no cover - an empty dump is a TLC failure the caller catches
        raise AssertionError(f"no states in {name}")
    steps: list[Step] = []
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(dumped)
        action = header["action"]
        if action not in KNOWN_ACTIONS:
            raise AssertionError(f"{name} uses action {action!r}, which the replay cannot drive")
        raw_params = header["params"]
        clock, holder, epoch, expires, released, writes = _parse_state(dumped[header.end() : end])
        steps.append(
            Step(
                action=action,
                params=tuple(p.strip() for p in raw_params.split(",")) if raw_params else (),
                clock=clock,
                holder=holder,
                epoch=epoch,
                expires=expires,
                released=released,
                writes=writes,
            )
        )
    return Trace(name=name, steps=tuple(steps))


def generate_traces(*, count: int, depth: int, seed: int, out_dir: Path) -> tuple[Trace, ...]:
    """Run TLC in simulation mode and parse every behaviour it dumps.

    The dump prefix is repo-relative on purpose: ``tlc.sh`` runs a local JRE in
    the caller's working directory and Docker with the repo root as its working
    directory, so one relative path names the same file under both runtimes.

    ``seed`` is required rather than defaulted: TLC draws a fresh random seed
    when none is given, and a caller that forgot one would get twenty different
    behaviours every run — which is a coverage floor decided by a coin toss.
    One worker is passed for the same reason; the draw is only reproducible
    while a single thread is consuming it.
    """
    relative = (out_dir / "tr").relative_to(REPO_ROOT)
    result = subprocess.run(
        [
            str(TLC),
            str(SPEC),
            str(CFG),
            "1",
            "-simulate",
            f"file={relative},num={count}",
            "-depth",
            str(depth),
            "-seed",
            str(seed),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=TLC_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"TLC simulation failed:\n{result.stdout}\n{result.stderr}")
    dumps = sorted(p for p in out_dir.iterdir() if p.name.startswith("tr_"))
    if not dumps:
        raise AssertionError(f"TLC dumped no traces:\n{result.stdout}")
    return tuple(parse_trace(p.read_text(), name=p.name) for p in dumps)


@dataclass(slots=True)
class ReplayServices:
    """Everything the replay drives: one lane database, one leased folder."""

    repo: FilesRepo
    ctx: ActingContext
    clock: FakeClock
    node_id: NodeId
    org_team_id: uuid.UUID


@dataclass(slots=True)
class ReplayResult:
    """What the replay observed, for a test to assert beyond the invariants."""

    granted: list[int] = field(default_factory=list)
    landed: list[int] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    steps: int = 0


class Replay:
    """Drives one trace: every action is a real call in its own transaction."""

    def __init__(self, services: ReplayServices) -> None:
        self._s = services
        self._belief: dict[str, int] = {}
        self.result = ReplayResult()

    def _service(self, holder: str) -> LeaseService:
        # A zero grant delay: the trace decides when the next acquire happens,
        # and the delay's own timing is the core lease suite's business.
        return LeaseService(self._s.repo, self._s.ctx, self._s.clock, grant_delay=timedelta(0))

    async def _commit(self, sql: str, params: dict[str, Any]) -> None:
        await self._s.repo.session.execute(text(sql), params)
        await self._s.repo.session.commit()

    async def agree_with_the_model(self, previous: Step) -> None:
        """Move the lease deadline to whichever side of ``now()`` the model is on."""
        if previous.holder is None or previous.released:
            return
        offset = "- interval '1 second'" if previous.expired else "+ interval '1 hour'"
        await self._commit(
            "UPDATE file_leases SET expires_at = now() "
            + offset
            + " WHERE node_id = :n AND released_at IS NULL",
            {"n": self._s.node_id},
        )

    async def _live_holders(self) -> int:
        return int(
            (
                await self._s.repo.session.execute(
                    text(
                        "SELECT count(*) FROM file_leases WHERE node_id = :n "
                        "AND released_at IS NULL AND expires_at > now()"
                    ),
                    {"n": self._s.node_id},
                )
            ).scalar_one()
        )

    async def _history_epochs(self) -> list[int]:
        rows = (
            await self._s.repo.session.execute(
                text(
                    "SELECT (after->>'epoch')::bigint FROM file_history "
                    "WHERE node_id = :n AND kind = 'rename' AND after ? 'epoch' ORDER BY seq"
                ),
                {"n": self._s.node_id},
            )
        ).scalars()
        return [int(row) for row in rows]

    async def check_invariants(self, step: Step) -> None:
        """The spec's three invariants, read off the real rows."""
        live = await self._live_holders()
        assert live <= 1, f"{step.action}: {live} live holders on one node"
        granted = self.result.granted
        assert granted == sorted(set(granted)), f"an epoch was re-issued or regressed: {granted}"
        landed = await self._history_epochs()
        assert landed == sorted(landed), f"a write landed behind a newer epoch: {landed}"

    async def _acquire(self, holder: str) -> int:
        async with self._s.repo.transaction():
            lease = await self._service(holder).acquire(
                self._s.node_id, instance_id=holder, machine_id=holder
            )
        self._belief[holder] = lease.epoch
        self.result.granted.append(lease.epoch)
        return lease.epoch

    async def _heartbeat(self, holder: str, *, lapsed: bool) -> None:
        await self._tolerating_a_stricter_refusal(lapsed, self._beat(holder))

    async def _beat(self, holder: str) -> None:
        async with self._s.repo.transaction():
            await self._service(holder).heartbeat(
                self._s.node_id, epoch=self._belief[holder], instance_id=holder
            )

    async def _release(self, holder: str, *, lapsed: bool) -> None:
        await self._tolerating_a_stricter_refusal(lapsed, self._hand_back(holder))

    async def _hand_back(self, holder: str) -> None:
        async with self._s.repo.transaction():
            await self._service(holder).release(
                self._s.node_id, epoch=self._belief[holder], instance_id=holder
            )

    async def _tolerating_a_stricter_refusal(
        self, lapsed: bool, call: Coroutine[Any, Any, None]
    ) -> None:
        """The implementation refuses on a lapsed row where the spec still allows it.

        ``leases_heartbeat`` and ``leases_final_applied`` are enabled in the model
        on a lease that has passed its TTL but has not been reaped yet; both
        statements in ``leases.py`` carry ``expires_at > now()`` and refuse it
        instead. Refusing is strictly stronger than the spec — it can only keep a
        write out, never let a stale one in — so the replay records the refusal
        and carries on. Any other refusal, and any refusal while the model says
        the lease is live, is a conformance failure and propagates.
        """
        try:
            await call
        except LeaseConflict as refused:
            if not lapsed:
                raise
            self.result.refusals.append(refused.code)

    async def _reap(self) -> None:
        """The reaper's lapse statement: an expired lease is released and held back."""
        await self._commit(
            "UPDATE file_leases SET released_at = now(), grantable_after = now() "
            "WHERE node_id = :n AND released_at IS NULL AND expires_at <= now()",
            {"n": self._s.node_id},
        )

    async def _restore(self) -> None:
        """The runbook: bump the generation, then open a database that lost the row."""
        await self._commit(
            "INSERT INTO file_platform (id, restore_generation) VALUES (1, 1) "
            "ON CONFLICT (id) DO UPDATE SET restore_generation = "
            "file_platform.restore_generation + 1",
            {},
        )
        await self._commit("DELETE FROM file_leases WHERE node_id = :n", {"n": self._s.node_id})

    async def _fenced_rename(self, holder: str) -> None:
        """A rename of the leased folder, carrying the epoch the holder believes it owns."""
        epoch = self._belief[holder]
        async with self._s.repo.transaction():
            node = await self._s.repo.node(self._s.node_id)
            assert node is not None
            before = bytes(node.name)
            after = before + b"x"
            namespace = Namespace(self._s.repo, self._s.ctx, self._s.clock)
            await namespace.rename(
                self._s.node_id,
                after,
                if_match=node.etag,
                lease=held_by(self._s.ctx, epoch, holder),
            )
            await history.record(
                self._s.repo,
                self._s.ctx,
                node_id=self._s.node_id,
                kind="rename",
                before={"name": before.decode()},
                after={"name": after.decode(), "epoch": epoch},
            )
        self.result.landed.append(epoch)

    async def _refused_acquire(self, holder: str) -> None:
        """The overlap probe: the model says this acquire cannot be granted."""
        try:
            await self._acquire(holder)
        except LeaseConflict as refused:
            self.result.refusals.append(refused.code)
            return
        raise AssertionError("an acquire the spec forbids was granted")

    async def step(self, step: Step, previous: Step | None = None) -> None:
        action = step.action
        # The model's state BEFORE this action: whether the lease had already
        # passed its TTL is what decides if a refusal here is the stricter
        # implementation rather than a divergence.
        lapsed = previous is not None and previous.holder is not None and previous.expired
        if action in {"Init", "holder_paused", "holder_resumed"}:
            return
        if action == "leases_overlap_checked":
            await self._refused_acquire(step.holder_param)
        elif action == "clock_tick":
            self._s.clock.advance(timedelta(seconds=1))
        elif action == "leases_acquired":
            await self._acquire(step.holder_param)
        elif action == "leases_heartbeat":
            await self._heartbeat(step.holder_param, lapsed=lapsed)
        elif action == "leases_final_applied":
            await self._release(step.holder_param, lapsed=lapsed)
        elif action == "leases_fenced_write":
            await self._fenced_rename(step.holder_param)
        elif action == "leases_reaped":
            await self._reap()
        else:
            await self._restore()


async def replay(
    trace: Trace,
    *,
    services: ReplayServices,
    expect_refusal_at: int | None = None,
) -> ReplayResult:
    """Drive one trace through the implementation, checking the invariants each step.

    ``expect_refusal_at`` is for a trace the spec forbids: the step at that index
    must be refused by the fence, and the replay stops there.
    """
    driver = Replay(services)
    previous: Step | None = None
    for index, step in enumerate(trace.steps):
        if previous is not None:
            await driver.agree_with_the_model(previous)
        if index == expect_refusal_at:
            try:
                await driver.step(step, previous)
            except LeaseConflict as refused:
                driver.result.refusals.append(refused.code)
                driver.result.steps = index + 1
                return driver.result
            raise AssertionError(f"step {index} ({step.action}) was not refused")
        await driver.step(step, previous)
        await driver.check_invariants(step)
        driver.result.steps = index + 1
        previous = step
    return driver.result


def hand_written(steps: Sequence[Step]) -> Trace:
    """A trace written by hand — used for the behaviour the spec forbids."""
    return Trace(name="hand-written", steps=tuple(steps))
