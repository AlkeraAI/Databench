"""Who a lease may belong to, derived from one request.

The fence compares one value, and this is where that value comes from. The
table below is the whole contract: a credential the server authenticated is
itself, a box is the machine it PROVED, and an agent assertion on its own is
nobody — which matters because the assertion is two headers any member may put
on their own session, naming an id the product publishes.
"""

from __future__ import annotations

import uuid

import pytest
from alkera_core.authz.enums import CredentialKind
from alkera_core.authz.principal import ActingContext
from alkera_core.files.leases import HolderIdentity, holder_identity

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
USER = uuid.UUID("22222222-2222-4222-8222-222222222222")
TOKEN = uuid.UUID("33333333-3333-4333-8333-333333333333")
MACHINE = uuid.UUID("44444444-4444-4444-8444-444444444444")


def _user() -> ActingContext:
    return ActingContext.for_user(user_id=USER, org_id=ORG, email="ana@example.com")


def _pat() -> ActingContext:
    return ActingContext.for_pat(
        token_id=TOKEN, org_id=ORG, label="deploy", user_id=USER, email="ana@example.com"
    )


def _service() -> ActingContext:
    return ActingContext.for_service(
        token_id=TOKEN, org_id=ORG, label="ci", credential=CredentialKind.CI_TOKEN
    )


def _agent(session_id: str) -> ActingContext:
    return ActingContext.for_agent(
        user_id=USER, org_id=ORG, email="ana@example.com", session_id=session_id
    )


@pytest.mark.parametrize(
    ("ctx", "verified", "expected"),
    [
        pytest.param(
            _user(), None, HolderIdentity(kind="user", id=USER), id="a-cookie-or-bearer-jwt"
        ),
        pytest.param(
            _pat(), None, HolderIdentity(kind="pat", id=TOKEN), id="a-personal-access-token"
        ),
        pytest.param(
            _service(), None, HolderIdentity(kind="service", id=TOKEN), id="a-ci-or-proxy-token"
        ),
        pytest.param(
            _agent(str(MACHINE)),
            str(MACHINE),
            HolderIdentity(kind="machine", id=MACHINE),
            id="a-box-whose-assertion-was-verified",
        ),
        pytest.param(_agent(str(MACHINE)), None, None, id="an-agent-nobody-verified-holds-nothing"),
        pytest.param(
            _agent(str(MACHINE)),
            str(uuid.uuid4()),
            None,
            id="a-box-verified-as-some-other-machine-holds-nothing",
        ),
        pytest.param(
            _agent("session-1"), "session-1", None, id="a-verified-id-that-is-no-machine-uuid"
        ),
    ],
)
def test_the_fencing_identity_of_one_request(
    ctx: ActingContext, verified: str | None, expected: HolderIdentity | None
) -> None:
    """The derivation table, credential by credential."""
    assert holder_identity(ctx, verified_machine_id=verified) == expected


def test_an_agent_assertion_never_becomes_the_user_it_rides_on() -> None:
    """The assertion buys nothing — it does not even fall back to the human.

    A box holds the folder; the person whose session the agent runs in does
    not, and must not, inherit that holdership by asserting an id. Falling back
    to the delegating user would be worse than useless: every member could then
    write under any lease their own user happened to hold by sending two
    headers, and the agent path would quietly widen rather than narrow.
    """
    assert holder_identity(_agent(str(MACHINE))) is None
    assert holder_identity(_user()) == HolderIdentity(kind="user", id=USER)


def test_a_user_and_a_machine_that_share_a_uuid_are_not_the_same_holder() -> None:
    """The kind is not decoration. A ``users`` row and a ``compute_allocations``
    row are drawn from different tables, so the same uuid can name one of each;
    a fence comparing the uuid alone would let one stand in for the other."""
    person = ActingContext.for_user(user_id=MACHINE, org_id=ORG, email="ana@example.com")
    box = _agent(str(MACHINE))

    as_person = holder_identity(person)
    as_box = holder_identity(box, verified_machine_id=str(MACHINE))

    assert as_person is not None and as_box is not None
    assert as_person.id == as_box.id
    assert as_person != as_box
