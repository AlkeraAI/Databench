"""Revision 0194: an org's dedicated box becomes a granted org machine.

Driven by Alembic against a scratch copy: seed the previous schema with one
``org_compute_assignments`` row, upgrade, check the back-fill, go down (the
tables and columns go), come back up (the back-fill repeats), and re-run the
revision over a schema that already has it (nothing is written twice). A box
that already belongs to another org stops the revision.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from alembic import command
from alkera_core.config import settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch

pytestmark = [pytest.mark.asyncio]

_PARENT = "0195"
_ENDED = datetime(2026, 1, 1, tzinfo=UTC)
_REVISION = "0196"


async def _scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar()


#: The workspaces every seeded org holds, and whether a back-fill that pins
#: the org's workspaces should pin each one.
_WORKSPACES: dict[str, bool] = {
    "main": True,
    "project": True,
    "deleted": False,
    "already_pinned": False,
    "foreign": False,
}


async def _workspace(
    session: AsyncSession, *, org: uuid.UUID, user: uuid.UUID, kind: str
) -> uuid.UUID:
    spec: dict[str, Any] = {"kind": "main" if kind == "main" else "project"}
    if kind == "already_pinned":
        spec["machine_pin"] = "elsewhere"
    ws = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO workspace_objects (id, org_team_id, logical_id, type, owner_user_id, "
            "visibility_scope, spec, deleted_at) "
            "VALUES (:id, :org, :l, 'workspace', :u, 'org', CAST(:spec AS jsonb), :deleted)"
        ),
        {
            "id": ws,
            "org": org,
            "l": f"ws-{ws.hex[:8]}",
            "u": user,
            "spec": json.dumps(spec),
            "deleted": 1.0 if kind == "deleted" else 0.0,
        },
    )
    return ws


async def _seed(
    session: AsyncSession,
    *,
    name: str = "GPU box",
    tenant: str = "none",
    plan: str = "none",
) -> dict[str, uuid.UUID]:
    """An org with one dedicated box assigned, as the previous schema writes it.
    ``plan`` is ``enterprise`` (an active plan), ``deactivated`` (a plan that
    ended) or ``none``."""
    org, other, user, machine_type, box = (uuid.uuid4() for _ in range(5))
    for team in (org, other):
        await session.execute(
            text("INSERT INTO teams (id, name, is_root) VALUES (:id, :name, true)"),
            {"id": team, "name": f"om-mig-{team.hex[:8]}"},
        )
    await session.execute(
        text("INSERT INTO users (id, org_team_id, email) VALUES (:id, :org, :email)"),
        {"id": user, "org": org, "email": f"om-mig-{secrets.token_hex(6)}@alkera.dev"},
    )
    await session.execute(
        text(
            "INSERT INTO compute_machine_types (id, provider, provider_type_id, display_name) "
            "VALUES (:id, 'runpod', :code, 'A40 48 GB')"
        ),
        {"id": machine_type, "code": f"om-mig-{machine_type.hex[:8]}"},
    )
    await session.execute(
        text(
            "INSERT INTO compute_allocations (id, user_id, org_team_id, machine_type_id, "
            "lifecycle, tenancy, state, name, storage_gb, tenant_org_id) "
            "VALUES (:id, :u, :org, :t, 'workspace', 'dedicated', 'ready', :name, 0, :tenant)"
        ),
        {
            "id": box,
            "u": user,
            "org": org,
            "t": machine_type,
            "name": name,
            "tenant": {"none": None, "own": org, "other": other}[tenant],
        },
    )
    await session.execute(
        text(
            "INSERT INTO org_compute_assignments (org_team_id, machine_id, fallback_to_pool, "
            "assigned_by) VALUES (:org, :box, false, :u)"
        ),
        {"org": org, "box": box, "u": user},
    )
    if plan != "none":
        await session.execute(
            text(
                "INSERT INTO enterprise_plans (id, org_team_id, billing_anchor, deactivated_at) "
                "VALUES (:id, :org, now(), :ended)"
            ),
            {"id": uuid.uuid4(), "org": org, "ended": None if plan == "enterprise" else _ENDED},
        )
    other_user = uuid.uuid4()
    await session.execute(
        text("INSERT INTO users (id, org_team_id, email) VALUES (:id, :org, :email)"),
        {"id": other_user, "org": other, "email": f"om-mig-{secrets.token_hex(6)}@alkera.dev"},
    )
    workspaces = {
        f"ws_{kind}": await _workspace(
            session,
            org=other if kind == "foreign" else org,
            user=other_user if kind == "foreign" else user,
            kind=kind,
        )
        for kind in _WORKSPACES
    }
    await session.commit()
    return {
        "org": org,
        "other": other,
        "user": user,
        "type": machine_type,
        "box": box,
        **workspaces,
    }


async def _back_filled(session: AsyncSession, ids: dict[str, uuid.UUID]) -> dict[str, Any]:
    machine = (
        (
            await session.execute(
                text(
                    "SELECT id, org_team_id, owner_team_id, offering_id, name, acquisition, "
                    "use_mode, storage_gb, current_allocation_id, desired_power, created_by, "
                    "free_until > now() + interval '364 days' AS free_for_a_year "
                    "FROM org_machines WHERE org_team_id = :org"
                ),
                {"org": ids["org"]},
            )
        )
        .mappings()
        .one()
    )
    offering = (
        (
            await session.execute(
                text(
                    "SELECT machine_type_id, name, pricing_mode, fixed_rate_per_minute_nanos, "
                    "audience, purchasable FROM compute_offerings WHERE id = :id"
                ),
                {"id": machine["offering_id"]},
            )
        )
        .mappings()
        .one()
    )
    box = (
        (
            await session.execute(
                text("SELECT org_machine_id, tenant_org_id FROM compute_allocations WHERE id = :b"),
                {"b": ids["box"]},
            )
        )
        .mappings()
        .one()
    )
    return {
        "machine": dict(machine),
        "offering": dict(offering),
        "box": dict(box),
        "listed": await _scalar(
            session,
            "SELECT array_agg(org_team_id) FROM compute_offering_orgs WHERE offering_id = :o",
            o=machine["offering_id"],
        ),
        "audience": [
            tuple(row)
            for row in (
                await session.execute(
                    text(
                        "SELECT grantee_kind, team_id, user_id FROM org_machine_audiences "
                        "WHERE org_machine_id = :m"
                    ),
                    {"m": machine["id"]},
                )
            ).all()
        ],
        "pins": {
            key: await _scalar(
                session,
                "SELECT spec->>'machine_pin' FROM workspace_objects WHERE id = :w",
                w=ids[key],
            )
            for key in ids
            if key.startswith("ws_")
        },
        "fallback": await _scalar(
            session,
            "SELECT shared_pool_fallback FROM org_compute_settings WHERE org_team_id = :org",
            org=ids["org"],
        ),
    }


def _assert_back_fill(
    found: dict[str, Any], ids: dict[str, uuid.UUID], name: str, use_mode: str = "assigned"
) -> None:
    machine = found["machine"]
    pinned = str(machine["id"])
    if use_mode == "pool":
        # The org pool serves the org's chats; nothing is pinned, no audience.
        assert found["audience"] == []
        expected_pins = {f"ws_{k}": None for k in _WORKSPACES}
    else:
        # An assigned machine serves the whole org, through every live workspace.
        assert found["audience"] == [("org", None, None)]
        expected_pins = {f"ws_{k}": pinned if pin else None for k, pin in _WORKSPACES.items()}
    expected_pins["ws_already_pinned"] = "elsewhere"
    assert found["pins"] == expected_pins
    assert {k: machine[k] for k in machine if k not in ("id", "offering_id")} == {
        "org_team_id": ids["org"],
        "owner_team_id": ids["org"],
        "name": name,
        "acquisition": "granted",
        "use_mode": use_mode,
        "storage_gb": 1,
        "current_allocation_id": ids["box"],
        "desired_power": "on",
        "created_by": ids["user"],
        "free_for_a_year": True,
    }
    assert found["offering"] == {
        "machine_type_id": ids["type"],
        "name": "A40 48 GB",
        "pricing_mode": "fixed",
        "fixed_rate_per_minute_nanos": 0,
        "audience": "listed",
        "purchasable": False,
    }
    assert found["listed"] == [ids["org"]]
    assert found["box"] == {"org_machine_id": machine["id"], "tenant_org_id": ids["org"]}
    assert found["fallback"] is False


async def _tables(session: AsyncSession) -> set[str]:
    rows = await session.execute(
        text("SELECT relname FROM pg_class WHERE relkind = 'r' AND relname = ANY(:names)"),
        {
            "names": [
                "compute_offerings",
                "compute_offering_orgs",
                "org_machines",
                "org_machine_audiences",
                "org_compute_settings",
                "workspace_machine_moves",
            ]
        },
    )
    return {str(r[0]) for r in rows}


@pytest.mark.parametrize(
    ("plan", "self_hosted", "use_mode"),
    [
        pytest.param("none", False, "assigned", id="self-serve-org-keeps-its-box-by-pins"),
        pytest.param("deactivated", False, "assigned", id="ended-enterprise-plan-is-self-serve"),
        pytest.param("enterprise", False, "pool", id="enterprise-org-gets-its-org-pool"),
        pytest.param("none", True, "pool", id="self-hosted-deployment-gets-the-org-pool"),
    ],
)
async def test_an_assignment_becomes_a_granted_machine_and_round_trips(
    monkeypatch: pytest.MonkeyPatch, plan: str, self_hosted: bool, use_mode: str
) -> None:
    monkeypatch.setattr(settings, "self_hosted", self_hosted)
    async with migration_scratch() as scratch:
        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            ids = await _seed(session, plan=plan)
            assert await _tables(session) == set()

        await scratch.upgrade(_REVISION)
        async with scratch.session() as session:
            _assert_back_fill(await _back_filled(session, ids), ids, "GPU box", use_mode)

        # Re-run over a schema that already has all of it: nothing is doubled.
        command.stamp(scratch.config, _PARENT)
        await scratch.upgrade(_REVISION)
        async with scratch.session() as session:
            _assert_back_fill(await _back_filled(session, ids), ids, "GPU box", use_mode)
            assert (
                await _scalar(
                    session,
                    "SELECT is_nullable FROM information_schema.columns WHERE table_name = "
                    "'workspace_machine_moves' AND column_name = 'flushed_before_switch'",
                )
                == "YES"
            )
            offerings = await _scalar(
                session,
                "SELECT count(*) FROM compute_offerings WHERE machine_type_id = :t",
                t=ids["type"],
            )
            assert offerings == 1

        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            assert await _tables(session) == set()
            columns = await _scalar(
                session,
                "SELECT count(*) FROM information_schema.columns WHERE table_name = "
                "'compute_allocations' AND column_name IN ('org_machine_id', 'failure_kind')",
            )
            assert columns == 0
            # The pins went with the machines they named; a pin naming
            # anything else is left as it was.
            pins = {
                key: await _scalar(
                    session,
                    "SELECT spec->>'machine_pin' FROM workspace_objects WHERE id = :w",
                    w=value,
                )
                for key, value in ids.items()
                if key.startswith("ws_")
            }
            assert pins == {f"ws_{k}": None for k in _WORKSPACES} | {
                "ws_already_pinned": "elsewhere"
            }
            # The assignment, and the tenant the back-fill gave the box, stay.
            assert (
                await _scalar(
                    session,
                    "SELECT tenant_org_id FROM compute_allocations WHERE id = :b",
                    b=ids["box"],
                )
                == ids["org"]
            )

        await scratch.upgrade(_REVISION)
        async with scratch.session() as session:
            _assert_back_fill(await _back_filled(session, ids), ids, "GPU box", use_mode)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("", "Machine 1", id="an-unnamed-box-is-machine-1"),
        pytest.param("   ", "Machine 1", id="a-blank-name-is-machine-1"),
        pytest.param("x" * 100, "x" * 64, id="a-long-name-is-cut-to-fit"),
    ],
)
async def test_the_machine_takes_the_boxs_name_or_machine_1(name: str, expected: str) -> None:
    async with migration_scratch() as scratch:
        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            ids = await _seed(session, name=name, tenant="own")
        await scratch.upgrade(_REVISION)
        async with scratch.session() as session:
            found = await _back_filled(session, ids)
    assert found["machine"]["name"] == expected


async def test_a_box_already_held_by_another_org_stops_the_revision() -> None:
    async with migration_scratch() as scratch:
        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            ids = await _seed(session, tenant="other")
        with pytest.raises(RuntimeError, match="already belong to another org"):
            await scratch.upgrade(_REVISION)
        assert scratch.revision() == _PARENT
        async with scratch.session() as session:
            # Nothing of the revision was left behind.
            assert await _tables(session) == set()
            # Repair the box and the revision goes through.
            await session.execute(
                text("DELETE FROM org_compute_assignments WHERE machine_id = :b"),
                {"b": ids["box"]},
            )
            await session.commit()
        await scratch.upgrade(_REVISION)
        assert scratch.revision() == _REVISION
