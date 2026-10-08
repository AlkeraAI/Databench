"""One person, two orgs: the preferences that name a model are kept per org.

``model_efforts``, ``default_chat_model`` and ``default_chat_effort`` name
something in one org's model catalog, so each org the person works in holds
its own copy (``user_org_preferences``); every other preference is the
identity's and is shared. Driven through the real routes with each org's
credential, and through the service for the rows they leave, against real
Postgres; only the model gateway is substituted.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import User, UserOrgPreference, UserPreference
from alkera_core.schemas.preferences import ORG_SCOPED_KEYS
from backend.services.chats import catalog as chat_catalog
from backend.services.org import preferences as preferences_service
from httpx import Response
from sqlalchemy import select
from tests.conftest import TwoOrg, app_client
from tests.test_me_preferences import CATALOG, _gateway

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("multi_org")]


def _token(t: TwoOrg, which: str) -> str:
    return t.token_a if which == "a" else t.token_b


def _org(t: TwoOrg, which: str) -> UUID:
    return t.org_a if which == "a" else t.org_b


async def _call(method: str, path: str, token: str, **kwargs: Any) -> Response:
    async with app_client() as client:
        return await client.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )


async def _prefs(t: TwoOrg, which: str) -> dict[str, Any]:
    resp = await _call("GET", "/api/v1/me/preferences", _token(t, which))
    assert resp.status_code == 200, resp.text
    document: dict[str, Any] = resp.json()["preferences"]
    return document


async def _patch(t: TwoOrg, which: str, fields: dict[str, Any]) -> Response:
    return await _call(
        "PATCH", "/api/v1/me/preferences", _token(t, which), json={"preferences": fields}
    )


async def _rows(user_id: UUID) -> tuple[dict[str, Any] | None, dict[UUID, dict[str, Any]]]:
    async with AsyncSessionLocal() as db:
        identity = await db.get(UserPreference, user_id)
        scoped = (
            await db.execute(select(UserOrgPreference).where(UserOrgPreference.user_id == user_id))
        ).scalars()
        return (
            None if identity is None else dict(identity.preferences),
            {row.org_team_id: dict(row.preferences) for row in scoped},
        )


@pytest.mark.parametrize(("writer", "reader"), [("a", "b"), ("b", "a")])
async def test_a_model_effort_saved_in_one_org_is_not_read_in_the_other(
    two_org_identity: TwoOrg, monkeypatch: pytest.MonkeyPatch, writer: str, reader: str
) -> None:
    """Both directions: the effort memory is keyed by model id, and the other
    org's catalog may not hold that model at all. The org that saved it still
    reads it back."""
    _gateway(monkeypatch, body=CATALOG)
    t = two_org_identity
    saved = await _patch(t, writer, {"model_efforts": {"gpt-5.2": "high"}})
    assert saved.status_code == 200, saved.text
    assert (await _prefs(t, writer))["model_efforts"] == {"gpt-5.2": "high"}
    assert (await _prefs(t, reader))["model_efforts"] == {}


async def test_an_identity_preference_saved_in_one_org_is_read_in_every_org(
    two_org_identity: TwoOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway(monkeypatch, body=CATALOG)
    t = two_org_identity
    saved = await _patch(t, "a", {"theme": "alkera-slate", "default_permission_mode": "plan"})
    assert saved.status_code == 200, saved.text
    read_in_b = await _prefs(t, "b")
    assert (read_in_b["theme"], read_in_b["default_permission_mode"]) == ("alkera-slate", "plan")
    # The stance a new chat opens in is the identity's: B's composer seed says so.
    seed = await _call("GET", "/api/v1/me/chat-defaults", t.token_b)
    assert seed.status_code == 200, seed.text
    assert seed.json()["permission_mode"] == "plan"


async def test_a_default_chat_model_picked_in_a_never_seeds_a_chat_in_b(
    two_org_identity: TwoOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pick reads as A's in A, and B (which never picked) reads the
    platform default, flagged as no pick, in its preferences, in its composer
    seed and in the model a new chat on B is created with."""
    _gateway(monkeypatch, body=CATALOG)
    t = two_org_identity
    unpicked = (await _call("GET", "/api/v1/me/chat-defaults", t.token_b)).json()["model"]
    assert unpicked != "gpt-5.2", "the pick must differ from the default to prove anything"
    saved = await _patch(t, "a", {"default_chat_model": "gpt-5.2"})
    assert saved.status_code == 200, saved.text

    assert (await _prefs(t, "a"))["default_chat_model"] == "gpt-5.2"
    in_b = await _call("GET", "/api/v1/me/preferences", t.token_b)
    assert in_b.json()["chat_model_defaulted"] is True
    assert in_b.json()["preferences"]["default_chat_model"] == unpicked
    assert (await _call("GET", "/api/v1/me/chat-defaults", t.token_b)).json()["model"] == unpicked
    async with AsyncSessionLocal() as db:
        user = await db.get(User, t.user.id)
        assert user is not None
        assert (await chat_catalog.default_pin_for(db, user, t.org_b)).id == unpicked
        assert (await chat_catalog.default_pin_for(db, user, t.org_a)).id == "gpt-5.2"


async def test_a_merge_splits_the_document_between_the_identity_and_the_org(
    two_org_identity: TwoOrg,
) -> None:
    """The org-scoped keys land in the org's own row and nowhere else; the rest
    lands in the identity row, whose old copy of the org-scoped keys (still read
    by an older task of a rolling deploy) is left exactly as it was."""
    t = two_org_identity
    async with AsyncSessionLocal() as db:
        db.add(
            UserPreference(
                user_id=t.user.id,
                preferences={"schema_version": "2.0.0", "default_chat_model": "legacy-model"},
            )
        )
        await db.commit()
    async with AsyncSessionLocal() as db:
        await preferences_service.merge(
            db,
            user_id=t.user.id,
            org_id=t.org_b,
            incoming={"default_chat_model": "gpt-5.2", "show_banner": False},
        )
        await db.commit()

    identity, scoped = await _rows(t.user.id)
    assert identity is not None
    assert identity["show_banner"] is False
    assert identity["default_chat_model"] == "legacy-model"
    assert set(scoped) == {t.org_b}, "no row for an org that wrote nothing"
    assert set(scoped[t.org_b]) == ORG_SCOPED_KEYS
    assert scoped[t.org_b]["default_chat_model"] == "gpt-5.2"


async def test_an_identity_only_save_writes_no_org_row(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    async with AsyncSessionLocal() as db:
        await preferences_service.merge(
            db, user_id=t.user.id, org_id=t.org_a, incoming={"reduce_motion": True}
        )
        await db.commit()
    identity, scoped = await _rows(t.user.id)
    assert identity is not None and identity["reduce_motion"] is True
    assert scoped == {}


@pytest.mark.parametrize("which", ["a", "b"])
async def test_an_org_with_no_row_never_reads_the_identity_rows_copy(
    two_org_identity: TwoOrg, which: str
) -> None:
    """The identity row still holds the org-scoped keys from before they were
    kept per org. An org with no row of its own reads them at their defaults,
    never that copy, whichever org it is."""
    t = two_org_identity
    async with AsyncSessionLocal() as db:
        db.add(
            UserPreference(
                user_id=t.user.id,
                preferences={
                    "schema_version": "2.0.0",
                    "default_chat_model": "legacy-model",
                    "default_chat_effort": "high",
                    "model_efforts": {"legacy-model": "high"},
                    "theme": "alkera-light",
                },
            )
        )
        await db.commit()
    async with AsyncSessionLocal() as db:
        read = await preferences_service.load(db, user_id=t.user.id, org_id=_org(t, which))
        saved = await preferences_service.stored(db, user_id=t.user.id, org_id=_org(t, which))
    for document in (read, saved):
        assert document is not None
        assert document.theme == "alkera-light"
        assert document.default_chat_model is None
        assert document.default_chat_effort is None
        assert document.model_efforts == {}


async def test_stored_is_none_until_the_identity_has_saved_anything(
    two_org_identity: TwoOrg,
) -> None:
    """``stored`` answers "has this person ever saved", which the stance floor
    depends on: an identity with no row is ``None`` in every org, and a save in
    one org makes it a saved document in both."""
    t = two_org_identity
    async with AsyncSessionLocal() as db:
        assert await preferences_service.stored(db, user_id=t.user.id, org_id=t.org_a) is None
        assert await preferences_service.stored(db, user_id=t.user.id, org_id=t.org_b) is None
        await preferences_service.merge(
            db, user_id=t.user.id, org_id=t.org_a, incoming={"model_efforts": {"m": "low"}}
        )
        await db.commit()
    async with AsyncSessionLocal() as db:
        in_b = await preferences_service.stored(db, user_id=t.user.id, org_id=t.org_b)
    assert in_b is not None
    assert in_b.model_efforts == {}
