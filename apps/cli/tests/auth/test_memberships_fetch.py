"""``fetch_memberships``: which failures say the stored sign-in is gone.

Only a 401 is the sign-in itself being refused; every caller that would sign
the person out keys on :class:`SignInRejectedError`. Anything else (a
refusal for one org, a server too old to have the route, a 5xx, no network, an
answer this client cannot read) is a plain :class:`MembershipsError`.
"""

from __future__ import annotations

import httpx
import pytest
from _profiles import ORG_A, make_jwt, store
from alkera_cli.account import memberships


def _fetch(handler: httpx.MockTransport) -> memberships.Memberships:
    profile = store(ORG_A, org_name="Acme", current=True, token=make_jwt(org=ORG_A))
    return memberships.fetch_memberships(profile, transport=handler)


def _answering(status: int, body: object = None) -> httpx.MockTransport:
    return httpx.MockTransport(lambda _r: httpx.Response(status, json=body or {}))


def test_a_401_is_the_sign_in_rejected() -> None:
    with pytest.raises(memberships.SignInRejectedError, match="alkera login"):
        _fetch(_answering(401))


@pytest.mark.parametrize(
    ("status", "says"),
    [
        pytest.param(403, "refused for this organization", id="403"),
        pytest.param(409, "refused for this organization", id="409"),
        pytest.param(404, "can't list your organizations yet", id="404"),
        pytest.param(500, "answered 500", id="500"),
        pytest.param(503, "answered 503", id="503"),
    ],
)
def test_other_failures_are_not_a_rejected_sign_in(status: int, says: str) -> None:
    with pytest.raises(memberships.MembershipsError, match=says) as raised:
        _fetch(_answering(status))
    assert not isinstance(raised.value, memberships.SignInRejectedError)


def test_no_network_is_not_a_rejected_sign_in() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(memberships.MembershipsError, match="Couldn't reach") as raised:
        _fetch(httpx.MockTransport(refuse))
    assert not isinstance(raised.value, memberships.SignInRejectedError)


@pytest.mark.parametrize(
    "body",
    [pytest.param(["not", "an", "object"], id="a-list"), pytest.param({"x": 1}, id="no-list")],
)
def test_an_unreadable_answer_is_not_a_rejected_sign_in(body: object) -> None:
    with pytest.raises(memberships.MembershipsError) as raised:
        _fetch(_answering(200, body))
    assert not isinstance(raised.value, memberships.SignInRejectedError)


def test_a_good_answer_parses() -> None:
    listed = _fetch(
        _answering(
            200,
            {
                "active_org_team_id": ORG_A,
                "memberships": [{"org_team_id": ORG_A, "org_name": "Acme", "role": "admin"}],
            },
        )
    )
    assert listed.active_org_team_id == ORG_A
    assert [m.org_name for m in listed.memberships] == ["Acme"]
