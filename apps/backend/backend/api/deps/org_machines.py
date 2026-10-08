"""What every org machine route starts from: the viewer a request acts as, a
machine decided on, the read a route answers, and the HTTP form of a service
refusal. Shared by the routes that serve an org's machines and the routes an
extension adds beside them."""

from __future__ import annotations

from alkera_core.authz import Action
from alkera_core.schemas.org_machines import OrgMachineRead
from fastapi import HTTPException, Request

from backend.auth.dependencies import CurrentUser, DbSession, current_principal
from backend.authz import enforce, role_resolver
from backend.services.compute import (
    OrgMachineError,
    OrgMachineRow,
    OrgMachineViewer,
    load_org_machine,
    org_machine_attrs,
    org_machine_audiences,
    org_machine_read_models,
    org_machine_resource,
    org_machine_viewer,
)


async def request_viewer(request: Request, db: DbSession, user: CurrentUser) -> OrgMachineViewer:
    ctx = await current_principal(request, db)
    return await org_machine_viewer(
        db, ctx=ctx, user=user, resolver=role_resolver(request, db, ctx)
    )


def failed(exc: OrgMachineError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


async def decided_row(
    request: Request,
    db: DbSession,
    viewer: OrgMachineViewer,
    machine_id: str,
    action: Action,
    *,
    lock: bool = False,
    **question: object,
) -> OrgMachineRow:
    """The machine, decided on: ``question`` is the purpose or operation and
    what it needs. A machine that is missing, deleted or another org's is the
    policy's not-found, on record like every other decision."""
    row = await load_org_machine(db, org_id=viewer.org_id, machine_id=machine_id, lock=lock)
    if row is None:
        await enforce(
            request,
            db,
            viewer.ctx,
            action,
            org_machine_resource(None, viewer=viewer, id=machine_id),
            {"in_org": False, **question},
        )
        raise AssertionError("a machine not found is never allowed")  # pragma: no cover
    grants_of = await org_machine_audiences(db, org_id=viewer.org_id, machine_ids=[row.machine.id])
    attrs = await org_machine_attrs(viewer, row.machine, grants_of[row.machine.id], **question)
    await enforce(
        request,
        db,
        viewer.ctx,
        action,
        org_machine_resource(row.machine, viewer=viewer, id=""),
        attrs,
    )
    return row


async def read_one(db: DbSession, viewer: OrgMachineViewer, row: OrgMachineRow) -> OrgMachineRead:
    [read] = await org_machine_read_models(db, viewer, [row])
    return read
