"""Signed content URLs: single use, session-bound, and no oracle on failure."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import SessionId, VersionId
from alkera_core.files.repo import FilesRepo
from alkera_core.files.signed_urls import (
    CONTENT_PATH_PREFIX,
    NONCE_BYTES,
    ContentClaim,
    mint_content_url,
    parse_claim,
    redeem,
)
from alkera_core.models.files.stores import FileDrive
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

KEY = b"a-signing-key-for-the-content-domain"
ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
USER = uuid.UUID("22222222-2222-4222-8222-222222222222")
OTHER_KEY = b"the-key-an-attacker-would-have-to-guess"


@pytest.fixture
def wall_clock() -> FakeClock:
    """Anchored to real time: the deadline is checked against Postgres ``now()``,
    so a clock parked in 2026-01-01 would mint grants that are already expired."""
    return FakeClock(now=datetime.now(UTC))


@pytest.fixture
async def version_id(
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
) -> VersionId:
    """A real ``file_versions`` row: the grant's foreign key points at one."""
    drive: FileDrive = await files_factory.drive()
    nodes = await files_factory.tree("doc.txt", drive=drive)
    made = uuid.uuid4()
    await files_session.execute(
        text(
            "INSERT INTO file_versions "
            "(id, org_team_id, node_id, seq, size_bytes, content_hash, source, "
            "keep_forever, held, lease_epoch, metadata) "
            "VALUES (:id, :org, :node, 1, 11, 'b3-deadbeef', 'upload', "
            "false, false, 0, '{}'::jsonb)"
        ),
        {"id": made, "org": files_org.org_team_id, "node": nodes["doc.txt"].id},
    )
    await files_session.commit()
    return VersionId(made)


@pytest.fixture
def statements() -> Iterator[list[str]]:
    """Every SQL statement that touched ``file_content_grants``, in order."""
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        if "file_content_grants" in statement:
            seen.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", record)


async def test_a_minted_url_round_trips_with_its_range_and_session(
    repo: FilesRepo, version_id: VersionId, wall_clock: FakeClock
) -> None:
    session = SessionId(uuid.uuid4())
    url = await mint_content_url(
        repo,
        version_id=version_id,
        session_id=session,
        range=(64, 65535),
        clock=wall_clock,
        key=KEY,
    )
    assert url.startswith(CONTENT_PATH_PREFIX)
    grant = await redeem(
        repo,
        token=url.removeprefix(CONTENT_PATH_PREFIX),
        session_id=session,
        clock=wall_clock,
        key=KEY,
    )
    assert grant is not None
    assert grant.version_id == version_id
    assert grant.session_id == session
    assert grant.range == (64, 65535)


async def test_a_grant_with_no_range_round_trips_as_no_range(
    repo: FilesRepo, version_id: VersionId, wall_clock: FakeClock
) -> None:
    """The negative twin of the range case: ``None`` must not become ``(0, 0)``."""
    session = SessionId(uuid.uuid4())
    url = await mint_content_url(
        repo, version_id=version_id, session_id=session, clock=wall_clock, key=KEY
    )
    grant = await redeem(
        repo,
        token=url.removeprefix(CONTENT_PATH_PREFIX),
        session_id=session,
        clock=wall_clock,
        key=KEY,
    )
    assert grant is not None
    assert grant.range is None


async def test_a_second_redemption_of_the_same_token_is_refused(
    repo: FilesRepo, version_id: VersionId, wall_clock: FakeClock
) -> None:
    session = SessionId(uuid.uuid4())
    url = await mint_content_url(
        repo, version_id=version_id, session_id=session, clock=wall_clock, key=KEY
    )
    token = url.removeprefix(CONTENT_PATH_PREFIX)
    assert await redeem(repo, token=token, session_id=session, clock=wall_clock, key=KEY)
    assert await redeem(repo, token=token, session_id=session, clock=wall_clock, key=KEY) is None


@pytest.mark.parametrize(
    "offset_seconds",
    [
        pytest.param(-1, id="one-second-past-the-deadline"),
        pytest.param(1, id="one-second-inside-the-deadline"),
    ],
)
async def test_the_deadline_decides_and_it_is_postgres_time(
    repo: FilesRepo,
    version_id: VersionId,
    offset_seconds: int,
) -> None:
    """The TTL boundary crossed in both directions, one second either side.

    The clock is stepped an hour into the past and the TTL chosen so the stored
    deadline lands just before or just after real Postgres ``now()`` — the only
    clock a redemption consults.
    """
    session = SessionId(uuid.uuid4())
    past = FakeClock(now=datetime.now(UTC) - timedelta(hours=1))
    url = await mint_content_url(
        repo,
        version_id=version_id,
        session_id=session,
        ttl=timedelta(hours=1, seconds=offset_seconds),
        clock=past,
        key=KEY,
    )
    grant = await redeem(
        repo,
        token=url.removeprefix(CONTENT_PATH_PREFIX),
        session_id=session,
        clock=past,
        key=KEY,
    )
    assert (grant is not None) is (offset_seconds > 0)


async def test_a_token_minted_for_one_session_is_refused_to_another(
    repo: FilesRepo, version_id: VersionId, wall_clock: FakeClock
) -> None:
    mine = SessionId(uuid.uuid4())
    theirs = SessionId(uuid.uuid4())
    url = await mint_content_url(
        repo, version_id=version_id, session_id=mine, clock=wall_clock, key=KEY
    )
    assert (
        await redeem(
            repo,
            token=url.removeprefix(CONTENT_PATH_PREFIX),
            session_id=theirs,
            clock=wall_clock,
            key=KEY,
        )
        is None
    )


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param("flip-signature", id="a-flipped-signature-byte"),
        pytest.param("flip-nonce", id="a-flipped-nonce-byte"),
        pytest.param("other-key", id="a-signature-from-another-key"),
        pytest.param("no-separator", id="a-token-with-no-signature-at-all"),
    ],
)
async def test_a_tampered_token_is_refused(
    repo: FilesRepo, version_id: VersionId, wall_clock: FakeClock, damage: str
) -> None:
    session = SessionId(uuid.uuid4())
    key = OTHER_KEY if damage == "other-key" else KEY
    url = await mint_content_url(
        repo, version_id=version_id, session_id=session, clock=wall_clock, key=key
    )
    nonce, _, signature = url.removeprefix(CONTENT_PATH_PREFIX).partition(".")
    if damage == "flip-signature":
        token = f"{nonce}.{'0' if signature[0] != '0' else '1'}{signature[1:]}"
    elif damage == "flip-nonce":
        token = f"{'0' if nonce[0] != '0' else '1'}{nonce[1:]}.{signature}"
    elif damage == "no-separator":
        token = nonce
    else:
        token = f"{nonce}.{signature}"
    assert await redeem(repo, token=token, session_id=session, clock=wall_clock, key=KEY) is None


async def test_every_failure_path_costs_the_same_one_statement(
    repo: FilesRepo,
    version_id: VersionId,
    wall_clock: FakeClock,
    statements: list[str],
) -> None:
    """No oracle by timing or by work: used, expired and foreign look alike."""
    mine = SessionId(uuid.uuid4())
    past = FakeClock(now=datetime.now(UTC) - timedelta(hours=1))

    used = (
        await mint_content_url(
            repo, version_id=version_id, session_id=mine, clock=wall_clock, key=KEY
        )
    ).removeprefix(CONTENT_PATH_PREFIX)
    await redeem(repo, token=used, session_id=mine, clock=wall_clock, key=KEY)
    expired = (
        await mint_content_url(
            repo,
            version_id=version_id,
            session_id=mine,
            ttl=timedelta(minutes=1),
            clock=past,
            key=KEY,
        )
    ).removeprefix(CONTENT_PATH_PREFIX)
    foreign = (
        await mint_content_url(
            repo, version_id=version_id, session_id=mine, clock=wall_clock, key=KEY
        )
    ).removeprefix(CONTENT_PATH_PREFIX)

    costs: list[int] = []
    for token, presenter in (
        (used, mine),
        (expired, mine),
        (foreign, SessionId(uuid.uuid4())),
    ):
        statements.clear()
        assert (
            await redeem(repo, token=token, session_id=presenter, clock=wall_clock, key=KEY) is None
        )
        costs.append(len(statements))
    assert costs == [1, 1, 1]


async def test_the_nonce_is_128_bits_of_randomness(
    repo: FilesRepo, version_id: VersionId, wall_clock: FakeClock
) -> None:
    """A guessable nonce would make the token's secrecy the only defence."""
    session = SessionId(uuid.uuid4())
    nonces = set()
    for _ in range(8):
        url = await mint_content_url(
            repo, version_id=version_id, session_id=session, clock=wall_clock, key=KEY
        )
        nonce = url.removeprefix(CONTENT_PATH_PREFIX).partition(".")[0]
        assert len(nonce) == NONCE_BYTES * 2 == 32
        assert int(nonce, 16) >= 0
        nonces.add(nonce)
    assert len(nonces) == 8


# --------------------------------------------------------------------------
# what "session-bound" means on an origin that carries no cookie
# --------------------------------------------------------------------------


def _respell_session(token: str, session_id: SessionId | None) -> str:
    """The same token with a different session spelled into its claim.

    The nonce and the signature are left exactly as minted, which is precisely
    the edit a holder of a leaked URL can make: it is only refused if the MAC
    covers the claim's session segment.

    Only the session segment is rewritten; every field after it is carried
    across untouched. The claim is free to grow — it already grew the
    disposition flag — and a splicer that rebuilt only the fields it knew about
    would drop one, which makes the token refuse because it lost a field rather
    than because the MAC covers the session.
    """
    nonce, claim, signature = token.split(".")
    raw = base64.urlsafe_b64decode(claim + "=" * (-len(claim) % 4))
    org, user, _, *rest = raw.split(b"\x1f")
    spelled = b"" if session_id is None else str(session_id).encode("ascii")
    rewritten = b"\x1f".join((org, user, spelled, *rest))
    edited = base64.urlsafe_b64encode(rewritten).decode("ascii").rstrip("=")
    return f"{nonce}.{edited}.{signature}"


async def test_respelling_a_session_edits_only_the_session(
    repo: FilesRepo, version_id: VersionId, wall_clock: FakeClock
) -> None:
    """The splice above is surgical — otherwise the test it feeds proves nothing.

    A token refused because the splicer dropped a claim field would look exactly
    like a token refused because the MAC covers the session, and the real
    binding could be gone without a single test going red. Pin that the
    respelled claim differs from the minted one in the session and in nothing
    else, with the disposition flag set so a splicer blind to it is caught.
    """
    minted = SessionId(uuid.uuid4())
    other = SessionId(uuid.uuid4())
    url = await mint_content_url(
        repo,
        version_id=version_id,
        session_id=minted,
        clock=wall_clock,
        key=KEY,
        user_id=uuid.uuid4(),
        attachment=True,
    )
    token = url.removeprefix(CONTENT_PATH_PREFIX)

    before = parse_claim(token)
    after = parse_claim(_respell_session(token, other))

    assert before is not None
    assert before.session_id == minted
    assert before.attachment is True
    assert after == replace(before, session_id=other)


async def test_a_claim_that_names_another_session_is_refused(
    repo: FilesRepo, version_id: VersionId, wall_clock: FakeClock
) -> None:
    """Minted under session A, presented with a claim naming session B: refused.

    The content origin has no cookie, so the claim is the only thing on the
    request that names a session — which makes it a field the MAC has to cover.
    Leave it uncovered and the token answers the question about itself: whoever
    holds the URL rewrites the segment and the grant's own session is never
    consulted.
    """
    minted = SessionId(uuid.uuid4())
    other = SessionId(uuid.uuid4())
    url = await mint_content_url(
        repo, version_id=version_id, session_id=minted, clock=wall_clock, key=KEY
    )
    spliced = _respell_session(url.removeprefix(CONTENT_PATH_PREFIX), other)

    assert await redeem(repo, token=spliced, session_id=minted, clock=wall_clock, key=KEY) is None


async def test_a_cookieless_redeemer_redeems_the_session_the_grant_names(
    repo: FilesRepo, version_id: VersionId, wall_clock: FakeClock
) -> None:
    """A redeemer that presents no session of its own still gets the binding.

    On the content domain nobody presents a session, so the check is between
    two things the mint decided: the session recorded on the grant row and the
    MAC-covered session in the claim. An untouched token from session A
    redeems; the previous test shows a rewritten one does not.
    """
    minted = SessionId(uuid.uuid4())
    url = await mint_content_url(
        repo, version_id=version_id, session_id=minted, clock=wall_clock, key=KEY
    )

    grant = await redeem(
        repo,
        token=url.removeprefix(CONTENT_PATH_PREFIX),
        session_id=None,
        clock=wall_clock,
        key=KEY,
    )

    assert grant is not None
    assert grant.session_id == minted


def _claim_segment(token: str) -> list[bytes]:
    encoded = token.split(".")[1]
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    return raw.split(b"\x1f")


def _respell_claim(token: str, fields: list[bytes]) -> str:
    nonce, _, signature = token.split(".")
    edited = base64.urlsafe_b64encode(b"\x1f".join(fields)).decode("ascii").rstrip("=")
    return f"{nonce}.{edited}.{signature}"


async def test_a_minted_claim_spells_the_file_kind_as_its_fifth_field(
    repo: FilesRepo, version_id: VersionId, wall_clock: FakeClock
) -> None:
    """The kind is a field of the claim, not an assumption about the route.

    Two grant families now share this token shape, and a token that did not say
    which one it belonged to would have to be told by whoever presents it.
    """
    url = await mint_content_url(
        repo, version_id=version_id, session_id=None, clock=wall_clock, key=KEY
    )
    token = url.removeprefix(CONTENT_PATH_PREFIX)

    assert _claim_segment(token)[4] == b"file"
    claim = parse_claim(token)
    assert claim is not None
    assert claim.kind == "file"
    # The sixth field is the machine the mint proved, empty for a person.
    assert _claim_segment(token)[5] == b""
    assert claim.machine_id is None


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param(3, ContentClaim(org_id=ORG, user_id=USER, session_id=None), id="three-field"),
        pytest.param(
            4,
            ContentClaim(org_id=ORG, user_id=USER, session_id=None, attachment=True),
            id="four-field",
        ),
    ],
)
def test_a_claim_minted_before_a_field_existed_reads_as_that_fields_default(
    fields: int, expected: ContentClaim
) -> None:
    """Older tokens still parse, and each missing field reads as what it was
    signed as: no disposition is ``inline``, no kind is a file download."""
    spelled = [
        str(ORG).encode("ascii"),
        str(USER).encode("ascii"),
        b"",
        b"1",
        b"page",
    ][:fields]
    token = _respell_claim("nonce.placeholder.signature", spelled)

    claim = parse_claim(token)

    assert claim == expected
    assert claim is not None
    assert claim.kind == "file"


def test_a_claim_naming_an_unknown_kind_does_not_parse() -> None:
    """An unrecognised kind is damage, not a default: a token that reads as
    "file" because its kind was unreadable would be the wrong fail-open."""
    token = _respell_claim(
        "nonce.placeholder.signature",
        [str(ORG).encode("ascii"), str(USER).encode("ascii"), b"", b"", b"folder"],
    )

    assert parse_claim(token) is None


async def test_the_proven_machine_is_under_the_signature_and_cannot_be_added(
    repo: FilesRepo, version_id: VersionId, wall_clock: FakeClock
) -> None:
    """The one field the content origin trusts, and the only reason it may.

    The origin holds no credential, so it cannot check an agent's assertion
    against a registration — it reads the machine the MINT proved out of the
    claim instead. That is safe exactly as far as the MAC reaches: a holder of
    a person's URL who writes a machine id into the claim must be refused, or
    the seal on a chat's bytes would come off with a text editor.
    """
    url = await mint_content_url(
        repo, version_id=version_id, session_id=None, clock=wall_clock, key=KEY
    )
    token = url.removeprefix(CONTENT_PATH_PREFIX)
    fields = _claim_segment(token)
    forged = _respell_claim(token, [*fields[:5], b"9f1c2f7a-0000-4000-8000-000000000001"])

    # The edited claim parses — parsing is signature-free by design — and the
    # forged machine is right there in it, which is what makes the refusal the
    # only thing standing between it and a sealed chat's bytes.
    parsed = parse_claim(forged)
    assert parsed is not None and parsed.machine_id == "9f1c2f7a-0000-4000-8000-000000000001"
    assert await redeem(repo, token=forged, session_id=None, clock=wall_clock, key=KEY) is None

    # …and an untouched mint of the same shape redeems, so the refusal above is
    # the forged field rather than a splice that broke the claim. A second mint
    # rather than the same one: a redeem takes its grant before it checks the
    # MAC, so the forged attempt has already spent the first nonce.
    control = (
        await mint_content_url(
            repo, version_id=version_id, session_id=None, clock=wall_clock, key=KEY
        )
    ).removeprefix(CONTENT_PATH_PREFIX)
    assert await redeem(repo, token=control, session_id=None, clock=wall_clock, key=KEY) is not None


async def test_a_mint_that_proved_a_machine_carries_it_back_to_the_origin(
    repo: FilesRepo, version_id: VersionId, wall_clock: FakeClock
) -> None:
    """…and the honest case still round-trips: the verified machine survives
    the mint, the MAC and the redeem, because the box pulling its own chat's
    folder is the one reader a sealed chat has."""
    url = await mint_content_url(
        repo,
        version_id=version_id,
        session_id=None,
        clock=wall_clock,
        key=KEY,
        machine_id="9f1c2f7a-0000-4000-8000-000000000002",
    )
    token = url.removeprefix(CONTENT_PATH_PREFIX)

    claim = parse_claim(token)
    assert claim is not None
    assert claim.machine_id == "9f1c2f7a-0000-4000-8000-000000000002"
    assert await redeem(repo, token=token, session_id=None, clock=wall_clock, key=KEY) is not None


async def test_the_kind_is_under_the_signature(
    repo: FilesRepo, version_id: VersionId, wall_clock: FakeClock
) -> None:
    """Relabelling a download as a page is refused even though the nonce is real.

    The two families redeem from different tables, so this is belt and braces —
    but a claim field outside the MAC is a field its holder owns, and the kind
    decides which authority a token is presented with.
    """
    url = await mint_content_url(
        repo, version_id=version_id, session_id=None, clock=wall_clock, key=KEY
    )
    token = url.removeprefix(CONTENT_PATH_PREFIX)
    fields = _claim_segment(token)
    relabelled = _respell_claim(token, [*fields[:4], b"page", *fields[5:]])

    assert parse_claim(relabelled) is not None
    assert await redeem(repo, token=relabelled, session_id=None, clock=wall_clock, key=KEY) is None
    # …and the untouched token still redeems, so the refusal above is the
    # relabelling rather than a splice that broke the claim.
    assert await redeem(repo, token=token, session_id=None, clock=wall_clock, key=KEY) is not None


# --------------------------------------------------------------------------
# the credential a box minted on


async def test_a_box_minting_on_its_credential_carries_it_back_to_the_origin(
    repo: FilesRepo, version_id: VersionId, wall_clock: FakeClock
) -> None:
    """A box on its own machine credential has no user for the URL to act as,
    so the claim carries the credential and the machine and nothing else: the
    origin rebuilds the box from the two and re-asks the credential's
    standing. Both round-trip the mint, the MAC and the redeem."""
    credential = uuid.uuid4()
    url = await mint_content_url(
        repo,
        version_id=version_id,
        session_id=None,
        clock=wall_clock,
        key=KEY,
        machine_id="9f1c2f7a-0000-4000-8000-000000000003",
        credential_id=credential,
    )
    token = url.removeprefix(CONTENT_PATH_PREFIX)
    claim = parse_claim(token)
    assert claim is not None
    assert claim.user_id is None
    assert claim.machine_id == "9f1c2f7a-0000-4000-8000-000000000003"
    assert claim.credential_id == credential
    assert await redeem(repo, token=token, session_id=None, clock=wall_clock, key=KEY) is not None


async def test_the_credential_is_under_the_signature_and_cannot_be_added(
    repo: FilesRepo, version_id: VersionId, wall_clock: FakeClock
) -> None:
    """A person's URL with a credential written into the claim is refused: the
    origin would otherwise rebuild a box out of whatever a holder typed."""
    url = await mint_content_url(
        repo, version_id=version_id, session_id=None, clock=wall_clock, key=KEY
    )
    token = url.removeprefix(CONTENT_PATH_PREFIX)
    fields = _claim_segment(token)
    forged = _respell_claim(token, [*fields[:6], str(uuid.uuid4()).encode("ascii")])
    parsed = parse_claim(forged)
    assert parsed is not None and parsed.credential_id is not None
    assert await redeem(repo, token=forged, session_id=None, clock=wall_clock, key=KEY) is None


def test_a_claim_minted_before_a_box_could_mint_reads_no_credential() -> None:
    """The six-field claim every earlier mint wrote still parses, and names no
    credential — an older URL never becomes a box's."""
    fields = [
        str(ORG).encode("ascii"),
        str(USER).encode("ascii"),
        b"",
        b"",
        b"file",
        b"9f1c2f7a-0000-4000-8000-000000000004",
    ]
    segment = base64.urlsafe_b64encode(b"\x1f".join(fields)).decode("ascii").rstrip("=")
    claim = parse_claim(f"{'0' * 32}.{segment}.{'0' * 64}")
    assert claim is not None
    assert claim.machine_id == "9f1c2f7a-0000-4000-8000-000000000004"
    assert claim.credential_id is None


def test_a_claim_whose_credential_is_not_an_id_does_not_parse() -> None:
    fields = [str(ORG).encode("ascii"), b"", b"", b"", b"file", b"machine-1", b"not-a-uuid"]
    segment = base64.urlsafe_b64encode(b"\x1f".join(fields)).decode("ascii").rstrip("=")
    assert parse_claim(f"{'0' * 32}.{segment}.{'0' * 64}") is None
