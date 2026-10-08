"""How a refused provision is tried again: one policy per failure kind.

A provider classifies its own refusal (:data:`~alkera_core.compute.provider.FailureKind`);
this module decides what follows from the kind alone, so every provider gets
the same behavior for the same kind of answer:

- ``transient`` (throttled, a server error, a timeout): the same target again,
  up to three more times, after 2, 6 and 18 seconds;
- ``capacity`` (no hardware of that shape free): never the same target twice;
  each fallback the machine type lists is tried once, in order, at once;
- ``quota``, ``invalid``, ``auth``, ``unknown``: not tried again, because asking
  the same provider the same thing changes nothing.

A *target* is the first choice (index ``-1``) or one fallback: a mapping of
provider-specific overrides from ``provider_config["fallbacks"]`` (another data
center, an alternative GPU id of the same class). Nothing here knows what the
overrides mean; the provider applies them to its own create.

Pure: :func:`next_attempt` maps the last attempt and its failure to the next
attempt, or ``None`` to stop. :func:`provision_with_policy` drives a create
through it with the sleep injected, so a test runs the whole schedule
instantly and reads the delays it was asked to wait. A new kind of failure, or
a different schedule for one, is a :func:`register_retry_policy` call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from alkera_core.compute.provider import (
    CAPACITY_FAILURE,
    TRANSIENT_FAILURE,
    ComputeProviderError,
)
from alkera_core.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")

#: The target index of the first choice (no overrides).
PRIMARY = -1


@dataclass(frozen=True)
class RetryPolicy:
    """What follows one kind of failure.

    ``delays`` are the waits before each retry of the SAME target: their count
    is how many retries a target gets. ``next_target`` moves to the next
    fallback instead (at once, never back to a target already tried)."""

    delays: tuple[float, ...] = ()
    next_target: bool = False


_POLICIES: dict[str, RetryPolicy] = {
    TRANSIENT_FAILURE: RetryPolicy(delays=(2.0, 6.0, 18.0)),
    CAPACITY_FAILURE: RetryPolicy(next_target=True),
}

#: Every kind not registered is not tried again.
NO_RETRY = RetryPolicy()


def register_retry_policy(kind: str, policy: RetryPolicy) -> None:
    """Set what follows a failure of ``kind``. Replaces an earlier one."""
    if not kind:
        raise ValueError("a failure kind cannot be empty")
    _POLICIES[kind] = policy


def policy_for(kind: str) -> RetryPolicy:
    """The policy for ``kind``; :data:`NO_RETRY` for one with none."""
    return _POLICIES.get(kind, NO_RETRY)


@dataclass(frozen=True)
class Attempt:
    """One try at a create: which target, its overrides, and the wait first.

    ``number`` counts every attempt of the whole provision from 1, which is
    what ``provision_attempts`` records; ``retry`` counts the retries of this
    target alone."""

    number: int
    target: int = PRIMARY
    overrides: Mapping[str, Any] = field(default_factory=dict)
    retry: int = 0
    delay_seconds: float = 0.0


def first_attempt() -> Attempt:
    """The first choice, at once."""
    return Attempt(number=1)


def next_attempt(
    kind: str, attempt: Attempt, fallbacks: Sequence[Mapping[str, Any]]
) -> Attempt | None:
    """The attempt after ``attempt`` failed with ``kind``, or ``None`` to stop.

    ``kind`` decides through its registered :class:`RetryPolicy`. A target
    with retries left is tried again after the next delay; a policy that moves
    on takes the next fallback in order. A move never returns to a target
    already tried, and a target's retry count starts over on a new target."""
    policy = policy_for(kind)
    if policy.next_target:
        target = attempt.target + 1
        if target >= len(fallbacks):
            return None
        return Attempt(number=attempt.number + 1, target=target, overrides=dict(fallbacks[target]))
    if attempt.retry < len(policy.delays):
        return Attempt(
            number=attempt.number + 1,
            target=attempt.target,
            overrides=attempt.overrides,
            retry=attempt.retry + 1,
            delay_seconds=policy.delays[attempt.retry],
        )
    return None


def fallbacks_of(provider_config: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The fallback targets a machine type lists, in order.

    ``provider_config["fallbacks"]`` is a list of override mappings; anything
    else (absent, not a list, an entry that is not a mapping) is no fallback,
    never a guess."""
    raw = (provider_config or {}).get("fallbacks")
    if not isinstance(raw, list):
        return []
    return [dict(entry) for entry in raw if isinstance(entry, Mapping)]


Sleep = Callable[[float], Awaitable[None]]


@dataclass
class ProvisionOutcome(Generic[T]):
    """What a driven provision did: its result, and how many attempts it took."""

    result: T
    attempts: int
    target: int


class ProvisionExhaustedError(ComputeProviderError):
    """Every attempt the policy allowed failed. Carries the last failure's
    kind and how many attempts were made."""

    def __init__(self, last: ComputeProviderError, *, attempts: int) -> None:
        super().__init__(str(last), status_code=last.status_code, kind=last.kind)
        self.last = last
        self.attempts = attempts


async def provision_with_policy(
    create: Callable[[Attempt], Awaitable[T]],
    *,
    fallbacks: Sequence[Mapping[str, Any]],
    sleep: Sleep,
    first: Attempt | None = None,
    on_failure: Callable[[Attempt, ComputeProviderError], None] | None = None,
) -> ProvisionOutcome[T]:
    """Call ``create`` for each attempt the policy allows until one succeeds.

    ``sleep`` is awaited with each attempt's delay before it (a test passes a
    recorder). Raises :class:`ProvisionExhaustedError` with the last failure
    when the policy stops; anything that is not a
    :class:`~alkera_core.compute.provider.ComputeProviderError` propagates at
    once, because only a provider's classified answer has a policy."""
    attempt = first or first_attempt()
    while True:
        if attempt.delay_seconds > 0:
            await sleep(attempt.delay_seconds)
        try:
            result = await create(attempt)
        except ComputeProviderError as exc:
            log.info(
                "compute.provision.attempt",
                attempt=attempt.number,
                target=attempt.target,
                kind=exc.kind,
                error=str(exc)[:300],
            )
            if on_failure is not None:
                on_failure(attempt, exc)
            following = next_attempt(exc.kind, attempt, fallbacks)
            if following is None:
                raise ProvisionExhaustedError(exc, attempts=attempt.number) from exc
            attempt = following
            continue
        return ProvisionOutcome(result=result, attempts=attempt.number, target=attempt.target)


__all__ = [
    "NO_RETRY",
    "PRIMARY",
    "Attempt",
    "ProvisionExhaustedError",
    "ProvisionOutcome",
    "RetryPolicy",
    "Sleep",
    "fallbacks_of",
    "first_attempt",
    "next_attempt",
    "policy_for",
    "provision_with_policy",
    "register_retry_policy",
]
