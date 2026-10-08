"""The retry schedule a refused provision follows, by failure kind.

Driven through :func:`provision_with_policy` with a scripted create and a
recording sleep, so the whole schedule runs instantly and every wait it asked
for is read back.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from alkera_core.compute.provider import (
    AUTH_FAILURE,
    CAPACITY_FAILURE,
    INVALID_FAILURE,
    QUOTA_FAILURE,
    TRANSIENT_FAILURE,
    UNKNOWN_FAILURE,
    ComputeProviderError,
)
from alkera_core.compute.provision_policy import (
    PRIMARY,
    Attempt,
    ProvisionExhaustedError,
    RetryPolicy,
    fallbacks_of,
    first_attempt,
    next_attempt,
    policy_for,
    provision_with_policy,
    register_retry_policy,
)

FALLBACKS: list[dict[str, Any]] = [
    {"dataCenterIds": ["US-TX-3"]},
    {"gpuTypeIds": ["NVIDIA L40S"]},
    {"cloudType": "COMMUNITY"},
]


class Script:
    """A create that answers from a script: an exception kind to raise, or a
    value to return. Records each attempt's target and overrides."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[int, dict[str, Any]]] = []

    async def __call__(self, attempt: Attempt) -> str:
        self.calls.append((attempt.target, dict(attempt.overrides)))
        answer = self.answers.pop(0)
        if answer.startswith("ok"):
            return answer
        raise ComputeProviderError(f"refused: {answer}", kind=answer)


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


async def test_transient_is_retried_three_times_at_2_6_18_then_fails() -> None:
    create = Script(*([TRANSIENT_FAILURE] * 4))
    sleeps = Sleeps()
    with pytest.raises(ProvisionExhaustedError) as caught:
        await provision_with_policy(create, fallbacks=FALLBACKS, sleep=sleeps)
    assert sleeps.delays == [2.0, 6.0, 18.0]
    assert caught.value.attempts == 4
    assert caught.value.kind == TRANSIENT_FAILURE
    # Every retry was the same target: a throttle is not a reason to move.
    assert [target for target, _ in create.calls] == [PRIMARY] * 4


async def test_a_transient_failure_that_clears_returns_the_result_and_the_count() -> None:
    create = Script(TRANSIENT_FAILURE, TRANSIENT_FAILURE, "ok-pod")
    sleeps = Sleeps()
    outcome = await provision_with_policy(create, fallbacks=[], sleep=sleeps)
    assert outcome.result == "ok-pod"
    assert outcome.attempts == 3
    assert outcome.target == PRIMARY
    assert sleeps.delays == [2.0, 6.0]


async def test_capacity_walks_the_fallbacks_in_order_without_waiting() -> None:
    create = Script(*([CAPACITY_FAILURE] * 4))
    sleeps = Sleeps()
    with pytest.raises(ProvisionExhaustedError) as caught:
        await provision_with_policy(create, fallbacks=FALLBACKS, sleep=sleeps)
    assert create.calls == [
        (PRIMARY, {}),
        (0, {"dataCenterIds": ["US-TX-3"]}),
        (1, {"gpuTypeIds": ["NVIDIA L40S"]}),
        (2, {"cloudType": "COMMUNITY"}),
    ]
    assert sleeps.delays == []
    assert caught.value.kind == CAPACITY_FAILURE
    assert caught.value.attempts == 4


async def test_capacity_never_tries_the_same_target_twice() -> None:
    create = Script(CAPACITY_FAILURE, CAPACITY_FAILURE, "ok")
    await provision_with_policy(create, fallbacks=FALLBACKS, sleep=Sleeps())
    targets = [target for target, _ in create.calls]
    assert len(targets) == len(set(targets))


async def test_capacity_with_no_fallbacks_stops_after_the_first_choice() -> None:
    create = Script(CAPACITY_FAILURE)
    with pytest.raises(ProvisionExhaustedError) as caught:
        await provision_with_policy(create, fallbacks=[], sleep=Sleeps())
    assert caught.value.attempts == 1
    assert len(create.calls) == 1


async def test_a_fallback_that_hits_a_throttle_retries_that_fallback() -> None:
    create = Script(CAPACITY_FAILURE, TRANSIENT_FAILURE, "ok-on-fallback")
    sleeps = Sleeps()
    outcome = await provision_with_policy(create, fallbacks=FALLBACKS, sleep=sleeps)
    assert [target for target, _ in create.calls] == [PRIMARY, 0, 0]
    assert outcome.target == 0
    assert sleeps.delays == [2.0]


async def test_a_new_target_gets_its_own_transient_retries() -> None:
    create = Script(
        TRANSIENT_FAILURE,
        TRANSIENT_FAILURE,
        TRANSIENT_FAILURE,
        CAPACITY_FAILURE,
        TRANSIENT_FAILURE,
        "ok",
    )
    sleeps = Sleeps()
    outcome = await provision_with_policy(create, fallbacks=FALLBACKS, sleep=sleeps)
    assert [target for target, _ in create.calls] == [PRIMARY, PRIMARY, PRIMARY, PRIMARY, 0, 0]
    assert sleeps.delays == [2.0, 6.0, 18.0, 2.0]
    assert outcome.attempts == 6


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param(INVALID_FAILURE, id="invalid"),
        pytest.param(AUTH_FAILURE, id="auth"),
        pytest.param(QUOTA_FAILURE, id="quota"),
        pytest.param(UNKNOWN_FAILURE, id="unknown"),
    ],
)
async def test_kinds_without_a_policy_are_never_retried(kind: str) -> None:
    create = Script(kind, "ok-never-reached")
    sleeps = Sleeps()
    with pytest.raises(ProvisionExhaustedError) as caught:
        await provision_with_policy(create, fallbacks=FALLBACKS, sleep=sleeps)
    assert len(create.calls) == 1
    assert sleeps.delays == []
    assert caught.value.kind == kind


async def test_an_error_that_is_not_a_provider_answer_propagates_untouched() -> None:
    async def create(_attempt: Attempt) -> str:
        raise ValueError("a bug, not a provider")

    with pytest.raises(ValueError):
        await provision_with_policy(create, fallbacks=FALLBACKS, sleep=Sleeps())


def test_next_attempt_numbers_every_attempt_across_targets() -> None:
    first = first_attempt()
    retry = next_attempt(TRANSIENT_FAILURE, first, FALLBACKS)
    assert retry is not None and (retry.number, retry.target, retry.retry) == (2, PRIMARY, 1)
    moved = next_attempt(CAPACITY_FAILURE, retry, FALLBACKS)
    assert moved is not None and (moved.number, moved.target, moved.retry) == (3, 0, 0)
    assert moved.delay_seconds == 0.0


def test_a_registered_policy_changes_what_follows_its_kind() -> None:
    before = policy_for(QUOTA_FAILURE)
    try:
        register_retry_policy(QUOTA_FAILURE, RetryPolicy(next_target=True))
        moved = next_attempt(QUOTA_FAILURE, first_attempt(), FALLBACKS)
        assert moved is not None and moved.target == 0
    finally:
        register_retry_policy(QUOTA_FAILURE, before)
    assert next_attempt(QUOTA_FAILURE, first_attempt(), FALLBACKS) is None


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        pytest.param(None, [], id="no-config"),
        pytest.param({}, [], id="no-key"),
        pytest.param({"fallbacks": "US-TX-3"}, [], id="not-a-list"),
        pytest.param(
            {"fallbacks": [{"dataCenterIds": ["A"]}, "junk", {"cloudType": "COMMUNITY"}]},
            [{"dataCenterIds": ["A"]}, {"cloudType": "COMMUNITY"}],
            id="non-mapping-entries-dropped",
        ),
    ],
)
def test_fallbacks_are_read_only_from_a_list_of_mappings(
    config: Mapping[str, Any] | None, expected: list[dict[str, Any]]
) -> None:
    assert fallbacks_of(config) == expected
