"""The three tables the connection lifecycle writes, round-tripped.

What is worth pinning here is not that a column exists but that the vocabulary
is enforced by the DATABASE: every one of these columns used to be a free string
whose typos a reader had to guess at, so each CHECK gets a case that proves a
value outside the vocabulary is refused, and each renamed column gets a case that
proves the fact it now carries is a different fact from the one it replaced.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.connections import CredentialState, Outcome, VerificationState
from alkera_core.connections.models import ConnectionVerification, TeamConnection, UserOAuthToken
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import Team, User
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


async def _org(session: AsyncSession) -> Team:
    team = Team(name=f"org-{secrets.token_hex(4)}", is_root=True)
    session.add(team)
    await session.flush()
    return team


async def _user(session: AsyncSession, team: Team) -> User:
    user = User(
        home_org_team_id=team.id,
        email=f"admin-{secrets.token_hex(6)}@alkera.dev",
        first_name="Con",
        last_name="Nect",
    )
    session.add(user)
    await session.flush()
    return user


async def _connection(session: AsyncSession, team: Team) -> TeamConnection:
    conn = TeamConnection(
        team_id=team.id,
        plugin="snowflake",
        handle=f"wh-{secrets.token_hex(4)}",
        auth_method="password",
    )
    session.add(conn)
    await session.flush()
    return conn


async def test_verification_round_trip_carries_the_dispatch_facts() -> None:
    """A record says where it is, what it found, and how the hand-off went —
    and the hand-off facts survive the process that wrote them."""
    async with AsyncSessionLocal() as session:
        team = await _org(session)
        requester = await _user(session, team)
        conn = await _connection(session, team)
        record = ConnectionVerification(
            team_id=team.id,
            connection_id=conn.id,
            plugin="snowflake",
            attributes={"account": "acme"},
            state=VerificationState.settled.value,
            outcome=Outcome.invalid_credential.value,
            detail="The warehouse refused the password.",
            endpoint_results={"primary": {"outcome": "invalid_credential", "detail": "refused"}},
            payload_hash="a" * 64,
            dispatched_at=datetime.now(UTC),
            dispatch_attempts=3,
            last_dispatch_error="no answer from the orchestrator within 2s",
            started_at=datetime.now(UTC),
            heartbeat_at=datetime.now(UTC),
            latency_ms=412,
            requested_by_id=requester.id,
            finished_at=datetime.now(UTC),
        )
        session.add(record)
        await session.commit()
        record_id = record.id

    async with AsyncSessionLocal() as session:
        loaded = (
            await session.execute(
                select(ConnectionVerification).where(ConnectionVerification.id == record_id)
            )
        ).scalar_one()
        assert loaded.state == VerificationState.settled
        assert loaded.outcome == Outcome.invalid_credential
        assert loaded.dispatch_attempts == 3
        assert loaded.last_dispatch_error.startswith("no answer")
        assert loaded.endpoint_results["primary"]["outcome"] == "invalid_credential"
        assert loaded.payload_hash == "a" * 64
        assert loaded.latency_ms == 412
        assert loaded.heartbeat_at is not None


async def test_draft_verification_has_no_connection_and_defaults_to_queued() -> None:
    """A draft is the same record with no row behind it yet; it starts queued,
    undispatched, with nothing found."""
    async with AsyncSessionLocal() as session:
        team = await _org(session)
        record = ConnectionVerification(team_id=team.id, plugin="snowflake", attributes={})
        session.add(record)
        await session.commit()
        record_id = record.id

    async with AsyncSessionLocal() as session:
        loaded = (
            await session.execute(
                select(ConnectionVerification).where(ConnectionVerification.id == record_id)
            )
        ).scalar_one()
        assert loaded.connection_id is None
        assert loaded.state == VerificationState.queued
        assert loaded.outcome is None
        assert loaded.dispatch_attempts == 0
        assert loaded.dispatched_at is None
        assert loaded.started_at is None


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param("state", "pending", id="state-is-not-the-old-probe-word"),
        pytest.param("outcome", "validating", id="outcome-is-not-a-state"),
    ],
)
async def test_verification_refuses_a_value_outside_the_vocabulary(column: str, value: str) -> None:
    async with AsyncSessionLocal() as session:
        team = await _org(session)
        record = ConnectionVerification(team_id=team.id, plugin="snowflake", attributes={})
        setattr(record, column, value)
        session.add(record)
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


async def test_verification_follows_its_connection_into_the_grave() -> None:
    """The record references the row it verified; deleting the row takes it."""
    async with AsyncSessionLocal() as session:
        team = await _org(session)
        conn = await _connection(session, team)
        record = ConnectionVerification(
            team_id=team.id, connection_id=conn.id, plugin="snowflake", attributes={}
        )
        session.add(record)
        await session.commit()
        record_id = record.id
        await session.delete(conn)
        await session.commit()
        assert (
            await session.execute(
                select(ConnectionVerification).where(ConnectionVerification.id == record_id)
            )
        ).scalar_one_or_none() is None


async def test_connection_keeps_the_two_axes_apart() -> None:
    """What the last check found, when it ran, whether one is in flight, and
    whether the credential opens at all are four independent facts."""
    verified = datetime.now(UTC) - timedelta(hours=30)
    async with AsyncSessionLocal() as session:
        team = await _org(session)
        conn = await _connection(session, team)
        conn.last_outcome = Outcome.ok.value
        conn.last_verified_at = verified
        conn.last_detail = ""
        conn.last_verification_id = uuid.uuid4()
        conn.verification_state = VerificationState.running.value
        conn.credential_state = CredentialState.unreadable.value
        conn.credential_state_reason = "Alkera can't open this credential; rotate it."
        await session.commit()
        conn_id = conn.id

    async with AsyncSessionLocal() as session:
        loaded = (
            await session.execute(select(TeamConnection).where(TeamConnection.id == conn_id))
        ).scalar_one()
        # An ok outcome 30 hours old, a check running right now, and a
        # credential nobody can open — all true of the same row at once.
        assert loaded.last_outcome == Outcome.ok
        assert loaded.last_verified_at is not None
        assert (datetime.now(UTC) - loaded.last_verified_at) > timedelta(hours=24)
        assert loaded.verification_state == VerificationState.running
        assert loaded.credential_state == CredentialState.unreadable
        assert loaded.credential_state_reason.startswith("Alkera can't open")
        assert loaded.last_verification_id is not None


async def test_connection_defaults_say_nothing_has_been_checked() -> None:
    """A fresh row has never been looked at — which the old ``unverified``
    could not distinguish from a check that ran and found nothing to say."""
    async with AsyncSessionLocal() as session:
        team = await _org(session)
        conn = await _connection(session, team)
        await session.commit()
        conn_id = conn.id

    async with AsyncSessionLocal() as session:
        loaded = (
            await session.execute(select(TeamConnection).where(TeamConnection.id == conn_id))
        ).scalar_one()
        assert loaded.last_outcome is None
        assert loaded.last_verified_at is None
        assert loaded.verification_state is None
        assert loaded.last_detail == ""
        assert loaded.credential_state == CredentialState.present
        assert loaded.credential_state_reason == ""


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param("last_outcome", "validating", id="outcome-is-not-a-state"),
        pytest.param("verification_state", "settled", id="in-flight-only"),
        pytest.param("verification_state", "unverified", id="no-old-words"),
        pytest.param("credential_state", "needs_reauth", id="shared-credential-cannot-reauth"),
    ],
)
async def test_connection_refuses_a_value_outside_the_vocabulary(column: str, value: str) -> None:
    async with AsyncSessionLocal() as session:
        team = await _org(session)
        conn = await _connection(session, team)
        setattr(conn, column, value)
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


async def test_oauth_grant_records_a_definitive_refusal() -> None:
    """A refusal the provider meant is a durable fact with its own sentence and
    the moment it happened — and the refresh token is kept, not discarded."""
    changed = datetime.now(UTC)
    async with AsyncSessionLocal() as session:
        team = await _org(session)
        member = await _user(session, team)
        conn = await _connection(session, team)
        token = UserOAuthToken(
            user_id=member.id,
            team_connection_id=conn.id,
            access_token_encrypted="cipher-access",
            refresh_token_encrypted="cipher-refresh",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            state=CredentialState.needs_reauth.value,
            state_reason="The provider refused to renew your session. Sign in again.",
            state_changed_at=changed,
            refreshed_at=changed - timedelta(hours=2),
        )
        session.add(token)
        await session.commit()
        token_id = token.id

    async with AsyncSessionLocal() as session:
        loaded = (
            await session.execute(select(UserOAuthToken).where(UserOAuthToken.id == token_id))
        ).scalar_one()
        assert loaded.state == CredentialState.needs_reauth
        assert loaded.state_reason.endswith("Sign in again.")
        assert loaded.state_changed_at is not None
        assert loaded.refreshed_at is not None
        # Kept: a member who signs in again renews the same grant.
        assert loaded.refresh_token_encrypted == "cipher-refresh"
        # The sweep deliberately never touches this.
        assert loaded.last_used_at is None


async def test_oauth_grant_defaults_to_present() -> None:
    async with AsyncSessionLocal() as session:
        team = await _org(session)
        member = await _user(session, team)
        conn = await _connection(session, team)
        token = UserOAuthToken(
            user_id=member.id,
            team_connection_id=conn.id,
            access_token_encrypted="cipher-access",
        )
        session.add(token)
        await session.commit()
        token_id = token.id

    async with AsyncSessionLocal() as session:
        loaded = (
            await session.execute(select(UserOAuthToken).where(UserOAuthToken.id == token_id))
        ).scalar_one()
        assert loaded.state == CredentialState.present
        assert loaded.state_reason == ""
        assert loaded.state_changed_at is None
        assert loaded.refreshed_at is None


async def test_oauth_grant_refuses_a_state_outside_the_vocabulary() -> None:
    async with AsyncSessionLocal() as session:
        team = await _org(session)
        member = await _user(session, team)
        conn = await _connection(session, team)
        token = UserOAuthToken(
            user_id=member.id,
            team_connection_id=conn.id,
            access_token_encrypted="cipher-access",
            state="unreadable",
        )
        session.add(token)
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
