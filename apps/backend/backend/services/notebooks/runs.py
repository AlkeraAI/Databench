"""A run's life in ``notebook_runs``, followed from the engine's events.

The run route records a run ``queued`` and sends it to the box under the
run's own id as the request id, so the box's ``answer`` to it names the run.
From then on the row follows what the box posts:

* the ``answer``: what the engine made of the request (``queued``,
  ``needs_confirmation``, ``coalesced``, ``refused``, ``planned``); a request
  the box could not serve ends ``refused`` with its error's code as the
  reason, except ``folder_not_held`` (a box that does not hold the folder its
  lease names), which leaves the run queued for the platform's next send;
* ``run.needs_confirmation``, ``run.started`` (``running``, the kernel and
  when) and ``run.finished`` (how it ended, why, and when).

A run the platform never recorded (an agent's run on the box, a widget's
re-run) is recorded from its ``run.queued`` event, under the engine's id and
attributed to who it names: an agent for the person whose chat it runs in,
when the chat is one this box serves in the notebook's workspace; a person
who still stands in the org; otherwise the platform itself. A box speaks for
the chats it serves and for nobody else.

A status only moves forward: an ended run stays ended, and a late ``answer``
saying ``queued`` never undoes ``running``.

A run the platform ends ``refused`` on its own record (the box's ``answer``
refused it, or the box never answered) is announced on the notebook's
channel as a ``run.finished`` saying so, since no engine will: the person
who asked sees why nothing ran.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from alkera_core.auth.tenancy import member_stands
from alkera_core.models import User
from alkera_core.notebooks.models import NotebookRun
from alkera_core.notebooks.runs import (
    FOLDER_NOT_HELD,
    MACHINE_SILENT,
    advance,
    announce_platform_events,
    end_runs,
    finished_event,
)
from alkera_core.notebooks.schemas import CELL_ID_RE, RUN_FINAL_STATUSES, RUN_TRIGGERS
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.notebooks import names
from backend.services.notebooks.callers import Target, chat_person
from backend.services.notebooks.errors import AgentChatRefusedError

#: The events a run's row follows.
RUN_EVENTS: Final = frozenset(
    {"answer", "run.queued", "run.needs_confirmation", "run.started", "run.finished"}
)
#: The longest run id the engine may name a run by.
MAX_RUN_ID: Final = 64


def _run_id_of(event: Mapping[str, Any]) -> str | None:
    raw = event.get("request_id") if event.get("type") == "answer" else event.get("run_id")
    if not isinstance(raw, str) or not raw or len(raw) > MAX_RUN_ID:
        return None
    return raw


async def follow_runs(
    db: AsyncSession,
    target: Target,
    *,
    machine_id: str,
    kernel_id: str,
    events: Sequence[Mapping[str, Any]],
    at: datetime,
) -> None:
    """Move the notebook's runs on by an accepted batch of ``kernel_id``'s
    events from ``machine_id`` (the machine the request proved it is). The
    caller commits."""
    wanted = {
        run_id
        for event in events
        if event.get("type") in RUN_EVENTS and (run_id := _run_id_of(event)) is not None
    }
    if not wanted:
        return
    found = (
        await db.execute(
            select(NotebookRun).where(
                NotebookRun.org_id == target.org_id,
                NotebookRun.item_id == target.item_id,
                NotebookRun.engine_run_id.in_(wanted),
            )
        )
    ).scalars()
    runs: dict[str, NotebookRun] = {str(run.engine_run_id): run for run in found}
    refused: list[NotebookRun] = []
    for event in events:
        kind = event.get("type")
        run_id = _run_id_of(event) if kind in RUN_EVENTS else None
        if run_id is None:
            continue
        run = runs.get(run_id)
        if run is None:
            if kind != "run.queued":
                continue
            run = await _begun(db, target, machine_id=machine_id, run_id=run_id, event=event)
            db.add(run)
            runs[run_id] = run
            continue
        if kind == "answer":
            if _answered(run, event, at=at) and run.status == "refused":
                refused.append(run)
        elif kind == "run.queued":
            advance(run, "queued")
        elif kind == "run.needs_confirmation":
            advance(run, "needs_confirmation")
        elif kind == "run.started":
            if advance(run, "running"):
                run.started_at = run.started_at or at
                run.kernel_id = kernel_id
                if not run.target:
                    run.target = _plan_target(event.get("plan"))
        elif kind == "run.finished":
            status = event.get("status")
            reason = event.get("reason")
            if advance(
                run,
                str(status) if isinstance(status, str) else "error",
                str(reason) if isinstance(reason, str) else None,
            ):
                run.finished_at = at
    await db.flush()
    await announce_platform_events(
        db,
        org_id=target.org_id,
        item_id=target.item_id,
        events=[finished_event(run) for run in refused],
    )


def _answered(run: NotebookRun, event: Mapping[str, Any], *, at: datetime) -> bool:
    """What the engine made of the run request; whether the run moved."""
    error = event.get("error")
    if isinstance(error, Mapping):
        if error.get("code") == FOLDER_NOT_HELD:
            # The box will serve it once it holds the folder: the run waits
            # for the platform's next send, and ends only if the wait does.
            return False
        if advance(run, "refused", str(error.get("code") or "refused")):
            run.finished_at = at
            return True
        return False
    result = event.get("result")
    if not isinstance(result, Mapping):
        return False
    status = result.get("status")
    if not isinstance(status, str):
        return False
    reason = result.get("reason")
    joined = result.get("run_id")
    if status == "coalesced" and isinstance(joined, str) and joined != run.engine_run_id:
        # The run it joined does the work; the reason names it.
        reason = joined
    if not advance(run, status, str(reason) if isinstance(reason, str) else None):
        return False
    if run.status in RUN_FINAL_STATUSES:
        run.finished_at = at
    return True


def _plan_target(plan: Any) -> dict[str, Any]:
    """The cells a run the box began named, from its plan's targets."""
    ids: list[str] = []
    if isinstance(plan, list):
        for step in plan:
            if not isinstance(step, Mapping) or step.get("reason") != "target":
                continue
            cell = step.get("cell_id")
            if isinstance(cell, str) and CELL_ID_RE.match(cell) and cell not in ids:
                ids.append(cell)
    return {"kind": "cells", "ids": ids} if ids else {}


async def _begun(
    db: AsyncSession,
    target: Target,
    *,
    machine_id: str,
    run_id: str,
    event: Mapping[str, Any],
) -> NotebookRun:
    """The row of a run the box began, attributed to who its ``run.queued``
    names when the box may speak for them."""
    trigger = event.get("trigger")
    kind, user, agent = await _requester(db, target, machine_id, event.get("requested_by"))
    if kind == "agent" and user is not None:
        display = names.agent_name(names.person_name(user))
    elif kind == "person" and user is not None:
        display = names.person_name(user)
    else:
        display = names.system_name()
    return NotebookRun(
        run_id=uuid.uuid4(),
        org_id=target.org_id,
        drive_id=target.drive_id,
        item_id=target.item_id,
        engine_run_id=run_id,
        actor_kind=kind,
        requested_by_user_id=None if user is None else user.id,
        requested_by_agent=agent,
        actor_display=display[:255],
        trigger=trigger if isinstance(trigger, str) and trigger in RUN_TRIGGERS else "run",
        target={},
        frontier=None,
        frontier_included=False,
        submitted={},
        status="queued",
    )


async def _requester(
    db: AsyncSession, target: Target, machine_id: str, requested_by: Any
) -> tuple[str, User | None, str | None]:
    """``(kind, person, agent chat)`` of who a run the box began names."""
    who = requested_by if isinstance(requested_by, Mapping) else {}
    actor_id = str(who.get("id") or "")
    if who.get("kind") == "agent" and actor_id.startswith("agent:"):
        chat_id = actor_id.removeprefix("agent:")
        try:
            person = await chat_person(db, target, machine_id, chat_id)
        except AgentChatRefusedError:
            return "system", None, None
        return "agent", person, chat_id
    if who.get("kind") == "person":
        user_id = names.user_of_key(actor_id)
        user = None if user_id is None else await db.get(User, user_id)
        if (
            user is not None
            and user.is_active
            and await member_stands(db, user=user, org_team_id=target.org_id)
        ):
            return "person", user, None
    return "system", None, None


async def end_unanswered(
    db: AsyncSession, *, org_id: uuid.UUID, run_id: uuid.UUID, at: datetime
) -> bool:
    """A run the box never answered ends ``refused`` (``machine_silent``) when
    it is still waiting, announced on the notebook's channel; whether it did.
    The caller commits."""
    run = await db.get(NotebookRun, run_id, with_for_update=True)
    if run is None or run.org_id != org_id or run.status != "queued":
        return False
    await end_runs(db, [run], status="refused", reason=MACHINE_SILENT, at=at)
    return True


__all__ = [
    "RUN_EVENTS",
    "end_unanswered",
    "follow_runs",
]
