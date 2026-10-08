"""Who a node a write creates is credited to (``history.owner_refs``).

A box on its own machine credential is credited to the owner of the deepest
charging folder above the node; everyone else is credited to their subject, as
before, without a statement. The folder rows come from the repo, so this pins
the choice among them: deepest wins, a prefix counts label by label, and a node
under no charging folder keeps the machine.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any, cast

import pytest
from alkera_core.authz.principal import ActingContext
from alkera_core.files.history import owner_refs
from alkera_core.files.ids import DriveId
from alkera_core.files.repo import FilesRepo

pytestmark = [pytest.mark.asyncio, pytest.mark.spread]

ORG = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
MACHINE = uuid.UUID("00000000-0000-4000-8000-0000000000bb")
ANA = uuid.UUID("00000000-0000-4000-8000-000000000001")
BO = uuid.UUID("00000000-0000-4000-8000-000000000002")
DRIVE = DriveId(uuid.UUID("00000000-0000-4000-8000-0000000000cc"))


class _Folders:
    """The repo's charging-folder read, answering from a fixed list."""

    def __init__(self, rows: list[tuple[str, uuid.UUID]]) -> None:
        self.rows = rows

    async def charging_owners_above(
        self, drive_id: DriveId, paths: Iterable[str]
    ) -> list[tuple[str, uuid.UUID]]:
        return self.rows


def _machine() -> ActingContext:
    return ActingContext.for_machine(
        machine_id=MACHINE, credential_id=uuid.uuid4(), org_id=ORG, label="box"
    )


async def _refs(repo: _Folders, ctx: ActingContext, paths: list[str]) -> list[uuid.UUID]:
    return await owner_refs(cast(FilesRepo, cast(Any, repo)), ctx, DRIVE, paths)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        pytest.param("1.2.3.4", BO, id="the-chat-inside-the-workspace-wins"),
        pytest.param("1.2.9", ANA, id="beside-the-chat-the-workspace-owner"),
        pytest.param("1.2", ANA, id="the-folder-itself"),
        pytest.param("1.20.5", MACHINE, id="a-shared-string-prefix-is-not-an-ancestor"),
        pytest.param("7.8", MACHINE, id="under-no-charging-folder-keeps-the-machine"),
    ],
)
async def test_a_box_is_credited_to_the_deepest_charging_folders_owner(
    path: str, expected: uuid.UUID
) -> None:
    repo = _Folders([("1.2", ANA), ("1.2.3", BO)])

    assert await _refs(repo, _machine(), [path]) == [expected]


async def test_a_batch_answers_each_path_in_order() -> None:
    repo = _Folders([("1.2", ANA), ("1.2.3", BO)])

    refs = await _refs(repo, _machine(), ["1.2.3.4", "1.2.9", "7.8"])

    assert refs == [BO, ANA, MACHINE]


@pytest.mark.parametrize(
    "ctx",
    [
        pytest.param(
            ActingContext.for_user(user_id=ANA, org_id=ORG, email="ana@example.com"), id="user"
        ),
        pytest.param(
            ActingContext.for_agent(
                user_id=ANA, org_id=ORG, email="ana@example.com", session_id=str(uuid.uuid4())
            ),
            id="agent-in-their-session",
        ),
    ],
)
async def test_a_person_or_their_agent_is_credited_to_the_person_whatever_the_folder(
    ctx: ActingContext,
) -> None:
    repo = _Folders([("1.2", BO)])

    assert await _refs(repo, ctx, ["1.2.3"]) == [ANA]
