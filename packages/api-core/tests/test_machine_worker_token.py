"""The org-bound worker credential: its token, the context it resolves to, and
the policy that keeps it off the machine's own standing.

A box serving several orgs gives each org's process a credential bound to one
org. These cases pin the pieces that make "bound" true without a database:
the token carries exactly the (credential, machine, org) it was minted for and
nothing forged decodes; the context it builds serves that org and no other;
the socket ticket says it was minted by a worker; and the machine-credential
policy refuses it whatever the facts.
"""

from __future__ import annotations

import time
from uuid import UUID, uuid4

import jwt
import pytest
from alkera_core.auth import (
    MACHINE_WORKER_TOKEN_MAX_TTL_SECONDS,
    InvalidTokenError,
    TokenExpiredError,
    decode_machine_worker_token,
    decode_ws_machine_ticket,
    encode_ws_machine_ticket,
    mint_machine_worker_token,
)
from alkera_core.auth.machine_token import (
    MACHINE_WORKER_TOKEN_PREFIX,
    looks_like_machine_token,
    looks_like_machine_worker_token,
    mint_machine_token,
)
from alkera_core.authz import (
    ActingContext,
    Action,
    CredentialKind,
    Effect,
    PrincipalKind,
    Resource,
    ResourceType,
    authorize,
)
from alkera_core.authz.principal import Principal
from alkera_core.config import settings

CREDENTIAL = UUID("00000000-0000-4000-8000-0000000000c1")
MACHINE = UUID("00000000-0000-4000-8000-0000000000d1")
ORG_A = UUID("00000000-0000-4000-8000-00000000000a")
ORG_B = UUID("00000000-0000-4000-8000-00000000000b")
OPERATOR = UUID("00000000-0000-4000-8000-0000000000ee")


def _mint(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "credential_id": CREDENTIAL,
        "machine_id": MACHINE,
        "org_id": ORG_A,
        **overrides,
    }
    token, _claims = mint_machine_worker_token(**kwargs)
    return token


# --------------------------------------------------------------------------- #
# the token
# --------------------------------------------------------------------------- #


def test_the_token_round_trips_exactly_what_it_was_minted_for() -> None:
    raw, claims = mint_machine_worker_token(
        credential_id=CREDENTIAL, machine_id=MACHINE, org_id=ORG_A, ttl_seconds=120, now=1_000
    )
    assert raw.startswith(MACHINE_WORKER_TOKEN_PREFIX)
    with pytest.raises(TokenExpiredError):
        decode_machine_worker_token(raw)
    fresh, minted = mint_machine_worker_token(
        credential_id=CREDENTIAL, machine_id=MACHINE, org_id=ORG_A
    )
    decoded = decode_machine_worker_token(fresh)
    assert decoded == minted
    assert (decoded.credential_id, decoded.machine_id, decoded.org_id) == (
        CREDENTIAL,
        MACHINE,
        ORG_A,
    )
    assert claims.expires_at - claims.issued_at == 120


def test_each_token_has_its_own_jti() -> None:
    assert decode_machine_worker_token(_mint()).jti != decode_machine_worker_token(_mint()).jti


@pytest.mark.parametrize("ttl", [0, -1, MACHINE_WORKER_TOKEN_MAX_TTL_SECONDS + 1])
def test_a_ttl_outside_the_window_is_refused_at_mint(ttl: int) -> None:
    with pytest.raises(ValueError, match="ttl"):
        mint_machine_worker_token(
            credential_id=CREDENTIAL, machine_id=MACHINE, org_id=ORG_A, ttl_seconds=ttl
        )


def _signed(payload: dict[str, object]) -> str:
    return MACHINE_WORKER_TOKEN_PREFIX + jwt.encode(
        payload, settings.effective_jwt_secret, algorithm=settings.auth_jwt_alg
    )


def _payload(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    base: dict[str, object] = {
        "typ": "machine_worker",
        "cred": CREDENTIAL.hex,
        "mach": MACHINE.hex,
        "org": ORG_A.hex,
        "jti": "j",
        "iat": now,
        "exp": now + 60,
    }
    return {**base, **overrides}


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(_mint().removeprefix(MACHINE_WORKER_TOKEN_PREFIX), id="no-marker"),
        pytest.param(_mint()[:-3] + "xyz", id="tampered-signature"),
        pytest.param(_signed(_payload(typ="ws_machine_ticket")), id="another-token-type"),
        pytest.param(_signed(_payload(typ=None)), id="no-token-type"),
        pytest.param(_signed({k: v for k, v in _payload().items() if k != "org"}), id="no-org"),
        pytest.param(_signed(_payload(org="not-hex")), id="malformed-org"),
        pytest.param(_signed(_payload(cred="")), id="empty-credential"),
        pytest.param(_signed(_payload(mach=7)), id="numeric-machine"),
        pytest.param(_signed(_payload(iat="now")), id="string-iat"),
        pytest.param(
            MACHINE_WORKER_TOKEN_PREFIX
            + jwt.encode(_payload(), "another-secret", algorithm=settings.auth_jwt_alg),
            id="signed-with-another-secret",
        ),
    ],
)
def test_nothing_the_box_did_not_mint_decodes(raw: str) -> None:
    with pytest.raises(InvalidTokenError):
        decode_machine_worker_token(raw)


def test_the_marker_routes_as_a_machine_credential_but_never_as_a_raw_secret() -> None:
    """Every door that routes a machine bearer before any database work (the
    rate limiter, the browser-session refusal, the product gate) routes a
    worker token the same way; a raw machine secret is never mistaken for
    one, because url-safe base64 has no ``.``."""
    worker = _mint()
    assert looks_like_machine_token(worker) and looks_like_machine_worker_token(worker)
    for _ in range(200):
        secret, _digest = mint_machine_token()
        assert looks_like_machine_token(secret)
        assert not looks_like_machine_worker_token(secret)


# --------------------------------------------------------------------------- #
# the context
# --------------------------------------------------------------------------- #


def test_the_worker_context_serves_its_org_alone() -> None:
    ctx = ActingContext.for_machine_worker(
        machine_id=MACHINE, credential_id=CREDENTIAL, org_id=ORG_A, label="pool"
    )
    assert ctx.is_machine and ctx.is_machine_worker
    assert ctx.acting_principal.id == str(MACHINE)
    assert ctx.credential_id == str(CREDENTIAL)
    assert ctx.serves(ORG_A)
    assert not ctx.serves(ORG_B)
    assert not ctx.serves(OPERATOR)


def test_the_machine_itself_is_not_a_worker() -> None:
    pool = ActingContext.for_machine(
        machine_id=MACHINE,
        credential_id=CREDENTIAL,
        org_id=OPERATOR,
        label="pool",
        serves_every_org=True,
    )
    assert pool.is_machine and not pool.is_machine_worker
    assert pool.serves(ORG_A) and pool.serves(ORG_B)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda w: ActingContext(acting_principal=w, served_org_ids=frozenset({ORG_B})),
            id="serving-another-org",
        ),
        pytest.param(
            lambda w: ActingContext(acting_principal=w, serves_every_org=True),
            id="serving-every-org",
        ),
    ],
)
def test_a_worker_context_that_reaches_past_its_org_cannot_be_built(build: object) -> None:
    worker = Principal(
        PrincipalKind.MACHINE,
        str(MACHINE),
        ORG_A,
        credential=CredentialKind.MACHINE_WORKER,
        credential_id=str(CREDENTIAL),
    )
    with pytest.raises(ValueError, match="bound to its one org"):
        build(worker)


def test_only_a_machine_can_carry_the_worker_credential() -> None:
    agent_like = Principal(
        PrincipalKind.SERVICE,
        str(uuid4()),
        ORG_A,
        credential=CredentialKind.MACHINE_WORKER,
    )
    with pytest.raises(ValueError, match="bound to its one org"):
        ActingContext(acting_principal=agent_like)


# --------------------------------------------------------------------------- #
# the socket ticket
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bound", [True, False])
def test_the_socket_ticket_says_whether_a_worker_minted_it(bound: bool) -> None:
    ticket = decode_ws_machine_ticket(
        encode_ws_machine_ticket(
            credential_id=CREDENTIAL, machine_id=MACHINE, org_id=ORG_A, org_bound=bound
        )
    )
    assert ticket.org_bound is bound


def test_a_ticket_minted_before_the_claim_existed_is_not_bound() -> None:
    now = int(time.time())
    legacy = jwt.encode(
        {
            "typ": "ws_machine_ticket",
            "cred": CREDENTIAL.hex,
            "mach": MACHINE.hex,
            "org": OPERATOR.hex,
            "jti": "j",
            "iat": now,
            "exp": now + 30,
        },
        settings.effective_jwt_secret,
        algorithm=settings.auth_jwt_alg,
    )
    assert decode_ws_machine_ticket(legacy).org_bound is False


def test_a_non_boolean_bound_claim_is_not_bound() -> None:
    now = int(time.time())
    odd = jwt.encode(
        {
            "typ": "ws_machine_ticket",
            "cred": CREDENTIAL.hex,
            "mach": MACHINE.hex,
            "org": ORG_A.hex,
            "jti": "j",
            "iat": now,
            "exp": now + 30,
            "bound": "yes",
        },
        settings.effective_jwt_secret,
        algorithm=settings.auth_jwt_alg,
    )
    assert decode_ws_machine_ticket(odd).org_bound is False


# --------------------------------------------------------------------------- #
# the machine-credential policy
# --------------------------------------------------------------------------- #

FACTS: dict[str, object] = {
    "is_machine": True,
    "credential_live": True,
    "machine_matches": True,
    "own_standing": True,
    "runs_org_workers": True,
    "org_reached": True,
}


@pytest.mark.parametrize("action", [Action.READ, Action.WRITE])
def test_the_machine_acts_on_its_standing_and_its_worker_does_not(action: Action) -> None:
    pool = ActingContext.for_machine(
        machine_id=MACHINE,
        credential_id=CREDENTIAL,
        org_id=OPERATOR,
        label="pool",
        serves_every_org=True,
    )
    worker = ActingContext.for_machine_worker(
        machine_id=MACHINE, credential_id=CREDENTIAL, org_id=ORG_A, label="pool"
    )
    own = Resource(ResourceType.MACHINE_CREDENTIAL, id="routing", org_id=ORG_A)
    assert authorize(pool, action, own, FACTS).effect is Effect.ALLOW
    refused = authorize(worker, action, own, FACTS)
    assert (refused.effect, refused.reason, refused.as_not_found) == (
        Effect.DENY,
        "org_bound_credential",
        True,
    )
    other = Resource(ResourceType.MACHINE_CREDENTIAL, id="worker", org_id=ORG_B)
    crossed = authorize(worker, action, other, FACTS)
    assert (crossed.effect, crossed.reason) == (Effect.DENY, "cross_org")


# --------------------------------------------------------------------------- #
# a person's own box
# --------------------------------------------------------------------------- #

OWNER = UUID("00000000-0000-4000-8000-0000000000f1")


def test_a_personal_box_and_its_worker_carry_the_one_person_they_serve() -> None:
    box = ActingContext.for_machine(
        machine_id=MACHINE,
        credential_id=CREDENTIAL,
        org_id=ORG_A,
        label="laptop",
        personal_owner_id=OWNER,
    )
    worker = ActingContext.for_machine_worker(
        machine_id=MACHINE,
        credential_id=CREDENTIAL,
        org_id=ORG_A,
        label="laptop",
        personal_owner_id=OWNER,
    )
    assert box.personal_owner_id == worker.personal_owner_id == OWNER
    assert box.serves(ORG_A) and not box.serves(ORG_B)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: ActingContext.for_user(
                user_id=OWNER, org_id=ORG_A, email="p@example.com"
            ).__class__(
                acting_principal=ActingContext.for_user(
                    user_id=OWNER, org_id=ORG_A, email="p@example.com"
                ).acting_principal,
                personal_owner_id=OWNER,
            ),
            id="a-person",
        ),
        pytest.param(
            lambda: ActingContext.for_machine(
                machine_id=MACHINE,
                credential_id=CREDENTIAL,
                org_id=ORG_A,
                label="x",
                serves_every_org=True,
                personal_owner_id=OWNER,
            ),
            id="a-pool-box",
        ),
        pytest.param(
            lambda: ActingContext.for_machine(
                machine_id=MACHINE,
                credential_id=CREDENTIAL,
                org_id=ORG_A,
                label="x",
                served_org_ids=frozenset({ORG_B}),
                personal_owner_id=OWNER,
            ),
            id="a-box-serving-another-org",
        ),
    ],
)
def test_only_a_one_org_machine_can_be_a_personal_box(build: object) -> None:
    with pytest.raises(ValueError, match="personal box"):
        build()
