"""Builders for stored sign-in profiles in tests.

A token here is an unsigned JWT whose claims carry the person and org a real
device grant would mint (``sub``, ``org_team_id``, ``email``, ``exp``): the CLI
keys a profile by those claims and never verifies the signature, the server
does."""

from __future__ import annotations

import base64
import json
import time

from alkera_cli.account import auth_file
from alkera_cli.account.auth_file import Profile

API = "https://api.example.test"
ORG_A = "11111111-1111-4111-8111-111111111111"
ORG_B = "22222222-2222-4222-8222-222222222222"
USER = "99999999-9999-4999-8999-999999999999"


def make_jwt(
    *, sub: str = USER, org: str | None = ORG_A, email: str = "a@x.com", exp: int | None = None
) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    claims: dict[str, object] = {"sub": sub, "email": email, "exp": exp or int(time.time()) + 3600}
    if org is not None:
        claims["org_team_id"] = org
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


def store(
    org: str,
    *,
    org_name: str = "",
    sub: str = USER,
    email: str = "a@x.com",
    api_url: str = API,
    current: bool = False,
    token: str | None = None,
) -> Profile:
    """Store a profile for ``org`` (its token's claims name it) and return it."""
    minted = token or make_jwt(sub=sub, org=org, email=email)
    profile = auth_file.profile_from_token(api_url, minted, email=email, org_name=org_name)
    return auth_file.save_profile(profile, make_current=current)


__all__ = ["API", "ORG_A", "ORG_B", "USER", "make_jwt", "store"]
