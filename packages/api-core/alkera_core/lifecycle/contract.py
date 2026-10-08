"""Every long-running operation ends: the declared shape of a state machine.

A non-terminal state is written by one actor (a box, a workflow, a lease
holder) and nothing guaranteed the actor came back to finish it: a turn read
"Working…" for good after its machine was stopped, a move waited in "waking"
on a deadline equal to the lease it waited for, a replaced machine slept on
and kept billing storage. Each machine had its own deadlines, chosen without
reference to the deadlines they depended on.

So a machine is declared here, once, and :func:`register` refuses it unless
every state it can rest in is reachable within a stated bound:

- every non-rest state has a :class:`Bound` (how long, which state it lands
  in, which outcome code it records) or an :class:`Exempt` that names what it
  waits on (a person, or another machine's bound);
- following each bound from any state reaches a rest state with no cycle, so
  :func:`longest_path_to_rest` is finite;
- a bound that waits on another (``must_exceed``) is strictly longer than that
  bound plus how late its ender may run, so two equal deadlines cannot be
  registered;
- every failure code the machine records has exactly one :class:`Outcome`,
  so two operations that fail the same way end in the same place.

Pure: no I/O. The enders that apply a bound live beside the state they own;
this module is the contract they are held to, and the registry the gates walk.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType
from typing import Final

#: How late a sweeping ender may run past a bound: its schedule's period.
DEFAULT_SLACK: Final = timedelta(seconds=60)

Duration = timedelta | Callable[[], timedelta]


def _resolve(value: Duration) -> timedelta:
    return value() if callable(value) else value


@dataclass(frozen=True, slots=True)
class Bound:
    """How long a state may last before its ender moves it on."""

    after: Duration
    #: The state the ender moves it to; must be an edge out of the state.
    lands_in: str
    #: The outcome code it records (a key of the machine's ``outcomes``), or
    #: ``""`` for a bound that is the state's ordinary way on.
    outcome: str = ""
    #: ``"machine.state"`` names of bounds this one waits on.
    must_exceed: tuple[str, ...] = ()
    #: How late its ender may run.
    slack: timedelta = DEFAULT_SLACK
    #: Who moves it on, in a sentence (for the registry's readers).
    ender: str = ""

    @property
    def duration(self) -> timedelta:
        return _resolve(self.after)


@dataclass(frozen=True, slots=True)
class Exempt:
    """A non-rest state with no bound of its own, and why that is safe."""

    reason: str
    #: ``"a person"``, or the ``"machine.state"`` whose bound covers it.
    waits_on: str


@dataclass(frozen=True, slots=True)
class Gap:
    """A non-rest state that nothing on the server is bound to move on yet.

    Declared rather than hidden: the registry gate lists every gap and the
    list may only shrink, so closing one means writing its ender and turning
    the gap into a :class:`Bound`."""

    reason: str


@dataclass(frozen=True, slots=True)
class Interval:
    """A deadline that is not a state's (a grant delay, a hand-back wait) but
    that a state's bound may have to exceed."""

    after: Duration
    slack: timedelta = timedelta(0)

    @property
    def duration(self) -> timedelta:
        return _resolve(self.after)


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a machine does when it ends for one failure code: where it lands
    and the effects applied, as data, the same wherever the ending starts."""

    lands_in: str
    effects: tuple[str, ...] = ()
    #: What the person is told.
    words: str = ""


@dataclass(frozen=True, slots=True)
class StateMachine:
    name: str
    states: frozenset[str]
    #: Terminal states, and the resting states of a cyclic machine.
    rest: frozenset[str]
    edges: Mapping[str, frozenset[str]]
    bounds: Mapping[str, Bound | Exempt | Gap]
    outcomes: Mapping[str, Outcome] = field(default_factory=dict)
    #: The ORM table and column(s) the state is persisted in, for the gate
    #: that holds every state column to a registered machine.
    columns: tuple[str, ...] = ()
    #: The longest a non-rest state may take to reach rest, all bounds and
    #: their slack summed; registration fails past it.
    ceiling: timedelta = timedelta(hours=24)


class LifecycleError(ValueError):
    """A state machine that cannot be registered."""


_MACHINES: dict[str, StateMachine] = {}
_INTERVALS: dict[str, Interval] = {}


def register_interval(name: str, interval: Interval) -> Interval:
    """Register a named deadline a bound may have to exceed (``"files.grant_delay"``)."""
    if name in _INTERVALS:
        raise LifecycleError(f"interval {name!r} is already registered")
    _INTERVALS[name] = interval
    return interval


def _deadline_of(name: str, machine: StateMachine) -> Bound | Interval:
    if name in _INTERVALS:
        return _INTERVALS[name]
    machine_name, _, state = name.partition(".")
    owner = machine if machine_name == machine.name else _MACHINES.get(machine_name)
    found = owner.bounds.get(state) if owner is not None else None
    if not isinstance(found, Bound):
        raise LifecycleError(f"{machine.name} waits on {name!r}, which is no registered bound")
    return found


def longest_path_to_rest(machine: StateMachine, state: str) -> timedelta:
    """The longest a machine in ``state`` takes to rest, following each state's
    bound (plus its ender's slack). An exempt state counts as zero here: what
    it waits on is bounded elsewhere or is a person."""
    total = timedelta(0)
    seen: set[str] = set()
    current = state
    while current not in machine.rest:
        if current in seen:
            raise LifecycleError(f"{machine.name}: bounds from {state!r} loop at {current!r}")
        seen.add(current)
        rule = machine.bounds.get(current)
        if rule is None:
            raise LifecycleError(f"{machine.name}.{current} has no bound and no exemption")
        if isinstance(rule, Exempt | Gap):
            return total
        total += rule.duration + rule.slack
        current = rule.lands_in
    return total


def _check(machine: StateMachine) -> None:
    name = machine.name
    if not machine.rest:
        raise LifecycleError(f"{name} has no rest state")
    unknown = (set(machine.edges) | set(machine.rest) | set(machine.bounds)) - machine.states
    for targets in machine.edges.values():
        unknown |= set(targets) - machine.states
    if unknown:
        raise LifecycleError(f"{name} names states it does not declare: {sorted(unknown)}")
    for state in sorted(machine.states - machine.rest):
        rule = machine.bounds.get(state)
        if rule is None:
            raise LifecycleError(f"{name}.{state} has no bound and no exemption")
        if isinstance(rule, Exempt):
            if not rule.reason.strip() or not rule.waits_on.strip():
                raise LifecycleError(
                    f"{name}.{state}: an exemption names its reason and what it waits on"
                )
            continue
        if isinstance(rule, Gap):
            if not rule.reason.strip():
                raise LifecycleError(f"{name}.{state}: a gap says what is missing")
            continue
        if rule.lands_in not in machine.edges.get(state, frozenset()):
            raise LifecycleError(f"{name}.{state} lands in {rule.lands_in!r}, which is not an edge")
        if rule.outcome and rule.outcome not in machine.outcomes:
            raise LifecycleError(f"{name}.{state} records {rule.outcome!r}, which has no outcome")
        if rule.duration <= timedelta(0):
            raise LifecycleError(f"{name}.{state} has a bound of no length")
        for other in rule.must_exceed:
            needed = _deadline_of(other, machine)
            if rule.duration <= needed.duration + needed.slack:
                raise LifecycleError(
                    f"{name}.{state} ({rule.duration}) must exceed {other} "
                    f"({needed.duration} plus {needed.slack} of slack)"
                )
    for code, outcome in machine.outcomes.items():
        if outcome.lands_in not in machine.states:
            raise LifecycleError(f"{name}: outcome {code!r} lands in an undeclared state")
    for state in sorted(machine.states - machine.rest):
        longest = longest_path_to_rest(machine, state)
        if longest > machine.ceiling:
            raise LifecycleError(
                f"{name}.{state} reaches rest in {longest}, past {machine.ceiling}"
            )


def register(machine: StateMachine) -> StateMachine:
    """Register ``machine`` after checking it ends from every state."""
    if machine.name in _MACHINES:
        raise LifecycleError(f"state machine {machine.name!r} is already registered")
    if "." in machine.name:
        raise LifecycleError("a state machine's name has no dot")
    _check(machine)
    _MACHINES[machine.name] = machine
    return machine


def recheck(name: str) -> None:
    """Check a registered machine again, against today's settings: a bound read
    from a setting can be configured below what it must exceed."""
    _check(_MACHINES[name])


def registered() -> Mapping[str, StateMachine]:
    return MappingProxyType(dict(_MACHINES))


def gaps() -> dict[str, str]:
    """Every declared gap, ``"machine.state" -> reason``."""
    return {
        f"{machine.name}.{state}": rule.reason
        for machine in _MACHINES.values()
        for state, rule in machine.bounds.items()
        if isinstance(rule, Gap)
    }


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
    "recheck",
    "register",
    "register_interval",
    "registered",
]
