"""``commit_gc.tla`` conformance: real commits racing the real sweep, checked by TLC.

The lease replay runs the other direction — TLC simulates behaviours and the
implementation is driven through them. A commit racing the janitor cannot be
driven that way: the interesting behaviours are *interleavings*, and which one
happens is decided by where the code parks, not by a step list. So this module
runs the race for real — ``ContentService.put_version`` on one session,
``Janitor.sweep`` on another, choreographed by ``PausingCheckpoints`` at the
checkpoints ``commit_gc.tla``'s module header names — records every step as one
of the spec's actions with the state read back from Postgres and the store, and
then asks TLC whether what happened is a behaviour the spec allows.

The check is a *trace module*: a generated module that extends ``commit_gc`` and
pins state ``i+1`` to the recorded state ``i+1`` while still requiring the spec's
own ``Next`` to justify the step. A recorded step the protocol cannot take leaves
TLC with no successor, and with ``CHECK_DEADLOCK TRUE`` that is an error naming
the index it got stuck at — an ``InvalidTrace``. The spec's four invariants are
checked at every recorded state on the way, so "a referenced object was moved
under ``deleted/``" fails here as ``NoDanglingReference``.

What is read back and what is bookkeeping, stated so a reviewer can disagree:
``objState`` comes from the store's own directory (present under ``objects/`` →
live or incoming, present under ``deleted/objects/`` → deleted, neither →
absent); ``refs``, ``grants`` and ``sessState`` come from ``file_versions``,
``file_content_grants`` and ``file_upload_sessions`` rows. ``owner``,
``writtenAt`` and the sweep's own record are the harness's: no column holds
"which session wrote this key", and the scan's reach set and candidate list are
its private working state. That is not a hole — the load-bearing conjuncts are
in ``gc_before_move``, and the janitor moving an object the model calls
reachable or too young is exactly the divergence TLC refuses.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.content import ContentService
from alkera_core.files.gc import Janitor
from alkera_core.files.ids import DomainId, NodeId, OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.keys import deleted_key
from alkera_core.files.store.scoped import _RootedDomainStore
from alkera_core.models.files.platform import FileSweepShard
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files._kit.tla import TLC_RUNTIME_MISSING, tlc_runtime
from tests.files.specs._trace import REPO_ROOT, TLC

# `ops/scripts/tlc.sh` and `ops/scripts/ensure-tla-tools.sh` are bash scripts:
# handing one to `subprocess.run` on Windows is `[WinError 193] %1 is not a
# valid Win32 application`, before any spec is read. The specs themselves are
# model-checked by the dedicated `files-specs (TLA+)` pr-gate job on Linux, so
# nothing goes unproven by skipping the replay here.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.files_specs,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "the TLC runner is a bash script; the specs are model-checked "
            "by the files-specs job on Linux"
        ),
    ),
]

#: The spec module the recorded behaviours are checked against.
SPEC_DIR: Final = REPO_ROOT / "packages" / "api-core" / "specs" / "files"
SPEC_NAME: Final = "commit_gc"

#: How long TLC may take on one recorded behaviour. A trace module has a single
#: behaviour to explore, so this is generous by two orders of magnitude.
TLC_TIMEOUT_SECONDS: Final = 600

#: The payload both sessions commit: identical bytes, so the second commit takes
#: the dedup branch and never writes the content key itself.
#: Over the 64 KiB inline threshold on purpose: an inline body never reaches
#: the store at all, so the checkpoints this module choreographs would never fire.
PAYLOAD: Final = b"the same bytes, twice" * 8192

#: The checkpoints the spec's module header maps to its actions, plus the one
#: between the dedup head and the publish that the third interleaving needs.
PUT: Final = "content.after_store_put"
AFTER_HEAD: Final = "content.after_head"
BEFORE_COMMIT: Final = "content.before_commit"
SCAN: Final = "gc.after_reachability"
BEFORE_MOVE: Final = "gc.before_move"
AFTER_MOVE: Final = "gc.after_move"


def _load_concurrency_kit() -> Any:
    """Borrow ``sessions(n)`` rather than open a second pool that could drift."""
    source = Path(__file__).resolve().parent.parent / "concurrency" / "conftest.py"
    spec = importlib.util.spec_from_file_location("files_commit_gc_kit", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_concurrency_kit = _load_concurrency_kit()
# `sessions` borrows its connections from the kit's engine, so both fixtures
# have to be visible where `sessions` is requested.
concurrency_engine = _concurrency_kit.concurrency_engine
sessions = _concurrency_kit.sessions


class PinnedAges:
    """The age horizon, pinned per store key: the seam the janitor reads write time through.

    A deployment reads the driver's listing timestamps. The replay pins them
    because the filesystem driver publishes by ``rename``, which carries the
    staging file's mtime onto the content key — so the disk cannot tell the
    janitor when the object arrived at the key it is being judged under. The
    replay presents the relation the recorded behaviour actually had, measured
    against the stamp the sweep itself took out of Postgres.
    """

    def __init__(self) -> None:
        self._pinned: dict[str, datetime] = {}

    def set(self, key: str, when: datetime) -> None:
        self._pinned[key] = when

    async def written_at(self, key: str) -> datetime | None:
        return self._pinned.get(key)


@dataclass(slots=True)
class _State:
    """One recorded state: the thirteen variables ``commit_gc.tla`` declares."""

    action: str
    params: tuple[str, ...]
    obj_state: dict[str, str]
    owner: dict[str, str]
    written_at: dict[str, int]
    deleted_at: dict[str, int]
    sess_state: dict[str, str]
    refs: set[str]
    grants: set[str]
    clock: int
    sweep_phase: str
    sweep_stamp: int
    sweep_reach: set[str]
    sweep_cand: set[str]
    moving: set[str]


@dataclass(slots=True)
class Rig:
    """Everything one interleaving drives: two connections, one bucket, one shard."""

    content: ContentService
    janitor: Janitor
    repo: FilesRepo
    scope: OrgScope
    domain_id: DomainId
    bucket: Path
    ages: PinnedAges
    checkpoints: PausingCheckpoints
    clock: FakeClock
    shard: int
    nodes: dict[str, NodeId]
    reader: Any

    def path(self, key: str) -> Path:
        return self.bucket / "domains" / str(self.domain_id) / key


class Recorder:
    """Turns what the code did into ``commit_gc.tla``'s actions and states."""

    #: Model values, assigned in first-seen order; the generated cfg declares them.
    OBJECTS: Final = ("ob1", "ob2")
    SESSIONS: Final = ("s1", "s2")

    def __init__(self, rig: Rig) -> None:
        self._rig = rig
        self._keys: dict[str, str] = {}
        self._sessions: dict[str, str] = {}
        self._owner: dict[str, str] = {}
        self._written: dict[str, int] = {}
        self._deleted: dict[str, int] = {}
        self._clock = 0
        self._phase = "idle"
        self._stamp = 0
        self._reach: set[str] = set()
        self._cand: set[str] = set()
        self._moving: set[str] = set()
        self._committing: set[str] = set()
        self._staged: dict[str, list[str]] = {}
        self.states: list[_State] = []

    # -- naming ----------------------------------------------------------

    def object_of(self, key: str) -> str:
        """The model value for a store key, assigned on first sight."""
        if key not in self._keys:
            if len(self._keys) >= len(self.OBJECTS):
                raise AssertionError(f"a third store key appeared: {key!r}")
            self._keys[key] = self.OBJECTS[len(self._keys)]
        return self._keys[key]

    def session_of(self, session_id: Any) -> str:
        text_id = str(session_id)
        if text_id not in self._sessions:
            if len(self._sessions) >= len(self.SESSIONS):
                raise AssertionError("a third upload session appeared")
            self._sessions[text_id] = self.SESSIONS[len(self._sessions)]
        return self._sessions[text_id]

    # -- read-back -------------------------------------------------------

    async def _refs(self) -> set[str]:
        """Keys a head version of a live node names: the spec's ``refs``."""
        rows = await self._rig.reader.execute(
            text(
                "SELECT v.store_key FROM file_versions v "
                "JOIN file_nodes n ON n.head_version_id = v.id "
                "WHERE v.org_team_id = :org AND v.store_key IS NOT NULL "
                "AND n.trashed_at IS NULL"
            ).bindparams(org=self._rig.scope.org_team_id)
        )
        return {self.object_of(row[0]) for row in rows.all()}

    async def _grants(self) -> set[str]:
        rows = await self._rig.reader.execute(
            text(
                "SELECT v.store_key FROM file_content_grants g "
                "JOIN file_versions v ON v.id = g.version_id "
                "WHERE g.org_team_id = :org AND v.store_key IS NOT NULL "
                "AND g.expires_at > now()"
            ).bindparams(org=self._rig.scope.org_team_id)
        )
        return {self.object_of(row[0]) for row in rows.all()}

    async def _session_states(self) -> dict[str, str]:
        rows = await self._rig.reader.execute(
            text("SELECT id, state FROM file_upload_sessions WHERE org_team_id = :org").bindparams(
                org=self._rig.scope.org_team_id
            )
        )
        seen: dict[str, str] = {}
        for row in rows.all():
            state = str(row[1])
            # `uploading` is `open` in the model: the session exists and has not
            # begun writing its version row. `expired` is an abandoned session.
            mapped = {"uploading": "open", "expired": "aborted"}.get(state, state)
            if mapped not in {"open", "committing", "done", "aborted"}:
                raise AssertionError(f"upload session state {state!r} has no model counterpart")
            seen[self.session_of(row[0])] = mapped
        for name in self._committing:
            # The seam has no column: a session parked at `content.before_commit`
            # is about to write its version row, which is exactly the model's
            # `committing`. Postgres only shows `committing` once the statement
            # inside that transaction has run.
            seen[name] = "committing"
        return seen

    def _object_state(self, key: str, refs: set[str]) -> str:
        """Where the bytes are, read off the store's own directory.

        The spec's object is the *bytes*, and between the put and the publish
        they sit under the session's staging prefix rather than the content key
        — so "the bytes are in the store under a key no row names yet" is
        staged-or-published, and only the content key can ever be referenced.
        """
        name = self.object_of(key)
        if self._rig.path(key).exists():
            return "live" if name in refs else "incoming"
        if any(self._rig.path(staged).exists() for staged in self._staged.get(name, ())):
            return "incoming"
        if self._rig.path(deleted_key(key)).exists():
            return "deleted"
        return "absent"

    def stage(self, name: str, staged_key: str) -> None:
        """Record where a session's bytes live before they reach the content key."""
        self._staged.setdefault(name, []).append(staged_key)

    def _roots(self, sess: dict[str, str], refs: set[str], grants: set[str]) -> set[str]:
        unfinished = {o for o, s in self._owner.items() if sess.get(s) in {"open", "committing"}}
        return refs | unfinished | grants

    async def _observe(self) -> tuple[dict[str, str], dict[str, str], set[str], set[str]]:
        refs = await self._refs()
        grants = await self._grants()
        sess = await self._session_states()
        obj_state = {self.object_of(key): self._object_state(key, refs) for key in list(self._keys)}
        for name in self.OBJECTS:
            obj_state.setdefault(name, "absent")
        for name in self.SESSIONS:
            sess.setdefault(name, "open")
        return obj_state, sess, refs, grants

    # -- recording --------------------------------------------------------

    async def record(self, action: str, *params: str) -> None:
        """Snapshot the world and file it under the spec action that produced it."""
        obj_state, sess, refs, grants = await self._observe()
        self.states.append(
            _State(
                action=action,
                params=params,
                obj_state=obj_state,
                owner={name: self._owner.get(name, "nobody") for name in self.OBJECTS},
                written_at={name: self._written.get(name, 0) for name in self.OBJECTS},
                deleted_at={name: self._deleted.get(name, 0) for name in self.OBJECTS},
                sess_state={name: sess[name] for name in self.SESSIONS},
                refs=set(refs),
                grants=set(grants),
                clock=self._clock,
                sweep_phase=self._phase,
                sweep_stamp=self._stamp,
                sweep_reach=set(self._reach),
                sweep_cand=set(self._cand),
                moving=set(self._moving),
            )
        )

    async def init(self) -> None:
        await self.record("Init")

    async def tick(self) -> None:
        self._clock += 1
        self._rig.clock.advance(timedelta(seconds=1))
        await self.record("clock_tick")

    async def put(self, session_id: Any, key: str, *, staged: str | None = None) -> None:
        name = self.object_of(key)
        if staged is not None:
            self.stage(name, staged)
        self._owner[name] = self.session_of(session_id)
        self._written[name] = self._clock
        await self.record("content_after_store_put", self.session_of(session_id), name)

    async def committing(self, session_id: Any, key: str) -> None:
        self._committing.add(self.session_of(session_id))
        await self.record("content_committing", self.session_of(session_id), self.object_of(key))

    async def committed(self, session_id: Any, key: str) -> None:
        self._committing.discard(self.session_of(session_id))
        await self.record("content_before_commit", self.session_of(session_id), self.object_of(key))

    async def crashed(self, session_id: Any) -> None:
        self._committing.discard(self.session_of(session_id))
        await self.record("session_crashed", self.session_of(session_id))

    async def scanned(self) -> None:
        """The scan's record, taken at the instant the sweep stamped it."""
        obj_state, sess, refs, grants = await self._observe()
        self._stamp = self._clock
        self._reach = self._roots(sess, refs, grants)
        self._cand = {
            name
            for name in self.OBJECTS
            if obj_state[name] in {"incoming", "live"}
            and name not in self._reach
            and self._written.get(name, 0) < self._stamp
        }
        self._phase = "scanned"
        await self.record("gc_after_reachability")

    async def before_move(self, key: str) -> None:
        self._moving.add(self.object_of(key))
        await self.record("gc_before_move", self.object_of(key))

    async def after_move(self, key: str) -> None:
        name = self.object_of(key)
        self._deleted[name] = self._clock
        self._moving.discard(name)
        await self.record("gc_after_move", name)

    @property
    def key_map(self) -> dict[str, str]:
        """Store key to model value, for the fixtures that pin per-key facts."""
        return dict(self._keys)

    @property
    def stamp_tick(self) -> int:
        return self._stamp

    def written_tick(self, name: str) -> int:
        return self._written.get(name, 0)

    @property
    def candidates(self) -> set[str]:
        """What the last recorded scan called garbage."""
        return set(self._cand)

    @property
    def max_clock(self) -> int:
        return max(state.clock for state in self.states)


# -- the trace module ------------------------------------------------------


def _value(value: Any) -> str:
    return f'"{value}"' if isinstance(value, str) else str(value)


def _fn(mapping: dict[str, Any], *, quote: bool = True) -> str:
    """A TLA+ function over model values: ``(ob1 :> "live") @@ (ob2 :> "absent")``."""
    parts = [
        f"({key} :> {_value(value) if quote else value})" for key, value in sorted(mapping.items())
    ]
    return " @@ ".join(parts)


def _set(names: set[str]) -> str:
    return "{" + ", ".join(sorted(names)) + "}"


_FIELDS: Final = (
    "objState",
    "owner",
    "writtenAt",
    "deletedAt",
    "sessState",
    "refs",
    "grants",
    "clock",
    "sweepPhase",
    "sweepStamp",
    "sweepReach",
    "sweepCand",
    "moving",
)


def _record(state: _State) -> str:
    """One recorded state as a TLA+ record; ``owner`` holds model values, not strings."""
    return (
        "[ objState |-> "
        + _fn(state.obj_state)
        + ", owner |-> "
        + _fn(state.owner, quote=False)
        + ", writtenAt |-> "
        + _fn(state.written_at)
        + ", deletedAt |-> "
        + _fn(state.deleted_at)
        + ", sessState |-> "
        + _fn(state.sess_state)
        + ", refs |-> "
        + _set(state.refs)
        + ", grants |-> "
        + _set(state.grants)
        + ", clock |-> "
        + str(state.clock)
        + ', sweepPhase |-> "'
        + state.sweep_phase
        + '"'
        + ", sweepStamp |-> "
        + str(state.sweep_stamp)
        + ", sweepReach |-> "
        + _set(state.sweep_reach)
        + ", sweepCand |-> "
        + _set(state.sweep_cand)
        + ", moving |-> "
        + _set(state.moving)
        + " ]"
    )


def trace_module(states: list[_State]) -> str:
    """A module that admits exactly the recorded behaviour, justified by ``Next``.

    Every step must be one of ``commit_gc``'s own actions *and* land on the state
    the implementation actually reached. A step the protocol cannot take has no
    successor, which under ``CHECK_DEADLOCK TRUE`` is the InvalidTrace report.
    """
    body = ",\n  ".join(_record(state) for state in states)
    matches = "\n".join(f"    /\\ {f} = TraceStates[i].{f}" for f in _FIELDS)
    next_matches = "\n".join(f"    /\\ {f}' = TraceStates[i].{f}" for f in _FIELDS)
    return f"""---- MODULE trace ----
EXTENDS {SPEC_NAME}, TLC, Sequences, Naturals

\\* The model values the recorded states are written in. TLC parses this module
\\* before it reads the configuration, so a bare `ob1` inside TraceStates would be
\\* an unknown operator; declaring them here and assigning each to itself in the
\\* configuration is what makes the literals resolve.
CONSTANTS ob1, ob2, s1, s2, nobody

VARIABLE ti

TraceStates == <<
  {body}
>>

Matches(i) ==
{matches}

MatchesNext(i) ==
{next_matches}

TraceInit == Init /\\ Matches(1) /\\ ti = 1

TraceNext ==
    \\/ /\\ ti < Len(TraceStates)
       /\\ Next
       /\\ MatchesNext(ti + 1)
       /\\ ti' = ti + 1
    \\/ /\\ ti = Len(TraceStates)
       /\\ UNCHANGED <<vars, ti>>

TraceSpec == TraceInit /\\ [][TraceNext]_<<vars, ti>>
====
"""


def trace_config(max_clock: int) -> str:
    """The constants the trace module is checked under, and the spec's invariants.

    ``Window`` is set past the recorded clock on purpose: no recorded behaviour
    reaches an expiry, and leaving the window open would let TLC justify a
    recorded step with ``object_expired`` instead of the action that produced it.
    """
    return f"""SPECIFICATION TraceSpec

CONSTANTS
    ob1 = ob1
    ob2 = ob2
    s1 = s1
    s2 = s2
    nobody = nobody
    Objects = {{ob1, ob2}}
    Sessions = {{s1, s2}}
    NoSession = nobody
    MaxClock = {max_clock}
    Window = {max_clock + 1}

INVARIANTS
    TypeOK
    NoDanglingReference
    NoLostObject
    NoOverwriteInPlace

CHECK_DEADLOCK TRUE
"""


class _AdminOnly:
    """A `ScopedStoreFactory` that hands out only the bucket-wide handle.

    The janitor is the one component allowed to widen to it, so the rig gives
    it the factory the library asks for rather than a bare store.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    async def for_domain(self, domain_id: Any) -> Any:
        raise AssertionError("the janitor never takes a domain-bound handle")

    def admin(self) -> Any:
        return self._store


@dataclass(frozen=True, slots=True)
class TlcVerdict:
    """What TLC said about one recorded behaviour."""

    ok: bool
    output: str

    @property
    def diverged_at(self) -> int | None:
        """The recorded index TLC could not extend, when it got stuck."""
        if self.ok:
            return None
        found = re.findall(r"\bti = (\d+)", self.output)
        return int(found[-1]) if found else None


def check_trace(states: list[_State], *, out_dir: Path, max_clock: int) -> TlcVerdict:
    """Replay one recorded behaviour against the spec with TLC."""
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(SPEC_DIR / f"{SPEC_NAME}.tla", out_dir / f"{SPEC_NAME}.tla")
    module = out_dir / "trace.tla"
    config = out_dir / "trace.cfg"
    module.write_text(trace_module(states))
    config.write_text(trace_config(max_clock))
    done = subprocess.run(
        [str(TLC), str(module), str(config), "1"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=TLC_TIMEOUT_SECONDS,
        check=False,
    )
    return TlcVerdict(ok=done.returncode == 0, output=done.stdout + done.stderr)


# -- the rig ---------------------------------------------------------------


async def _stream(payload: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(payload), 8):
        yield payload[start : start + 8]


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


@pytest.fixture
async def rig(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    tmp_path: Path,
    sessions: Callable[..., Any],
) -> Rig:
    """One bucket, one drive with two files, and the janitor on its own connection."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("papers/ papers/a.bin papers/b.bin", drive=drive)
    domain_id = DomainId(drive.dedup_domain_id)
    bucket = tmp_path / "bucket"
    admin = FilesystemStore(bucket, clock=clock, layout="bucket")
    domain_store = _RootedDomainStore(
        FilesystemStore(bucket / "domains" / str(domain_id), clock=clock), domain_id
    )
    scope = OrgScope(org_team_id=files_org.org_team_id)
    repo = FilesRepo(files_session, scope)

    number = uuid.uuid4().int % 1_000_000 + 2_000
    files_session.add(FileSweepShard(shard=number, cursor={}))
    # A gibibyte of unrelated live bytes, so the 2 % circuit breaker — a separate
    # mechanism with its own suite — never decides the outcome of a race about
    # reachability. Its key is never on disk, so it is neither a candidate nor a
    # head version the recorder would have to name.
    files_session.add(
        FileVersion(
            id=uuid.uuid4(),
            org_team_id=files_org.org_team_id,
            node_id=tree["papers/b.bin"].id,
            seq=1,
            size_bytes=1 << 30,
            content_hash=uuid.uuid4().hex,
            store_key="objects/00/00/" + "0" * 64,
            source="upload",
        )
    )
    await files_session.commit()

    janitor_session, reader_session = await sessions(2)
    janitor_repo = FilesRepo(janitor_session, scope)
    ages = PinnedAges()
    checkpoints = PausingCheckpoints(timeout=30.0)
    return Rig(
        content=ContentService(repo, _ctx(files_org), clock, domain_store, checkpoints=checkpoints),
        janitor=Janitor(
            lambda _scope: janitor_repo,
            _AdminOnly(admin),
            clock,
            checkpoints,
            age_source=ages,
        ),
        repo=repo,
        scope=scope,
        domain_id=domain_id,
        bucket=bucket,
        ages=ages,
        checkpoints=checkpoints,
        clock=clock,
        shard=number,
        nodes={"a.bin": NodeId(tree["papers/a.bin"].id), "b.bin": NodeId(tree["papers/b.bin"].id)},
        reader=reader_session,
    )


async def _pin_ages(rig: Rig, recorder: Recorder) -> None:
    """Present the janitor with the model's ``writtenAt < sweepStamp`` relation.

    The stamp is read back from the shard row the sweep just wrote, so the two
    instants being compared are the ones the run really used.
    """
    row = (
        await rig.reader.execute(
            text("SELECT sweep_started_at FROM file_sweep_shards WHERE shard = :shard").bindparams(
                shard=rig.shard
            )
        )
    ).all()
    assert row and row[0][0] is not None, "the sweep did not stamp its shard"
    stamp: datetime = row[0][0]
    for key, name in recorder.key_map.items():
        older = recorder.written_tick(name) < recorder.stamp_tick
        rig.ages.set(
            f"domains/{rig.domain_id}/{key}", stamp + timedelta(seconds=-1 if older else 1)
        )


async def _sweep(rig: Rig) -> Any:
    return await rig.janitor.sweep(rig.domain_id, org=rig.scope, shard=rig.shard, dry_run=False)


@pytest.fixture
def traces_dir(tmp_path: Path, tla_tools_jar: Path) -> Path:
    """Where one replay's generated trace module goes.

    Depending on ``tla_tools_jar`` here is what installs the jar: every case in this
    module reaches TLC through this fixture, so running the module alone provisions it
    instead of inheriting it from whichever suite ran first.
    """
    assert tla_tools_jar.is_file()
    return tmp_path / "tla"


def _require_tlc() -> None:
    if tlc_runtime() is None:
        pytest.skip(TLC_RUNTIME_MISSING)


async def test_commit_before_the_scan_makes_the_object_a_root(rig: Rig, traces_dir: Path) -> None:
    """The committed object is in ``refs`` when the scan runs, so it is never a candidate."""
    _require_tlc()
    recorder = Recorder(rig)
    await recorder.init()

    key: list[str] = []
    session_ids: list[Any] = []
    etag = await _etag(rig, "a.bin")

    async def committer() -> None:
        await rig.content.put_version(
            rig.nodes["a.bin"], _stream(PAYLOAD), size_declared=len(PAYLOAD), if_match=etag
        )

    rig.checkpoints.pause(PUT)
    task = asyncio.create_task(committer())
    await rig.checkpoints.wait_paused(PUT)
    key.append(await _staged_key(rig))
    session_ids.append(await _open_session_id(rig))
    await recorder.put(session_ids[0], key[0], staged=f"incoming/{session_ids[0]}/object")
    rig.checkpoints.pause(BEFORE_COMMIT)
    rig.checkpoints.release(PUT)
    await rig.checkpoints.wait_paused(BEFORE_COMMIT)
    await recorder.committing(session_ids[0], key[0])
    rig.checkpoints.release(BEFORE_COMMIT)
    await task
    await recorder.committed(session_ids[0], key[0])

    await recorder.tick()
    rig.checkpoints.pause(SCAN)
    sweep = asyncio.create_task(_sweep(rig))
    await rig.checkpoints.wait_paused(SCAN)
    await recorder.scanned()
    await _pin_ages(rig, recorder)
    rig.checkpoints.release(SCAN)
    result = await sweep

    assert result.moved == (), "a referenced object was swept"
    verdict = check_trace(recorder.states, out_dir=traces_dir, max_clock=recorder.max_clock)
    assert verdict.ok, f"InvalidTrace at step {verdict.diverged_at}:\n{verdict.output}"


async def _etag(rig: Rig, name: str) -> int:
    """The node version a mutation must carry; the factory does not promise 1."""
    async with rig.repo.transaction():
        node = await rig.repo.node(rig.nodes[name])
    assert node is not None
    return int(node.etag)


async def _staged_key(rig: Rig) -> str:
    """The content key the streaming put is about to publish under."""
    base = rig.bucket / "domains" / str(rig.domain_id) / "objects"
    from alkera_core.files.hashing import StreamHasher

    hasher = StreamHasher()
    hasher.update(PAYLOAD)
    from alkera_core.files.store import keys as key_layout

    del base
    return key_layout.object_key(hasher.finalize().content_hash)


async def _open_session_id(rig: Rig) -> Any:
    row = (
        await rig.reader.execute(
            text(
                "SELECT id FROM file_upload_sessions WHERE org_team_id = :org "
                "ORDER BY created_at DESC LIMIT 1"
            ).bindparams(org=rig.scope.org_team_id)
        )
    ).all()
    assert row, "no upload session row for the put in flight"
    return row[0][0]


async def test_the_replay_fails_when_the_sweep_forgets_its_own_reach_set(
    rig: Rig, traces_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative twin: a conformance replay that cannot go red proves nothing.

    Two queries stand between a referenced object and the move: ``_roots``, which
    keeps it out of the candidate list, and ``_reachable_now``, which asks the
    same question again under the object's own lock immediately before the move.
    Blinding only the first is not a mutation the sweep acts on — the re-check
    still lets the key go — so the whole reach set is blinded here. With it gone
    the sweep moves an object a ready version names, a step ``gc_before_move``
    cannot justify because the recorded scan never put that object in its
    candidate list. TLC has no successor there and names the index.
    """
    _require_tlc()

    from alkera_core.files.gc import _Roots

    async def no_roots(*_args: object, **_kwargs: object) -> _Roots:
        return _Roots()

    async def unreachable(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(Janitor, "_roots", no_roots)
    monkeypatch.setattr(Janitor, "_reachable_now", unreachable)

    recorder = Recorder(rig)
    await recorder.init()
    etag = await _etag(rig, "a.bin")

    async def committer() -> None:
        await rig.content.put_version(
            rig.nodes["a.bin"], _stream(PAYLOAD), size_declared=len(PAYLOAD), if_match=etag
        )

    rig.checkpoints.pause(PUT)
    task = asyncio.create_task(committer())
    await rig.checkpoints.wait_paused(PUT)
    key = await _staged_key(rig)
    session_id = await _open_session_id(rig)
    await recorder.put(session_id, key, staged=f"incoming/{session_id}/object")
    rig.checkpoints.pause(BEFORE_COMMIT)
    rig.checkpoints.release(PUT)
    await rig.checkpoints.wait_paused(BEFORE_COMMIT)
    await recorder.committing(session_id, key)
    rig.checkpoints.release(BEFORE_COMMIT)
    await task
    await recorder.committed(session_id, key)

    await recorder.tick()
    rig.checkpoints.pause(SCAN)
    rig.checkpoints.pause(BEFORE_MOVE)
    sweep = asyncio.create_task(_sweep(rig))
    await rig.checkpoints.wait_paused(SCAN)
    await recorder.scanned()
    assert recorder.candidates == set(), "the model still calls the referenced object reachable"
    await _pin_ages(rig, recorder)
    rig.checkpoints.release(SCAN)

    await rig.checkpoints.wait_paused(BEFORE_MOVE)
    await recorder.before_move(key)
    rig.checkpoints.release(BEFORE_MOVE)
    await sweep

    verdict = check_trace(recorder.states, out_dir=traces_dir, max_clock=recorder.max_clock)
    assert not verdict.ok, "the mutated sweep replayed as a legal behaviour"
    assert verdict.diverged_at is not None
    diverged = recorder.states[verdict.diverged_at]
    assert diverged.action == "gc_before_move", (
        f"TLC got stuck at {diverged.action!r}, not at the move the mutation made illegal"
    )


async def test_a_commit_landing_between_the_scan_and_the_move_is_not_swept(
    rig: Rig, traces_dir: Path
) -> None:
    """The interleaving the reach set cannot cover, replayed against the spec.

    The scan sees the bytes under ``incoming/<session>/`` and the commit
    publishes them onto ``objects/<hash>`` while the sweep is parked — so by
    the time it lists, the object is under a key no root from that scan names,
    and the model's ``writtenAt`` is pinned *before* the stamp so the age
    horizon cannot rescue it either. The sweep must take the candidate back at
    the move, and the recorded behaviour must justify every step under
    ``NoDanglingReference``.
    """
    _require_tlc()
    recorder = Recorder(rig)
    await recorder.init()
    etag = await _etag(rig, "a.bin")

    async def committer() -> None:
        await rig.content.put_version(
            rig.nodes["a.bin"], _stream(PAYLOAD), size_declared=len(PAYLOAD), if_match=etag
        )

    rig.checkpoints.pause(PUT)
    task = asyncio.create_task(committer())
    await rig.checkpoints.wait_paused(PUT)
    key = await _staged_key(rig)
    session_id = await _open_session_id(rig)
    await recorder.put(session_id, key, staged=f"incoming/{session_id}/object")

    await recorder.tick()
    rig.checkpoints.pause(SCAN)
    sweep = asyncio.create_task(_sweep(rig))
    await rig.checkpoints.wait_paused(SCAN)
    await recorder.scanned()
    await _pin_ages(rig, recorder)

    # The commit finishes while the sweep holds a reach set that predates it.
    rig.checkpoints.pause(BEFORE_COMMIT)
    rig.checkpoints.release(PUT)
    await rig.checkpoints.wait_paused(BEFORE_COMMIT)
    await recorder.committing(session_id, key)
    rig.checkpoints.release(BEFORE_COMMIT)
    await task
    await recorder.committed(session_id, key)

    rig.checkpoints.release(SCAN)
    result = await sweep

    assert result.moved == (), "the just-committed object was swept"
    assert result.skipped == (key,), "the sweep did not report taking the candidate back"
    verdict = check_trace(recorder.states, out_dir=traces_dir, max_clock=recorder.max_clock)
    assert verdict.ok, f"InvalidTrace at step {verdict.diverged_at}:\n{verdict.output}"
