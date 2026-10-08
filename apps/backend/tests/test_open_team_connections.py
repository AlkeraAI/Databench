"""An open install refuses to delete a team that still holds a connection.

Runs in a fresh interpreter with only the open backend's extensions installed
(``backend.open_product``): the suite's own app installed the product already,
and a point freezes once read. The probe creates an org, a sub-team and a
connection the sub-team holds, then asks the team service to delete the team.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.xdist_group("open_team_connections")]

PROBE = """
import asyncio, json, uuid

from backend import open_product

open_product.install()

from backend.app_factory import create_app

create_app()

from alkera_core.connections.models import TeamConnection
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.extensions import installed_extensions

from backend.services.connections.team_connections import team_delete_refusal
from backend.services.org import teams
from backend.services.org.teams import TeamConflictError


async def delete_a_team(holds_a_connection):
    async with AsyncSessionLocal() as db:
        org = await teams.create_org_rows(db, org_name=f"Open probe {uuid.uuid4().hex[:8]}")
        team = await teams.create_subteam(db, org_team_id=org.id, name="Analytics")
        if holds_a_connection:
            db.add(
                TeamConnection(
                    team_id=team.id, org_team_id=org.id, plugin="generic_sql", handle="wh"
                )
            )
        await db.flush()
        product_reason = await team_delete_refusal(db, team.id)
        try:
            await teams.delete_team(db, team)
            refused = None
        except TeamConflictError as exc:
            refused = str(exc)
        await db.rollback()
    return refused, product_reason


async def both():
    # One event loop: the engine's pooled connections belong to the loop that opened them.
    return await delete_a_team(True), await delete_a_team(False)


(held, reason), (empty, _) = asyncio.run(both())
print("REPORT" + json.dumps({
    "installed": list(installed_extensions()),
    "held_refusal": held,
    "product_reason": reason,
    "empty_refusal": empty,
}))
"""


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    home = tmp_path_factory.mktemp("home")
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(PROBE)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={**os.environ, "ALKERA_HOME": str(home)},
        cwd=Path(__file__).parent,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    line = next(line for line in result.stdout.splitlines() if line.startswith("REPORT"))
    parsed: dict[str, Any] = json.loads(line.removeprefix("REPORT"))
    return parsed


def test_only_the_open_extensions_are_installed(report: dict[str, Any]) -> None:
    assert report["installed"] == ["alkera.team-connections"]


def test_a_team_holding_a_connection_is_not_deleted(report: dict[str, Any]) -> None:
    assert report["held_refusal"] is not None
    assert report["held_refusal"] == report["product_reason"]


def test_an_empty_team_is_deleted(report: dict[str, Any]) -> None:
    assert report["empty_refusal"] is None
