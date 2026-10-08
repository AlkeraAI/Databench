"""Every word the compute plane can put on the wire is a word the wire admits.

The row's ``state`` column is bounded by ``COMPUTE_ALLOCATION_STATES``; a
machine's reachability by ``MachineStatus`` / ``MachineState``. The response
models spell those vocabularies again as ``Literal`` types — for the SDKs and
the console — and a literal that lags the source is a 500 on the read: a
sleeping machine's row failed ``MachineRow.state`` validation the moment
``asleep`` joined the lifecycle, and the console could not show it at all.
These pins make a new state fail here, at import time of the suite, rather
than on the first row that reaches it.
"""

from __future__ import annotations

from typing import get_args

from alkera_core.compute.machines import MachineState, MachineStatus
from alkera_core.models.compute import COMPUTE_ALLOCATION_STATES
from alkera_core.schemas import compute, compute_machines, machine_admin

LIVE = set(get_args(MachineStatus))
STATES = set(get_args(MachineState))


def test_the_console_row_state_admits_every_lifecycle_state() -> None:
    assert set(get_args(compute_machines.MachineStateLiteral)) == set(COMPUTE_ALLOCATION_STATES)


def test_the_console_liveness_admits_every_machine_state() -> None:
    assert set(get_args(compute_machines.LivenessLiteral)) == STATES


def test_the_banner_state_admits_every_machine_state_and_the_pool() -> None:
    assert set(get_args(compute.MachineStateLiteral)) == STATES | {"pool"}


def test_the_machine_read_status_admits_every_reachability() -> None:
    assert set(get_args(compute.MachineStatusLiteral)) == LIVE


def test_the_platform_machine_read_status_admits_every_machine_state() -> None:
    assert set(get_args(machine_admin.MachineStatusLiteral)) == STATES


def test_a_machine_state_is_a_reachability_or_none() -> None:
    assert STATES == LIVE | {"none"}
