"""The membership claims every org-bound token carries: ``mid`` and ``mep``.

A token minted for a membership names it (``mid``) and the membership's
credential epoch at mint (``mep``); one minted before memberships existed
names neither, and every decoder must keep accepting it. The pair travels
together or not at all, and a gateway token or socket ticket carries its
parent session's pair so a membership revocation reaches them too.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from alkera_core.auth import (
    InvalidTokenError,
    decode_gateway_credential,
    decode_gateway_token,
    decode_session_token,
    decode_ws_ticket,
    encode_cli_token,
    encode_session_token,
    encode_ws_ticket,
    mint_gateway_token,
    mint_machine_gateway_token,
)
from alkera_core.config import settings

USER = UUID(int=1)
ORG = UUID(int=2)
MEMBERSHIP = UUID(int=3)


def _sign(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg)


def _raw(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


@pytest.mark.parametrize("encode", [encode_session_token, encode_cli_token])
def test_a_membership_token_names_its_membership_and_epoch(encode: Any) -> None:
    token, claims = encode(
        user_id=USER,
        email="u@x.dev",
        org_team_id=ORG,
        platform_role=None,
        membership_id=MEMBERSHIP,
        membership_epoch=7,
    )
    assert _raw(token)["mid"] == MEMBERSHIP.hex
    assert _raw(token)["mep"] == 7
    decoded = decode_session_token(token)
    assert (decoded.membership_id, decoded.membership_epoch) == (MEMBERSHIP, 7)
    assert (claims.membership_id, claims.membership_epoch) == (MEMBERSHIP, 7)


def test_a_token_minted_before_memberships_carries_neither_and_still_decodes() -> None:
    token, _ = encode_session_token(
        user_id=USER, email="u@x.dev", org_team_id=ORG, platform_role=None
    )
    assert "mid" not in _raw(token) and "mep" not in _raw(token)
    decoded = decode_session_token(token)
    assert (decoded.membership_id, decoded.membership_epoch) == (None, None)
    assert decoded.org_team_id == ORG


@pytest.mark.parametrize(
    ("membership_id", "epoch"),
    [
        pytest.param(None, 1, id="epoch-without-membership"),
        pytest.param(MEMBERSHIP, None, id="membership-without-epoch"),
        pytest.param(MEMBERSHIP, -1, id="negative-epoch"),
    ],
)
def test_the_encoder_refuses_half_a_membership(
    membership_id: UUID | None, epoch: int | None
) -> None:
    with pytest.raises(ValueError):
        encode_session_token(
            user_id=USER,
            email="u@x.dev",
            org_team_id=ORG,
            platform_role=None,
            membership_id=membership_id,
            membership_epoch=epoch,
        )


def _session_payload(**extra: Any) -> dict[str, Any]:
    now = int(time.time())
    return {
        "sub": USER.hex,
        "email": "u@x.dev",
        "org_team_id": ORG.hex,
        "platform_role": None,
        "jti": uuid4().hex,
        "iat": now,
        "exp": now + 600,
        **extra,
    }


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"mid": MEMBERSHIP.hex}, id="mid-without-mep"),
        pytest.param({"mep": 0}, id="mep-without-mid"),
        pytest.param({"mid": "not-a-uuid", "mep": 0}, id="malformed-mid"),
        pytest.param({"mid": MEMBERSHIP.hex, "mep": "0"}, id="string-mep"),
        pytest.param({"mid": MEMBERSHIP.hex, "mep": True}, id="boolean-mep"),
        pytest.param({"mid": MEMBERSHIP.hex, "mep": -3}, id="negative-mep"),
        pytest.param({"mid": 5, "mep": 0}, id="numeric-mid"),
    ],
)
def test_a_token_with_a_broken_membership_pair_is_not_a_token(extra: dict[str, Any]) -> None:
    """A forged or truncated pair never reads as a legacy token with one fewer
    check: it is refused outright, on every decoder that reads sessions."""
    token = _sign(_session_payload(**extra))
    with pytest.raises(InvalidTokenError):
        decode_session_token(token)
    with pytest.raises(InvalidTokenError):
        decode_gateway_credential(token)


def _session(**membership: Any) -> Any:
    _, claims = encode_session_token(
        user_id=USER, email="u@x.dev", org_team_id=ORG, platform_role=None, **membership
    )
    return claims


@pytest.mark.parametrize(
    "membership",
    [
        pytest.param({"membership_id": MEMBERSHIP, "membership_epoch": 4}, id="bound"),
        pytest.param({}, id="legacy-parent"),
    ],
)
def test_a_session_parented_gateway_token_carries_its_sessions_membership(
    membership: dict[str, Any],
) -> None:
    session = _session(**membership)
    token, claims = mint_gateway_token(
        user_id=USER, org_team_id=ORG, chat_id=uuid4(), session=session
    )
    decoded = decode_gateway_token(token)
    expected = (membership.get("membership_id"), membership.get("membership_epoch"))
    assert (decoded.membership_id, decoded.membership_epoch) == expected
    assert (claims.membership_id, claims.membership_epoch) == expected


def test_a_machine_parented_gateway_token_carries_the_owners_membership() -> None:
    token, _ = mint_machine_gateway_token(
        user_id=USER,
        org_team_id=ORG,
        chat_id=uuid4(),
        credential_id=uuid4(),
        credential_issued_at=int(time.time()),
        membership_id=MEMBERSHIP,
        membership_epoch=2,
    )
    decoded = decode_gateway_token(token)
    assert (decoded.membership_id, decoded.membership_epoch) == (MEMBERSHIP, 2)


def test_a_socket_ticket_carries_its_sessions_membership_and_org() -> None:
    session = _session(membership_id=MEMBERSHIP, membership_epoch=9)
    ticket = decode_ws_ticket(encode_ws_ticket(user_id=USER, org_id=ORG, session=session))
    assert (ticket.org_id, ticket.membership_id, ticket.membership_epoch) == (ORG, MEMBERSHIP, 9)


def test_a_socket_ticket_is_never_minted_for_another_org_than_its_session() -> None:
    with pytest.raises(ValueError, match="org"):
        encode_ws_ticket(user_id=USER, org_id=uuid4(), session=_session())
