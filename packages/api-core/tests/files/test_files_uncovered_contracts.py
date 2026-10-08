"""Contracts the coverage gate found unproven after the W0 merge.

Every case here reaches a line the suite was not executing and pins the
behaviour that line implements — a refusal, a guard, a prefix match. They live
together because they were found together (by `make files-coverage`), not
because they share a subject; each one belongs to the contract of the object it
constructs, and would fail if that refusal were deleted.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from alkera_core.files.clock import FakeClock
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.scoped import PrefixGuard
from alkera_test_support.files.faulty_store import Fault

DOMAIN = UUID("33333333-4444-5555-6666-777777777777")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        pytest.param({"count": 0}, "at least once", id="count-zero"),
        pytest.param({"count": -1}, "at least once", id="count-negative"),
        pytest.param({"call_index": -1}, "cannot be negative", id="call-index-negative"),
        pytest.param(
            {"key": "objects/a", "key_prefix": "objects/"},
            "never both",
            id="key-and-prefix",
        ),
    ],
)
def test_an_unschedulable_fault_is_refused_at_construction(
    kwargs: dict[str, object], message: str
) -> None:
    """A schedule that could never fire must fail loudly, not silently never fire.

    A `count=0` fault, a negative call index or a fault addressing both a key
    and a prefix would each leave a test asserting resilience against a
    degradation that was never injected — the worst kind of green.
    """
    with pytest.raises(ValueError, match=message):
        Fault(kind="unavailable", **kwargs)  # type: ignore[arg-type]


def test_a_fault_addresses_by_key_by_prefix_or_everything() -> None:
    """The three addressing modes, including the prefix arm and the catch-all."""
    assert Fault(kind="unavailable", key="objects/a").addresses("objects/a")
    assert not Fault(kind="unavailable", key="objects/a").addresses("objects/b")

    prefixed = Fault(kind="unavailable", key_prefix="objects/ab/")
    assert prefixed.addresses("objects/ab/cd")
    assert not prefixed.addresses("objects/zz/cd")

    assert Fault(kind="unavailable").addresses("anything/at/all")


def test_a_guard_prefix_that_does_not_end_in_a_slash_is_refused(
    tmp_path: Path, clock: FakeClock
) -> None:
    """`PrefixGuard("domains/<id>")` would let `domains/<id>-other/...` through.

    The trailing slash is the whole isolation guarantee, so a prefix without one
    is refused at construction rather than silently narrowing to a string match.
    """
    store = FilesystemStore(tmp_path / "store", clock=clock.now)
    with pytest.raises(ValueError, match="must end with"):
        PrefixGuard(store, f"domains/{DOMAIN}")
