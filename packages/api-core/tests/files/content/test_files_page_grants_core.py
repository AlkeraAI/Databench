"""Page grants: multi-use but bounded, rooted where the mint said, and closable.

A page grant is the one Files credential that answers more than one request, so
every bound on it is load-bearing and every one of them is pinned here: the
deadline, the served-request ceiling, the revoke, the org, and the fact that the
root and the entry are the mint's decision rather than the holder's.

The deadline is Postgres ``now()`` in the statement, never a Python clock, so
the expiry cases move the *minting* clock instead of pretending to move the
database's.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.config import settings
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import NodeId
from alkera_core.files.page_grants import (
    PAGE_PATH_PREFIX,
    mint_page_grant,
    page_grant_max_requests,
    page_grant_ttl,
    redeem_page_grant,
    revoke_page_grants,
)
from alkera_core.files.repo import FilesRepo
from alkera_core.files.signed_urls import mint_content_url, parse_claim, redeem
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

KEY = b"a-signing-key-for-the-content-domain"
OTHER_KEY = b"the-key-an-attacker-would-have-to-guess"

#: The name is a display tail on the URL, so a stored name with a slash, a
#: percent and a space is the interesting one: none of it may become structure.
ENTRY_NAME = b"q3 report/50%.html"


@pytest.fixture
def wall_clock() -> FakeClock:
    """Anchored to real time: the deadline is checked against Postgres ``now()``,
    so a clock parked in the past would mint grants that are already expired."""
    return FakeClock(now=datetime.now(UTC))


class Page:
    """A folder with a page inside it: the root a grant is minted over."""

    def __init__(self, root: NodeId, entry: NodeId, outside: NodeId, drive_root: NodeId) -> None:
        self.root = root
        self.entry = entry
        #: A node outside the grant's root, for the revoke-by-chain cases.
        self.outside = outside
        #: The drive's own root — the first link of every chain in this drive.
        self.drive_root = drive_root


@pytest.fixture
async def page(files_factory: FilesFactory) -> Page:
    drive = await files_factory.drive()
    tree = await files_factory.tree(
        "report/ report/index.html elsewhere/ elsewhere/other.html", drive=drive
    )
    return Page(
        root=NodeId(tree["report"].id),
        entry=NodeId(tree["report/index.html"].id),
        outside=NodeId(tree["elsewhere"].id),
        drive_root=NodeId(drive.root_node_id),
    )


async def _mint(
    repo: FilesRepo,
    page: Page,
    clock: FakeClock,
    *,
    user_id: uuid.UUID | None = None,
    credential_id: str | None = "the-session-jti",
    name: bytes = ENTRY_NAME,
) -> str:
    return await mint_page_grant(
        repo,
        root_node_id=page.root,
        entry_node_id=page.entry,
        entry_name=name,
        user_id=user_id or uuid.uuid4(),
        credential_id=credential_id,
        clock=clock,
        key=KEY,
    )


def _token(url: str) -> str:
    return url.removeprefix(PAGE_PATH_PREFIX).split("/")[0]


async def _row(session: AsyncSession, nonce: str) -> dict[str, object]:
    row = (
        await session.execute(
            text(
                "SELECT root_node_id, entry_node_id, minted_by_user_id, credential_id, "
                "requests_served, expires_at, revoked_at FROM file_page_grants "
                "WHERE nonce = :nonce"
            ),
            {"nonce": nonce},
        )
    ).mappings()
    return dict(row.one())


async def test_a_mint_writes_the_row_and_answers_the_page_path(
    repo: FilesRepo, page: Page, wall_clock: FakeClock, files_session: AsyncSession
) -> None:
    """The URL is the token plus the entry's name, and the row is what bounds it."""
    minted_by = uuid.uuid4()

    url = await _mint(repo, page, wall_clock, user_id=minted_by)

    token = _token(url)
    assert url == f"{PAGE_PATH_PREFIX}{token}/q3%20report%2F50%25.html"
    assert len(token.split(".")) == 3
    row = await _row(files_session, token.split(".")[0])
    assert row["root_node_id"] == uuid.UUID(str(page.root))
    assert row["entry_node_id"] == uuid.UUID(str(page.entry))
    assert row["minted_by_user_id"] == minted_by
    assert row["credential_id"] == "the-session-jti"
    assert row["requests_served"] == 0
    assert row["revoked_at"] is None
    assert row["expires_at"] == wall_clock.now() + page_grant_ttl()


async def test_the_claim_names_the_page_kind_and_the_minting_user(
    repo: FilesRepo, page: Page, wall_clock: FakeClock
) -> None:
    """The kind is in the claim, which is what lets a route refuse the other one."""
    minted_by = uuid.uuid4()

    claim = parse_claim(_token(await _mint(repo, page, wall_clock, user_id=minted_by)))

    assert claim is not None
    assert claim.kind == "page"
    assert claim.user_id == minted_by


async def test_one_grant_serves_many_requests_and_counts_each_one(
    repo: FilesRepo, page: Page, wall_clock: FakeClock, files_session: AsyncSession
) -> None:
    """A page is many requests — that is the whole reason this grant exists."""
    token = _token(await _mint(repo, page, wall_clock))

    first = await redeem_page_grant(repo, token=token, clock=wall_clock, key=KEY)
    second = await redeem_page_grant(repo, token=token, clock=wall_clock, key=KEY)

    assert first is not None
    assert second is not None
    assert first.root_node_id == page.root
    assert first.entry_node_id == page.entry
    assert (first.requests_served, second.requests_served) == (1, 2)
    assert (await _row(files_session, token.split(".")[0]))["requests_served"] == 2


@pytest.mark.parametrize(
    ("age", "redeems"),
    [
        pytest.param(page_grant_ttl() - timedelta(minutes=1), True, id="a-minute-inside-the-ttl"),
        pytest.param(page_grant_ttl() + timedelta(seconds=1), False, id="a-second-past-the-ttl"),
    ],
)
async def test_the_deadline_decides_and_it_is_the_database_clock(
    repo: FilesRepo, page: Page, age: timedelta, redeems: bool
) -> None:
    """Both sides of the boundary, with the mint moved rather than the database.

    A grant stamped ``age`` ago is exactly a grant whose ``expires_at`` is
    ``page_grant_ttl() - age`` from now; the statement compares it against
    Postgres ``now()``, so a skewed API instance cannot extend anyone's page.
    """
    stamped = FakeClock(now=datetime.now(UTC) - age)
    token = _token(await _mint(repo, page, stamped))

    grant = await redeem_page_grant(repo, token=token, clock=stamped, key=KEY)

    assert (grant is not None) is redeems


async def test_a_revoked_grant_stops_serving(
    repo: FilesRepo, page: Page, wall_clock: FakeClock
) -> None:
    """Revoking is what a logout and a permission change have to be able to do."""
    owner = uuid.uuid4()
    token = _token(await _mint(repo, page, wall_clock, user_id=owner))
    assert await redeem_page_grant(repo, token=token, clock=wall_clock, key=KEY) is not None

    revoked = await revoke_page_grants(repo, user_id=owner)

    assert revoked == 1
    assert await redeem_page_grant(repo, token=token, clock=wall_clock, key=KEY) is None


async def test_a_revoke_by_credential_leaves_the_users_other_sessions_open(
    repo: FilesRepo, page: Page, wall_clock: FakeClock
) -> None:
    """One logout closes one session's pages, not everything the person opened."""
    owner = uuid.uuid4()
    here = _token(await _mint(repo, page, wall_clock, user_id=owner, credential_id="this-session"))
    there = _token(
        await _mint(repo, page, wall_clock, user_id=owner, credential_id="another-session")
    )

    revoked = await revoke_page_grants(repo, credential_id="this-session")

    assert revoked == 1
    assert await redeem_page_grant(repo, token=here, clock=wall_clock, key=KEY) is None
    assert await redeem_page_grant(repo, token=there, clock=wall_clock, key=KEY) is not None


async def test_a_revoke_on_a_chain_closes_the_pages_rooted_on_it_and_no_others(
    repo: FilesRepo, page: Page, wall_clock: FakeClock
) -> None:
    """A share revoke reaches the grants rooted at or above the node it changed.

    The grant rooted on the chain dies; a grant rooted on a sibling folder, minted
    by the same person at the same moment, keeps serving — so the selector is the
    chain and not simply "everything".
    """
    chain_ids = [page.drive_root, page.root]
    on_chain = _token(await _mint(repo, page, wall_clock))
    sibling = Page(
        root=page.outside, entry=page.entry, outside=page.root, drive_root=page.drive_root
    )
    off_chain = _token(await _mint(repo, sibling, wall_clock))

    revoked = await revoke_page_grants(repo, root_chain=chain_ids)

    assert revoked == 1
    assert await redeem_page_grant(repo, token=on_chain, clock=wall_clock, key=KEY) is None
    assert await redeem_page_grant(repo, token=off_chain, clock=wall_clock, key=KEY) is not None


async def test_a_revoke_naming_nothing_is_refused_rather_than_revoking_everything(
    repo: FilesRepo, page: Page, wall_clock: FakeClock
) -> None:
    """The selector-less call is the one a typo makes; it must not be the org-wide one."""
    token = _token(await _mint(repo, page, wall_clock))

    with pytest.raises(ValueError, match="user, a credential or a root chain"):
        await revoke_page_grants(repo)

    assert await redeem_page_grant(repo, token=token, clock=wall_clock, key=KEY) is not None


async def test_the_ceiling_stops_the_grant_and_the_counter_stops_with_it(
    repo: FilesRepo, page: Page, wall_clock: FakeClock, files_session: AsyncSession
) -> None:
    """The last allowed request is served; the next is refused and costs nothing."""
    token = _token(await _mint(repo, page, wall_clock))
    nonce = token.split(".")[0]
    await files_session.execute(
        text("UPDATE file_page_grants SET requests_served = :served WHERE nonce = :nonce"),
        {"served": page_grant_max_requests() - 1, "nonce": nonce},
    )
    await files_session.commit()

    last = await redeem_page_grant(repo, token=token, clock=wall_clock, key=KEY)
    over = await redeem_page_grant(repo, token=token, clock=wall_clock, key=KEY)

    assert last is not None
    assert last.requests_served == page_grant_max_requests()
    assert over is None
    assert (await _row(files_session, nonce))["requests_served"] == page_grant_max_requests()


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param("flip-signature", id="a-flipped-signature-byte"),
        pytest.param("flip-nonce", id="a-flipped-nonce-byte"),
        pytest.param("other-key", id="a-signature-from-another-key"),
        pytest.param("no-signature", id="a-token-with-no-signature-at-all"),
    ],
)
async def test_a_tampered_token_is_refused_and_spends_nothing(
    repo: FilesRepo,
    page: Page,
    wall_clock: FakeClock,
    files_session: AsyncSession,
    damage: str,
) -> None:
    """A forged token must not be able to burn a real grant's budget either.

    That is the reason the signature is compared before the counter moves: a
    nonce is guessable in principle, and a refusal that still spent a request
    would let anyone exhaust a page that is being read.
    """
    token = _token(await _mint(repo, page, wall_clock))
    nonce, claim, signature = token.split(".")
    if damage == "flip-signature":
        forged = f"{nonce}.{claim}.{'0' if signature[0] != '0' else '1'}{signature[1:]}"
    elif damage == "flip-nonce":
        forged = f"{'0' if nonce[0] != '0' else '1'}{nonce[1:]}.{claim}.{signature}"
    elif damage == "no-signature":
        forged = f"{nonce}.{claim}"
    else:
        other = await _mint(repo, page, FakeClock(now=wall_clock.now()))
        forged = f"{nonce}.{claim}.{_token(other).split('.')[2]}"

    assert await redeem_page_grant(repo, token=forged, clock=wall_clock, key=KEY) is None
    assert (await _row(files_session, nonce))["requests_served"] == 0


async def test_a_signature_from_another_key_is_refused(
    repo: FilesRepo, page: Page, wall_clock: FakeClock
) -> None:
    """The key is what makes the token unforgeable; a URL signed with any other
    one reads as damage."""
    url = await mint_page_grant(
        repo,
        root_node_id=page.root,
        entry_node_id=page.entry,
        entry_name=ENTRY_NAME,
        user_id=uuid.uuid4(),
        clock=wall_clock,
        key=OTHER_KEY,
    )

    assert await redeem_page_grant(repo, token=_token(url), clock=wall_clock, key=KEY) is None


@pytest.mark.parametrize(
    "column",
    [
        pytest.param("root_node_id", id="the-root"),
        pytest.param("entry_node_id", id="the-entry"),
    ],
)
async def test_the_signature_covers_the_root_and_the_entry(
    repo: FilesRepo,
    page: Page,
    wall_clock: FakeClock,
    files_session: AsyncSession,
    column: str,
) -> None:
    """Moving the grant's root after the mint invalidates the token.

    The root is the whole authority a page grant carries, so it must be under
    the MAC and not merely beside it: repoint the row and the signature the
    holder has no longer matches the grant it names.
    """
    token = _token(await _mint(repo, page, wall_clock))
    assert await redeem_page_grant(repo, token=token, clock=wall_clock, key=KEY) is not None
    await files_session.execute(
        # The column name comes from this test's own parametrize, never a caller.
        text(f"UPDATE file_page_grants SET {column} = :moved WHERE nonce = :nonce"),
        {"moved": uuid.uuid4(), "nonce": token.split(".")[0]},
    )
    await files_session.commit()

    assert await redeem_page_grant(repo, token=token, clock=wall_clock, key=KEY) is None


async def test_the_signature_covers_the_deadline(
    repo: FilesRepo, page: Page, wall_clock: FakeClock, files_session: AsyncSession
) -> None:
    """A row edited to expire later is a grant whose token no longer verifies —
    the deadline cannot be extended behind the MAC's back."""
    token = _token(await _mint(repo, page, wall_clock))
    await files_session.execute(
        text("UPDATE file_page_grants SET expires_at = now() + interval '1 day' WHERE nonce = :n"),
        {"n": token.split(".")[0]},
    )
    await files_session.commit()

    assert await redeem_page_grant(repo, token=token, clock=wall_clock, key=KEY) is None


async def test_a_page_token_is_refused_by_the_single_use_route(
    repo: FilesRepo, page: Page, wall_clock: FakeClock
) -> None:
    """The kinds do not cross: a page token presented as a download is nothing."""
    token = _token(await _mint(repo, page, wall_clock))

    assert await redeem(repo, token=token, session_id=None, clock=wall_clock, key=KEY) is None


async def test_a_file_token_is_refused_by_the_page_route(
    repo: FilesRepo,
    page: Page,
    wall_clock: FakeClock,
    files_org: FilesOrg,
    files_session: AsyncSession,
) -> None:
    """And the reverse, which is the direction that would otherwise leak a folder."""
    version = uuid.uuid4()
    await files_session.execute(
        text(
            "INSERT INTO file_versions (id, org_team_id, node_id, seq, size_bytes, "
            "content_hash, source, keep_forever, held, lease_epoch, metadata) "
            "VALUES (:id, :org, :node, 1, 11, 'b3-deadbeef', 'upload', false, false, 0, "
            "'{}'::jsonb)"
        ),
        {"id": version, "org": files_org.org_team_id, "node": uuid.UUID(str(page.entry))},
    )
    await files_session.commit()
    url = await mint_content_url(
        repo,
        version_id=version,  # type: ignore[arg-type]
        session_id=None,
        clock=wall_clock,
        key=KEY,
        user_id=uuid.uuid4(),
    )

    file_token = url.removeprefix("/c/")

    assert await redeem_page_grant(repo, token=file_token, clock=wall_clock, key=KEY) is None


async def test_rewriting_the_kind_in_the_claim_does_not_make_a_page_token(
    repo: FilesRepo, page: Page, wall_clock: FakeClock
) -> None:
    """The kind is under the MAC, so relabelling it is just damage.

    Without this the refusal above would only prove that the two routes read
    different tables — the claim itself has to be unforgeable.
    """
    token = _token(await _mint(repo, page, wall_clock))
    nonce, claim, signature = token.split(".")
    raw = base64.urlsafe_b64decode(claim + "=" * (-len(claim) % 4))
    fields = raw.split(b"\x1f")
    # The kind is the claim's fifth field (after org, user, session and the
    # disposition flag); the fields after it are the machine and credential a
    # box's mint signs, empty on a person's.
    relabelled = base64.urlsafe_b64encode(b"\x1f".join([*fields[:4], b"file", *fields[5:]]))
    spliced = f"{nonce}.{relabelled.decode('ascii').rstrip('=')}.{signature}"

    assert parse_claim(spliced) is not None
    assert await redeem(repo, token=spliced, session_id=None, clock=wall_clock, key=KEY) is None
    assert await redeem_page_grant(repo, token=spliced, clock=wall_clock, key=KEY) is None


async def test_another_orgs_repo_cannot_redeem_or_revoke_this_grant(
    repo: FilesRepo,
    page: Page,
    wall_clock: FakeClock,
    files_session: AsyncSession,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
) -> None:
    """The org is in the claim, in the predicate and in the policy.

    A grant minted in org A is not redeemable by a repo scoped to org B, and B's
    revoke cannot reach into A either — so a page cannot be closed or opened
    across a tenant boundary.
    """
    other = await files_org_factory()
    token = _token(await _mint(repo, page, wall_clock))
    stranger = FilesRepo(files_session, other.scope)

    assert await redeem_page_grant(stranger, token=token, clock=wall_clock, key=KEY) is None
    assert await revoke_page_grants(stranger, root_chain=[page.root]) == 0
    assert await redeem_page_grant(repo, token=token, clock=wall_clock, key=KEY) is not None


@pytest.mark.parametrize(
    "seconds",
    [
        pytest.param(60, id="a-minute-for-a-strict-deployment"),
        pytest.param(7200, id="two-hours-for-a-long-report"),
    ],
)
async def test_the_page_deadline_is_the_deployment_setting_not_a_fixed_quarter_hour(
    repo: FilesRepo,
    page: Page,
    wall_clock: FakeClock,
    files_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    seconds: int,
) -> None:
    """A deployment that shortens or lengthens the page window gets that window.

    The deadline is read when the grant is minted, so the very next mint after
    the change carries it — there is no restart between the two here.
    """
    monkeypatch.setattr(settings, "files_page_grant_ttl_seconds", seconds)

    url = await _mint(repo, page, wall_clock)

    row = await _row(files_session, _token(url).split(".")[0])
    assert row["expires_at"] == wall_clock.now() + timedelta(seconds=seconds)


@pytest.mark.parametrize(
    "seconds",
    [
        pytest.param(30, id="half-a-minute"),
        pytest.param(3600, id="an-hour-for-a-slow-link"),
    ],
)
async def test_the_download_deadline_is_the_deployment_setting_not_a_fixed_five_minutes(
    repo: FilesRepo,
    page: Page,
    files_org: FilesOrg,
    wall_clock: FakeClock,
    files_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    seconds: int,
) -> None:
    """A single-file download URL lives as long as the deployment says.

    A slow link needs longer than five minutes just to start the transfer, and
    a deployment that wants a tighter window for a leaked URL needs shorter;
    both are the same setting, read when the URL is minted.
    """
    monkeypatch.setattr(settings, "files_content_url_ttl_seconds", seconds)
    version = uuid.uuid4()
    await files_session.execute(
        text(
            "INSERT INTO file_versions (id, org_team_id, node_id, seq, size_bytes, "
            "content_hash, source, keep_forever, held, lease_epoch, metadata) "
            "VALUES (:id, :org, :node, 1, 11, 'b3-deadbeef', 'upload', false, false, 0, "
            "'{}'::jsonb)"
        ),
        {"id": version, "org": files_org.org_team_id, "node": uuid.UUID(str(page.entry))},
    )
    await files_session.commit()

    url = await mint_content_url(
        repo,
        version_id=version,  # type: ignore[arg-type]
        session_id=None,
        clock=wall_clock,
        key=KEY,
        user_id=uuid.uuid4(),
    )

    nonce = url.removeprefix("/c/").split(".")[0]
    grant = (
        (
            await files_session.execute(
                text("SELECT expires_at FROM file_content_grants WHERE nonce = :nonce"),
                {"nonce": nonce},
            )
        )
        .mappings()
        .one()
    )
    assert grant["expires_at"] == wall_clock.now() + timedelta(seconds=seconds)


@pytest.mark.parametrize(
    "ceiling",
    [
        pytest.param(1, id="a-single-request-grant"),
        pytest.param(3, id="a-three-request-grant"),
    ],
)
async def test_the_request_ceiling_is_the_deployment_setting_not_a_fixed_five_thousand(
    repo: FilesRepo,
    page: Page,
    wall_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    ceiling: int,
) -> None:
    """A lowered ceiling refuses at the lowered number, with nothing seeded.

    The ceiling is read on the redemption that checks it, so a grant minted
    before the change is bounded by the new figure too — which is what makes
    the setting usable to close down a deployment that is already serving.
    """
    monkeypatch.setattr(settings, "files_page_grant_max_requests", ceiling)
    token = _token(await _mint(repo, page, wall_clock))

    served = [
        await redeem_page_grant(repo, token=token, clock=wall_clock, key=KEY)
        for _ in range(ceiling + 1)
    ]

    assert [grant is not None for grant in served] == [True] * ceiling + [False]
    assert [grant.requests_served for grant in served if grant is not None] == list(
        range(1, ceiling + 1)
    )
