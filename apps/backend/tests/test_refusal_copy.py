"""A refusal is worded for the credential that receives it, and carries no
exception text.

A browser tab reads a refusal as a sentence in the portal; a CLI, an editor, a
box or a token reads it in a log. A sentence about another window or an
organization's single sign-on is false for the second reader, and a decoder's
own error text tells either of them nothing it can act on.
"""

from __future__ import annotations

import pytest
from alkera_core.config import settings
from backend.auth.refusals import INVALID_SESSION, REFUSALS, Reader, Refusal
from httpx import AsyncClient

pytestmark = [pytest.mark.spread]

#: Words that only make sense to a person at a browser tab.
BROWSER_ONLY = ("window", "page", "tab", "reload", "single sign-on", "sign in again")


@pytest.mark.parametrize("refusal", REFUSALS, ids=lambda r: r.code)
def test_no_client_reads_a_sentence_meant_for_a_browser(refusal: Refusal) -> None:
    client = refusal.message(Reader.CLIENT).lower()
    assert not [word for word in BROWSER_ONLY if word in client], client
    assert refusal.message(Reader.BROWSER) != refusal.message(Reader.CLIENT)
    sent = refusal.to(Reader.CLIENT)
    assert (sent.status_code, sent.detail) == (
        refusal.status,
        {"code": refusal.code, "message": refusal.message(Reader.CLIENT)},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token", "browser"),
    [
        pytest.param("not-a-jwt", False, id="bearer-not-a-jwt"),
        pytest.param("e30.e30.éé", False, id="bearer-undecodable-bytes"),
        pytest.param("e30.e30.x", True, id="cookie-not-a-jwt"),
    ],
)
async def test_an_undecodable_session_says_so_without_the_decoders_words(
    client: AsyncClient, token: str, browser: bool
) -> None:
    if browser:
        client.cookies.set(settings.auth_cookie_name, token)
        resp = await client.get("/api/v1/auth/me")
    else:
        resp = await client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}".encode()}
        )
    assert resp.status_code == 401, resp.text
    error = resp.json()["error"]
    reader = Reader.of(browser_session=browser)
    assert (error["code"], error["message"]) == ("unauthorized", INVALID_SESSION.message(reader))
