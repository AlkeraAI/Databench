"""What a workspace is, decided from its rows: no I/O here.

A workspace holds chats that share one file tree. Two facts about one are
orthogonal and live on its spec:

* ``kind`` is what the workspace is FOR. Each member of an org has one
  ``main`` workspace, created the first time it is asked for, where a chat
  lands when nobody named a place for it; every other workspace is a
  ``project``, made for one piece of work.
* ``layout`` is what its folder is. A ``native`` workspace owns a
  ``<Name>.alkeraworkspace`` folder holding ``files/`` and ``.chats/``; an
  ``adopted`` one is a workspace of one made for a chat that already had a
  folder, and points at that folder instead of moving a byte.

Every existing chat became an adopted ``project`` workspace of one, and a new
chat still gets one while the box runs one sandbox per chat. A main workspace
is native from birth.

Each chat's own spec says where it runs, and :func:`effective_binding` is the
one place a workspace's binding is read from: derived from its chats, never from
a copy nothing keeps current.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final

from alkera_core.schemas.objects.specs import (
    ChatSpec,
    MachineStatus,
    MirrorState,
    SandboxState,
    WorkspaceKind,
    WorkspaceSpec,
)

#: The namespace a workspace of one is minted into, keyed by its chat's id: a
#: retried adoption lands on the row it already made, and no client id can
#: collide with it, since a client mints into the default namespace.
ADOPTED_NAMESPACE: Final = "workspace_of_chat"
#: The namespace a member's main workspace is minted into, keyed by their user
#: id: one per member by the table's own unique constraint, so two first
#: requests racing each other make one.
MAIN_NAMESPACE: Final = "workspace_main"
#: What a main workspace is called when it is made.
MAIN_TITLE: Final = "Main"


def workspace_spec_of(raw: dict[str, Any] | None) -> WorkspaceSpec:
    """``raw`` as a :class:`WorkspaceSpec`, whatever a past writer left."""
    return WorkspaceSpec.model_validate(raw or {})


@dataclass(frozen=True, slots=True)
class WorkspaceBinding:
    """Where a workspace runs and what it needs, as a reader is told it."""

    machine_id: str | None
    machine_status: MachineStatus
    mirror_state: MirrorState | None
    wake_requested_at: str | None
    #: Every chat in it is a spare nobody has claimed: nobody may see it yet.
    spare: bool
    #: Some chat in it may write, so it needs a gVisor machine.
    writable: bool
    #: The sandbox as its box last said (``waking`` / ``awake`` / ``asleep``),
    #: or ``None`` for a workspace no box that runs workspaces has served.
    sandbox_state: SandboxState | None = None


def effective_binding(
    chats: Sequence[ChatSpec],
    *,
    is_writable: Callable[[str | None], bool],
    spec: WorkspaceSpec | None = None,
) -> WorkspaceBinding:
    """A workspace's binding, derived from the chats in it by the rules a person
    would read off them: a machine is named only when every chat is on the
    same one; the workspace is awake when any chat is, asleep only when all
    are; a wake stands when any chat asked for one (the latest); it is a spare
    only when every chat is one; and it needs a writable machine when any chat
    may write. A workspace with no live chat names no machine and is not a
    spare.

    ``is_writable`` is the placement's own answer for a permission mode, passed
    in so this module does not hold a second copy of which modes write.

    Given the workspace's ``spec`` with ``binding_authority == "workspace"``,
    the box's own report wins for the two facts it alone knows: which box
    holds the workspace and whether its sandbox is awake. A chat whose agent
    server stopped while another chat keeps the sandbox up reads asleep, and
    the workspace still reads awake, which the chats alone cannot say. The
    rest stays the chats'. An older box never reports, so its workspaces keep
    the derived answer.
    """
    derived = _derived(chats, is_writable=is_writable)
    if spec is None or spec.binding_authority != "workspace":
        return derived
    mirror: MirrorState | None = derived.mirror_state
    if spec.sandbox_state is not None:
        mirror = "asleep" if spec.sandbox_state == "asleep" else "awake"
    return replace(
        derived,
        machine_id=spec.machine_id or derived.machine_id,
        mirror_state=mirror,
        sandbox_state=spec.sandbox_state,
    )


def _derived(
    chats: Sequence[ChatSpec], *, is_writable: Callable[[str | None], bool]
) -> WorkspaceBinding:
    if not chats:
        return WorkspaceBinding(
            machine_id=None,
            machine_status="none",
            mirror_state=None,
            wake_requested_at=None,
            spare=False,
            writable=False,
        )
    machines = {chat.machine_id for chat in chats}
    statuses = {chat.machine_status for chat in chats}
    mirrors = [chat.mirror_state for chat in chats]
    mirror: MirrorState | None
    if "awake" in mirrors:
        mirror = "awake"
    elif all(state == "asleep" for state in mirrors):
        mirror = "asleep"
    else:
        mirror = None
    return WorkspaceBinding(
        machine_id=next(iter(machines)) if len(machines) == 1 else None,
        machine_status=next(iter(statuses)) if len(statuses) == 1 else "none",
        mirror_state=mirror,
        wake_requested_at=_latest(chat.wake_requested_at for chat in chats),
        spare=all(chat.spare for chat in chats),
        writable=any(is_writable(chat.permission_mode) for chat in chats),
    )


def record_box_report(
    spec: WorkspaceSpec,
    *,
    machine_id: str,
    sandbox_state: SandboxState,
    memory_used_mb: int | None,
    at: str,
) -> WorkspaceSpec:
    """``spec`` with the box's report on the workspace's sandbox written in.

    Pure. The first report flips ``binding_authority`` to ``workspace``: from
    then on the box's word on which box holds the workspace and whether its
    sandbox is awake is read off the workspace (:func:`effective_binding`).
    """
    return spec.model_copy(
        update={
            "binding_authority": "workspace",
            "machine_id": machine_id,
            "sandbox_state": sandbox_state,
            "sandbox_memory_used_mb": memory_used_mb,
            "sandbox_reported_at": at,
        }
    )


def forget_box_report(spec: WorkspaceSpec) -> WorkspaceSpec:
    """``spec`` without the box's word on which box holds the workspace and
    how its sandbox is.

    Pure. For a workspace whose chats left the box that reported it: the
    report says who held the workspace, and once that box holds none of its
    chats it is no longer true. ``binding_authority`` stays ``workspace``, so
    the next box to report is read exactly as the first was, and until then
    the binding is the one the chats give.
    """
    return spec.model_copy(
        update={
            "machine_id": None,
            "sandbox_state": None,
            "sandbox_memory_used_mb": None,
            "sandbox_reported_at": None,
        }
    )


def _latest(stamps: Iterable[str | None]) -> str | None:
    """The latest of some ISO instants, compared as instants rather than as
    text (two writers' offsets need not agree), as it was written. A stamp
    that does not parse is not a wake."""
    best: tuple[datetime, str] | None = None
    for stamp in stamps:
        if not stamp:
            continue
        try:
            instant = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        if best is None or instant > best[0]:
            best = (instant, stamp)
    return best[1] if best else None


__all__ = [
    "ADOPTED_NAMESPACE",
    "MAIN_NAMESPACE",
    "MAIN_TITLE",
    "WorkspaceBinding",
    "WorkspaceKind",
    "effective_binding",
    "forget_box_report",
    "record_box_report",
    "workspace_spec_of",
]
