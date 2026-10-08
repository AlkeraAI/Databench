"""How long a wait that asserts nothing may sit before it is called a hang.

``_live_window`` covers the other kind of wait: a deadline that IS the claim —
"a file written on the box reaches the member inside the window a person would
call live" — where the number is the thing being proven and widening it weakens
the test.

The waits here prove nothing by their length. A test that sends a frame and
waits for the server's answer is asserting that the answer comes and what it
says, never that it came inside eight seconds; a test that polls a table until
the row is there is asserting the row. The number attached to such a wait
exists only so a wait that will NEVER end fails naming what it waited for,
instead of stalling the run until the session timeout ends it with a stack
dump and no sentence.

Pinned to an idle box's clock that number stops being a safety net and becomes
a second, unstated claim — about the host rather than the product. That is the
whole failure mode this module exists for: six socket cases went red on a
full-suite run at load 27, and the live folder's fixture errored setting up,
with nothing about the gateway or the drive changed.

So the number is not chosen here either. It is a share of the per-test budget
the run already sets (``timeout`` in the root ``pyproject.toml``, which
pytest-timeout also reads from ``PYTEST_TIMEOUT``), which keeps two properties
a hand-picked constant cannot: a wait always fails before the session timeout
does, so the failure still says what it was waiting for; and a run that gives
its tests more time gives its waits more time with them, from the one knob
that already governs both. ``ALKERA_TEST_WAIT_SCALE`` widens or narrows every
such wait on top of that, for a run that wants to measure rather than tolerate.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

#: Multiplies every ceiling below. For a run that wants them tighter (a quiet
#: box bisecting a real hang) or looser (a machine slower than the gate's).
SCALE_ENV = "ALKERA_TEST_WAIT_SCALE"

#: pytest-timeout's own environment override, which outranks the ini when set.
TIMEOUT_ENV = "PYTEST_TIMEOUT"

#: What a test's budget is taken to be when the ini cannot be read — only a
#: source checkout without its ``pyproject.toml`` gets here, and a ceiling is
#: still better than none.
FALLBACK_TEST_TIMEOUT = 600.0

#: The share of one test's whole budget a single wait may take. A tenth leaves
#: room for the several waits a test makes in sequence — a subscribe, an ack, a
#: roster, a frame — and still fires with its own message long before the
#: session timeout fires with none.
DEFAULT_SHARE = 0.1

_ROOT = Path(__file__).resolve().parents[3]


def _configured_timeout() -> float:
    """The per-test budget this run is under.

    Read rather than restated: a copy of the number here would go stale the
    first time the root ini moved, and a ceiling ABOVE the session timeout is
    exactly the failure this module is meant to prevent.
    """
    from_env = os.environ.get(TIMEOUT_ENV)
    if from_env:
        try:
            return float(from_env)
        except ValueError:
            pass
    try:
        with (_ROOT / "pyproject.toml").open("rb") as handle:
            configured = tomllib.load(handle)["tool"]["pytest"]["ini_options"]["timeout"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return FALLBACK_TEST_TIMEOUT
    return float(configured)


def wait_scale() -> float:
    """What this run multiplies a test-only wait by. One unless asked."""
    raw = os.environ.get(SCALE_ENV)
    if not raw:
        return 1.0
    scale = float(raw)
    if scale <= 0:
        raise ValueError(f"{SCALE_ENV} must be positive, got {raw!r}")
    return scale


def wait_ceiling(share: float = DEFAULT_SHARE) -> float:
    """The safety net for one wait on a boundary, in seconds.

    ``share`` is of the per-test budget, so a caller that waits on several
    boundaries in a row can take a smaller slice of it and still leave the
    session timeout as the backstop it is meant to be.
    """
    return _configured_timeout() * share * wait_scale()


__all__ = [
    "DEFAULT_SHARE",
    "FALLBACK_TEST_TIMEOUT",
    "SCALE_ENV",
    "TIMEOUT_ENV",
    "wait_ceiling",
    "wait_scale",
]
