"""How a notebook's view is put together from the document, the kernel's
latest snapshot and the recorded runs: each cell's run state, every run and
caret named by who made it, and the cells a run target names.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeVar

from alkera_core.models import User
from alkera_core.notebooks import edits as edit_log
from alkera_core.notebooks.models import NotebookEdit, NotebookRun
from alkera_core.notebooks.schemas import ActorRef
from alkera_notebook.engine.models import (
    CellState,
    GraphSummary,
    KernelInfo,
    Presence,
    QueuedRun,
    RunAttribution,
    Settings,
)
from alkera_notebook.format.settings import setting_sources
from pydantic import BaseModel, ValidationError

from backend.services.notebooks import names
from backend.services.notebooks.callers import Caller
from backend.services.notebooks.carets import Caret

ModelT = TypeVar("ModelT", bound=BaseModel)


def graph_summary(graph: Mapping[str, Any] | None) -> GraphSummary:
    """The diagnostics an operations result carries: the graph analysed at
    exactly the result's state, when this replica has it (the analysis runs
    after commits, debounced, so a fresh result usually says not yet)."""
    return GraphSummary.pending() if graph is None else GraphSummary.of_analysis(graph)


#: Settings the file records, read one by one so a value the document holds
#: but the engine does not know is dropped rather than failing the whole view.
_SETTING_KEYS: Final = tuple(k for k in Settings.model_fields if k != "sources")


def view_settings(view: Mapping[str, Any]) -> Settings:
    """The document's settings as the engine's model."""
    raw = view.get("settings")
    found: dict[str, Any] = dict(raw) if isinstance(raw, Mapping) else {}
    found["format"] = str(view.get("format") or "1.0")
    kept: dict[str, Any] = {}
    for key in _SETTING_KEYS:
        if key not in found or found[key] is None:
            continue
        try:
            Settings.model_validate({key: found[key]})
        except ValidationError:
            continue
        kept[key] = found[key]
    stored = {k: v for k, v in kept.items() if k != "format"}
    return Settings.model_validate({**kept, "sources": setting_sources(stored)})


def snapshot_view(snapshot: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    view = None if snapshot is None else snapshot.get("view")
    return view if isinstance(view, Mapping) else None


#: What a cell's run state is made of, as the kernel's snapshot reports it.
_RUN_STATE_KEYS: Final = (
    "status",
    "defs",
    "refs",
    "graph_errors",
    "output",
    "output_outdated",
    "output_origin",
    "outputs",
    "last_run",
)


#: What of a cell's run state outlives the kernel that made it: its outputs
#: and who ran it. Its status does not (a cell is ``not_run`` by a kernel
#: that has not run it).
_KEPT_ACROSS_KERNELS: Final = ("output", "outputs", "output_outdated", "last_run")


def run_state(
    snapshot: Mapping[str, Any] | None, kernel: KernelInfo
) -> dict[str, Mapping[str, Any]]:
    """``cell id -> its run state`` from the kernel's latest snapshot. Of the
    notebook's current kernel, all of it; of a kernel that is gone (a box
    that restarted, a workspace put to sleep), only the outputs, as saved
    ones, so a notebook reopened later still shows what it last showed."""
    if snapshot is None:
        return {}
    reported = snapshot.get("kernel")
    reported_id = reported.get("kernel_id") if isinstance(reported, Mapping) else None
    current = kernel.kernel_id is not None and reported_id == kernel.kernel_id
    found: dict[str, Mapping[str, Any]] = {}
    for cell in snapshot.get("cells") or []:
        if not isinstance(cell, Mapping) or not isinstance(cell.get("id"), str):
            continue
        if current:
            found[cell["id"]] = cell
            continue
        kept = {k: cell[k] for k in _KEPT_ACROSS_KERNELS if k in cell}
        if kept.get("output") is not None or kept.get("outputs"):
            found[cell["id"]] = {**kept, "output_origin": "saved"}
    return found


def _display_data(output: Any) -> dict[str, Any] | None:
    """The MIME bundle of one live output, when it is a rich one."""
    if not isinstance(output, Mapping) or output.get("type") != "display":
        return None
    data = output.get("data")
    return dict(data) if isinstance(data, Mapping) else None


def live_bundles(
    snapshot: Mapping[str, Any] | None, events: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Every rich output the cells show now, as this replica heard them: the
    kernel's latest snapshot with the ``cell.output`` and
    ``cell.outputs_cleared`` events after it folded in, the way a reader's
    view folds them (``replace`` starts the cell's outputs over)."""
    shown: dict[str, list[dict[str, Any]]] = {}
    for cell in (snapshot or {}).get("cells") or []:
        if not isinstance(cell, Mapping) or not isinstance(cell.get("id"), str):
            continue
        bundles = [_display_data(item) for item in cell.get("outputs") or []]
        shown[cell["id"]] = [bundle for bundle in bundles if bundle is not None]
    for event in events:
        kind = event.get("type")
        if kind == "cell.outputs_cleared":
            for cell_id in event.get("cell_ids") or []:
                shown.pop(str(cell_id), None)
            continue
        cell_id = event.get("cell_id")
        bundle = _display_data(event.get("output"))
        if kind != "cell.output" or not isinstance(cell_id, str) or bundle is None:
            continue
        base = shown.get(cell_id, []) if event.get("mode") == "append" else []
        shown[cell_id] = [*base, bundle]
    return [bundle for bundles in shown.values() for bundle in bundles]


@dataclass(frozen=True, slots=True)
class NamedRun:
    """A recorded run and who ran it, named."""

    actor: ActorRef
    run: NotebookRun


def with_by(model: type[ModelT], data: Mapping[str, Any], actor: ActorRef) -> ModelT:
    """``data`` as ``model`` with ``by`` naming ``actor``: the whole actor
    (kind, id, name and whom an agent acts for) where the engine's model
    carries one, its name where it carries a string."""
    try:
        return model.model_validate({**data, "by": actor.model_dump(mode="json")})
    except ValidationError:
        return model.model_validate({**data, "by": actor.display_name})


def last_run_id(state: Mapping[str, Any]) -> str | None:
    last = state.get("last_run")
    run_id = last.get("run_id") if isinstance(last, Mapping) else None
    return run_id if isinstance(run_id, str) else None


def named_queue(kernel: KernelInfo, named: Mapping[str, NamedRun]) -> KernelInfo:
    """The kernel's queue with each recorded run's requester named."""
    if not kernel.queue:
        return kernel
    queue: list[QueuedRun] = []
    for queued in kernel.queue:
        found = named.get(queued.run_id)
        if found is None:
            queue.append(queued)
            continue
        try:
            queue.append(with_by(QueuedRun, queued.model_dump(mode="json"), found.actor))
        except ValidationError:
            queue.append(queued)
    return kernel.model_copy(update={"queue": queue})


def named_last_run(reported: Any, named: Mapping[str, NamedRun]) -> Any:
    """A cell's last run as the kernel reported it, its requester named from
    the run's record, with when it started and ended there."""
    if not isinstance(reported, Mapping):
        return reported
    found = named.get(str(reported.get("run_id")))
    if found is None:
        return reported
    run = found.run
    data = {
        **reported,
        "trigger": run.trigger,
        "started_at": run.started_at,
        "finished_at": run.finished_at or reported.get("finished_at"),
    }
    try:
        return with_by(RunAttribution, data, found.actor).model_dump(mode="json")
    except ValidationError:
        return reported


def cell_state(
    raw: Mapping[str, Any],
    states: Mapping[str, Mapping[str, Any]],
    named: Mapping[str, NamedRun] | None = None,
) -> CellState:
    """One live cell of the document, with the run state the kernel reported
    for it (a cell it never reported is ``not_run``), its last run's
    requester named from the run's record."""
    cell = {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "kind": raw.get("kind"),
        "index": raw.get("index"),
        "source": raw.get("source"),
        "config": raw.get("config") or {},
        "meta": raw.get("meta") or {},
        "extra": raw.get("extra") or {},
        "status": "not_run",
    }
    reported = states.get(str(raw.get("id")))
    if reported is not None:
        candidate = {**cell, **{k: reported[k] for k in _RUN_STATE_KEYS if k in reported}}
        if named and "last_run" in candidate:
            candidate["last_run"] = named_last_run(candidate["last_run"], named)
        try:
            return CellState.model_validate(candidate)
        except ValidationError:
            # A snapshot from a newer or broken engine: the document's cell
            # still shows, without the run state it could not read.
            pass
    return CellState.model_validate(cell)


def caret_user(caret: Caret) -> uuid.UUID | None:
    try:
        return None if caret.user_id is None else uuid.UUID(caret.user_id)
    except ValueError:
        return None


def caret_actor(caret: Caret) -> str:
    """Whose caret it is as an actor: the key the person's runs and edits
    are made under, so a run's requester is told from everybody else."""
    user = caret_user(caret)
    return edit_log.actor_key(user_id=user) if user is not None else f"peer:{caret.peer}"


def caret_name(known: names.Names, caret: Caret) -> str:
    """Whose caret it is: the person it was sent as, by their name now."""
    return known.person(caret_user(caret), caret.who)


def edit_name(known: names.Names, row: NotebookEdit) -> str:
    return known.label(
        kind=row.actor_kind,
        actor_key=row.actor_key,
        user_id=row.user_id,
        recorded=row.actor_display,
    )


def requested_by(caller: Caller) -> dict[str, Any]:
    """Who a request to the engine is made for, as the engine's actor: the
    person, or the agent with the person it acts for."""
    found: dict[str, Any] = {
        "kind": caller.kind,
        "id": caller.actor_key,
        "display_name": caller.display,
    }
    if caller.may_edit is not None:
        # Sent only where a route decided it; the box reads absent as no.
        found["can_edit"] = caller.may_edit
    if caller.kind == "agent" and caller.user is not None:
        found["acting_for"] = {
            "id": edit_log.actor_key(user_id=caller.user.id),
            "display_name": names.person_name(caller.user),
        }
    return found


def person_requested_by(user: User) -> dict[str, Any]:
    """A person as the engine's actor, for a request made on their behalf
    with no caller behind it (a socket that left)."""
    return {
        "kind": "person",
        "id": edit_log.actor_key(user_id=user.id),
        "display_name": names.person_name(user),
    }


def run_actor(known: names.Names, run: NotebookRun) -> ActorRef:
    return known.actor(
        kind=run.actor_kind,
        actor_key=run_actor_key(run),
        user_id=run.requested_by_user_id,
        recorded=run.actor_display,
    )


def run_cells(run: NotebookRun) -> list[str]:
    """The cells a run named: those it submitted text for, else its target's."""
    cells = run.submitted.get("cells") if isinstance(run.submitted, dict) else None
    if isinstance(cells, dict) and cells:
        return sorted(cells)
    ids = run.target.get("ids") if isinstance(run.target, dict) else None
    return sorted(str(one) for one in ids) if isinstance(ids, list) else []


def run_actor_key(run: NotebookRun) -> str:
    if run.requested_by_agent:
        return edit_log.actor_key(agent_id=run.requested_by_agent)
    if run.requested_by_user_id is not None:
        return edit_log.actor_key(user_id=run.requested_by_user_id)
    return "system"


def targets_text(view: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, str]:
    """``cell id -> current text`` of the cells a run target names, in
    document order: the named cells, every cell, the cells above or below one
    (inclusive), or for ``stale`` every cell (the engine narrows it to the
    stale ones against its own kernel graph)."""
    cells = [cell for cell in view.get("cells", []) if isinstance(cell, Mapping)]
    order = [str(cell.get("id")) for cell in cells]
    text = {str(cell.get("id")): str(cell.get("source") or "") for cell in cells}
    kind = target.get("kind")
    chosen: list[str]
    if kind == "cells":
        wanted = {str(one) for one in target.get("ids", [])}
        chosen = [cell_id for cell_id in order if cell_id in wanted]
    elif kind in ("all", "stale"):
        chosen = order
    elif kind in ("above", "below"):
        anchor = str(target.get("id"))
        if anchor not in order:
            chosen = []
        else:
            at = order.index(anchor)
            chosen = order[: at + 1] if kind == "above" else order[at:]
    else:
        chosen = []
    return {cell_id: text[cell_id] for cell_id in chosen}


#: The platform event that carries who is in which cell now.
PRESENCE_EVENT: Final = "presence"


def presence_event(presence: list[Presence]) -> dict[str, Any]:
    """The notebook channel's word on who is in which cell now: the whole
    list, which replaces the one a reader holds."""
    return {"type": PRESENCE_EVENT, "presence": [p.model_dump(mode="json") for p in presence]}


__all__ = [
    "PRESENCE_EVENT",
    "NamedRun",
    "caret_actor",
    "caret_name",
    "caret_user",
    "cell_state",
    "edit_name",
    "graph_summary",
    "last_run_id",
    "live_bundles",
    "named_last_run",
    "named_queue",
    "person_requested_by",
    "presence_event",
    "requested_by",
    "run_actor",
    "run_actor_key",
    "run_cells",
    "run_state",
    "snapshot_view",
    "targets_text",
    "view_settings",
    "with_by",
]
