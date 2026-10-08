"""A person's preferences, stored on the server.

The desktop has kept these in ``~/.alkera/preferences.yml`` since the CLI
existed; a browser reader has no such file, so every chat they started ran the
box's own defaults with nothing they could change. This is the server's copy.

Three properties carry the whole design, and each has its own test below:

* a PATCH MERGES. Two clients of different ages write this one document, and a
  replace would let the older one — which cannot even name the newer one's
  field — delete a setting the person chose in a client it has never seen;
* an unknown key SURVIVES. ``Preferences`` allows extras on purpose, and this
  store must not be the place they are quietly dropped;
* a transient gateway outage NEVER rewrites the saved default. The resolver has
  an outage branch precisely so a hiccup nobody saw does not wipe a choice.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.gateway import HOSTED_DEFAULT_MODEL_SLUG
from alkera_core.models import UserOrgPreference, UserPreference
from alkera_core.schemas.preferences import ORG_SCOPED_KEYS, Preferences
from backend.services.chats import catalog as chat_catalog
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login

pytestmark = pytest.mark.asyncio


CATALOG: dict[str, Any] = {
    "object": "list",
    "data": [
        {
            "id": "claude-opus-4.5",
            "display_name": "Claude Opus 4.5",
            "family": "claude",
            "wire": "anthropic",
            "efforts": ["low", "medium", "high"],
            "default_effort": "medium",
        },
        {
            "id": "gpt-5.2",
            "display_name": "GPT-5.2",
            "family": "gpt",
            "wire": "openai",
            "efforts": [],
            "default_effort": None,
        },
    ],
}


# The same catalog with the hosted pin added in SECOND place: first place could not
# tell "the pin was honoured" from "entry zero was taken".
HOSTED_CATALOG: dict[str, Any] = {
    "object": "list",
    "data": [
        CATALOG["data"][0],
        {
            "id": HOSTED_DEFAULT_MODEL_SLUG,
            "display_name": "Claude Sonnet 5.5",
            "family": "claude",
            "wire": "anthropic",
            "efforts": ["low", "medium", "high", "xhigh", "max"],
            "default_effort": "medium",
        },
        CATALOG["data"][1],
    ],
}


def _gateway(
    monkeypatch: pytest.MonkeyPatch, *, body: dict[str, Any] | None = None, down: bool = False
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if down:
            raise httpx.ConnectError("no gateway here", request=request)
        return httpx.Response(200, json=body if body is not None else CATALOG)

    real = chat_catalog.fetch_catalog

    async def patched(*args: Any, **kwargs: Any) -> chat_catalog.Catalog:
        kwargs.setdefault("client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        return await real(*args, **kwargs)

    monkeypatch.setattr(chat_catalog, "fetch_catalog", patched)


async def _save_directly(org_admin: OrgWithAdmin, **fields: Any) -> None:
    """A preference row as an earlier day left it — a model saved while the
    catalog still offered it, since retired. The route refuses to store one now,
    so the only way to have one is to have saved it before. The catalog-scoped
    keys land where the per-org back-fill put them: in the org's own row, with
    the identity row keeping its old copy."""
    async with AsyncSessionLocal() as session:
        session.add(
            UserPreference(
                user_id=org_admin.admin_id, preferences={"schema_version": "2.0.0", **fields}
            )
        )
        scoped = {k: v for k, v in fields.items() if k in ORG_SCOPED_KEYS}
        if scoped:
            session.add(
                UserOrgPreference(
                    user_id=org_admin.admin_id, org_team_id=org_admin.org_id, preferences=scoped
                )
            )
        await session.commit()


async def _stored(org_admin: OrgWithAdmin) -> dict[str, Any] | None:
    """The saved document as the org reads it (``None`` when nothing was ever
    saved): the identity row with the org's own copy of the catalog-scoped keys
    laid over it."""
    async with AsyncSessionLocal() as session:
        row = await session.get(UserPreference, org_admin.admin_id)
        if row is None:
            return None
        document = {k: v for k, v in row.preferences.items() if k not in ORG_SCOPED_KEYS}
        scoped = await session.get(UserOrgPreference, (org_admin.admin_id, org_admin.org_id))
        document.update(Preferences.model_validate({}).model_dump(include=set(ORG_SCOPED_KEYS)))
        if scoped is not None:
            document.update(scoped.preferences)
        return document


# ---------------------------------------------------------------------------
# Read and write
# ---------------------------------------------------------------------------


async def test_a_reader_who_never_saved_gets_the_defaults_not_a_404(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Nothing stored" and "the defaults" are the same state. A 404 would make
    every client spell the default document a second time, which is exactly how
    two surfaces come to disagree about what "default" means. The default chat
    model reads as the platform default, flagged as not the reader's pick."""
    monkeypatch.setattr(settings, "self_hosted", False)
    _gateway(monkeypatch, body=HOSTED_CATALOG)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    response = await client.get("/api/v1/me/preferences")

    assert response.status_code == 200, response.text
    prefs = response.json()["preferences"]
    assert prefs["default_permission_mode"] == "default"
    assert prefs["default_chat_model"] == HOSTED_DEFAULT_MODEL_SLUG == "claude-sonnet-5.5"
    assert prefs["default_chat_effort"] == "medium"
    assert response.json()["chat_model_defaulted"] is True
    # Nothing was written just by looking.
    assert await _stored(org_admin) is None


async def test_a_save_keeps_the_fields_it_did_not_name(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merge rule, stated as the failure it prevents: the second save names
    only the model, and must not take the stance the first save chose with it."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    await client.patch(
        "/api/v1/me/preferences", json={"preferences": {"default_permission_mode": "plan"}}
    )
    second = await client.patch(
        "/api/v1/me/preferences", json={"preferences": {"default_chat_model": "gpt-5.2"}}
    )

    assert second.status_code == 200, second.text
    prefs = second.json()["preferences"]
    assert prefs["default_permission_mode"] == "plan"
    assert prefs["default_chat_model"] == "gpt-5.2"
    assert second.json()["chat_model_defaulted"] is False


async def test_a_key_this_server_does_not_know_survives_a_save(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A newer client's field has to ride through an older reader untouched, or
    the first person to open the older surface silently loses it. The store is
    the one place that could drop it, so this is where it is pinned."""
    await login(client, org_admin.admin_email, org_admin.admin_password)

    await client.patch(
        "/api/v1/me/preferences", json={"preferences": {"a_field_from_2027": {"nested": 1}}}
    )
    # …and a LATER save that knows nothing about it must not take it out.
    await client.patch(
        "/api/v1/me/preferences", json={"preferences": {"default_permission_mode": "plan"}}
    )

    prefs = (await client.get("/api/v1/me/preferences")).json()["preferences"]
    assert prefs["a_field_from_2027"] == {"nested": 1}


@pytest.mark.parametrize(
    "patch",
    [
        pytest.param({"telemetry_enabled": "sure"}, id="a toggle that is not a boolean"),
        pytest.param({"show_banner": "yep"}, id="another toggle that is not a boolean"),
    ],
)
async def test_a_value_the_schema_refuses_is_not_stored(
    client: AsyncClient, org_admin: OrgWithAdmin, patch: dict[str, Any]
) -> None:
    """Revalidating on the way in is what keeps an unusable value out of the
    document. Stored, it would surface as a crash in whichever reader touched
    it next — a long way from the save that caused it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)

    response = await client.patch("/api/v1/me/preferences", json={"preferences": patch})

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_preference"
    assert await _stored(org_admin) is None


async def test_a_garbage_timeout_restores_the_cap_rather_than_removing_it(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """``Preferences`` is deliberately forgiving where a hard refusal would
    brick the desktop's file, and it degrades in the SAFE direction: a corrupt
    statement timeout comes back as the protective 900s, never as ``0``, which
    is the one value that disables the cap and lets a runaway read bill
    unbounded warehouse compute. Stored through this route, that normalisation
    has to survive — a route that wrote the body through raw would be the way
    around the guard."""
    await login(client, org_admin.admin_email, org_admin.admin_password)

    saved = await client.patch(
        "/api/v1/me/preferences", json={"preferences": {"sql_statement_timeout_seconds": -1}}
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["preferences"]["sql_statement_timeout_seconds"] == 900
    stored = await _stored(org_admin)
    assert stored is not None
    assert stored["sql_statement_timeout_seconds"] == 900


async def test_one_reader_cannot_read_anothers_preferences(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route names no user — the caller's identity IS the scope. Saving as
    one person and reading as another must show the reader their own document,
    never the other's."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await client.patch(
        "/api/v1/me/preferences", json={"preferences": {"default_chat_model": "gpt-5.2"}}
    )

    from tests.conftest import make_member

    async with AsyncSessionLocal() as session:
        other, other_password = await make_member(session, org_id=org_admin.org_id, verified=True)
        other_email = other.email
    assert other_password is not None
    await login(client, other_email, other_password)

    read = (await client.get("/api/v1/me/preferences")).json()
    assert read["preferences"]["default_chat_model"] != "gpt-5.2"
    assert read["chat_model_defaulted"] is True


async def test_preferences_are_not_readable_without_a_session(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/me/preferences")).status_code == 401


# ---------------------------------------------------------------------------
# The new-chat seed
# ---------------------------------------------------------------------------


async def test_the_seed_keeps_a_saved_choice_the_catalog_still_offers(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await client.patch(
        "/api/v1/me/preferences",
        json={
            "preferences": {"default_chat_model": "claude-opus-4.5", "default_chat_effort": "high"}
        },
    )

    seed = (await client.get("/api/v1/me/chat-defaults")).json()

    assert seed == {"model": "claude-opus-4.5", "effort": "high", "permission_mode": "default"}


async def test_a_saved_model_the_catalog_dropped_is_corrected_and_the_correction_kept(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that is genuinely gone must not keep seeding chats that cannot
    run — and re-deriving the same correction on every read would leave the
    person looking at a value the store disagrees with. The correction is "no
    pick": the reader follows the platform default from then on, rather than
    being pinned to whichever model the default was on the day of the fix."""
    await _save_directly(
        org_admin, default_chat_model="claude-opus-4.0", default_chat_effort="high"
    )
    await login(client, org_admin.admin_email, org_admin.admin_password)
    _gateway(monkeypatch)

    seed = (await client.get("/api/v1/me/chat-defaults")).json()

    assert seed["model"] == "claude-opus-4.5"
    stored = await _stored(org_admin)
    assert stored is not None
    assert stored["default_chat_model"] is None
    assert stored["default_chat_effort"] is None


async def test_a_gateway_outage_returns_the_saved_default_untouched(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this endpoint exists to prevent.

    A resolver that "resets to the first model" when the catalog comes back
    empty cannot tell a gateway that is DOWN from an org with no models — and
    the first transient 401 silently replaces the reader's deliberate choice
    with whatever happened to be first. The saved value must come back
    unchanged, and nothing may be written."""
    await _save_directly(
        org_admin, default_chat_model="claude-opus-4.0", default_chat_effort="high"
    )
    await login(client, org_admin.admin_email, org_admin.admin_password)
    before = await _stored(org_admin)
    _gateway(monkeypatch, down=True)

    seed = (await client.get("/api/v1/me/chat-defaults")).json()

    assert seed == {"model": "claude-opus-4.0", "effort": "high", "permission_mode": "default"}
    assert await _stored(org_admin) == before


async def test_a_hosted_reader_who_never_chose_is_seeded_on_the_hosted_default(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Alkera's SaaS everybody who has not chosen a model starts on the same
    one. Without the pin this reader would open on whatever the gateway happened
    to list first, which differs per org (entitlement + BYOK filtering)."""
    monkeypatch.setattr(settings, "self_hosted", False)
    _gateway(monkeypatch, body=HOSTED_CATALOG)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    seed = (await client.get("/api/v1/me/chat-defaults")).json()

    assert seed["model"] == HOSTED_DEFAULT_MODEL_SLUG
    assert seed["effort"] == "medium"
    # Nothing is written: the default is the platform's, and a reader who never
    # chose must move with it when it changes — a stored copy would pin them.
    assert await _stored(org_admin) is None


async def test_a_self_hosted_reader_who_never_chose_keeps_their_own_catalog_order(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A customer's install is not steered onto a model Alkera picked — the same
    catalog seeds its own first entry."""
    monkeypatch.setattr(settings, "self_hosted", True)
    _gateway(monkeypatch, body=HOSTED_CATALOG)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    seed = (await client.get("/api/v1/me/chat-defaults")).json()

    assert seed["model"] == "claude-opus-4.5"


async def test_a_hosted_reader_own_choice_survives_the_hosted_default(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin fills a gap; it never overwrites a deliberate choice."""
    monkeypatch.setattr(settings, "self_hosted", False)
    _gateway(monkeypatch, body=HOSTED_CATALOG)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await client.patch(
        "/api/v1/me/preferences",
        json={
            "preferences": {"default_chat_model": "claude-opus-4.5", "default_chat_effort": "high"}
        },
    )

    seed = (await client.get("/api/v1/me/chat-defaults")).json()

    assert seed["model"] == "claude-opus-4.5"
    assert seed["effort"] == "high"


# ---------------------------------------------------------------------------
# What the preference actually does
# ---------------------------------------------------------------------------


async def test_a_reader_who_never_chose_starts_in_default(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A reader who has never saved a stance starts a chat that asks before
    each change, not one that refuses every change."""
    await login(client, org_admin.admin_email, org_admin.admin_password)

    created = await client.post("/api/v1/chats", json={"title": "Ops"})

    assert created.json()["permission_mode"] == "default"


async def test_a_new_chat_opens_in_the_saved_stance(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A preference nobody's next chat used would be a setting in name only."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await client.patch(
        "/api/v1/me/preferences", json={"preferences": {"default_permission_mode": "plan"}}
    )

    created = await client.post("/api/v1/chats", json={"title": "Ops"})

    assert created.json()["permission_mode"] == "plan"


@pytest.mark.parametrize(
    "saved",
    [
        pytest.param("bypass", id="bypass"),
        pytest.param("plan", id="plan"),
    ],
)
async def test_the_stances_the_editor_lent_the_web_reach_a_browser_chat(
    client: AsyncClient, org_admin: OrgWithAdmin, saved: str
) -> None:
    """The preference is ONE value shared with the editor, and every stance the
    doors can SET is honoured from it. A saved ``bypass`` that opened a browser
    chat in read-only would be the preference quietly overruled by a policy that
    no longer exists — the reader saves the stance they want their next chat in
    and gets some other one, with nothing on screen saying why."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    saved_ok = await client.patch(
        "/api/v1/me/preferences", json={"preferences": {"default_permission_mode": saved}}
    )
    assert saved_ok.status_code == 200, saved_ok.text

    created = await client.post("/api/v1/chats", json={"title": "Ops"})

    assert created.json()["permission_mode"] == saved


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        pytest.param("plan", "plan", id="plan"),
        pytest.param("default", "default", id="default — the one stance that may write"),
        pytest.param("read_only", "read_only", id="read_only"),
        pytest.param("bypass", "bypass", id="bypass — the one that asks about nothing"),
    ],
)
async def test_the_new_chat_seed_states_the_stance_the_next_chat_actually_opens_in(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    saved: str,
    expected: str,
) -> None:
    """The seed is what a composer with no chat yet displays, so it has to be
    the same answer the create gives — read from the same resolver, not guessed
    beside it. A composer that guessed showed `Read-only` to a reader whose next
    chat opened in `default`: the label understated what the agent was about to
    be allowed to do, which is the direction that matters for a safety label."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await client.patch(
        "/api/v1/me/preferences", json={"preferences": {"default_permission_mode": saved}}
    )

    seed = (await client.get("/api/v1/me/chat-defaults")).json()
    created = await client.post("/api/v1/chats", json={"title": "Churn by segment"})

    assert seed["permission_mode"] == expected
    assert created.json()["permission_mode"] == seed["permission_mode"]


async def test_a_seed_that_corrects_the_model_states_the_stance_that_correction_leaves(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This read can WRITE: a saved model the catalog no longer offers is
    corrected and the correction persisted. The stance has to be read after that
    write, because the write is a write to the preference row the create reads —
    answered before it, the composer names one stance and the chat the very next
    click makes opens in another."""
    await _save_directly(org_admin, default_chat_model="claude-opus-4.0")
    await login(client, org_admin.admin_email, org_admin.admin_password)
    _gateway(monkeypatch)

    seed = (await client.get("/api/v1/me/chat-defaults")).json()
    created = await client.post("/api/v1/chats", json={"title": "Churn by segment"})

    assert seed["model"] == "claude-opus-4.5"
    assert created.json()["permission_mode"] == seed["permission_mode"]


async def test_a_gateway_outage_still_states_the_stance(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalog and the stance come from different places, and only one of
    them is behind the gateway. Losing the model seed to an outage must not also
    blank the stance the composer shows — a chip with nothing to say falls back
    to a guess, which is the bug this field exists to remove."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await client.patch(
        "/api/v1/me/preferences", json={"preferences": {"default_permission_mode": "plan"}}
    )
    _gateway(monkeypatch, down=True)

    seed = (await client.get("/api/v1/me/chat-defaults")).json()

    assert seed["permission_mode"] == "plan"
    # A create during the outage is refused outright — a chat is never opened
    # without a model — so the stance's only surface here is the seed above.
    created = await client.post("/api/v1/chats", json={"title": "Ops"})
    assert created.status_code == 422, created.text
    assert created.json()["error"]["code"] == "model_catalog_unavailable"


# ---------------------------------------------------------------------------
# The shape of the door
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {"default_permission_mode": "plan", "default_chat_model": "gpt-5.2"},
            id="the flat body a client writes when it misses the envelope",
        ),
        pytest.param(
            {"preferences": {"default_permission_mode": "plan"}, "replace": True},
            id="the envelope plus a field this route does not have",
        ),
        pytest.param({"prefs": {"default_permission_mode": "plan"}}, id="the envelope misspelled"),
    ],
)
async def test_a_body_that_is_not_the_documented_shape_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin, body: dict[str, Any]
) -> None:
    """A wrong-shaped PATCH used to answer 200 having written nothing: the
    envelope defaulted to empty, the merge merged nothing, and the response
    showed the OLD document — which reads exactly like a save that worked. The
    person who set their stance in that client would find it unset later, with
    no error anywhere to explain it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)

    response = await client.patch("/api/v1/me/preferences", json=body)

    assert response.status_code == 422, response.text
    assert await _stored(org_admin) is None


async def test_a_stance_no_surface_knows_is_refused_and_never_stored(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """An unknown VALUE for a known key is not a newer client's field — it is
    garbage that every later reader has to coerce, and the coercion is silent.
    The 422 names what the key accepts, because the caller sent a word and has
    no other way to learn the vocabulary."""
    await login(client, org_admin.admin_email, org_admin.admin_password)

    response = await client.patch(
        "/api/v1/me/preferences", json={"preferences": {"default_permission_mode": "sideways"}}
    )

    assert response.status_code == 422, response.text
    assert "read_only" in response.text and "bypass" in response.text
    assert await _stored(org_admin) is None

    # And the stance the reader thought they set is not the stance they get.
    created = await client.post("/api/v1/chats", json={"title": "Ops"})
    assert created.json()["permission_mode"] == "default"


async def test_an_unknown_key_still_rides_through_the_door(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The narrowing is on VALUES, not keys. Forbidding unknown keys inside the
    envelope would break the forward compatibility the stored document exists
    for — this pins that the two rules did not get conflated."""
    await login(client, org_admin.admin_email, org_admin.admin_password)

    response = await client.patch(
        "/api/v1/me/preferences",
        json={"preferences": {"a_field_from_next_year": {"nested": True}}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["preferences"]["a_field_from_next_year"] == {"nested": True}


async def test_a_stance_a_previous_build_stored_lands_on_the_floor(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The door refuses these now; a row written before it did still holds one.
    A new chat must not open on it — it lands on the floor, which is the same
    normalisation the three legal stances already get."""
    async with AsyncSessionLocal() as session:
        session.add(
            UserPreference(
                user_id=org_admin.admin_id,
                preferences={"schema_version": "2.0.0", "default_permission_mode": "sideways"},
            )
        )
        await session.commit()
    await login(client, org_admin.admin_email, org_admin.admin_password)

    created = await client.post("/api/v1/chats", json={"title": "Ops"})

    assert created.status_code == 201, created.text
    assert created.json()["permission_mode"] == "read_only"


# ---------------------------------------------------------------------------
# Reading a saved default falls back gracefully — on every read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("saved", "why"),
    [
        pytest.param("claude-opus-4.0", "retired", id="a model deleted from the catalog"),
        pytest.param("gpt-5.2", "disabled", id="a model disabled or left with no route"),
        pytest.param("not-a-model-at-all", "unknown", id="a slug that was never a model"),
    ],
)
async def test_a_saved_model_that_no_longer_resolves_reads_as_the_platform_default(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    saved: str,
    why: str,
) -> None:
    """The gateway lists only enabled models with an enabled route, so a
    disabled or unrouted model is simply absent from the catalog — the same
    case, to the reader, as one deleted or never real. Every one reads as the
    platform default, never as an error and never as a value no chat can open
    on; and the read writes nothing."""
    monkeypatch.setattr(settings, "self_hosted", False)
    # gpt-5.2 is left out: the gateway no longer offers it.
    _gateway(monkeypatch, body={"object": "list", "data": HOSTED_CATALOG["data"][:2]})
    await _save_directly(org_admin, default_chat_model=saved, default_chat_effort="high")
    before = await _stored(org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    response = await client.get("/api/v1/me/preferences")

    assert response.status_code == 200, (why, response.text)
    assert response.json()["preferences"]["default_chat_model"] == HOSTED_DEFAULT_MODEL_SLUG
    assert response.json()["preferences"]["default_chat_effort"] == "medium"
    assert response.json()["chat_model_defaulted"] is True
    assert await _stored(org_admin) == before


@pytest.mark.parametrize(
    ("saved_model", "saved_effort", "model", "effort"),
    [
        pytest.param(
            "claude-opus-4.5", "max", "claude-opus-4.5", "medium", id="an effort the model dropped"
        ),
        pytest.param(
            "claude-opus-4.5", "low", "claude-opus-4.5", "low", id="a live pick and effort"
        ),
        pytest.param(
            "claude-opus-4.5::high", None, "claude-opus-4.5", "high", id="a model::effort variant"
        ),
        pytest.param(
            "claude-opus-4.5::max",
            None,
            "claude-opus-4.5",
            "medium",
            id="a variant the model no longer offers",
        ),
    ],
)
async def test_a_saved_pick_that_resolves_is_kept_with_an_effort_it_offers(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    saved_model: str,
    saved_effort: str | None,
    model: str,
    effort: str,
) -> None:
    """An explicit choice beats the platform default for as long as it resolves.
    An effort the model does not offer falls to that model's own default effort
    — the model is kept, and the answer is still the reader's pick."""
    monkeypatch.setattr(settings, "self_hosted", False)
    _gateway(monkeypatch, body=HOSTED_CATALOG)
    await _save_directly(
        org_admin, default_chat_model=saved_model, default_chat_effort=saved_effort
    )
    await login(client, org_admin.admin_email, org_admin.admin_password)

    read = (await client.get("/api/v1/me/preferences")).json()

    assert read["preferences"]["default_chat_model"] == model
    assert read["preferences"]["default_chat_effort"] == effort
    assert read["chat_model_defaulted"] is False


async def test_an_outage_reads_the_saved_default_as_saved(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing to resolve against, the read answers what is stored rather
    than failing or guessing."""
    await _save_directly(org_admin, default_chat_model="claude-opus-4.0")
    _gateway(monkeypatch, down=True)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    response = await client.get("/api/v1/me/preferences")

    assert response.status_code == 200, response.text
    assert response.json()["preferences"]["default_chat_model"] == "claude-opus-4.0"
    assert response.json()["chat_model_defaulted"] is False


@pytest.mark.parametrize(
    ("saved_model", "saved_effort", "model", "effort"),
    [
        pytest.param(None, None, HOSTED_DEFAULT_MODEL_SLUG, "medium", id="nothing chosen"),
        pytest.param(
            "claude-opus-4.0", "high", HOSTED_DEFAULT_MODEL_SLUG, "medium", id="a retired pick"
        ),
        pytest.param(
            "claude-opus-4.5", "max", "claude-opus-4.5", "medium", id="an effort the pick dropped"
        ),
        pytest.param("claude-opus-4.5", "low", "claude-opus-4.5", "low", id="a live pick"),
    ],
)
async def test_a_new_chat_opens_on_the_resolved_default(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    saved_model: str | None,
    saved_effort: str | None,
    model: str,
    effort: str,
) -> None:
    """The create reads the saved default through the same resolver: a stale
    pick opens the chat on the platform default, an explicit live one on itself."""
    monkeypatch.setattr(settings, "self_hosted", False)
    _gateway(monkeypatch, body=HOSTED_CATALOG)
    await _save_directly(
        org_admin, default_chat_model=saved_model, default_chat_effort=saved_effort
    )
    await login(client, org_admin.admin_email, org_admin.admin_password)

    created = await client.post("/api/v1/chats", json={"title": "Ops"})

    assert created.status_code == 201, created.text
    assert created.json()["model"]["id"] == model
    assert created.json()["model"]["effort"] == effort


# ---------------------------------------------------------------------------
# Saving a default is checked against the catalog
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        pytest.param(
            {"default_chat_model": "claude-opus-4.0"}, "model_not_offered", id="a retired model"
        ),
        pytest.param(
            {"default_chat_model": "not-a-model"}, "model_not_offered", id="an unknown slug"
        ),
        pytest.param(
            {"default_chat_model": "claude-opus-4.5", "default_chat_effort": "max"},
            "effort_not_offered",
            id="an effort the model does not offer",
        ),
        pytest.param(
            {"default_chat_model": "gpt-5.2", "default_chat_effort": "high"},
            "effort_not_offered",
            id="an effort for a model with none",
        ),
        pytest.param(
            {"default_chat_model": "claude-opus-4.5::ultra"},
            "effort_not_offered",
            id="a variant naming an effort the model does not offer",
        ),
    ],
)
async def test_a_default_the_catalog_does_not_offer_is_refused_and_never_stored(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    patch: dict[str, Any],
    code: str,
) -> None:
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    response = await client.patch("/api/v1/me/preferences", json={"preferences": patch})

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code
    assert await _stored(org_admin) is None


async def test_a_variant_is_stored_as_its_model_and_effort(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    response = await client.patch(
        "/api/v1/me/preferences",
        json={"preferences": {"default_chat_model": "claude-opus-4.5::low"}},
    )

    assert response.status_code == 200, response.text
    stored = await _stored(org_admin)
    assert stored is not None
    assert (stored["default_chat_model"], stored["default_chat_effort"]) == (
        "claude-opus-4.5",
        "low",
    )


async def test_a_new_pick_during_an_outage_is_refused_but_other_saves_go_through(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pick that cannot be checked is not stored — it would be the stale value
    every reader then falls back from. But the Settings page sends the model it
    loaded with every save, and an outage must not stop somebody saving their
    stance; nor clearing their pick, which needs no catalog."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    first = await client.patch(
        "/api/v1/me/preferences", json={"preferences": {"default_chat_model": "gpt-5.2"}}
    )
    assert first.status_code == 200, first.text
    _gateway(monkeypatch, down=True)

    new_pick = await client.patch(
        "/api/v1/me/preferences", json={"preferences": {"default_chat_model": "claude-opus-4.5"}}
    )
    same_pick = await client.patch(
        "/api/v1/me/preferences",
        json={"preferences": {"default_chat_model": "gpt-5.2", "default_permission_mode": "plan"}},
    )
    cleared = await client.patch(
        "/api/v1/me/preferences", json={"preferences": {"default_chat_model": None}}
    )

    assert new_pick.status_code == 422, new_pick.text
    assert new_pick.json()["error"]["code"] == "model_catalog_unavailable"
    assert same_pick.status_code == 200, same_pick.text
    assert cleared.status_code == 200, cleared.text
    stored = await _stored(org_admin)
    assert stored is not None
    assert stored["default_permission_mode"] == "plan"
    assert stored["default_chat_model"] is None
