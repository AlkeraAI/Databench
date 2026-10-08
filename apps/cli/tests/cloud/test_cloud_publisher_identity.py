"""The box's identity and the turn's principal, without a gateway.

* a :class:`MachineIdentity` comes from flags first, the environment second,
  and refuses to register — naming the env var AND the flag — when the pod
  id or the catalog code is missing; the provider alone has a default;
* its register body IS the route's request model;
* a turn is attributed to the member the relay names, the events it produces
  are remembered under that member, and a promote's receipt names them with
  the machine as the agent;
* the refusal codes after which the mirror stops publishing are exactly the
  ones that mean "not your chat", never a transient or a per-entry defect.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alkera_cli.cloud.publisher_identity import (
    ENV_MACHINE_PROVIDER,
    ENV_MACHINE_PROVIDER_POD_ID,
    ENV_MACHINE_TYPE_CODE,
    PUBLISHING_REFUSALS,
    MachineIdentity,
    MachineIdentityError,
    PublishingRefusal,
    TurnAttribution,
    is_publishing_refusal,
    receipt_principal,
    relaying_user_of,
    require_machine_identity,
)
from alkera_core.schemas.chat import (
    MessageCreated,
    PartCreated,
    SessionStatusChanged,
    TextPart,
    ToolCall,
)

_T = datetime(2026, 9, 6, tzinfo=UTC)
OPERATOR = "00000000-0000-4000-8000-000000000001"
MEMBER = "00000000-0000-4000-8000-000000000002"
MACHINE = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c222"


# --------------------------------------------------------------------------- #
# the machine identity
# --------------------------------------------------------------------------- #


def test_flags_win_over_the_environment_and_the_provider_alone_defaults() -> None:
    env = {
        ENV_MACHINE_PROVIDER: "envpod",
        ENV_MACHINE_PROVIDER_POD_ID: "pod-env",
        ENV_MACHINE_TYPE_CODE: "cpu-env",
    }
    from_env = MachineIdentity.from_env(name="box", env=env)
    assert (from_env.provider, from_env.provider_pod_id, from_env.machine_type_code) == (
        "envpod",
        "pod-env",
        "cpu-env",
    )
    from_flags = MachineIdentity.from_env(
        name="box",
        provider=" flagpod ",
        provider_pod_id=" pod-flag ",
        machine_type_code=" cpu-flag ",
        env=env,
    )
    assert (from_flags.provider, from_flags.provider_pod_id, from_flags.machine_type_code) == (
        "flagpod",
        "pod-flag",
        "cpu-flag",
    )
    bare = MachineIdentity.from_env(name="box", env={})
    assert bare.provider == "self_hosted", "the one fact with a default"
    assert bare.provider_pod_id == "" and bare.machine_type_code == ""


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        pytest.param(
            {},
            [
                "ALKERA_MACHINE_PROVIDER_POD_ID (--provider-pod-id)",
                "ALKERA_MACHINE_TYPE_CODE (--machine-type-code)",
            ],
            id="both-missing",
        ),
        pytest.param(
            {ENV_MACHINE_TYPE_CODE: "cpu3c"},
            ["ALKERA_MACHINE_PROVIDER_POD_ID (--provider-pod-id)"],
            id="no-pod-id",
        ),
        pytest.param(
            {ENV_MACHINE_PROVIDER_POD_ID: "pod-1"},
            ["ALKERA_MACHINE_TYPE_CODE (--machine-type-code)"],
            id="no-type-code",
        ),
        pytest.param(
            {ENV_MACHINE_PROVIDER_POD_ID: "   ", ENV_MACHINE_TYPE_CODE: "cpu3c"},
            ["ALKERA_MACHINE_PROVIDER_POD_ID (--provider-pod-id)"],
            id="whitespace-is-missing",
        ),
    ],
)
def test_a_box_that_cannot_register_is_refused_naming_the_env_and_the_flag(
    env: dict[str, str], missing: list[str]
) -> None:
    identity = MachineIdentity.from_env(name="box", env=env)
    assert identity.missing() == missing
    with pytest.raises(MachineIdentityError) as refused:
        require_machine_identity(identity)
    message = str(refused.value)
    for gap in missing:
        assert gap in message
    assert "cannot register" in message and "must not serve" in message


def test_a_complete_identity_names_what_the_register_route_needs() -> None:
    identity = MachineIdentity.from_env(
        name="demo-box",
        env={ENV_MACHINE_PROVIDER_POD_ID: "pod-7", ENV_MACHINE_TYPE_CODE: "cpu3c"},
    )
    assert require_machine_identity(identity) is identity
    assert (
        identity.provider,
        identity.provider_pod_id,
        identity.name,
        identity.machine_type_code,
    ) == ("self_hosted", "pod-7", "demo-box", "cpu3c")


# --------------------------------------------------------------------------- #
# the turn's principal
# --------------------------------------------------------------------------- #


def _tool_call(event_id: str) -> ToolCall:
    return ToolCall(
        event_id=event_id,
        time=_T,
        session_id="s",
        tool_call_id="c1",
        message_id="m1",
        tool_name="sql.query",
        tool_kind="read",
        input={},
        status="running",
    )


def _part(event_id: str) -> PartCreated:
    return PartCreated(
        event_id=event_id,
        time=_T,
        session_id="s",
        part=TextPart(part_id="p1", message_id="m1", text="x"),
    )


def test_a_relay_names_its_sender_or_nobody() -> None:
    assert relaying_user_of({"kind": "prompt", "user_id": MEMBER}) == MEMBER
    assert relaying_user_of({"kind": "prompt"}) is None
    assert relaying_user_of({"kind": "prompt", "user_id": ""}) is None
    assert relaying_user_of({"kind": "prompt", "user_id": 42}) is None


def test_events_of_a_turn_are_attributed_to_the_member_who_asked() -> None:
    attribution = TurnAttribution(fallback_user_id=OPERATOR)
    assert attribution.current == OPERATOR, "before any prompt: the box's own user"
    attribution.begin(MEMBER)
    assert attribution.current == MEMBER
    attribution.observe(_tool_call("call-1"))
    attribution.observe(_part("res-1"))
    # Status and message events are not what a promote names; they leave no trace.
    attribution.observe(
        SessionStatusChanged(event_id="st-1", time=_T, session_id="s", status="running")
    )
    attribution.observe(
        MessageCreated(event_id="mc-1", time=_T, session_id="s", message_id="m1", role="assistant")
    )
    assert attribution.user_for("call-1") == MEMBER
    assert attribution.user_for("res-1") == MEMBER
    assert attribution.user_for("st-1") == OPERATOR
    assert attribution.user_for("never-seen") == OPERATOR

    # The next prompt comes from someone else: their turn, their events.
    attribution.begin(OPERATOR)
    attribution.observe(_part("res-2"))
    assert attribution.user_for("res-2") == OPERATOR
    assert attribution.user_for("res-1") == MEMBER, "an earlier turn keeps its member"

    # A prompt that names nobody falls back to the box's user.
    attribution.begin(None)
    attribution.observe(_part("res-3"))
    assert attribution.user_for("res-3") == OPERATOR


def test_an_explicit_attribution_and_the_bounded_memory() -> None:
    attribution = TurnAttribution(fallback_user_id=OPERATOR, memory=2)
    attribution.attribute("run-1-result", MEMBER)
    attribution.attribute("run-2-result", None)
    assert attribution.user_for("run-1-result") == MEMBER
    assert attribution.user_for("run-2-result") == OPERATOR
    attribution.attribute("run-3-result", MEMBER)
    assert attribution.user_for("run-1-result") == OPERATOR, "the oldest was forgotten"
    assert attribution.user_for("run-3-result") == MEMBER


def test_the_receipt_chain_is_the_member_then_the_machine() -> None:
    principal = receipt_principal(user_id=MEMBER, agent_id=MACHINE)
    assert (principal.user_id, principal.agent_id) == (MEMBER, MACHINE)
    assert principal.chain == [MEMBER, MACHINE]
    unregistered = receipt_principal(user_id="", agent_id="chat-1")
    assert unregistered.chain == ["chat-1"], "an empty link is not a link"


# --------------------------------------------------------------------------- #
# the refusals that end publishing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("code", "final"),
    [
        pytest.param("forbidden", True, id="forbidden"),
        pytest.param("not_found", True, id="not-found"),
        pytest.param("quota_exceeded", True, id="quota"),
        pytest.param("blocked", True, id="blocked"),
        pytest.param("disconnected", False, id="disconnected-is-retried"),
        pytest.param("stale_epoch", False, id="stale-epoch-is-retried"),
        pytest.param("timeout", False, id="timeout-is-retried"),
        pytest.param("op_too_large", False, id="too-large-is-shrunk"),
        pytest.param("bad_op", False, id="bad-op-is-one-entrys-defect"),
        pytest.param("unsupported_kind", False, id="unsupported-kind-is-one-entrys-defect"),
        pytest.param("closed", False, id="closed"),
    ],
)
def test_only_a_verdict_on_the_publisher_ends_publishing(code: str, final: bool) -> None:
    assert is_publishing_refusal(code) is final
    assert (code in PUBLISHING_REFUSALS) is final


def test_a_refusal_reads_as_its_code_and_message() -> None:
    assert PublishingRefusal("forbidden", "only the publisher").reason == (
        "forbidden: only the publisher"
    )
    assert PublishingRefusal("not_found", "").reason == "not_found"
