"""A TOTP code that has been accepted once must not verify a second time.

Skew tolerance means one 6-digit code is valid across three 30-second steps —
roughly a 90-second band in which a code captured by a phishing relay, a
screenshot, or a shoulder-surf can be presented again. RFC 6238 §5.2 requires the
verifier to refuse a code it has already validated, and only a record of what was
accepted can do that: narrowing the window shrinks the band but never closes it.

These pin the record-keeping contract — the accepted step is reported back, and a
step at or before the last accepted one is refused — without narrowing the skew
tolerance a legitimate user with a drifting clock depends on.
"""

from __future__ import annotations

import pytest
from alkera_core.auth import totp

# RFC 6238 §B uses the ASCII secret "12345678901234567890" → this base32.
_RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

# t=59 sits in step 1; the RFC's published code for that step.
_STEP_1_CODE = "287082"


def _code_for(step: int) -> str:
    return totp._hotp(_RFC_SECRET, step)


def test_the_accepted_step_is_reported_so_a_caller_can_record_it() -> None:
    """Single-use is only possible if the verifier says WHICH step it accepted."""
    assert totp.matching_counter(_RFC_SECRET, _STEP_1_CODE, at=59, window=1) == 1
    # Accepted through the skew band from a neighbouring step, still reported as
    # the step the code actually belongs to.
    assert totp.matching_counter(_RFC_SECRET, _STEP_1_CODE, at=89, window=1) == 1
    assert totp.matching_counter(_RFC_SECRET, "000000", at=59, window=1) is None


@pytest.mark.parametrize(
    "at",
    [
        pytest.param(59, id="replay-in-the-same-step"),
        pytest.param(89, id="replay-one-step-later"),
        pytest.param(29, id="replay-one-step-earlier"),
    ],
)
def test_a_recorded_code_is_refused_everywhere_in_its_skew_band(at: int) -> None:
    """The whole ~90-second acceptance band is closed to a spent code, not just
    the step it was first presented in — that band is exactly the replay window."""
    assert totp.matching_counter(_RFC_SECRET, _STEP_1_CODE, at=at, window=1) == 1
    assert (
        totp.matching_counter(_RFC_SECRET, _STEP_1_CODE, at=at, window=1, last_used_counter=1)
        is None
    )
    assert not totp.verify(_RFC_SECRET, _STEP_1_CODE, at=at, window=1, last_used_counter=1)


def test_recording_a_step_does_not_void_the_next_code() -> None:
    """Burning a code must cost the user nothing beyond that code: the very next
    step still authenticates, so single-use is not a lockout."""
    assert (
        totp.matching_counter(_RFC_SECRET, _code_for(2), at=59, window=1, last_used_counter=1) == 2
    )
    assert totp.verify(_RFC_SECRET, _code_for(2), at=59, window=1, last_used_counter=1)


def test_an_older_step_is_refused_even_when_it_was_never_itself_spent() -> None:
    """A step BEFORE the last accepted one is refused too. Codes are only ever
    accepted forwards, so a captured earlier code cannot be spent by waiting for
    the clock to bring it back into the window."""
    assert (
        totp.matching_counter(_RFC_SECRET, _STEP_1_CODE, at=89, window=1, last_used_counter=2)
        is None
    )
    assert (
        totp.matching_counter(_RFC_SECRET, _code_for(3), at=89, window=1, last_used_counter=2) == 3
    )


def test_no_record_keeps_the_plain_skew_tolerant_behaviour() -> None:
    """A caller that persists nothing gets exactly what it got before, so the
    record is opt-in rather than a silent behaviour change."""
    assert totp.verify(_RFC_SECRET, _STEP_1_CODE, at=59, window=1)
    assert totp.verify(_RFC_SECRET, _STEP_1_CODE, at=89, window=1)
    assert not totp.verify(_RFC_SECRET, _STEP_1_CODE, at=300, window=1)


@pytest.mark.parametrize(
    "code",
    [
        pytest.param("12345", id="too-short"),
        pytest.param("1234567", id="too-long"),
        pytest.param("abcdef", id="non-digit"),
        pytest.param("", id="empty"),
    ],
)
def test_malformed_input_reports_no_step(code: str) -> None:
    assert totp.matching_counter(_RFC_SECRET, code, at=59) is None


def test_a_step_at_the_unix_epoch_is_never_negative() -> None:
    """The window reaches below step 0 near the epoch; those steps are skipped
    rather than packed into a negative counter."""
    assert totp.matching_counter(_RFC_SECRET, _code_for(0), at=0, window=1) == 0
