"""The parts of the event stream that are pure: who may see which event, the
connection caps, the frame text and the cursor parser. ``load_entitlements``
is the one function here that reads the database, so it runs against it."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from alkera_core.config import settings
from alkera_core.events import ACCESS_CHANGED_KEY, EventType, HubEvent
from alkera_core.models import TeamRole, User
from backend.services.org import memberships as membership_service
from backend.services.org import teams as team_service
from backend.services.realtime import sse
from backend.services.realtime.filters import (
    EntitlementRef,
    EntitlementSnapshot,
    load_entitlements,
    reauthorizes,
    stream_predicate,
    visible_to,
)
from backend.services.realtime.limits import ConnectionGate
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, make_member

ORG = UUID("00000000-0000-4000-8000-00000000000a")
OTHER_ORG = UUID("00000000-0000-4000-8000-00000000000b")
ME = UUID("00000000-0000-4000-8000-000000000001")
SOMEONE_ELSE = UUID("00000000-0000-4000-8000-000000000002")
TEAM = UUID("00000000-0000-4000-8000-000000000011")
SIBLING_TEAM = UUID("00000000-0000-4000-8000-000000000012")


def _event(
    *,
    org_id: UUID = ORG,
    visibility: str = "org",
    team_id: UUID | None = None,
    lane: str = "durable",
    type: str = "kb_item.changed",
    id: int | None = 7,
    payload: dict[str, Any] | None = None,
) -> HubEvent:
    body = dict(payload or {})
    if team_id is not None:
        body["team_id"] = str(team_id)
    return HubEvent(
        lane=lane,  # type: ignore[arg-type]
        org_id=org_id,
        type=type,
        entity="kb_item",
        entity_id="item-1",
        version=2,
        visibility=visibility,
        payload=body,
        id=id,
    )


def _ent(
    *,
    team_ids: set[UUID] | None = None,
    org_admin: bool = False,
    platform: bool = False,
    org_id: UUID | None = ORG,
) -> EntitlementSnapshot:
    return EntitlementSnapshot(
        org_id=org_id,
        team_ids=frozenset(team_ids or set()),
        org_admin=org_admin,
        platform=platform,
    )


# ---------------------------------------------------------------------------
# which announcements re-open a decision an open socket already made
# ---------------------------------------------------------------------------


def test_a_share_announcement_always_reauthorizes() -> None:
    """The standing case: a grant made, revoked or moved."""
    assert reauthorizes(_event(type=EventType.FILE_NODE_CHANGED.value), user_id=ME) is True


def test_an_ordinary_chat_update_does_not_reauthorize() -> None:
    """A title change and every streamed turn ring the same doorbell. Deciding
    on them would put a database read per channel on the streaming path."""
    event = _event(type=EventType.CHAT_UPDATED.value, payload={"team_id": None})
    assert reauthorizes(event, user_id=ME) is False


def test_a_chat_update_marked_access_changed_reauthorizes() -> None:
    """The chat came or went — deleted, trashed, restored — so anybody already
    watching it has to be decided again rather than told to re-read."""
    event = _event(type=EventType.CHAT_UPDATED.value, payload={ACCESS_CHANGED_KEY: True})
    assert reauthorizes(event, user_id=ME) is True


@pytest.mark.parametrize(
    "marker",
    [pytest.param("true", id="a-string"), pytest.param(1, id="a-number")],
)
def test_only_a_real_true_marks_an_access_change(marker: object) -> None:
    """A truthy value is not the marker: the key is a contract between the
    producer and this predicate, and guessing at it is how a streaming path
    acquires a per-frame query nobody intended."""
    event = _event(type=EventType.CHAT_UPDATED.value, payload={ACCESS_CHANGED_KEY: marker})
    assert reauthorizes(event, user_id=ME) is False


def test_a_membership_change_naming_this_user_reauthorizes() -> None:
    """A role downgrade, a removal from a team, a deactivation: the person's own
    standing moved, and what they may read moved with it."""
    event = _event(
        type=EventType.MEMBERSHIP_CHANGED.value,
        payload={"team_id": str(TEAM), "user_id": str(ME), "role": "member"},
    )
    assert reauthorizes(event, user_id=ME) is True


def test_a_membership_change_naming_somebody_else_does_not() -> None:
    """An org's directory churns; a socket re-deciding on every colleague's
    membership row would re-decide on nothing that concerns it."""
    event = _event(
        type=EventType.MEMBERSHIP_CHANGED.value,
        payload={"team_id": str(TEAM), "user_id": str(SOMEONE_ELSE), "role": "member"},
    )
    assert reauthorizes(event, user_id=ME) is False


@pytest.mark.parametrize(
    "named",
    [pytest.param(None, id="no-user-named"), pytest.param(7, id="not-a-string")],
)
def test_a_membership_change_that_names_nobody_usable_does_not_reauthorize(named: object) -> None:
    payload: dict[str, Any] = {"team_id": str(TEAM)}
    if named is not None:
        payload["user_id"] = named
    event = _event(type=EventType.MEMBERSHIP_CHANGED.value, payload=payload)
    assert reauthorizes(event, user_id=ME) is False


def test_the_transcript_lane_itself_never_reauthorizes() -> None:
    """The doc-sync frames a subscriber is there for carry no access news."""
    assert reauthorizes(_event(type=EventType.DOC_OP.value), user_id=ME) is False


# ---------------------------------------------------------------------------
# visible_to
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "ent", "expected"),
    [
        pytest.param(_event(), _ent(), True, id="org-visible-to-any-member"),
        pytest.param(
            _event(org_id=OTHER_ORG),
            _ent(org_admin=True, platform=True),
            False,
            id="foreign-org-refused-even-for-admin-staff",
        ),
        pytest.param(_event(visibility=f"user:{ME}"), _ent(), True, id="addressed-to-me"),
        pytest.param(
            _event(visibility=f"user:{SOMEONE_ELSE}"),
            _ent(org_admin=True),
            False,
            id="addressed-to-someone-else-refused-even-for-admin",
        ),
        pytest.param(
            _event(visibility="platform"), _ent(platform=True), True, id="platform-row-for-staff"
        ),
        pytest.param(
            _event(visibility="platform"),
            _ent(org_admin=True),
            False,
            id="platform-row-refused-for-org-admin",
        ),
        pytest.param(
            _event(visibility="team:x"),
            _ent(org_admin=True, platform=True),
            False,
            id="unknown-visibility-grammar-refused",
        ),
        pytest.param(
            _event(visibility=""), _ent(org_admin=True), False, id="empty-visibility-refused"
        ),
        pytest.param(_event(team_id=TEAM), _ent(team_ids={TEAM}), True, id="team-member"),
        pytest.param(
            _event(team_id=TEAM),
            _ent(team_ids={SIBLING_TEAM}),
            False,
            id="sibling-team-member-refused",
        ),
        pytest.param(_event(team_id=TEAM), _ent(org_admin=True), True, id="org-admin-sees-teams"),
        pytest.param(
            _event(team_id=TEAM),
            _ent(platform=True),
            False,
            id="platform-role-alone-does-not-grant-a-team",
        ),
        pytest.param(
            _event(visibility=f"user:{ME}", team_id=TEAM),
            _ent(),
            False,
            id="addressed-to-me-but-team-scoped-still-needs-the-team",
        ),
        pytest.param(
            _event(payload={"team_id": "not-a-uuid"}),
            _ent(),
            True,
            id="unparseable-team-id-is-no-team-scope",
        ),
    ],
)
def test_visible_to(event: HubEvent, ent: EntitlementSnapshot, expected: bool) -> None:
    assert visible_to(event, user_id=ME, org_id=ORG, ent=ent) is expected


# ---------------------------------------------------------------------------
# stream_predicate
# ---------------------------------------------------------------------------


def test_stream_predicate_admits_only_durable_client_deliverable_visible_rows() -> None:
    accept = stream_predicate(user_id=ME, org_id=ORG, ref=EntitlementRef(_ent()))
    assert accept(_event()) is True
    assert accept(_event(lane="ephemeral", id=None)) is False, "ephemeral never reaches the stream"
    assert accept(_event(type=EventType.DOC_OP.value)) is False, "doc.op is server-only"
    assert accept(_event(type=EventType.AUTHZ_DECISION.value)) is False
    assert accept(_event(org_id=OTHER_ORG)) is False


def test_stream_predicate_reads_the_entitlement_ref_live() -> None:
    """The keepalive tick swaps the snapshot; a predicate captured at open must
    see the new one without being rebuilt."""
    ref = EntitlementRef(_ent())
    accept = stream_predicate(user_id=ME, org_id=ORG, ref=ref)
    team_row = _event(team_id=TEAM)
    assert accept(team_row) is False
    ref.value = _ent(team_ids={TEAM})
    assert accept(team_row) is True
    ref.value = _ent()
    assert accept(team_row) is False


# ---------------------------------------------------------------------------
# load_entitlements (real database)
# ---------------------------------------------------------------------------


async def test_load_entitlements_for_an_org_admin(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    admin = await real_session.get(User, org_admin.admin_id)
    assert admin is not None
    ent = await load_entitlements(real_session, admin, org_id=admin.home_org_team_id)
    assert ent.org_admin is True
    assert ent.platform is False
    assert org_admin.org_id in ent.team_ids


async def test_load_entitlements_for_a_subteam_member_carries_the_materialized_chain(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    eng = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Eng", parent_team_id=org_admin.org_id
    )
    data = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Data", parent_team_id=eng.id
    )
    sibling = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Platform", parent_team_id=eng.id
    )
    await real_session.commit()
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    # ``make_member`` gives a root MEMBER row; add the leaf through the service so
    # the chain rows are materialized the way production writes them.
    await membership_service.add_member(
        real_session, team_id=data.id, user_id=member.id, role=TeamRole.MEMBER
    )
    await real_session.commit()

    ent = await load_entitlements(real_session, member, org_id=member.home_org_team_id)
    assert ent.org_admin is False
    assert ent.platform is False
    assert {data.id, eng.id, org_admin.org_id} <= ent.team_ids
    assert sibling.id not in ent.team_ids


async def test_load_entitlements_marks_platform_staff(
    real_session: AsyncSession, platform_support: OrgWithAdmin
) -> None:
    staff = await real_session.get(User, platform_support.admin_id)
    assert staff is not None
    ent = await load_entitlements(real_session, staff, org_id=staff.home_org_team_id)
    assert ent.platform is True
    assert ent.org_admin is True


# ---------------------------------------------------------------------------
# ConnectionGate
# ---------------------------------------------------------------------------


def test_gate_caps_per_key_and_in_total() -> None:
    gate = ConnectionGate(total=lambda: 3, per_user=lambda: 2)
    assert gate.try_acquire("a") and gate.try_acquire("a")
    assert gate.try_acquire("a") is False, "per-key cap"
    assert gate.active_for("a") == 2
    assert gate.try_acquire("b") is True
    assert gate.try_acquire("c") is False, "process-wide cap"
    assert gate.active == 3
    gate.release("a")
    assert gate.active_for("a") == 1
    assert gate.try_acquire("c") is True
    gate.release("b")
    assert gate.active_for("b") == 0
    assert "b" not in gate._by_key


def test_gate_caps_a_person_and_all_their_machines_together() -> None:
    """A machine's key is its own, so a box never takes one of its owner's
    slots; the per-principal cap is what bounds how many machine keys one
    person spreads across, and another person is untouched by it."""
    gate = ConnectionGate(total=lambda: 10, per_user=lambda: 1, per_principal=lambda: 3)
    assert gate.try_acquire("u1")
    assert gate.try_acquire("agent:u1:m1")
    assert gate.try_acquire("agent:u1:m2")
    assert gate.try_acquire("agent:u1:m3") is False, "per-principal cap"
    assert gate.try_acquire("u2") and gate.try_acquire("agent:u2:m1"), "another person"
    assert gate.active_for_principal("agent:u1:m9") == 3
    gate.release("agent:u1:m1")
    assert gate.try_acquire("agent:u1:m3") is True
    for key in ("u1", "agent:u1:m2", "agent:u1:m3"):
        gate.release(key)
    assert gate.active_for_principal("u1") == 0
    assert "u1" not in gate._by_principal


def test_gate_release_never_goes_negative() -> None:
    gate = ConnectionGate(total=lambda: 1, per_user=lambda: 1)
    gate.release("nobody")
    assert gate.active == 0
    assert gate.active_for("nobody") == 0
    assert gate.try_acquire("x") is True


def test_gate_reads_its_limits_live(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = ConnectionGate.sse()
    monkeypatch.setattr(settings, "realtime_sse_max_streams_per_user", 1)
    assert gate.try_acquire("u") is True
    assert gate.try_acquire("u") is False
    monkeypatch.setattr(settings, "realtime_sse_max_streams_per_user", 2)
    assert gate.try_acquire("u") is True


def test_sse_and_ws_gates_are_distinct_singletons() -> None:
    assert ConnectionGate.sse() is ConnectionGate.sse()
    assert ConnectionGate.ws() is ConnectionGate.ws()
    assert ConnectionGate.sse() is not ConnectionGate.ws()
    ConnectionGate.sse().try_acquire("u")
    assert ConnectionGate.ws().active == 0
    ConnectionGate.reset_for_tests()
    assert ConnectionGate.sse().active == 0


# ---------------------------------------------------------------------------
# Frame text and the cursor parser
# ---------------------------------------------------------------------------


def test_event_frame_is_id_event_and_a_thin_data_body() -> None:
    frame = sse.event_frame(_event(id=42))
    assert frame == (
        "id: 42\nevent: kb_item.changed\n"
        'data: {"type":"kb_item.changed","entity":"kb_item","entity_id":"item-1",'
        f'"version":2,"org_id":"{ORG}"}}\n\n'
    )


@pytest.mark.parametrize(
    ("value", "carried"),
    [
        pytest.param(True, True, id="set-by-the-writer"),
        pytest.param(False, False, id="false-is-absent"),
        pytest.param("true", False, id="a-string-is-not-the-flag"),
        pytest.param(1, False, id="a-number-is-not-the-flag"),
    ],
)
def test_a_lease_frame_carries_subtree_only_when_the_writer_set_it(
    value: Any, carried: bool
) -> None:
    """The one flag a frame may carry: a tree report that touched too many
    folders to name tells every client to refresh what it has open under the
    lease. Its landing count stays on the row -- the frame is a nudge, and the
    count is read back through the listing."""
    frame = sse.event_frame(
        _event(
            type=EventType.FILE_LEASE_CHANGED.value,
            payload={"subtree": value, "landing_count": 3, "lease_node_id": str(TEAM)},
        )
    )
    data = json.loads(frame.split("data: ", 1)[1])
    assert ("subtree" in data) is carried
    if carried:
        assert data["subtree"] is True
    assert "landing_count" not in data
    assert data["lease_node_id"] == str(TEAM)


def test_event_frame_refuses_a_row_without_an_id() -> None:
    with pytest.raises(ValueError, match="outbox id"):
        sse.event_frame(_event(id=None))


@pytest.mark.parametrize("type_", [EventType.DOC_OP.value, EventType.AUTHZ_DECISION.value])
def test_event_frame_refuses_a_server_only_type(type_: str) -> None:
    with pytest.raises(ValueError):
        sse.event_frame(_event(type=type_))


def test_reset_error_retry_and_comment_frames_are_exact() -> None:
    assert sse.reset_frame("overflow") == 'event: reset\ndata: {"reason":"overflow"}\n\n'
    assert sse.error_frame("unauthorized") == 'event: error\ndata: {"code": "unauthorized"}\n\n'
    assert sse.retry_frame(2000) == "retry: 2000\n\n"
    assert sse.CONNECTED_COMMENT == ": connected\n\n"
    assert sse.KEEPALIVE_COMMENT == ": keepalive\n\n"
    assert "id:" not in sse.reset_frame("overflow"), "a reset must not move the cursor"


def test_cursor_frame_is_an_id_alone() -> None:
    """No ``event:`` and no ``data:``: a client dispatches nothing for it and
    only takes the id as its resume cursor."""
    assert sse.cursor_frame(42) == "id: 42\n\n"


def test_framed_ids_admit_an_id_once() -> None:
    framed = sse.FramedIds()
    assert framed.add(5) is True
    assert framed.add(5) is False
    assert 5 in framed and 6 not in framed
    assert len(framed) == 1


def test_framed_ids_are_seeded_and_order_does_not_matter() -> None:
    """A straggler has a LOWER id than what was framed before it; the set must
    not treat 'lower than the last' as 'already seen'."""
    framed = sse.FramedIds([10, 12])
    assert framed.add(12) is False, "seeded as already framed"
    assert framed.add(11) is True, "between two framed ids, never framed itself"
    assert framed.add(3) is True, "far below everything framed, never framed itself"
    assert framed.add(3) is False


def test_framed_ids_forget_the_oldest_past_the_limit() -> None:
    framed = sse.FramedIds(limit=3)
    for id_ in (1, 2, 3):
        assert framed.add(id_) is True
    assert framed.add(4) is True
    assert 1 not in framed, "the oldest is evicted first"
    assert all(id_ in framed for id_ in (2, 3, 4))
    assert len(framed) == 3
    assert framed.add(1) is True, "an evicted id is no longer known — the window is bounded"


def test_framed_ids_default_limit_matches_the_listener_window() -> None:
    from alkera_core.events.listener import RECENT_ID_MEMORY

    assert sse.FRAMED_ID_MEMORY == RECENT_ID_MEMORY == 4096


@pytest.mark.parametrize("limit", [0, -1])
def test_framed_ids_refuse_a_limit_that_cannot_hold_an_id(limit: int) -> None:
    with pytest.raises(ValueError, match=">= 1"):
        sse.FramedIds(limit=limit)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(None, None, id="absent"),
        pytest.param("", None, id="empty"),
        pytest.param("abc", None, id="text"),
        pytest.param("-1", None, id="negative"),
        pytest.param("1.5", None, id="fraction"),
        pytest.param("1e3", None, id="exponent"),
        pytest.param("0", 0, id="zero"),
        pytest.param("007", 7, id="leading-zeros"),
        pytest.param(" 12 ", 12, id="whitespace-trimmed"),
        pytest.param("9223372036854775807", 9223372036854775807, id="bigint-max"),
        pytest.param("9223372036854775808", None, id="past-bigint"),
        pytest.param("\uff11\uff12", None, id="fullwidth-digits-are-not-a-cursor"),
    ],
)
def test_parse_cursor(value: str | None, expected: int | None) -> None:
    assert sse.parse_cursor(value) == expected
