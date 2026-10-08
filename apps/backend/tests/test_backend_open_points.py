"""What the open backend does at the points private domains register into,
when nothing is registered.

Runs in a fresh interpreter: the points freeze once read, and the suite's own
app may have installed extensions into them already. The probe
builds the app through the open factory alone and reports what each point
answers. The product side of each point is pinned by the domain's own tests:
the live artifact document in ``test_docsync.py``, a new org's knowledge sync
in ``test_org_sync_default.py``, a team holding a connection in
``test_teams_routes.py``, the gate's vocabulary on the org settings read in
``test_org_settings_routes.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Any

import pytest
from alkera_core.extensions import ExtensionError, ExtensionPoint
from alkera_core.models import OrgSettings
from backend.services.org import removal_hooks
from backend.services.org.settings_sections import OrgSettingsSection, section_fields
from backend.services.realtime.doc_kinds import RealtimeDocKind, registered_doc_kinds

pytestmark = [pytest.mark.xdist_group("backend_open_points")]

PROBE = """
import asyncio, json, uuid

from backend.app_factory import create_app

create_app()

from backend.services.realtime.channels import Channel, ChannelError, authorize
from backend.services.realtime.docsync import DocOpRejectedError, DocRegistry
from backend.services.realtime.filters import EntitlementSnapshot

report = {}


async def artifact_channel():
    ent = EntitlementSnapshot(
        org_id=uuid.uuid4(), team_ids=frozenset(), org_admin=True, platform=False
    )
    channel = Channel(doc_type="artifact", doc_id=str(uuid.uuid4()))
    try:
        await authorize(None, object(), channel, ent=ent)
    except ChannelError as exc:
        return exc.code
    return "granted"


report["artifact_channel"] = asyncio.run(artifact_channel())

registry = DocRegistry()
try:
    registry.strategy("artifact")
    report["artifact_strategy"] = "served"
except DocOpRejectedError as exc:
    report["artifact_strategy"] = exc.code
report["chat_strategy"] = type(registry.strategy("chat")).__name__

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import OrgSyncSettings
from sqlalchemy import func, select

from backend.services.org import removal_hooks, teams
from backend.services.org import settings as org_settings


async def org_rows():
    async with AsyncSessionLocal() as db:
        org = await teams.create_org_rows(db, org_name=f"Open probe {uuid.uuid4().hex[:8]}")
        await db.flush()
        sync_rows = await db.scalar(
            select(func.count())
            .select_from(OrgSyncSettings)
            .where(OrgSyncSettings.org_team_id == org.id)
        )
        refusal = await removal_hooks.team_delete_refusal(db, team_id=org.id)
        read = org_settings.read_shape(await org_settings.get_effective(db, org.id))
        await db.rollback()
    return sync_rows, refusal, read.gate_builtin_force_rules, read.gate_known_rules


(
    report["org_sync_rows"],
    report["team_delete_refusal"],
    report["gate_builtin_force_rules"],
    report["gate_known_rules"],
) = asyncio.run(org_rows())

print("REPORT" + json.dumps(report))
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


def test_an_unregistered_live_document_kind_is_a_channel_nobody_can_find(
    report: dict[str, Any],
) -> None:
    # Refused before any row is read: the probe hands the socket no database.
    assert report["artifact_channel"] == "not_found"


def test_an_unregistered_live_document_kind_has_no_strategy(report: dict[str, Any]) -> None:
    assert report["artifact_strategy"] == "unsupported_kind"


def test_the_socket_serves_chats_with_nothing_registered(report: dict[str, Any]) -> None:
    assert report["chat_strategy"] == "ChatAppendById"


def test_an_org_starts_with_its_own_rows_alone_when_nothing_registered(
    report: dict[str, Any],
) -> None:
    # The knowledge base's sync settings are a registered domain's row.
    assert report["org_sync_rows"] == 0


def test_nothing_blocks_a_team_deletion_when_nothing_registered(report: dict[str, Any]) -> None:
    assert report["team_delete_refusal"] is None


def test_the_org_settings_read_states_no_domain_vocabulary_when_nothing_registered(
    report: dict[str, Any],
) -> None:
    # The gate's floor and rule ids are the gate's to state.
    assert report["gate_builtin_force_rules"] == []
    assert report["gate_known_rules"] == []


async def _no_grant(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("never asked")


def _kind(doc_type: str) -> RealtimeDocKind:
    # A kind's strategy is never touched by the registry read.
    return RealtimeDocKind(doc_type=doc_type, authorize=_no_grant, strategy=object())


@pytest.mark.parametrize(
    ("doc_types", "message"),
    [
        pytest.param(("chat",), "served by the socket itself", id="chat-is-native"),
        pytest.param(("notebook",), "served by the socket itself", id="crdt-is-native"),
        pytest.param(
            ("chat_workspace",), "served by the socket itself", id="legacy-chat-spelling-is-native"
        ),
        pytest.param(("wiki",), "no channel can name", id="not-on-the-wire"),
        pytest.param(("artifact", "artifact"), "registered twice", id="registered-twice"),
    ],
)
def test_a_kind_that_could_never_be_served_is_refused_at_the_first_read(
    doc_types: tuple[str, ...], message: str
) -> None:
    point: ExtensionPoint[RealtimeDocKind] = ExtensionPoint(f"probe-{uuid.uuid4()}")
    for doc_type in doc_types:
        point.register(_kind(doc_type))
    with pytest.raises(ExtensionError, match=message):
        registered_doc_kinds(point)


def test_a_registered_kind_is_found_by_its_document_type() -> None:
    point: ExtensionPoint[RealtimeDocKind] = ExtensionPoint(f"probe-{uuid.uuid4()}")
    kind = _kind("artifact")
    point.register(kind)
    assert registered_doc_kinds(point) == {"artifact": kind}


# --- org removal hooks ------------------------------------------------------


async def _noop_hook(_db: object, _event: object) -> None:
    return None


def test_a_removal_hook_registered_after_its_moment_ran_is_refused() -> None:
    """A hook that arrives late would leave every removal that already ran
    without it; it is refused instead of silently missing them."""
    removal_hooks.registered()
    with pytest.raises(ExtensionError, match="already read"):
        removal_hooks.on_team_deleted("arrived.late")(_noop_hook)
    assert "arrived.late" not in removal_hooks.registered()["team_deleted"]


async def _other_hook(_db: object, _event: object) -> None:
    return None


def test_two_removal_hooks_under_one_name_are_refused_at_the_first_run() -> None:
    point: ExtensionPoint[removal_hooks.NamedHook[removal_hooks.TeamDeleted]] = ExtensionPoint(
        f"probe-{uuid.uuid4()}"
    )
    point.register(removal_hooks.NamedHook("org_machines.owner", _noop_hook))
    point.register(removal_hooks.NamedHook("org_machines.owner", _other_hook))
    with pytest.raises(ExtensionError, match="two hooks"):
        removal_hooks.hooks_of(point)


# --- org settings sections --------------------------------------------------


def _section(name: str, fields: dict[str, object]) -> OrgSettingsSection:
    return OrgSettingsSection(name, lambda _row: fields)


def _sections(*sections: OrgSettingsSection) -> ExtensionPoint[OrgSettingsSection]:
    point: ExtensionPoint[OrgSettingsSection] = ExtensionPoint(f"probe-{uuid.uuid4()}")
    for section in sections:
        point.register(section)
    return point


def test_every_sections_fields_are_folded_into_the_read() -> None:
    point = _sections(
        _section("floor", {"gate_builtin_force_rules": ["TABLE_DROPPED"]}),
        _section("rules", {"gate_known_rules": ["TABLE_DROPPED", "TYPE_CHANGED"]}),
    )
    assert section_fields(OrgSettings(), point) == {
        "gate_builtin_force_rules": ["TABLE_DROPPED"],
        "gate_known_rules": ["TABLE_DROPPED", "TYPE_CHANGED"],
    }


@pytest.mark.parametrize(
    ("sections", "message"),
    [
        pytest.param(
            (_section("typo", {"gate_known_rule": []}),),
            "fields the read has not",
            id="field-the-read-has-not",
        ),
        pytest.param(
            (
                _section("first", {"gate_known_rules": ["A"]}),
                _section("second", {"gate_known_rules": ["B"]}),
            ),
            "already stated",
            id="field-stated-twice",
        ),
    ],
)
def test_a_section_that_would_be_dropped_or_overwritten_is_refused(
    sections: tuple[OrgSettingsSection, ...], message: str
) -> None:
    with pytest.raises(ExtensionError, match=message):
        section_fields(OrgSettings(), _sections(*sections))
