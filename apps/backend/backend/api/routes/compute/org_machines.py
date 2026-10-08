"""An org's machines: buy, list, read, change, start, stop, replace, delete,
and the org's compute settings.

Every route decides through ``org_machine.access`` at the choke point, so
every decision is on record. A machine of another org and one that does not
exist answer the same 404, decided by the policy (the id is looked up in the
caller's org only, and finding nothing reads as not in the org). Writes take
``If-Match`` with the machine's ``version``; one built on a reading that is no
longer true is refused with a 409 and nothing is written.

Power changes answer 202: the org machine's intent is written here and the
reconcile, nudged after the commit, does the provider work.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.authz import Action
from alkera_core.authz.policies import org_machine as policy
from alkera_core.compute.ssh import SshTransport
from alkera_core.schemas.org_machines import (
    DiskGrowRequest,
    MachineBuyingRead,
    MachineQuote,
    OrgComputeSettingsRead,
    OrgComputeSettingsUpdate,
    OrgMachineAudienceUpdate,
    OrgMachineDetail,
    OrgMachineRead,
    OrgMachineUpdate,
)
from alkera_core.schemas.ssh_machines import SshMachineAdd, SshMachineTarget, SshMachineTestRead
from fastapi import APIRouter, Depends, Query, Request, Response, status

from backend.api.deps.compute import refused
from backend.api.deps.org_machines import decided_row, failed, read_one, request_viewer
from backend.api.params import PathId
from backend.api.preconditions import IfMatch, require_current
from backend.auth.dependencies import CurrentUser, DbSession
from backend.authz import enforce
from backend.services import infra
from backend.services.compute import (
    ComputeRefusedError,
    OrgMachineError,
    OrgMachineRow,
    OrgMachineViewer,
    add_ssh_machine,
    can_add_org_machine,
    default_ssh_transport,
    delete_org_machine,
    grow_org_machine_disk,
    load_org_machine,
    load_org_machines,
    org_machine_buying_read,
    org_machine_detail,
    org_machine_read_models,
    org_machine_resource,
    org_pool_applies,
    quote_org_machine_disk_grow,
    read_org_compute_settings,
    replace_org_machine,
    replace_org_machine_audience,
    ssh_machine_add_attrs,
    start_org_machine,
    stop_org_machine,
    test_ssh_machine,
    update_org_compute_settings,
    update_org_machine,
)

router = APIRouter(prefix="/api/v1/org", tags=["org-machines"])


@router.get("/machines", response_model=list[OrgMachineRead])
async def list_machines(request: Request, db: DbSession, user: CurrentUser) -> list[OrgMachineRead]:
    """The org's machines the caller may see: those they manage, those shared
    with them, and every org pool machine."""
    viewer = await request_viewer(request, db, user)
    org = await viewer.roles_on(viewer.org_id)
    await enforce(
        request,
        db,
        viewer.ctx,
        Action.READ,
        org_machine_resource(None, viewer=viewer, id=str(viewer.org_id)),
        {"in_org": org.in_org, "roles": org.roles, "purpose": policy.LIST_PURPOSE},
    )
    return await org_machine_read_models(
        db, viewer, await load_org_machines(db, org_id=viewer.org_id)
    )


@router.get("/machines/buying", response_model=MachineBuyingRead)
async def machine_buying(request: Request, db: DbSession, user: CurrentUser) -> MachineBuyingRead:
    """Whether the org may buy another machine now, and if not, why: its plan
    buys none, or it holds as many as it may. Any member may ask, so a team
    admin learns it before trying; the plan itself is not named."""
    viewer = await request_viewer(request, db, user)
    org = await viewer.roles_on(viewer.org_id)
    await enforce(
        request,
        db,
        viewer.ctx,
        Action.READ,
        org_machine_resource(None, viewer=viewer, id=str(viewer.org_id)),
        {"in_org": org.in_org, "roles": org.roles, "purpose": policy.LIST_PURPOSE},
    )
    read = await org_machine_buying_read(db, ctx=viewer.ctx)
    return read.model_copy(update={"can_add": await can_add_org_machine(db, viewer)})


async def ssh_transport() -> SshTransport:
    """How the add and test routes reach a host; tests override this."""
    return default_ssh_transport()


async def _decide_add(
    request: Request, db: DbSession, viewer: OrgMachineViewer, *, sets_pool: bool
) -> None:
    await enforce(
        request,
        db,
        viewer.ctx,
        Action.WRITE,
        org_machine_resource(None, viewer=viewer, id="new"),
        await ssh_machine_add_attrs(db, viewer, sets_pool=sets_pool),
    )


@router.post("/machines/ssh/test", response_model=SshMachineTestRead)
async def check_ssh_connection(
    request: Request,
    body: SshMachineTarget,
    db: DbSession,
    user: CurrentUser,
    transport: SshTransport = Depends(ssh_transport),
) -> SshMachineTestRead:
    """Connect to a host before adding it: whether it answered, the
    fingerprint of the key it presented, and whether it can run a node. 422
    for a host this deployment does not connect to."""
    viewer = await request_viewer(request, db, user)
    await _decide_add(request, db, viewer, sets_pool=False)
    try:
        return await test_ssh_machine(body, transport)
    except OrgMachineError as exc:
        raise failed(exc) from exc


@router.post("/machines/ssh", response_model=OrgMachineRead, status_code=status.HTTP_202_ACCEPTED)
async def add_machine_over_ssh(
    request: Request,
    body: SshMachineAdd,
    db: DbSession,
    user: CurrentUser,
    transport: SshTransport = Depends(ssh_transport),
) -> OrgMachineRead:
    """Add a host the org runs as an org machine. 409 when the host presents a
    key other than the confirmed fingerprint or the name is in use, 422 when
    it cannot be reached, refuses the credential or lacks a prerequisite."""
    viewer = await request_viewer(request, db, user)
    await _decide_add(request, db, viewer, sets_pool=body.use_mode == "pool")
    try:
        machine = await add_ssh_machine(db, viewer, body, transport)
    except OrgMachineError as exc:
        raise failed(exc) from exc
    row = await load_org_machine(db, org_id=viewer.org_id, machine_id=machine.id)
    assert row is not None
    read = await read_one(db, viewer, row)
    await db.commit()
    await infra.nudge_org_machine_reconcile()
    return read


@router.get("/machines/{machine_id}", response_model=OrgMachineDetail)
async def get_machine(
    request: Request, machine_id: PathId, db: DbSession, user: CurrentUser
) -> OrgMachineDetail:
    viewer = await request_viewer(request, db, user)
    row = await decided_row(
        request, db, viewer, machine_id, Action.READ, purpose=policy.READ_PURPOSE
    )
    return await org_machine_detail(db, viewer, row)


async def _manage(
    request: Request,
    db: DbSession,
    viewer: OrgMachineViewer,
    machine_id: str,
    *,
    if_match: str | None,
    sets_pool: bool = False,
) -> OrgMachineRow:
    """The machine a manager is about to change, locked, decided on, and still
    at the version the caller read."""
    row = await decided_row(
        request,
        db,
        viewer,
        machine_id,
        Action.WRITE,
        lock=True,
        operation=policy.MANAGE,
        sets_pool=sets_pool,
        org_allows_pool=await org_pool_applies(db, org_id=viewer.org_id),
    )
    require_current(if_match, str(row.machine.version), what="machine")
    return row


@router.patch("/machines/{machine_id}", response_model=OrgMachineRead)
async def update_machine(
    request: Request,
    machine_id: PathId,
    body: OrgMachineUpdate,
    db: DbSession,
    user: CurrentUser,
    if_match: IfMatch = None,
) -> OrgMachineRead:
    """Rename it, or change when it stops idle, its monthly cap or its use
    mode. Putting it in the org pool takes an org admin and a plan with one."""
    viewer = await request_viewer(request, db, user)
    probe = await load_org_machine(db, org_id=viewer.org_id, machine_id=machine_id)
    sets_pool = (
        body.use_mode == "pool"
        and "use_mode" in body.model_fields_set
        and probe is not None
        and probe.machine.use_mode != "pool"
    )
    row = await _manage(request, db, viewer, machine_id, if_match=if_match, sets_pool=sets_pool)
    try:
        await update_org_machine(db, viewer, row.machine, body)
    except OrgMachineError as exc:
        raise failed(exc) from exc
    read = await read_one(db, viewer, row)
    await db.commit()
    return read


@router.put("/machines/{machine_id}/audience", response_model=OrgMachineRead)
async def set_audience(
    request: Request,
    machine_id: PathId,
    body: OrgMachineAudienceUpdate,
    db: DbSession,
    user: CurrentUser,
    if_match: IfMatch = None,
) -> OrgMachineRead:
    """Replace who may use the machine."""
    viewer = await request_viewer(request, db, user)
    row = await _manage(request, db, viewer, machine_id, if_match=if_match)
    try:
        await replace_org_machine_audience(db, viewer, row.machine, body.audience)
    except OrgMachineError as exc:
        raise failed(exc) from exc
    read = await read_one(db, viewer, row)
    await db.commit()
    return read


@router.post(
    "/machines/{machine_id}/start",
    response_model=OrgMachineRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_machine(
    request: Request, machine_id: PathId, db: DbSession, user: CurrentUser, if_match: IfMatch = None
) -> OrgMachineRead:
    """Start it: 402 when the credit cannot, 429 at the plan's quota."""
    viewer = await request_viewer(request, db, user)
    row = await _manage(request, db, viewer, machine_id, if_match=if_match)
    try:
        await start_org_machine(db, viewer, row)
    except ComputeRefusedError as exc:
        raise refused(exc) from exc
    return await _commit_power(db, viewer, row.machine.id)


@router.post(
    "/machines/{machine_id}/stop",
    response_model=OrgMachineRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def stop_machine(
    request: Request,
    machine_id: PathId,
    db: DbSession,
    user: CurrentUser,
    if_match: IfMatch = None,
    now: bool = Query(
        default=False, description="Stop running chats now instead of letting them finish."
    ),
) -> OrgMachineRead:
    """Stop it, keeping its disk. Running chats get a short grace to finish
    unless ``now``."""
    viewer = await request_viewer(request, db, user)
    row = await _manage(request, db, viewer, machine_id, if_match=if_match)
    await stop_org_machine(db, viewer, row.machine, now_=now)
    return await _commit_power(db, viewer, row.machine.id)


@router.post(
    "/machines/{machine_id}/replace",
    response_model=OrgMachineRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def replace_machine(
    request: Request, machine_id: PathId, db: DbSession, user: CurrentUser, if_match: IfMatch = None
) -> OrgMachineRead:
    """Start it on new hardware of the same offering; its disk is lost. Only
    for a machine that is stopped, waiting for hardware or couldn't start."""
    viewer = await request_viewer(request, db, user)
    row = await _manage(request, db, viewer, machine_id, if_match=if_match)
    try:
        await replace_org_machine(db, viewer, row)
    except OrgMachineError as exc:
        raise failed(exc) from exc
    except ComputeRefusedError as exc:
        raise refused(exc) from exc
    return await _commit_power(db, viewer, row.machine.id)


@router.post(
    "/machines/{machine_id}/disk",
    response_model=OrgMachineRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def grow_machine_disk(
    request: Request,
    machine_id: PathId,
    body: DiskGrowRequest,
    db: DbSession,
    user: CurrentUser,
    if_match: IfMatch = None,
) -> OrgMachineRead:
    """Grow its disk: only larger (422), only where the provider can (409),
    402 when the credit cannot carry the bigger disk. A running machine
    restarts to grow it, its running chats finishing first."""
    viewer = await request_viewer(request, db, user)
    row = await _manage(request, db, viewer, machine_id, if_match=if_match)
    try:
        await grow_org_machine_disk(db, viewer, row, body.volume_gb)
    except OrgMachineError as exc:
        raise failed(exc) from exc
    except ComputeRefusedError as exc:
        raise refused(exc) from exc
    return await _commit_power(db, viewer, row.machine.id)


@router.post("/machines/{machine_id}/disk/quote", response_model=MachineQuote)
async def quote_machine_disk(
    request: Request, machine_id: PathId, body: DiskGrowRequest, db: DbSession, user: CurrentUser
) -> MachineQuote:
    """What the machine would cost with a bigger disk, and whether the grow
    would be admitted now. For its managers; nothing changes."""
    viewer = await request_viewer(request, db, user)
    row = await decided_row(
        request,
        db,
        viewer,
        machine_id,
        Action.WRITE,
        lock=False,
        operation=policy.MANAGE,
        sets_pool=False,
        org_allows_pool=False,
    )
    try:
        return await quote_org_machine_disk_grow(db, viewer, row, body.volume_gb)
    except OrgMachineError as exc:
        raise failed(exc) from exc


async def _commit_power(
    db: DbSession, viewer: OrgMachineViewer, machine_id: UUID
) -> OrgMachineRead:
    row = await load_org_machine(db, org_id=viewer.org_id, machine_id=machine_id)
    assert row is not None
    read = await read_one(db, viewer, row)
    await db.commit()
    await infra.nudge_org_machine_reconcile()
    return read


@router.delete("/machines/{machine_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_machine(
    request: Request, machine_id: PathId, db: DbSession, user: CurrentUser, if_match: IfMatch = None
) -> Response:
    """Delete it: it stops, then its disk is destroyed. Workspaces pinned to
    it return to the org's default."""
    viewer = await request_viewer(request, db, user)
    row = await decided_row(request, db, viewer, machine_id, Action.DELETE, lock=True)
    require_current(if_match, str(row.machine.version), what="machine")
    await delete_org_machine(db, viewer, row.machine)
    await db.commit()
    await infra.nudge_org_machine_reconcile()
    return Response(status_code=status.HTTP_202_ACCEPTED)


async def _settings_decision(
    request: Request, db: DbSession, viewer: OrgMachineViewer, action: Action, **question: object
) -> None:
    await enforce(
        request,
        db,
        viewer.ctx,
        action,
        org_machine_resource(None, viewer=viewer, id=str(viewer.org_id)),
        {
            "in_org": (await viewer.roles_on(viewer.org_id)).in_org,
            "is_org_admin": viewer.is_org_admin,
            **question,
        },
    )


@router.get("/compute/settings", response_model=OrgComputeSettingsRead)
async def get_compute_settings(
    request: Request, db: DbSession, user: CurrentUser
) -> OrgComputeSettingsRead:
    """Whether the org's regular chats may run on shared machines while no org
    pool machine can take them, how many pool machines stay awake, and the
    machine new workspaces run on unless their creator names another."""
    viewer = await request_viewer(request, db, user)
    await _settings_decision(request, db, viewer, Action.READ, purpose=policy.SETTINGS_PURPOSE)
    return await read_org_compute_settings(db, org_id=viewer.org_id)


@router.put("/compute/settings", response_model=OrgComputeSettingsRead)
async def put_compute_settings(
    request: Request,
    body: OrgComputeSettingsUpdate,
    db: DbSession,
    user: CurrentUser,
    if_match: IfMatch = None,
) -> OrgComputeSettingsRead:
    """Change the settings. ``default_org_machine_id`` must name a live
    machine of the org (anything else is the same 404 a missing machine is);
    sent as null it clears the default."""
    viewer = await request_viewer(request, db, user)
    await _settings_decision(
        request,
        db,
        viewer,
        Action.WRITE,
        operation=policy.SETTINGS,
        email_verified=viewer.email_verified,
    )
    current = await read_org_compute_settings(db, org_id=viewer.org_id)
    require_current(if_match, str(current.version), what="setting")
    try:
        await update_org_compute_settings(db, viewer, body)
    except OrgMachineError as exc:
        raise failed(exc) from exc
    read = await read_org_compute_settings(db, org_id=viewer.org_id)
    await db.commit()
    return read


__all__ = ["router"]
