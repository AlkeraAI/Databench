"""Every long-running operation ends: the state machine contract and the
product's registry of them (:mod:`alkera_core.lifecycle.machines`)."""

from alkera_core.lifecycle import machines
from alkera_core.lifecycle.contract import (
    DEFAULT_SLACK,
    Bound,
    Exempt,
    Gap,
    Interval,
    LifecycleError,
    Outcome,
    StateMachine,
    gaps,
    longest_path_to_rest,
    recheck,
    register,
    register_interval,
    registered,
)

__all__ = [
    "DEFAULT_SLACK",
    "Bound",
    "Exempt",
    "Gap",
    "Interval",
    "LifecycleError",
    "Outcome",
    "StateMachine",
    "gaps",
    "longest_path_to_rest",
    "machines",
    "recheck",
    "register",
    "register_interval",
    "registered",
]
