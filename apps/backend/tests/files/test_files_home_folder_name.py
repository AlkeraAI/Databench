"""A member's home folder is named by the same grammar as every other folder.

An address is not a name: ``EmailStr`` admits ``a/b@example.com``, and a home
named after it verbatim is a node whose name contains the separator every path
walker splits on -- ``root:`` cannot reach it, a zip nests it, a pull writes it
as two directories. These cases sign up through the real routes and read the
home back through the real routes: its name passes ``names.validate`` and the
``root:`` form resolves it to the same node the drive read answered.
"""

from __future__ import annotations

import secrets
from urllib.parse import quote

import pytest
from alkera_core.files import names
from alkera_core.files.drives import HOME_NAME, TEAMS_NAME
from httpx import AsyncClient
from tests.conftest import app_client

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"
PASSWORD = "home-name-pass-12345"


def _domain() -> str:
    return f"{secrets.token_hex(4)}.example.com"


async def _signup(
    client: AsyncClient, email: str, verifications: list[dict[str, str]], **extra: str
) -> None:
    """Sign up through the route, then follow the verification link it sent."""
    resp = await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": PASSWORD, **extra}
    )
    assert resp.status_code == 201, resp.text
    tokens = [sent["token"] for sent in verifications if sent["email"] == email]
    if tokens:
        verified = await client.post(f"/api/v1/auth/verify-email/{tokens[-1]}")
        assert verified.status_code == 200, verified.text


async def _home(client: AsyncClient) -> tuple[str, str, str]:
    """``(drive_id, home_id, home name)`` for whoever holds ``client``."""
    drive = await client.get(f"{BASE}/drives")
    assert drive.status_code == 200, drive.text
    drive_id, home_id = str(drive.json()["id"]), str(drive.json()["homeId"])
    item = await client.get(f"{BASE}/drives/{drive_id}/items/{home_id}")
    assert item.status_code == 200, item.text
    return drive_id, home_id, str(item.json()["name"])


async def _by_path(client: AsyncClient, drive_id: str, name: str) -> str | None:
    """The id ``root:/home/<name>`` resolves to, or ``None``."""
    home = HOME_NAME.decode()
    resp = await client.get(f"{BASE}/drives/{drive_id}/root:/{home}/{quote(name, safe='@')}")
    return str(resp.json()["id"]) if resp.status_code == 200 else None


def _assert_valid(name: str) -> None:
    names.validate(name.encode("utf-8"))
    assert "/" not in name


@pytest.mark.parametrize(
    "local",
    [
        pytest.param("a/b", id="one-slash"),
        pytest.param("/", id="only-a-slash"),
        pytest.param("a/b/c/d", id="several-slashes"),
    ],
)
async def test_a_signup_whose_address_holds_a_slash_gets_a_home_root_can_reach(
    client: AsyncClient,
    files_on: None,
    monkeypatch_verification_send: list[dict[str, str]],
    local: str,
) -> None:
    email = f"{local}@{_domain()}"
    await _signup(client, email, monkeypatch_verification_send)
    drive_id, home_id, name = await _home(client)
    _assert_valid(name)
    assert await _by_path(client, drive_id, name) == home_id
    # Completing the profile names the person; the home keeps its name and
    # stays reachable.
    done = await client.post(
        "/api/v1/auth/complete-profile", json={"first_name": "Slash", "last_name": "Mark"}
    )
    assert done.status_code == 200, done.text
    assert await _home(client) == (drive_id, home_id, name)


@pytest.mark.parametrize(
    "twin_local",
    [
        pytest.param("a%2Fb", id="percent-escaped-twin"),
        pytest.param("a_b", id="underscore-twin"),
        pytest.param("a//b", id="doubled-slash-twin"),
        pytest.param("a\u2215b", id="division-slash-twin"),
    ],
)
async def test_two_members_whose_addresses_repair_alike_get_two_homes(
    client: AsyncClient,
    files_on: None,
    monkeypatch_email_send: list[dict[str, str]],
    monkeypatch_verification_send: list[dict[str, str]],
    twin_local: str,
) -> None:
    """The fallback is an injective escape of the whole address, so no second
    address -- whatever a lossy repair would have folded it onto -- is handed
    the first member's folder."""
    domain = _domain()
    first = f"a/b@{domain}"
    twin = f"{twin_local}@{domain}"
    await _signup(client, first, monkeypatch_verification_send)
    drive_id, first_home, first_name = await _home(client)

    team = await client.post("/api/v1/teams", json={"name": "Twins"})
    assert team.status_code in (200, 201), team.text
    invited = await client.post(
        f"/api/v1/teams/{team.json()['id']}/invitations", json={"email": twin, "role": "member"}
    )
    assert invited.status_code in (200, 201), invited.text
    token = monkeypatch_email_send[-1]["invitation_token"]

    async with app_client() as other:
        await _signup(other, twin, monkeypatch_verification_send, invite_token=token)
        twin_drive, twin_home, twin_name = await _home(other)
        assert twin_drive == drive_id
        _assert_valid(twin_name)
        assert twin_home != first_home
        assert twin_name != first_name
        # Each resolves its own home by path, and not the other's.
        assert await _by_path(other, drive_id, twin_name) == twin_home
        assert await _by_path(other, drive_id, first_name) is None

    _assert_valid(first_name)
    assert await _by_path(client, drive_id, first_name) == first_home


async def test_a_team_whose_name_holds_a_slash_gets_a_folder_root_can_reach(
    client: AsyncClient, files_on: None, monkeypatch_verification_send: list[dict[str, str]]
) -> None:
    """A team name is display text (``R/D`` is a fine name for a team), and its
    folder under ``Teams/`` is derived by the same escape as a home."""
    await _signup(client, f"lead@{_domain()}", monkeypatch_verification_send)
    drive_id, _, _ = await _home(client)
    made = await client.post("/api/v1/teams", json={"name": "R/D"})
    assert made.status_code in (200, 201), made.text
    teams = TEAMS_NAME.decode()
    found = await client.get(f"{BASE}/drives/{drive_id}/root:/{teams}/{quote('R%2FD')}")
    assert found.status_code == 200, found.text
    _assert_valid(str(found.json()["name"]))
