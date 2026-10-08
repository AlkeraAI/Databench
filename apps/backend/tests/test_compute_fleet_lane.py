"""Which compute modules share a worker, and which only clean up after
themselves.

A fleet-wide pass acts on EVERY meterable allocation in the database, so two of
them must never run at once inside one; that is what the shared ``compute-fleet``
xdist group is for, and it is a serial lane by construction. A module that only
CREATES allocations needs the cleanup, not the lane — so it declares
``compute_rows`` instead and is handed out in parallel like everything else.

Both halves are pinned here: the declaration (read off the modules themselves,
so a new compute module that forgets cannot pass) and the cleanup it buys (a
live box this module put on the plane is off it before the next test runs).

Neither the passes nor the group name are restated. The passes are the ones the
worker's own compute family runs, read off ``worker/tasks/compute.py`` — a new
scheduled pass joins the rule by being scheduled — and the group is the
constant the conftest defines, read out of each module's ``pytestmark`` as a
mark rather than matched as a substring. A call made through a shared test
helper counts for every module that imports the helper, so moving a pass one
call deep does not take a module out of the rule.
"""

from __future__ import annotations

import ast
import uuid
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models.compute import COMPUTE_METERED_STATES, ComputeAllocation, ComputeMachineType
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import COMPUTE_FLEET_GROUP, COMPUTE_ROWS_MARK, OrgWithAdmin

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

TESTS = Path(__file__).resolve().parent
_REPO = TESTS.parents[2]
#: The worker family that schedules the compute passes, and the package they
#: live in: between them they say which functions are fleet-wide.
WORKER_COMPUTE = _REPO / "apps" / "worker" / "worker" / "tasks" / "compute.py"
COMPUTE_PACKAGE = _REPO / "packages" / "api-core" / "alkera_core" / "compute"

#: Written by the first test below and read by the second: the box this module
#: put on the plane, which nothing in the second test creates.
_LEFT_RUNNING: list[uuid.UUID] = []

#: A shared helper that runs a fleet-wide pass, and the two shapes of module
#: that reach it or do not. Sources rather than files: what the rules read is
#: a parsed module, so a case can hand the detector one directly.
HELPER_THAT_SWEEPS = """
from alkera_core.compute.meter import meter_and_cutoff


async def sweep_everything(db):
    return await meter_and_cutoff(db)
"""

USES_THE_HELPER = """
from tests._planted import sweep_everything


async def test_x(db):
    await sweep_everything(db)
"""

USES_NOTHING = """
from tests.conftest import OrgWithAdmin


async def test_x():
    assert OrgWithAdmin
"""

MENTIONS_THE_GROUP = """
import pytest

#: Deliberately not in the compute-fleet group: it runs no pass.
pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]
"""

DECLARES_THE_GROUP = """
import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group("compute-fleet")]
"""

DECLARES_BY_CONSTANT = """
import pytest

from tests.conftest import COMPUTE_FLEET_GROUP

pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group(COMPUTE_FLEET_GROUP)]
"""

DECLARES_BY_KEYWORD = """
import pytest

pytestmark = pytest.mark.xdist_group(name="compute-fleet")
"""


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@cache
def _session_first_functions() -> frozenset[str]:
    """Every public ``alkera_core.compute`` coroutine whose first argument is
    the session — the shape of something that acts on rows it selects itself,
    rather than on a subject it was handed."""
    found: set[str] = set()
    for path in sorted(COMPUTE_PACKAGE.glob("*.py")):
        for node in _tree(path).body:
            if not isinstance(node, ast.AsyncFunctionDef) or node.name.startswith("_"):
                continue
            args = [arg.arg for arg in node.args.args]
            if args and args[0] in ("db", "session"):
                found.add(node.name)
    return frozenset(found)


@cache
def fleet_wide_passes() -> frozenset[str]:
    """The passes the worker's compute family actually runs on a schedule.

    Derived rather than listed: the worker is where a fleet-wide pass becomes
    one, so a pass added to that family joins this rule without anybody
    remembering to. The session-first filter drops the helpers it imports
    alongside them (an API key, a provider lookup), which act on nothing.
    """
    imported: set[str] = set()
    for node in ast.walk(_tree(WORKER_COMPUTE)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "alkera_core.compute"
        ):
            imported |= {alias.name for alias in node.names}
    return frozenset(imported & _session_first_functions())


def _module_marks(tree: ast.Module) -> list[ast.expr]:
    """Every mark in a module-level ``pytestmark``, as an expression."""
    marks: list[ast.expr] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            continue
        value = node.value
        marks.extend(value.elts if isinstance(value, ast.List | ast.Tuple) else [value])
    return marks


#: What a module may call the shared group besides writing it out, and what
#: each of those names is worth: the conftest docstring tells people to import
#: the constant, so a module that did exactly as it was told has to count — and
#: the name is resolved to its value rather than merely recognised, so asking
#: about one group cannot be answered by a module that declared another.
GROUP_CONSTANTS: dict[str, str] = {"COMPUTE_FLEET_GROUP": COMPUTE_FLEET_GROUP}


def _names_the_group(arg: ast.expr, group: str) -> bool:
    if isinstance(arg, ast.Constant):
        return arg.value == group
    if isinstance(arg, ast.Name):
        return GROUP_CONSTANTS.get(arg.id) == group
    if isinstance(arg, ast.Attribute):
        return GROUP_CONSTANTS.get(arg.attr) == group
    return False


def _declares_group(tree: ast.Module, group: str) -> bool:
    """Whether the module's ``pytestmark`` names ``group`` as its xdist group.

    Parsed as a mark, not matched as text: a module that mentions the group in
    a docstring does not declare it. The constant counts as well as the string,
    because the conftest prescribes the constant and a rule that accepted only
    the literal would report a module written exactly as instructed as missing
    the group it is in.
    """
    for mark in _module_marks(tree):
        if not isinstance(mark, ast.Call) or not isinstance(mark.func, ast.Attribute):
            continue
        if mark.func.attr != "xdist_group":
            continue
        named = [*mark.args, *(kw.value for kw in mark.keywords if kw.arg == "name")]
        if any(_names_the_group(arg, group) for arg in named):
            return True
    return False


def _declares_mark(tree: ast.Module, name: str) -> bool:
    return any(
        isinstance(mark, ast.Attribute) and mark.attr == name for mark in _module_marks(tree)
    )


def _called_names(tree: ast.Module) -> set[str]:
    """Every name this module calls, however it spells the call."""
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
    return called


def _helpers_under(root: Path) -> frozenset[str]:
    """The suite's own shared modules that call a fleet-wide pass.

    A pass reached through a helper is still a pass the module runs. Read from
    the whole tree, subpackages included: the backend suite keeps helpers in
    ``apps/backend/tests/files/``, ``apps/backend/tests/chat/`` and others, and a
    scan of the top level alone would let a pass hide one directory down.
    """
    passes = fleet_wide_passes()
    return frozenset(
        path.stem
        for path in sorted(root.rglob("*.py"))
        if not path.name.startswith("test_") and _called_names(_tree(path)) & passes
    )


@cache
def _helpers_that_run_a_pass() -> frozenset[str]:
    """The suite's own shared modules, as the rules read them."""
    return _helpers_under(TESTS)


def _imported_modules(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add((node.module or "").rsplit(".", 1)[-1])
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.Import):
            names |= {alias.name.rsplit(".", 1)[-1] for alias in node.names}
    return names


def _runs_a_fleet_wide_pass(
    path: Path, tree: ast.Module, helpers: frozenset[str] | set[str] | None = None
) -> bool:
    if path.name == Path(__file__).name:
        return False
    if _called_names(tree) & fleet_wide_passes():
        return True
    return bool(
        _imported_modules(tree) & (_helpers_that_run_a_pass() if helpers is None else helpers)
    )


def _test_modules() -> list[tuple[Path, ast.Module]]:
    return [(path, _tree(path)) for path in sorted(TESTS.glob("test_*.py"))]


async def test_the_passes_this_rule_is_about_are_the_ones_the_worker_runs() -> None:
    """The derivation, not a restatement: a rule that read its subject off an
    empty set would pass over every module in the suite."""
    passes = fleet_wide_passes()

    assert "meter_and_cutoff" in passes
    assert len(passes) >= 3
    # The single-allocation half of the same module is NOT a fleet-wide pass;
    # a rule that swept it in would put half the suite in the serial lane.
    assert "settle_final" not in passes


async def test_every_module_that_runs_a_fleet_wide_pass_shares_the_lane() -> None:
    """The rule the lane exists for. A compute module added without it is how
    two passes come to overlap again — and the failure it causes lands in some
    OTHER module, which is why it has to be caught by reading, not by running."""
    missing = sorted(
        path.name
        for path, tree in _test_modules()
        if _runs_a_fleet_wide_pass(path, tree) and not _declares_group(tree, COMPUTE_FLEET_GROUP)
    )

    assert missing == [], (
        f"{missing} run a fleet-wide pass but do not declare "
        f'pytest.mark.xdist_group("{COMPUTE_FLEET_GROUP}")'
    )


async def test_nothing_in_the_serial_lane_is_there_without_running_a_pass() -> None:
    """The other direction, and the one that keeps the lane small. The lane is
    ONE worker: every module in it that runs no pass is a module the suite has
    to wait for in series for no reason. ``compute_rows`` is what such a module
    declares instead — it buys the cleanup and keeps its own worker."""
    idle = sorted(
        path.name
        for path, tree in _test_modules()
        if _declares_group(tree, COMPUTE_FLEET_GROUP) and not _runs_a_fleet_wide_pass(path, tree)
    )

    assert idle == [], (
        f"{idle} sit in the serial {COMPUTE_FLEET_GROUP} lane without running a "
        f"fleet-wide pass; declare pytest.mark.{COMPUTE_ROWS_MARK} instead"
    )


async def test_a_pass_reached_through_a_shared_helper_still_counts() -> None:
    """The escape a substring match leaves open. A module that calls a pass
    through one of the suite's own helpers runs it just as much as one that
    names it, so the detector is asked about a module that does exactly that
    and about one that touches nothing."""
    helper = ast.parse(HELPER_THAT_SWEEPS)
    through_a_helper = ast.parse(USES_THE_HELPER)
    innocent = ast.parse(USES_NOTHING)

    assert _called_names(helper) & fleet_wide_passes()
    assert _runs_a_fleet_wide_pass(TESTS / "test_planted.py", through_a_helper, {"_planted"})
    assert not _runs_a_fleet_wide_pass(TESTS / "test_planted.py", innocent, {"_planted"})


async def test_the_group_counts_however_the_conftest_told_the_module_to_spell_it() -> None:
    """The conftest tells people to declare the group with the constant. A rule
    that accepted only the literal string would read a module that did exactly
    that as missing the group — failing safe, but naming the wrong module and
    contradicting the instruction it was following."""
    by_constant = ast.parse(DECLARES_BY_CONSTANT)
    by_keyword = ast.parse(DECLARES_BY_KEYWORD)

    assert _declares_group(by_constant, COMPUTE_FLEET_GROUP)
    assert _declares_group(by_keyword, COMPUTE_FLEET_GROUP)
    # And the name is worth its VALUE, not merely recognised: asked about some
    # other group, a module that declared this one answers no.
    assert not _declares_group(by_constant, "some-other-group")
    assert not _declares_group(by_keyword, "some-other-group")


async def test_a_helper_in_a_subpackage_is_found_too(tmp_path: Path) -> None:
    """The suite keeps helpers under ``apps/backend/tests/files/``, ``apps/backend/tests/chat/`` and
    others, so a scan of the top level alone lets a pass hide one directory
    down — and a module reaching it would read as touching nothing."""
    (tmp_path / "top_level.py").write_text(USES_NOTHING, encoding="utf-8")
    nested = tmp_path / "sub" / "deeper"
    nested.mkdir(parents=True)
    (nested / "_buried.py").write_text(HELPER_THAT_SWEEPS, encoding="utf-8")
    (nested / "test_not_a_helper.py").write_text(HELPER_THAT_SWEEPS, encoding="utf-8")

    found = _helpers_under(tmp_path)

    assert found == {"_buried"}, "a helper one directory down is still a helper"


async def test_a_module_that_only_mentions_the_group_does_not_declare_it() -> None:
    """Read as a mark, not as text. A module whose docstring or comment names
    the group is not in it — and a substring match would put it there, which
    is how a module ends up serialized for a sentence it wrote about itself."""
    talked_about = ast.parse(MENTIONS_THE_GROUP)
    declared = ast.parse(DECLARES_THE_GROUP)

    assert not _declares_group(talked_about, COMPUTE_FLEET_GROUP)
    assert _declares_group(declared, COMPUTE_FLEET_GROUP)
    assert _declares_mark(talked_about, COMPUTE_ROWS_MARK)


async def _machine_type(db: AsyncSession, **kw: Any) -> ComputeMachineType:
    row = ComputeMachineType(
        provider="runpod",
        provider_type_id=f"T-{uuid.uuid4().hex[:8]}",
        display_name="Lane",
        compute_class="gpu",
        gpu_count=1,
        provider_price_per_minute_nanos=1000,
        available_for_new=True,
        availability="unknown",
        **kw,
    )
    db.add(row)
    await db.flush()
    return row


async def test_a_module_outside_the_lane_still_leaves_a_live_box_behind_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Put a ready box on the plane and commit it, exactly as the modules that
    left the lane do. Leaving it is the point: the next test is what proves it
    does not survive."""
    machine_type = await _machine_type(real_session)
    alloc = ComputeAllocation(
        user_id=org_admin.admin_id,
        org_team_id=org_admin.org_id,
        machine_type_id=machine_type.id,
        state="ready",
        provider_machine_id=f"pod-{uuid.uuid4().hex[:8]}",
        price_per_minute_nanos=700,
        true_cost_per_minute_nanos=1000,
    )
    real_session.add(alloc)
    await real_session.commit()
    _LEFT_RUNNING.append(alloc.id)

    assert alloc.state in COMPUTE_METERED_STATES


async def test_the_box_the_previous_test_left_is_off_the_plane() -> None:
    """The cleanup ``compute_rows`` buys, and the whole reason a module may
    leave the serial lane: a worker runs its modules one after another in ONE
    database, so a box left running here would be swept by a later module's
    fleet-wide pass — under that module's frozen clock, in the middle of its
    assertions."""
    assert _LEFT_RUNNING, "the previous test did not run: this pair must stay in one module"
    async with AsyncSessionLocal() as session:
        state = (
            await session.execute(
                select(ComputeAllocation.state).where(ComputeAllocation.id == _LEFT_RUNNING[0])
            )
        ).scalar_one()

    assert state == "released"
    assert state not in COMPUTE_METERED_STATES
