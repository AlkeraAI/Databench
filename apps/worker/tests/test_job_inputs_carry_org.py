"""Every background job names the org it runs in.

A Temporal job crosses a process boundary as plain arguments. When one of those
arguments names a tenant row (a chat, a Files operation, a connection, a
machine...), the job must also carry the org it acts in, as ``org_team_id`` or
``org_id``. The activity then acts under that org, scopes its reads to it, and a
row from another org simply is not found. Without it, the job has to guess the
org from somewhere else (the person who asked, the first row it loads) and a
job could run against one org's data under another org's authority.

What the gate reads (the "carriers"):

- the ``run`` signature of every workflow the worker serves, and the signature
  of every activity, as found by the worker's own registries;
- every dataclass or Pydantic model one of those signatures takes, recursively;
- every dataclass or model defined in ``alkera_core.temporal.contract`` and in
  ``alkera_core.schemas.temporal``, used today or not.

The rule, per carrier: a field whose name is in :data:`TENANT_ROW_IDS` requires
a field in :data:`ORG_FIELDS` on the same carrier. A sweep that takes no input,
or only a clock, names no row and passes trivially.

The one exception, :data:`ROW_KEYED`, is an exact set with a reason per entry:
a job keyed by a single row whose org the activity reads from THAT row, inside
its own transaction, before it touches anything else. The row is then the
authority on the org, and a second copy in the input could only disagree with
it. An entry that no longer matches a carrier fails the gate, so the set never
holds a stale excuse.

Every ``*_id`` name a carrier uses must be classified, either as a tenant row
(:data:`TENANT_ROW_IDS`) or as something else (:data:`NOT_TENANT_IDS`, with a
reason), so a new id cannot slip past the rule by having a name nobody listed.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import pkgutil
import types
import typing
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import alkera_core.schemas.temporal as temporal_schemas
import alkera_core.temporal.contract as temporal_contract
import pytest
from pydantic import BaseModel
from temporalio import activity, workflow
from worker.temporal.queues import (
    ALL_ACTIVITIES,
    ALL_WORKFLOWS,
    activity_type_name,
    workflow_type_name,
)

ORG_FIELDS = frozenset({"org_team_id", "org_id"})

TENANT_ROW_IDS = frozenset(
    {
        "allocation_id",
        "chat_id",
        "connection_id",
        "default_machine_id",
        "drive_id",
        "file_id",
        "folder_id",
        "gate_run_id",
        "install_id",
        "installation_row_id",
        "invitation_id",
        "item_id",
        "machine_id",
        "membership_id",
        "move_id",
        "node_id",
        "object_id",
        "op_id",
        "operation_id",
        "sandbox_id",
        "session_id",
        "share_id",
        "snapshot_id",
        "team_id",
        "template_id",
        "verification_id",
        "waiver_id",
        "workspace_id",
        # An identity spans orgs: a job keyed by a person is ambiguous about
        # which of their orgs it serves unless it also names one.
        "user_id",
    }
)

NOT_TENANT_IDS: Mapping[str, str] = {
    "call_id": "a tool call's id inside its session; the carrier's session and org scope it",
    "export_id": "a personal data export is the identity's own, across its orgs; no org owns it",
}

ROW_KEYED: Mapping[str, str] = {
    "workflow connections.run_verification(verification_id)": (
        "the verification row names its team, whose root is its org; the activity "
        "loads the row by id and acts on that row's connection alone"
    ),
    "activity connections.run_verification(verification_id)": (
        "the verification row names its team, whose root is its org; the activity "
        "loads the row by id and acts on that row's connection alone"
    ),
    "workflow github.render_gate_check(gate_run_id)": (
        "the gate run row carries org_team_id; the render reads it from the row and "
        "checks the GitHub installation against that org before posting"
    ),
    "activity github.render_gate_check(gate_run_id)": (
        "the gate run row carries org_team_id; the render reads it from the row and "
        "checks the GitHub installation against that org before posting"
    ),
}


@dataclass(frozen=True, slots=True)
class Carrier:
    """One set of names that crosses the boundary together."""

    name: str
    fields: Mapping[str, Any]


# --- collection ---------------------------------------------------------------


def _is_model(tp: object) -> bool:
    return isinstance(tp, type) and (dataclasses.is_dataclass(tp) or issubclass(tp, BaseModel))


def _models_in(annotation: object) -> Iterable[type]:
    """Every model type an annotation names, through Optional, unions and generics."""
    if _is_model(annotation):
        yield typing.cast(type, annotation)
        return
    origin = typing.get_origin(annotation)
    if origin is None and not isinstance(annotation, types.UnionType):
        return
    for arg in typing.get_args(annotation):
        yield from _models_in(arg)


def _model_fields(model: type) -> dict[str, Any]:
    if issubclass(model, BaseModel):
        return {name: f.annotation for name, f in model.model_fields.items()}
    hints = typing.get_type_hints(model)
    return {f.name: hints.get(f.name, f.type) for f in dataclasses.fields(model)}


def _signature_fields(fn: Callable[..., Any]) -> dict[str, Any]:
    hints = typing.get_type_hints(fn)
    return {
        name: hints.get(name, Any) for name in inspect.signature(fn).parameters if name != "self"
    }


def _model_carrier_name(model: type) -> str:
    return f"model {model.__module__}.{model.__qualname__}"


def _expand(carrier: Carrier, out: dict[str, Carrier]) -> None:
    if carrier.name in out:
        return
    out[carrier.name] = carrier
    for annotation in carrier.fields.values():
        for model in _models_in(annotation):
            _expand(Carrier(_model_carrier_name(model), _model_fields(model)), out)


def _modules_of(package: ModuleType) -> list[ModuleType]:
    found = [package]
    for info in pkgutil.iter_modules(getattr(package, "__path__", [])):
        found.append(importlib.import_module(f"{package.__name__}.{info.name}"))
    return found


def collect_carriers(
    workflows: Iterable[type],
    activities: Iterable[Callable[..., Any]],
    modules: Iterable[ModuleType] = (),
) -> dict[str, Carrier]:
    out: dict[str, Carrier] = {}
    for cls in workflows:
        defn = workflow._Definition.from_class(cls)
        assert defn is not None and defn.run_fn is not None, cls
        _expand(Carrier(f"workflow {workflow_type_name(cls)}", _signature_fields(defn.run_fn)), out)
    for fn in activities:
        _expand(Carrier(f"activity {activity_type_name(fn)}", _signature_fields(fn)), out)
    for module in modules:
        for obj in vars(module).values():
            if _is_model(obj) and obj.__module__ == module.__name__:
                _expand(Carrier(_model_carrier_name(obj), _model_fields(obj)), out)
    return out


def real_carriers() -> dict[str, Carrier]:
    return collect_carriers(
        ALL_WORKFLOWS,
        ALL_ACTIVITIES,
        [temporal_contract, *_modules_of(temporal_schemas)],
    )


# --- the rule -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Findings:
    missing_org: list[str]
    """``"<carrier>(<field>)"`` for a tenant row id with no org beside it."""
    unclassified: list[str]
    """``"<carrier>(<field>)"`` for an ``*_id`` name in neither list."""
    stale_row_keyed: list[str]
    """Exception entries that excuse nothing any more."""


def check(
    carriers: Mapping[str, Carrier],
    *,
    tenant_ids: frozenset[str] = TENANT_ROW_IDS,
    not_tenant: Mapping[str, str] = NOT_TENANT_IDS,
    row_keyed: Mapping[str, str] = ROW_KEYED,
) -> Findings:
    missing: list[str] = []
    unclassified: list[str] = []
    used: set[str] = set()
    for carrier in carriers.values():
        names = set(carrier.fields)
        carries_org = bool(names & ORG_FIELDS)
        for field in sorted(names):
            key = f"{carrier.name}({field})"
            if field.endswith("_id") and field not in ORG_FIELDS:
                if field not in tenant_ids and field not in not_tenant:
                    unclassified.append(key)
            if field in tenant_ids and not carries_org:
                if key in row_keyed:
                    used.add(key)
                else:
                    missing.append(key)
    stale = sorted(set(row_keyed) - used)
    return Findings(sorted(missing), sorted(unclassified), stale)


# --- the gate on the real tree ------------------------------------------------


def test_every_job_input_naming_a_tenant_row_carries_its_org() -> None:
    findings = check(real_carriers())
    assert findings.missing_org == [], (
        "these job inputs name a tenant row but not the org the job runs in; add "
        "org_team_id beside the id (the starter passes the org of the row it is "
        f"about): {findings.missing_org}"
    )


def test_every_id_a_job_takes_is_classified() -> None:
    findings = check(real_carriers())
    assert findings.unclassified == [], (
        "classify each new id name: a tenant row goes in TENANT_ROW_IDS, anything "
        f"else in NOT_TENANT_IDS with a reason: {findings.unclassified}"
    )


def test_the_row_keyed_exceptions_are_exact() -> None:
    findings = check(real_carriers())
    assert findings.stale_row_keyed == [], (
        f"these exceptions excuse no job input any more; remove them: {findings.stale_row_keyed}"
    )
    assert all(reason.strip() for reason in ROW_KEYED.values())
    assert all(reason.strip() for reason in NOT_TENANT_IDS.values())


def test_the_scan_reads_the_real_tree() -> None:
    """A collector that silently found nothing would pass every rule above."""
    carriers = real_carriers()
    assert len([n for n in carriers if n.startswith("workflow ")]) == len(ALL_WORKFLOWS)
    assert len([n for n in carriers if n.startswith("activity ")]) == len(ALL_ACTIVITIES)
    files_input = carriers[_model_carrier_name(temporal_contract.FilesOperationInput)]
    assert set(files_input.fields) == {"op_id", "org_team_id"}
    tool_call = carriers[_model_carrier_name(temporal_schemas.ToolCallActivityInput)]
    assert {"session_id", "org_id"} <= set(tool_call.fields)
    promote = carriers[f"activity {temporal_contract.WorkflowType.FILES_PROMOTE.value}"]
    assert set(promote.fields) == {"op_id", "org_team_id"}


# --- the decoys: the same collector and rule, on jobs built to fail -----------


@dataclass(frozen=True)
class _ChatJob:
    chat_id: str


@dataclass(frozen=True)
class _ChatJobWithOrg:
    chat_id: str
    org_team_id: str


class _NestedChatJob(BaseModel):
    note: str = ""
    inner: list[_ChatJob] = []


@workflow.defn(name="decoy-model-input")
class _DecoyModelInput:
    @workflow.run
    async def run(self, job: _ChatJob | None = None) -> None:
        return None


@workflow.defn(name="decoy-nested-input")
class _DecoyNestedInput:
    @workflow.run
    async def run(self, job: _NestedChatJob) -> None:
        return None


@workflow.defn(name="decoy-good-input")
class _DecoyGoodInput:
    @workflow.run
    async def run(self, job: _ChatJobWithOrg) -> None:
        return None


@activity.defn(name="decoy-scalar")
async def _decoy_scalar(machine_id: str) -> None:
    return None


@activity.defn(name="decoy-scalar-with-org")
async def _decoy_scalar_with_org(machine_id: str, org_id: str) -> None:
    return None


@activity.defn(name="decoy-unlisted-id")
async def _decoy_unlisted_id(widget_id: str, org_team_id: str) -> None:
    return None


@activity.defn(name="decoy-sweep")
async def _decoy_sweep() -> int:
    return 0


def test_decoy_a_model_input_naming_a_row_without_org_is_caught() -> None:
    findings = check(collect_carriers([_DecoyModelInput], []))
    assert findings.missing_org == [f"{_model_carrier_name(_ChatJob)}(chat_id)"]


def test_decoy_a_row_id_nested_in_a_generic_is_caught() -> None:
    findings = check(collect_carriers([_DecoyNestedInput], []))
    assert findings.missing_org == [f"{_model_carrier_name(_ChatJob)}(chat_id)"]


def test_decoy_a_scalar_row_id_without_org_is_caught() -> None:
    findings = check(collect_carriers([], [_decoy_scalar]))
    assert findings.missing_org == ["activity decoy-scalar(machine_id)"]


@pytest.mark.parametrize(
    ("workflows", "activities"),
    [
        pytest.param([_DecoyGoodInput], [], id="model-with-org"),
        pytest.param([], [_decoy_scalar_with_org], id="scalar-with-org"),
        pytest.param([], [_decoy_sweep], id="sweep-without-input"),
    ],
)
def test_decoy_inputs_that_carry_the_org_pass(
    workflows: list[type], activities: list[Callable[..., Any]]
) -> None:
    findings = check(collect_carriers(workflows, activities))
    assert findings.missing_org == []
    assert findings.unclassified == []


def test_decoy_an_unclassified_id_is_caught_even_beside_an_org() -> None:
    findings = check(collect_carriers([], [_decoy_unlisted_id]))
    assert findings.unclassified == ["activity decoy-unlisted-id(widget_id)"]
    assert findings.missing_org == []


def test_decoy_a_row_keyed_exception_excuses_only_its_own_entry() -> None:
    carriers = collect_carriers([], [_decoy_scalar])
    excused = check(carriers, row_keyed={"activity decoy-scalar(machine_id)": "reason"})
    assert excused.missing_org == []
    assert excused.stale_row_keyed == []
    other = check(carriers, row_keyed={"activity decoy-other(machine_id)": "reason"})
    assert other.missing_org == ["activity decoy-scalar(machine_id)"]
    assert other.stale_row_keyed == ["activity decoy-other(machine_id)"]
