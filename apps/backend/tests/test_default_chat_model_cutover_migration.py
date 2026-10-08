"""Migration ``0127_clear_saved_default_chat_model`` against the real schema.

The new-chat default moves to Claude Opus 5. A reader who never chose lands
there on their own — the resolver falls through to the deployment's default —
but an explicit ``default_chat_model`` beats any default forever, so every
account that ever opened the Settings pane would stay on the model that was
current the day they looked. The migration clears that saved pick once.

Four properties, one test each, and each fails if its mechanism is taken out:

* a saved pick (and the effort chosen with it) is GONE, while every other key
  in the document — including one this server does not know — is untouched;
* a document that never named a model comes back byte-identical;
* a second run rewrites nothing at all (the row's version does not move), which
  is what makes a re-deploy or a re-run safe;
* and the point of the whole exercise: a formerly-pinned hosted reader's next
  new chat opens on the hosted default, through the real route.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from alkera_core.config import settings
from alkera_core.db.schema_head import EXPECTED_SCHEMA_HEAD
from alkera_core.gateway import HOSTED_DEFAULT_MODEL_SLUG
from alkera_core.models import UserPreference
from backend.services.chats import catalog as chat_catalog
from httpx import AsyncClient
from sqlalchemy import text
from tests.conftest import login
from tests.migration_harness import ScratchDatabase, migration_scratch, seed_org_admin

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "alembic" / "versions" / "0127_clear_saved_default_chat_model.py"
_REVISION = _MIGRATION.name.split("_", 1)[0]
_PARENT = "0126"

#: A document with a pick in it, plus the neighbours that must survive: a key
#: this server does not know, the per-model effort memory (keyed by model id, so
#: it steers no choice), and two ordinary settings.
_PINNED: dict[str, Any] = {
    "schema_version": "2.0.0",
    "default_chat_model": "claude-opus-4.5",
    "default_chat_effort": "high",
    "default_permission_mode": "plan",
    "theme": "alkera-slate",
    "model_efforts": {"claude-opus-4.5": "high"},
    "a_field_from_2027": {"nested": 1},
}

#: The same shape from somebody who never opened the model picker.
_UNPINNED: dict[str, Any] = {
    "schema_version": "2.0.0",
    "default_permission_mode": "auto",
    "telemetry_enabled": False,
    "model_efforts": {"claude-opus-4.5": "low"},
}

#: The hosted pin sits SECOND, so "the pin was honoured" cannot be confused with
#: "whatever the gateway listed first".
_CATALOG: dict[str, Any] = {
    "object": "list",
    "data": [
        {
            "id": "claude-opus-4.5",
            "object": "model",
            "display_name": "Claude Opus 4.5",
            "family": "claude",
            "wire": "anthropic",
            "efforts": ["low", "medium", "high"],
            "default_effort": "medium",
            "tier": "frontier",
            "context_window": 200000,
            "max_output_tokens": 64000,
        },
        {
            "id": HOSTED_DEFAULT_MODEL_SLUG,
            "object": "model",
            "display_name": "Claude Sonnet 5.5",
            "family": "claude",
            "wire": "anthropic",
            "efforts": ["low", "medium", "high", "xhigh", "max"],
            "default_effort": "medium",
            "tier": "frontier",
            "context_window": 1000000,
            "max_output_tokens": 64000,
        },
    ],
    "org_flags": {"web_search_enabled": True},
}


async def _write(db: ScratchDatabase, user_id: UUID, document: dict[str, Any]) -> None:
    async with db.session() as session:
        await session.merge(UserPreference(user_id=user_id, preferences=dict(document)))
        await session.commit()


async def _read(db: ScratchDatabase, user_id: UUID) -> dict[str, Any]:
    async with db.session() as session:
        row = await session.get(UserPreference, user_id)
        assert row is not None, "the preferences row vanished"
        return dict(row.preferences)


async def _row_version(db: ScratchDatabase, user_id: UUID) -> str:
    """The row's ``xmin`` — Postgres's inserting/updating transaction id. Any
    UPDATE that touches the row moves it, even one that writes the same bytes,
    so it is how "the statement skipped this row" is told from "it rewrote it
    to the same value"."""
    async with db.session() as session:
        return str(
            (
                await session.execute(
                    text("SELECT xmin::text FROM user_preferences WHERE user_id = :uid"),
                    {"uid": user_id},
                )
            ).scalar_one()
        )


def _serve_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_CATALOG)

    monkeypatch.setattr(
        chat_catalog,
        "async_client",
        lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_the_cutover_is_part_of_the_schema_the_code_expects() -> None:
    assert _REVISION == "0127"
    assert int(EXPECTED_SCHEMA_HEAD) >= int(_REVISION)


@pytest.mark.asyncio
async def test_a_saved_pick_is_cleared_and_the_rest_of_the_document_survives() -> None:
    """The cutover, stated as what it must and must not do: the two keys that
    pin a model go, and nothing else moves — a document-wide rewrite here would
    quietly drop the newer client's field it cannot even name."""
    async with migration_scratch() as db:
        org_admin = await seed_org_admin(db)
        await _write(db, org_admin.admin_id, _PINNED)
        await db.downgrade(_PARENT)
        assert await _read(db, org_admin.admin_id) == _PINNED

        await db.upgrade()
        after = await _read(db, org_admin.admin_id)
    assert "default_chat_model" not in after
    assert "default_chat_effort" not in after
    assert after == {
        key: value
        for key, value in _PINNED.items()
        if key not in {"default_chat_model", "default_chat_effort"}
    }


@pytest.mark.asyncio
async def test_a_document_that_never_named_a_model_is_left_identical() -> None:
    """Most accounts never opened the picker. Their rows are in the table too,
    and the statement has no business touching them."""
    async with migration_scratch() as db:
        org_admin = await seed_org_admin(db)
        await _write(db, org_admin.admin_id, _UNPINNED)
        await db.downgrade(_PARENT)
        before_version = await _row_version(db, org_admin.admin_id)

        await db.upgrade()
        assert await _read(db, org_admin.admin_id) == _UNPINNED
        assert await _row_version(db, org_admin.admin_id) == before_version


@pytest.mark.asyncio
async def test_a_second_run_rewrites_nothing() -> None:
    """Idempotence is not "the result looks the same" — a statement with no
    guard would rewrite every row on every run. After the pick is gone there is
    nothing left to change, so the row must not be written again at all."""
    async with migration_scratch() as db:
        org_admin = await seed_org_admin(db)
        await _write(db, org_admin.admin_id, _PINNED)
        await db.downgrade(_PARENT)
        await db.upgrade()
        cleared = await _read(db, org_admin.admin_id)
        settled_version = await _row_version(db, org_admin.admin_id)

        # The downgrade is a no-op by design, so this upgrade is the SECOND run
        # of the same statement over an already-cleared document.
        await db.downgrade(_PARENT)
        await db.upgrade()

        assert await _read(db, org_admin.admin_id) == cleared
        assert await _row_version(db, org_admin.admin_id) == settled_version


@pytest.mark.asyncio
async def test_a_formerly_pinned_hosted_reader_opens_on_the_hosted_default(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why any of this exists. Before the cutover this reader's new chats seed
    on the model they pinned once; after it, the deployment's default applies
    and they start on the hosted default like a fresh account — without anyone
    clearing a row by hand."""
    monkeypatch.setattr(settings, "self_hosted", False)
    _serve_catalog(monkeypatch)
    # The app serves from the copy for the whole story, so the route that saves
    # the pin and the route that reads it back both see what the revision did.
    async with migration_scratch() as db:
        org_admin = await seed_org_admin(db)
        with db.serving():
            await login(client, org_admin.admin_email, org_admin.admin_password)
            # The real writer of the key: the Settings pane's save.
            await client.patch(
                "/api/v1/me/preferences",
                json={
                    "preferences": {
                        "default_chat_model": "claude-opus-4.5",
                        "default_chat_effort": "high",
                    }
                },
            )
            pinned = (await client.get("/api/v1/me/chat-defaults")).json()
            assert pinned["model"] == "claude-opus-4.5", "the pin has to hold before it is cleared"

            await db.downgrade(_PARENT)
            await db.upgrade()
            # The round trip re-creates every org membership, so a session
            # bound to the one it dropped is refused; sign in again.
            await login(client, org_admin.admin_email, org_admin.admin_password)

            seed = (await client.get("/api/v1/me/chat-defaults")).json()
    assert seed["model"] == HOSTED_DEFAULT_MODEL_SLUG
    assert seed["effort"] == "medium"
