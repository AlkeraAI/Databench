"""The allocation lifecycle is TOTAL: every state is classified in one place.

``draining`` cost real money because the model's docstring said a draining box
was metered while the meter's own state tuple did not name it — the lifecycle
was classified twice and the two copies drifted. These tests hold the single
classification to its word:

- the three classes PARTITION the vocabulary (disjoint, and their union is it);
- every state the compute source actually writes to, or compares
  ``ComputeAllocation.state`` against, is in that vocabulary — written as a
  literal OR as a named constant, which is the style these modules use — so a
  new state nobody classified fails here instead of falling silently out of
  the meter;
- the money-bearing tuples are consistent with the partition.

The source scan is AST-based, not a grep: it reads assignments, comparisons and
``ComputeAllocation(state=...)`` keywords out of the modules that own the
lifecycle, resolving named constants (the model's own and each module's) to the
states they hold, so adding a fourth writer of the column is caught the moment
it names a state the model does not know. A name it cannot resolve is REPORTED,
not skipped, so the failure mode is a red test rather than a blind spot.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
from alkera_core.models.compute import (
    ASLEEP,
    BOOTSTRAPPING,
    COMPUTE_ACTIVE_STATES,
    COMPUTE_ALLOCATION_STATES,
    COMPUTE_BILLABLE_STATES,
    COMPUTE_DORMANT_STATES,
    COMPUTE_METERED_STATES,
    COMPUTE_PRE_PROVISION_STATES,
    COMPUTE_STORAGE_METERED_STATES,
    COMPUTE_TERMINAL_STATES,
    DRAINING,
    PENDING,
    PROVISIONING,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]

#: The modules that own an allocation's state — everything that writes the
#: column or branches on it. A new one is added here when it appears.
_LIFECYCLE_SOURCES = (
    "packages/api-core/alkera_core/models/compute.py",
    "packages/api-core/alkera_core/billing/compute_meter.py",
    "packages/api-core/alkera_core/compute/machines.py",
    "packages/api-core/alkera_core/compute/reconcile.py",
    "packages/api-core/alkera_core/compute/org_machines.py",
    "apps/backend/backend/services/compute/service.py",
    "apps/backend/backend/services/compute/machines.py",
    "apps/backend/backend/services/compute/grants.py",
    "apps/backend/backend/api/admin/machines.py",
)


def _model_constants() -> dict[str, list[str]]:
    """Every state-valued constant the model exports, name → the states it names.

    This is what lets the scan see through the style the code actually writes.
    ``alloc.state = FAILED`` is an ``ast.Name``, not a literal, so a scanner
    that only understood literals went blind the moment the modules it watches
    were tidied up to use the constants — which is exactly what happened.
    Tuples resolve too, so ``…state.in_(COMPUTE_TERMINAL_STATES)`` reads as the
    states it holds rather than as an unknown name."""
    module = importlib.import_module("alkera_core.models.compute")
    out: dict[str, list[str]] = {}
    for name, value in vars(module).items():
        if not name.isupper():
            continue
        if isinstance(value, str):
            out[name] = [value]
        elif isinstance(value, tuple | list | frozenset | set) and all(
            isinstance(v, str) for v in value
        ):
            out[name] = sorted(value)
    return out


class _StateLiterals(ast.NodeVisitor):
    """Every state this module uses as an allocation state — written as a
    literal, or as a named constant this scan can resolve."""

    def __init__(self, bindings: dict[str, list[str]] | None = None) -> None:
        self.found: set[str] = set()
        #: Module-level name → states bindings, seeded with the model's own
        #: constants (which the watched modules import by name).
        self.bindings: dict[str, list[str]] = dict(bindings or {})

    @staticmethod
    def _is_state_attr(node: ast.expr) -> bool:
        return isinstance(node, ast.Attribute) and node.attr == "state"

    def _strings(self, node: ast.expr) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.Name):
            # ``alloc.state = FAILED`` — resolve the constant, or report the
            # NAME itself so an unresolvable one fails loudly instead of
            # vanishing. A name that is not a constant at all would be a false
            # positive, so only screaming-case names are treated as candidates.
            if node.id in self.bindings:
                return list(self.bindings[node.id])
            return [node.id] if node.id.isupper() else []
        if isinstance(node, ast.Starred):  # ``(*COMPUTE_METERED_STATES, …)``
            return self._strings(node.value)
        if isinstance(node, ast.Tuple | ast.List | ast.Set):
            out: list[str] = []
            for element in node.elts:
                out.extend(self._strings(element))
            return out
        if isinstance(node, ast.IfExp):  # ``"released" if … else "failed"``
            return [*self._strings(node.body), *self._strings(node.orelse)]
        return []

    def _bind_imports(self, node: ast.Module) -> None:
        """Follow ``from … import DRAINING as DRAINING_STATE``.

        A module is free to rename a state constant on the way in, and
        ``compute/machines.py`` does exactly that. Without this the renamed
        name is unresolvable, which the scan reports as an unknown state — the
        right failure, but for the wrong reason."""
        for stmt in ast.walk(node):
            if not isinstance(stmt, ast.ImportFrom):
                continue
            for alias in stmt.names:
                if alias.asname and alias.name in self.bindings:
                    self.bindings[alias.asname] = list(self.bindings[alias.name])

    def _bind_module_constants(self, node: ast.Module) -> None:
        """Seed this module's own ``NAME = …`` constants.

        Two passes, because a module may alias one constant to another before
        or after defining it (``_METERABLE_STATES = COMPUTE_METERED_STATES``),
        and an alias is only resolvable once its target is known."""
        for _ in range(2):
            for stmt in node.body:
                targets: list[ast.expr] = []
                value: ast.expr | None = None
                if isinstance(stmt, ast.Assign):
                    targets, value = list(stmt.targets), stmt.value
                elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                    targets, value = [stmt.target], stmt.value
                if value is None:
                    continue
                resolved = self._strings(value)
                if not resolved:
                    continue
                for target in targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        self.bindings[target.id] = resolved

    def visit_Module(self, node: ast.Module) -> None:
        self._bind_imports(node)
        self._bind_module_constants(node)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if any(self._is_state_attr(t) for t in node.targets):
            self.found.update(self._strings(node.value))
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if self._is_state_attr(node.left):
            for comparator in node.comparators:
                self.found.update(self._strings(comparator))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # ``ComputeAllocation(state="…")`` and ``…state.in_(("a", "b"))``.
        if isinstance(node.func, ast.Name) and node.func.id == "ComputeAllocation":
            for kw in node.keywords:
                if kw.arg == "state":
                    self.found.update(self._strings(kw.value))
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "in_"
            and self._is_state_attr(node.func.value)
        ):
            for arg in node.args:
                self.found.update(self._strings(arg))
        self.generic_visit(node)


def _literals(path: Path, *, bindings: dict[str, list[str]] | None = None) -> set[str]:
    visitor = _StateLiterals(bindings if bindings is not None else _model_constants())
    visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
    return visitor.found


def test_the_classes_partition_the_vocabulary() -> None:
    classes = (
        set(COMPUTE_PRE_PROVISION_STATES),
        set(COMPUTE_METERED_STATES),
        set(COMPUTE_DORMANT_STATES),
        set(COMPUTE_TERMINAL_STATES),
    )
    union: set[str] = set()
    for one in classes:
        assert not union & one, f"a state is classified twice: {sorted(union & one)}"
        union |= one
    assert union == set(COMPUTE_ALLOCATION_STATES)
    # No duplicates inside any tuple, and the vocabulary is spelled once.
    assert len(COMPUTE_ALLOCATION_STATES) == len(set(COMPUTE_ALLOCATION_STATES))


def test_asleep_is_dormant_kept_but_never_metered_and_never_terminal() -> None:
    """The power axis's ``asleep``: stopped and unbilled, so out of the metered
    set; kept for its org, so it still counts against the grant ceiling; not an
    end state, so it can wake back to a live one."""
    assert ASLEEP in COMPUTE_DORMANT_STATES
    assert ASLEEP not in COMPUTE_METERED_STATES
    assert ASLEEP not in COMPUTE_BILLABLE_STATES
    assert ASLEEP not in COMPUTE_TERMINAL_STATES
    assert ASLEEP in COMPUTE_ACTIVE_STATES


def test_a_draining_box_is_metered_because_a_draining_box_costs_money() -> None:
    """The contract the model's docstring states, asserted against the tuple
    the meter actually enumerates."""
    assert DRAINING in COMPUTE_METERED_STATES
    assert DRAINING in COMPUTE_BILLABLE_STATES
    assert DRAINING in COMPUTE_ACTIVE_STATES
    assert DRAINING not in COMPUTE_TERMINAL_STATES


def test_every_storage_billed_state_is_a_known_state_that_holds_a_volume() -> None:
    """Storage is billed while the provider holds the volume of a machine that
    came up: a stopped machine keeps its disk, so ``asleep`` bills storage
    though it bills no minutes; a start still coming up bills none, nothing
    exists before provisioning and nothing after the end."""
    assert set(COMPUTE_STORAGE_METERED_STATES) <= set(COMPUTE_ALLOCATION_STATES)
    assert len(set(COMPUTE_STORAGE_METERED_STATES)) == len(COMPUTE_STORAGE_METERED_STATES)
    assert ASLEEP in COMPUTE_STORAGE_METERED_STATES
    assert set(COMPUTE_BILLABLE_STATES) <= set(COMPUTE_STORAGE_METERED_STATES)
    assert not {PENDING, PROVISIONING, BOOTSTRAPPING} & set(COMPUTE_STORAGE_METERED_STATES)
    assert not set(COMPUTE_STORAGE_METERED_STATES) & set(COMPUTE_TERMINAL_STATES)


def test_every_billable_state_is_metered_and_no_terminal_state_is() -> None:
    assert set(COMPUTE_BILLABLE_STATES) <= set(COMPUTE_METERED_STATES)
    assert not set(COMPUTE_TERMINAL_STATES) & set(COMPUTE_METERED_STATES)
    # A state that counts against a grant's ceiling is never a terminal one.
    assert not set(COMPUTE_ACTIVE_STATES) & set(COMPUTE_TERMINAL_STATES)
    assert set(COMPUTE_ACTIVE_STATES) <= set(COMPUTE_ALLOCATION_STATES)


@pytest.mark.parametrize("relative", _LIFECYCLE_SOURCES)
def test_every_state_the_source_names_is_classified(relative: str) -> None:
    path = _REPO_ROOT / relative
    if not path.exists():  # a checkout without that app (the package installed alone)
        pytest.skip(f"{relative} is not in this checkout")
    unknown = _literals(path) - set(COMPUTE_ALLOCATION_STATES)
    assert not unknown, (
        f"{relative} uses allocation states nothing classifies: {sorted(unknown)}. "
        "Add each to one — and only one — of COMPUTE_PRE_PROVISION_STATES, "
        "COMPUTE_METERED_STATES, COMPUTE_DORMANT_STATES or COMPUTE_TERMINAL_STATES."
    )


def test_the_scanner_would_catch_an_unclassified_state_written_as_a_literal(
    tmp_path: Path,
) -> None:
    """The guard above is only worth its green if it goes red on a new state:
    four shapes of writer, each one introducing a state nobody classified."""
    module = tmp_path / "rogue.py"
    module.write_text(
        "def f(alloc):\n"
        "    alloc.state = 'quiesced'\n"
        "    if alloc.state == 'hibernating':\n"
        "        pass\n"
        "    ComputeAllocation(state='parked')\n"
        "    q.where(ComputeAllocation.state.in_(('evicted', 'ready')))\n",
        encoding="utf-8",
    )
    found = _literals(module)
    assert {"quiesced", "hibernating", "parked", "evicted"} <= found
    assert found - set(COMPUTE_ALLOCATION_STATES) == {
        "quiesced",
        "hibernating",
        "parked",
        "evicted",
    }


def test_the_scanner_sees_through_named_constants(tmp_path: Path) -> None:
    """The style the compute modules actually write. A scanner that understood
    only literals was blind to every one of these — and the code was tidied
    onto constants in the same change that added the scan, so the guard had
    quietly stopped watching the modules it names."""
    module = tmp_path / "named.py"
    module.write_text(
        'QUIESCED = "quiesced"\n'
        'HIBERNATING = "hibernating"\n'
        'PARKED = "parked"\n'
        "ODD_STATES = (QUIESCED, HIBERNATING)\n"
        "def f(alloc):\n"
        "    alloc.state = QUIESCED\n"
        "    if alloc.state == HIBERNATING:\n"
        "        pass\n"
        "    ComputeAllocation(state=PARKED)\n"
        "    q.where(ComputeAllocation.state.in_(ODD_STATES))\n",
        encoding="utf-8",
    )
    found = _literals(module)
    assert {"quiesced", "hibernating", "parked"} <= found
    assert found - set(COMPUTE_ALLOCATION_STATES) == {"quiesced", "hibernating", "parked"}


def test_a_state_added_to_the_model_and_used_but_never_classified_is_caught(
    tmp_path: Path,
) -> None:
    """The scenario the guard exists for, end to end: someone adds a state
    constant to the model, imports it by name and writes it to the column, but
    never puts it in one of the three classes. The scan must fail — which is
    what stops the new state from falling silently out of the meter, the way
    ``draining`` did.

    The model is read as it really is and the new constant is added to the
    binding map exactly as an import of it would resolve, so nothing here
    depends on editing the shipped module."""
    bindings = dict(_model_constants())
    bindings["QUIESCED"] = ["quiesced"]  # the new constant, imported by name
    writer = tmp_path / "writer.py"
    writer.write_text(
        "from alkera_core.models.compute import QUIESCED\n"
        "def stop(alloc):\n"
        "    alloc.state = QUIESCED\n",
        encoding="utf-8",
    )

    unknown = _literals(writer, bindings=bindings) - set(COMPUTE_ALLOCATION_STATES)

    assert unknown == {"quiesced"}, (
        "a state added to the model and written to the column, but classified "
        "nowhere, must fail the scan"
    )
    # And the moment it IS classified, the same scan is quiet.
    assert not (
        _literals(writer, bindings=bindings) - (set(COMPUTE_ALLOCATION_STATES) | {"quiesced"})
    )


def test_a_state_constant_renamed_on_import_still_resolves(tmp_path: Path) -> None:
    """``from alkera_core.models.compute import DRAINING as DRAINING_STATE`` —
    the real spelling in ``compute/machines.py``. The rename must not read as
    an unknown state, or the guard cries wolf on every module that tidies an
    import."""
    module = tmp_path / "aliased.py"
    module.write_text(
        "from alkera_core.models.compute import DRAINING as DRAINING_STATE\n"
        "def f(alloc):\n"
        "    alloc.state = DRAINING_STATE\n",
        encoding="utf-8",
    )
    found = _literals(module)
    assert found == {"draining"}
    assert not found - set(COMPUTE_ALLOCATION_STATES)


def test_an_unresolvable_constant_is_reported_rather_than_skipped(tmp_path: Path) -> None:
    """Fail loud, not silent: a state written as a name the scan cannot resolve
    (a cross-module constant it does not import) is reported as unknown, so the
    answer is a red test to investigate rather than a blind spot."""
    module = tmp_path / "opaque.py"
    module.write_text(
        "def f(alloc):\n    alloc.state = SOME_OTHER_MODULE_STATE\n", encoding="utf-8"
    )
    assert _literals(module) == {"SOME_OTHER_MODULE_STATE"}
